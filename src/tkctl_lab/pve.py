"""Proxmox VE REST API client (API token, form-encoded requests, JSON responses)."""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class PveError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"PVE {status}: {message}")
        self.status = status


def tags(resource: dict) -> set[str]:
    return {t for t in (resource.get("tags") or "").split(";") if t}


def _detail(body: str) -> str:
    try:
        errors = json.loads(body).get("errors")
    except ValueError, AttributeError:
        return body
    if isinstance(errors, str):
        return errors
    return " ".join(f"{k}: {v}" for k, v in (errors or {}).items())


def tls_context(ca_file: str | None) -> ssl.SSLContext:
    """Verified TLS (chain + hostname). PVE's self-signed CA lacks the Authority Key Identifier
    that Python 3.13+'s VERIFY_X509_STRICT demands, so strictness alone is switched off."""
    ctx = ssl.create_default_context(cafile=ca_file and os.path.expanduser(ca_file))
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


class Pve:
    def __init__(
        self,
        url: str,
        token_id: str,
        secret: str,
        *,
        ca_file: str | None = None,
        opener=None,
        sleep=time.sleep,
    ):
        self.base = url.rstrip("/") + "/api2/json"
        self.token_id = token_id
        self._auth = f"PVEAPIToken={token_id}={secret}"
        self.sleep = sleep
        if opener is not None:
            self.opener = opener
        else:
            # TLS is always verified; PVE's self-signed CA goes in pve.ca_file. No insecure mode.
            ctx = tls_context(ca_file)
            self.opener = lambda req, timeout=None, context=None: urllib.request.urlopen(
                req, timeout=timeout, context=ctx
            )

    def __repr__(self) -> str:
        return f"Pve({self.base}, {self.token_id})"

    def request(self, method: str, path: str, params: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        data = None
        if params:
            encoded = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if method in ("GET", "DELETE"):
                url += "?" + encoded
            else:
                data = encoded.encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener(req, timeout=60) as resp:
                return json.loads(resp.read() or b"{}").get("data")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
            detail = _detail(body)
            raise PveError(
                e.code, f"{method} {path}: {e.reason}{': ' + detail if detail else ''}"
            ) from e
        except urllib.error.URLError as e:
            hint = ""
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                hint = "; set pve.ca_file to the PVE CA (/etc/pve/pve-root-ca.pem)"
            raise PveError(0, f"{method} {path}: {e.reason}{hint}") from e

    # --- inventory
    def resources(self, kind: str) -> list[dict]:
        return self.request("GET", "/cluster/resources", {"type": kind}) or []

    def vmid_free(self, vmid: int) -> bool:
        """True when /cluster/nextid (which sees the whole cluster) reports the id as free."""
        try:
            self.request("GET", "/cluster/nextid", {"vmid": vmid})
        except PveError as e:
            if e.status == 400:
                return False
            raise
        return True

    def next_vmid(self, lo: int, hi: int, exclude: frozenset[int] | set[int] = frozenset()) -> int:
        """Lowest free VMID in the range that is neither visible in use nor excluded.

        The resources listing only shows VMs the token may audit, so each candidate is
        confirmed with /cluster/nextid before it is handed out.
        """
        used = {r["vmid"] for r in self.resources("vm")} | set(exclude)
        for vmid in range(lo, hi + 1):
            if vmid not in used and self.vmid_free(vmid):
                return vmid
        raise PveError(409, f"no free VMID in {lo}-{hi}")

    def online_nodes(self) -> list[str]:
        """Online nodes, most free memory first when the token may see node stats."""
        nodes = [n for n in self.resources("node") if n.get("status") == "online"]
        if not nodes:
            raise PveError(503, "no online node")
        nodes.sort(key=lambda n: -(n.get("maxmem", 0) - n.get("mem", 0)))
        return [n["node"] for n in nodes]

    def vm_node(self, vmid: int) -> str:
        for r in self.resources("vm"):
            if r.get("vmid") == vmid:
                return r["node"]
        raise PveError(404, f"VM {vmid} not found in the cluster")

    def pool_vms(self, pool: str) -> list[dict]:
        return [r for r in self.resources("vm") if r.get("pool") == pool]

    def status(self, node: str, vmid: int) -> str:
        return self.request("GET", f"/nodes/{node}/qemu/{vmid}/status/current")["status"]

    def vm_config(self, node: str, vmid: int) -> dict:
        return self.request("GET", f"/nodes/{node}/qemu/{vmid}/config") or {}

    def storage_content(self, node: str, storage: str, content: str) -> list[dict]:
        params = {"content": content}
        return self.request("GET", f"/nodes/{node}/storage/{storage}/content", params) or []

    # --- lifecycle
    def clone(self, node: str, template: int, newid: int, name: str, pool: str, target: str) -> str:
        params = {"newid": newid, "name": name, "pool": pool, "full": 0, "target": target}
        return self.request("POST", f"/nodes/{node}/qemu/{template}/clone", params)

    def set_config(self, node: str, vmid: int, **kv) -> None:
        # PUT is the synchronous variant; POST would fork a worker whose result we never see.
        self.request("PUT", f"/nodes/{node}/qemu/{vmid}/config", kv)

    def start(self, node: str, vmid: int) -> str:
        return self.request("POST", f"/nodes/{node}/qemu/{vmid}/status/start")

    def stop(self, node: str, vmid: int) -> str:
        return self.request("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")

    def delete(self, node: str, vmid: int) -> str:
        params = {"purge": 1, "destroy-unreferenced-disks": 1}
        return self.request("DELETE", f"/nodes/{node}/qemu/{vmid}", params)

    # --- template build
    def download_url(
        self,
        node: str,
        storage: str,
        content: str,
        url: str,
        filename: str,
        checksum: str,
        algorithm: str = "sha512",
    ) -> str:
        params = {
            "content": content,
            "url": url,
            "filename": filename,
            "checksum": checksum,
            "checksum-algorithm": algorithm,
        }
        return self.request("POST", f"/nodes/{node}/storage/{storage}/download-url", params)

    def create_vm(self, node: str, vmid: int, **kv) -> str:
        return self.request("POST", f"/nodes/{node}/qemu", {"vmid": vmid, **kv})

    def resize(self, node: str, vmid: int, disk: str, size: str) -> str | None:
        """Grow a disk; newer PVE runs this as a task and returns its UPID."""
        params = {"disk": disk, "size": size}
        return self.request("PUT", f"/nodes/{node}/qemu/{vmid}/resize", params)

    def make_template(self, node: str, vmid: int) -> str | None:
        return self.request("POST", f"/nodes/{node}/qemu/{vmid}/template")

    def wait_status(self, node: str, vmid: int, want: str, timeout: int = 1800) -> None:
        elapsed = 0
        while self.status(node, vmid) != want:
            if elapsed >= timeout:
                raise PveError(504, f"VM {vmid} did not reach {want} within {timeout}s")
            self.sleep(10)
            elapsed += 10

    def wait_task(self, upid: str, timeout: int = 300) -> None:
        """Poll a task on the node that runs it: the UPID names it (UPID:<node>:...)."""
        parts = upid.split(":")
        if len(parts) < 3 or parts[0] != "UPID":
            raise PveError(500, f"not a task id: {upid!r}")
        node = parts[1]
        elapsed = 0
        quoted = urllib.parse.quote(upid, safe="")
        while True:
            st = self.request("GET", f"/nodes/{node}/tasks/{quoted}/status")
            if st.get("status") == "stopped":
                exit_status = str(st.get("exitstatus"))
                if exit_status != "OK" and not exit_status.startswith("WARNINGS"):
                    raise PveError(500, f"task {upid} failed: {exit_status}")
                return
            if elapsed >= timeout:
                raise PveError(504, f"task {upid} did not finish within {timeout}s")
            self.sleep(2)
            elapsed += 2

    def agent_ipv4(self, node: str, vmid: int, timeout: int = 300) -> str | None:
        elapsed = 0
        while True:
            try:
                data = self.request(
                    "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
                )
                for iface in data.get("result", []):
                    if iface.get("name") == "lo":
                        continue
                    for a in iface.get("ip-addresses", []):
                        ip = a.get("ip-address", "")
                        if a.get("ip-address-type") != "ipv4":
                            continue
                        if not ip.startswith(("127.", "169.254.")):
                            return ip
            except PveError as e:
                if e.status not in (500, 0):
                    raise
            if elapsed >= timeout:
                return None
            self.sleep(5)
            elapsed += 5
