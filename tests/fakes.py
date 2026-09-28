"""Recording fakes for Pve and Guac with injectable failures."""

from __future__ import annotations

from tkctl_lab.guac import GuacError
from tkctl_lab.pve import PveError


class FakePve:
    def __init__(
        self,
        *,
        used=(),
        nodes=("pve-node7",),
        template_node="pve-node6",
        ips=None,
        fail_clone_for=(),
        agent_timeout_for=(),
    ):
        self.used = set(used)
        self.nodes = list(nodes)
        self.template_node = template_node
        self.ips = ips or {}
        self.fail_clone_for = set(fail_clone_for)
        self.agent_timeout_for = set(agent_timeout_for)
        self.vms: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.deleted: list[int] = []

    def next_vmid(self, lo, hi):
        for v in range(lo, hi + 1):
            if v not in self.used:
                self.used.add(v)
                return v
        raise PveError(409, f"no free VMID in {lo}-{hi}")

    def pick_node(self):
        return self.nodes[0]

    def vm_node(self, vmid):
        if vmid in self.vms:
            return self.vms[vmid]["node"]
        return self.template_node

    def clone(self, node, template, newid, name, pool, target):
        self.calls.append(("clone", node, newid, name, target))
        if name in self.fail_clone_for:
            raise PveError(500, f"clone failed for {name}")
        self.vms[newid] = {
            "vmid": newid,
            "name": name,
            "node": target,
            "pool": pool,
            "status": "stopped",
            "tags": "",
            "cores": 8,
            "memory": 24576,
            "balloon": 8192,
        }
        return f"UPID:{newid}"

    def wait_task(self, node, upid, timeout=300):
        self.calls.append(("wait", upid))

    def set_config(self, node, vmid, **kv):
        self.calls.append(("config", vmid, kv))
        keep = ("tags", "cores", "memory", "balloon")
        self.vms[vmid].update({k: v for k, v in kv.items() if k in keep})

    def start(self, node, vmid):
        self.vms[vmid]["status"] = "running"
        return f"UPID:start-{vmid}"

    def stop(self, node, vmid):
        self.vms[vmid]["status"] = "stopped"
        return f"UPID:stop-{vmid}"

    def delete(self, node, vmid):
        self.deleted.append(vmid)
        del self.vms[vmid]
        return f"UPID:del-{vmid}"

    def status(self, node, vmid):
        return self.vms[vmid]["status"]

    def agent_ipv4(self, node, vmid, timeout=300):
        name = self.vms[vmid]["name"]
        if name in self.agent_timeout_for:
            return None
        return self.ips.get(name, "192.168.1.100")

    def pool_vms(self, pool):
        return [dict(v) for v in self.vms.values() if v["pool"] == pool]

    def request(self, method, path, params=None):
        if path.endswith("/config"):
            vmid = int(path.split("/")[4])
            v = self.vms[vmid]
            return {
                "cores": v["cores"],
                "memory": v["memory"],
                "balloon": v["balloon"],
                "scsi0": "nas-iscsi-lvm:base-3900-disk-0/vm-3101-disk-0,size=60G",
            }
        raise NotImplementedError(path)


class FakeGuac:
    def __init__(self, *, fail_user_for=()):
        self.fail_user_for = set(fail_user_for)
        self.groups: dict[str, str] = {}
        self.users: dict[str, str] = {}
        self.connections: dict[str, dict] = {}
        self.grants: dict[str, set[str]] = {}
        self._n = 0

    def ensure_group(self, name):
        return self.groups.setdefault(name, f"g{len(self.groups) + 1}")

    def find_group(self, name):
        return self.groups.get(name)

    def create_user(self, username, password):
        if username in self.fail_user_for:
            raise GuacError(400, "Username already exists")
        self.users[username] = password

    def create_connection(self, parent, name, protocol, parameters):
        self._n += 1
        cid = str(self._n)
        self.connections[cid] = {
            "parent": parent,
            "name": name,
            "protocol": protocol,
            "parameters": parameters,
        }
        return cid

    def grant(self, username, *, connections=(), groups=()):
        self.grants.setdefault(username, set()).update(connections)

    def group_connections(self, gid):
        return [
            {"identifier": k, "name": v["name"]}
            for k, v in self.connections.items()
            if v["parent"] == gid
        ]

    def delete_connection(self, cid):
        del self.connections[cid]

    def delete_user(self, username):
        # Guacamole answers 404 for a user that does not exist.
        if username not in self.users:
            raise GuacError(404, f'User "{username}" does not exist')
        del self.users[username]

    def delete_group(self, gid):
        for k in [k for k, v in self.groups.items() if v == gid]:
            del self.groups[k]
