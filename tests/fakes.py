"""Recording fakes for Pve and Guac with injectable failures.

FakePve mirrors what the real API shows a pool-scoped token: VMIDs outside the pool are
invisible to next_vmid (`hidden`), only /cluster/nextid (vmid_free) sees the whole cluster,
and next_vmid never reserves anything.
"""

from __future__ import annotations

from tkctl_lab.guac import GuacError
from tkctl_lab.pve import PveError


class FakePve:
    def __init__(
        self,
        *,
        used=(),
        hidden=(),
        nodes=("pve-node7",),
        node_stats=True,
        template_node="pve-node6",
        template_disk="60G",
        ips=None,
        fail_clone_for=(),
        agent_timeout_for=(),
        snippets=(),
        imports=(),
        avail=None,
    ):
        self.used = set(used)
        self.hidden = set(hidden)
        self.nodes = list(nodes)
        self.node_stats = node_stats
        self.template_node = template_node
        self.template_disk = template_disk
        self.ips = ips or {}
        self.fail_clone_for = set(fail_clone_for)
        self.agent_timeout_for = set(agent_timeout_for)
        self.snippets = list(snippets)
        self.imports = list(imports)
        self.avail = dict(avail or {})  # (node, storage) -> free bytes
        self.vms: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.deleted: list[int] = []
        self.clone_lock_calls = 0

    # --- inventory
    def next_vmid(self, lo, hi, exclude=frozenset()):
        visible = self.used | set(self.vms) | set(exclude)
        for v in range(lo, hi + 1):
            if v not in visible and self.vmid_free(v):
                return v
        raise PveError(409, f"no free VMID in {lo}-{hi}")

    def vmid_free(self, vmid):
        return vmid not in self.used and vmid not in self.vms and vmid not in self.hidden

    def online_nodes(self):
        return list(self.nodes)

    def vm_node(self, vmid):
        if vmid in self.vms:
            return self.vms[vmid]["node"]
        return self.template_node

    def vm_config(self, node, vmid):
        if vmid in self.vms:
            v = self.vms[vmid]
            return {
                "cores": v["cores"],
                "memory": v["memory"],
                "balloon": v["balloon"],
                "scsi0": f"nas-nfs:3900/vm-{vmid}-disk-0.qcow2,size={v['disk']}",
            }
        return {"scsi0": f"nas-nfs:3900/base-3900-disk-0.qcow2,size={self.template_disk}"}

    def pool_vms(self, pool):
        return [dict(v) for v in self.vms.values() if v["pool"] == pool]

    def status(self, node, vmid):
        return self.vms[vmid]["status"]

    def storage_avail(self, node, storage):
        self.calls.append(("storage_avail", node, storage))
        return self.avail.get((node, storage), 500 * 2**30)

    def storage_content(self, node, storage, content):
        names = self.snippets if content == "snippets" else self.imports
        return [{"volid": f"{storage}:{content}/{n}"} for n in names]

    # --- lifecycle
    def clone(self, node, template, newid, name, pool, target, *, full=False, storage=None):
        self.calls.append(("clone", node, newid, name, target, full, storage))
        if not self.vmid_free(newid):
            raise PveError(500, f"newid: unable to create VM {newid}: config file already exists")
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
            "disk": self.template_disk,
        }
        return f"UPID:{node}:clone-{newid}"

    def wait_task(self, upid, timeout=300):
        assert upid.startswith("UPID:"), upid
        self.calls.append(("wait", upid, timeout))

    def set_config(self, node, vmid, **kv):
        self.calls.append(("config", vmid, kv))
        keep = ("tags", "cores", "memory", "balloon")
        self.vms[vmid].update({k: v for k, v in kv.items() if k in keep})

    def resize(self, node, vmid, disk, size):
        self.calls.append(("resize", vmid, disk, size))
        self.vms[vmid]["disk"] = size
        return f"UPID:{node}:resize-{vmid}"

    def start(self, node, vmid):
        self.vms[vmid]["status"] = "running"
        return f"UPID:{node}:start-{vmid}"

    def stop(self, node, vmid):
        self.vms[vmid]["status"] = "stopped"
        return f"UPID:{node}:stop-{vmid}"

    def delete(self, node, vmid):
        self.deleted.append(vmid)
        del self.vms[vmid]
        return f"UPID:{node}:del-{vmid}"

    def agent_ipv4(self, node, vmid, timeout=300):
        name = self.vms[vmid]["name"]
        if name in self.agent_timeout_for:
            return None
        return self.ips.get(name, "192.168.1.100")


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
        if username in self.users:
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


