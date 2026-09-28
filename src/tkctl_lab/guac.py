"""Apache Guacamole REST API client: users, connection groups, connections, permissions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TOTP_FIELD = "guac-totp"
TOTP_SECRET_ENV = "TK_LAB_GUAC_TOTP_SECRET"


class GuacError(Exception):
    def __init__(self, status: int, message: str, body: dict | None = None):
        super().__init__(f"Guacamole {status}: {message}")
        self.status = status
        self.body = body or {}


def totp(secret: str, *, at: float | None = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 TOTP (SHA-1), for a Guacamole TOTP challenge on the service account."""
    clean = secret.replace(" ", "").replace("=", "").upper()
    key = base64.b32decode(clean + "=" * (-len(clean) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**digits).zfill(digits)


def SSH_PARAMS(host: str, user: str, password: str) -> dict:
    return {"hostname": host, "port": "22", "username": user, "password": password}


def RDP_PARAMS(host: str, user: str, password: str) -> dict:
    return {
        "hostname": host,
        "port": "3389",
        "username": user,
        "password": password,
        "ignore-cert": "true",
        "security": "any",
        "resize-method": "display-update",
    }


def _body(err: urllib.error.HTTPError) -> dict:
    try:
        data = json.loads(err.read())
        return data if isinstance(data, dict) else {}
    except ValueError, AttributeError:
        return {}


class Guac:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        *,
        totp_secret: str | None = None,
        opener=None,
        sleep=time.sleep,
    ):
        self.base = url.rstrip("/") + "/api"
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        self.sleep = sleep
        # Default opener verifies TLS with the system trust store; no insecure mode.
        self.opener = opener or (
            lambda req, timeout=None, context=None: urllib.request.urlopen(req, timeout=timeout)
        )
        self.token: str | None = None
        self.data_source: str | None = None

    def _call(self, method: str, path: str, body: Any = None, *, form: bool = False) -> Any:
        req = urllib.request.Request(self.base + path, method=method)
        if form:
            req.data = urllib.parse.urlencode(body).encode()
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        elif body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Guacamole-Token", self.token)
        try:
            with self.opener(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body = _body(e)
            detail = body.get("message", "")
            raise GuacError(
                e.code, f"{method} {path}: {e.reason}{': ' + detail if detail else ''}", body
            ) from e
        except urllib.error.URLError as e:
            raise GuacError(0, f"{method} {path}: {e.reason}") from e

    def login(self) -> None:
        creds = {"username": self.username, "password": self.password}
        try:
            r = self._call("POST", "/tokens", creds, form=True)
        except GuacError as e:
            expected = {x.get("name") for x in e.body.get("expected", [])}
            if TOTP_FIELD not in expected:
                raise
            if not self.totp_secret:
                raise GuacError(
                    e.status,
                    f"login needs a TOTP code; enrol {self.username} once and set "
                    f"{TOTP_SECRET_ENV} to its secret",
                ) from e
            r = self._login_with_code(creds)
        self.token, self.data_source = r["authToken"], r["dataSource"]

    def _login_with_code(self, creds: dict) -> dict:
        """A code already used in this 30 s period is refused; wait for the next one and retry."""
        assert self.totp_secret
        creds[TOTP_FIELD] = totp(self.totp_secret)
        try:
            return self._call("POST", "/tokens", creds, form=True)
        except GuacError as e:
            if "TOTP" not in str(e):
                raise
        self.sleep(31 - time.time() % 30)
        creds[TOTP_FIELD] = totp(self.totp_secret)
        return self._call("POST", "/tokens", creds, form=True)

    def _data(self, method: str, path: str, body: Any = None) -> Any:
        if not self.token:
            self.login()
        return self._call(method, f"/session/data/{self.data_source}{path}", body)

    # --- connection groups (one per class)
    def find_group(self, name: str) -> str | None:
        tree = self._data("GET", "/connectionGroups/ROOT/tree")
        for g in tree.get("childConnectionGroups", []):
            if g.get("name") == name:
                return g["identifier"]
        return None

    def ensure_group(self, name: str) -> str:
        gid = self.find_group(name)
        if gid is not None:
            return gid
        body = {
            "parentIdentifier": "ROOT",
            "name": name,
            "type": "ORGANIZATIONAL",
            "attributes": {},
        }
        return self._data("POST", "/connectionGroups", body)["identifier"]

    def group_connections(self, group_id: str) -> list[dict]:
        tree = self._data("GET", f"/connectionGroups/{group_id}/tree")
        return [
            {"identifier": c["identifier"], "name": c["name"]}
            for c in tree.get("childConnections", [])
        ]

    def delete_group(self, gid: str) -> None:
        self._data("DELETE", f"/connectionGroups/{gid}")

    # --- users and connections
    def create_user(self, username: str, password: str) -> None:
        self._data("POST", "/users", {"username": username, "password": password, "attributes": {}})

    def create_connection(self, parent: str, name: str, protocol: str, parameters: dict) -> str:
        body = {
            "parentIdentifier": parent,
            "name": name,
            "protocol": protocol,
            "parameters": parameters,
            "attributes": {},
        }
        return self._data("POST", "/connections", body)["identifier"]

    def grant(self, username: str, *, connections: list[str] = (), groups: list[str] = ()) -> None:
        ops = [
            {"op": "add", "path": f"/connectionPermissions/{c}", "value": "READ"}
            for c in connections
        ]
        ops += [
            {"op": "add", "path": f"/connectionGroupPermissions/{g}", "value": "READ"}
            for g in groups
        ]
        self._data("PATCH", f"/users/{urllib.parse.quote(username)}/permissions", ops)

    def delete_connection(self, cid: str) -> None:
        self._data("DELETE", f"/connections/{cid}")

    def delete_user(self, username: str) -> None:
        self._data("DELETE", f"/users/{urllib.parse.quote(username)}")
