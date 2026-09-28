"""Apache Guacamole REST API client: users, connection groups, connections, permissions."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class GuacError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Guacamole {status}: {message}")
        self.status = status


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


def _detail(err: urllib.error.HTTPError) -> str:
    try:
        return json.loads(err.read()).get("message", "")
    except ValueError, AttributeError:
        return ""


class Guac:
    def __init__(self, url: str, username: str, password: str, *, opener=None):
        self.base = url.rstrip("/") + "/api"
        self.username = username
        self.password = password
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
            detail = _detail(e)
            raise GuacError(
                e.code, f"{method} {path}: {e.reason}{': ' + detail if detail else ''}"
            ) from e
        except urllib.error.URLError as e:
            raise GuacError(0, f"{method} {path}: {e.reason}") from e

    def login(self) -> None:
        creds = {"username": self.username, "password": self.password}
        r = self._call("POST", "/tokens", creds, form=True)
        self.token, self.data_source = r["authToken"], r["dataSource"]

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