class FakePveAdmin:
    """In-memory PVE access control for setup tests."""

    def __init__(
        self,
        *,
        roles=None,
        users=(),
        pools=(),
        tokens=None,
        acl=(),
        nodes=("n1",),
        ca="-----BEGIN CERTIFICATE-----\nCA\n",
    ):
        self._roles = {k: set(v) for k, v in (roles or {}).items()}
        self._users = set(users)
        self._pools = set(pools)
        self._tokens = {k: set(v) for k, v in (tokens or {}).items()}
        self._acl = list(acl)
        self._nodes = list(nodes)
        self._ca = ca
        self.calls: list[tuple] = []
        self.secrets_issued = 0

    def login(self, totp=None):
        self.calls.append(("login", totp))

    def roles(self):
        return {k: set(v) for k, v in self._roles.items()}

    def role_add(self, roleid, privs):
        self.calls.append(("role_add", roleid, list(privs)))
        self._roles[roleid] = set(privs)

    def role_set(self, roleid, privs):
        self.calls.append(("role_set", roleid, list(privs)))
        self._roles[roleid] = set(privs)

    def users(self):
        return set(self._users)

    def user_add(self, userid, comment):
        self.calls.append(("user_add", userid))
        self._users.add(userid)

    def pools(self):
        return set(self._pools)

    def pool_add(self, poolid, comment):
        self.calls.append(("pool_add", poolid))
        self._pools.add(poolid)

    def tokens(self, userid):
        return set(self._tokens.get(userid, set()))

    def token_add(self, userid, tokenid):
        self.calls.append(("token_add", userid, tokenid))
        self._tokens.setdefault(userid, set()).add(tokenid)
        self.secrets_issued += 1
        return f"secret-{tokenid}-{self.secrets_issued}"

    def token_remove(self, userid, tokenid):
        self.calls.append(("token_remove", userid, tokenid))
        self._tokens[userid].discard(tokenid)

    def acl(self):
        return [dict(a) for a in self._acl]

    def acl_add(self, path, roleid, *, user=None, token=None):
        self.calls.append(("acl_add", path, roleid, user or token))
        kind, ugid = ("user", user) if user else ("token", token)
        self._acl.append(
            {"path": path, "roleid": roleid, "type": kind, "ugid": ugid, "propagate": 1}
        )

    def nodes(self):
        return list(self._nodes)

    def storages(self):
        return [
            {"storage": "nas-nfs", "type": "nfs", "content": "images,snippets,import", "shared": 1}
        ]

    def bridges(self, node):
        return ["vmbr0"]

    def ca_pem(self, node):
        return self._ca

    def vmid_free(self, vmid):
        return True

    def vm_is_template(self, vmid):
        return False


class FakeGuacAdmin:
    """Admin-side Guacamole for setup tests; also fakes the service account's own login."""

    def __init__(self, *, users=None, perms=None, totp="off"):
        # totp: "off" (no extension), "fresh" (challenge offers a secret), "enrolled" (no secret)
        self.users = dict(users or {})  # username -> password
        self.perms = {k: set(v) for k, v in (perms or {}).items()}
        self.totp = totp
        self.enrolled_secret = None
        self.calls: list[tuple] = []

    def get_user(self, username):
        return {"username": username, "attributes": {}} if username in self.users else None

    def create_user(self, username, password):
        self.calls.append(("create_user", username))
        self.users[username] = password

    def set_password(self, username, password):
        self.calls.append(("set_password", username))
        self.users[username] = password

    def system_permissions(self, username):
        return set(self.perms.get(username, set()))

    def grant_system(self, username, perms):
        self.calls.append(("grant_system", username, list(perms)))
        self.perms.setdefault(username, set()).update(perms)

    def clear_totp(self, username):
        self.calls.append(("clear_totp", username))
        self.totp = "fresh"
        self.enrolled_secret = None

    def logout(self):
        self.calls.append(("logout",))

    # --- the service account logging in through the same fake
    def service_login(self, username, password, totp_secret):
        if self.users.get(username) != password:
            raise GuacError(
                403, "Invalid login", {"expected": [{"name": "username"}, {"name": "password"}]}
            )
        if self.totp == "off":
            return {"secret": None}
        if self.totp == "fresh":
            self.totp = "enrolled"
            self.enrolled_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
            return {"secret": self.enrolled_secret}
        if totp_secret == self.enrolled_secret:
            return {"secret": None}
        raise GuacError(403, "Verification code required", {"expected": [{"name": "guac-totp"}]})
