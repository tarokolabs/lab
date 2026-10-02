import json
import ssl
import urllib.error
from io import BytesIO

import pytest

from tklab import pve

BASE = "https://pve:8006/api2/json"


class FakeResp(BytesIO):
    def __init__(self, obj, status=200):
        super().__init__(json.dumps(obj).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_with(routes):
    """routes: {(METHOD, path): payload | callable(req) -> payload | Exception}; records calls."""
    calls = []

    def open_(req, timeout=None, context=None):
        key = (req.get_method(), req.full_url.split("?")[0].replace(BASE, ""))
        calls.append((key, req.data.decode() if req.data else None, dict(req.header_items())))
        if key not in routes:
            body = BytesIO(b'{"errors":"no route"}')
            raise urllib.error.HTTPError(req.full_url, 501, f"no route {key}", {}, body)
        r = routes[key]
        r = r(req) if callable(r) else r
        if isinstance(r, Exception):
            raise r
        return FakeResp({"data": r})

    open_.calls = calls
    return open_


def client(routes):
    op = opener_with(routes)
    c = pve.Pve("https://pve:8006", "lab@pve!tklab", "SECRET", opener=op, sleep=lambda s: None)
    return c, op


def http_error(status, body: bytes):
    return urllib.error.HTTPError("u", status, "err", {}, BytesIO(body))


def test_auth_header_and_form_body():
    c, op = client({("POST", "/nodes/n1/qemu/3900/clone"): "UPID:x"})
    assert c.clone("n1", 3900, 3101, "lab-x-01", "lab", "n2") == "UPID:x"
    ((_key, body, headers),) = op.calls
    assert headers["Authorization"] == "PVEAPIToken=lab@pve!tklab=SECRET"
    # secrets never appear in the string form of the client or its errors
    assert "SECRET" not in repr(c)
    assert body == "newid=3101&name=lab-x-01&pool=lab&full=0&target=n2"


def test_error_carries_status_and_pve_message():
    err = http_error(500, b'{"errors":{"newid":"VM 3101 already exists"},"data":null}')
    c, _ = client({("POST", "/nodes/n1/qemu/3900/clone"): err})
    with pytest.raises(pve.PveError) as e:
        c.clone("n1", 3900, 3101, "x", "lab", "n1")
    assert e.value.status == 500 and "3101 already exists" in str(e.value)


def test_next_vmid_skips_used_ids_in_range():
    res = [
        {"vmid": 3100, "type": "qemu"},
        {"vmid": 3101, "type": "lxc"},
        {"vmid": 3900, "type": "qemu"},
    ]
    nextid = lambda req: int(req.full_url.rsplit("=", 1)[1])  # noqa: E731
    c, _ = client({("GET", "/cluster/resources"): res, ("GET", "/cluster/nextid"): nextid})
    assert c.next_vmid(3100, 3199) == 3102


def test_next_vmid_exhausted():
    routes = {("GET", "/cluster/resources"): [{"vmid": 3100, "type": "qemu"}]}
    c, _ = client(routes)
    with pytest.raises(pve.PveError, match="no free VMID"):
        c.next_vmid(3100, 3100)


def test_online_nodes_sorted_by_free_memory_when_visible():
    res = [
        {"node": "n1", "status": "online", "maxmem": 100, "mem": 90},
        {"node": "n2", "status": "online", "maxmem": 100, "mem": 10},
        {"node": "n3", "status": "offline", "maxmem": 100, "mem": 0},
    ]
    c, _ = client({("GET", "/cluster/resources"): res})
    assert c.online_nodes() == ["n2", "n1"]
    # without Sys.Audit the stats are missing; the nodes are still listed, in cluster order
    bare = [{"node": "n1", "status": "online"}, {"node": "n2", "status": "online"}]
    c2, _ = client({("GET", "/cluster/resources"): bare})
    assert c2.online_nodes() == ["n1", "n2"]
    c3, _ = client({("GET", "/cluster/resources"): []})
    with pytest.raises(pve.PveError, match="no online node"):
        c3.online_nodes()


def test_next_vmid_confirms_with_cluster_nextid_and_honours_exclude():
    # 3100 is used by a VM the token cannot see: /cluster/resources omits it, /cluster/nextid knows
    def nextid(req):
        if "vmid=3100" in req.full_url:
            return http_error(400, b'{"errors":{"vmid":"VM 3100 already exists"}}')
        return int(req.full_url.rsplit("=", 1)[1])

    routes = {("GET", "/cluster/resources"): [], ("GET", "/cluster/nextid"): nextid}
    c, _ = client(routes)
    assert c.next_vmid(3100, 3199) == 3101
    assert c.next_vmid(3100, 3199, exclude={3101, 3102}) == 3103
    assert c.vmid_free(3100) is False and c.vmid_free(3105) is True


def test_wait_task_polls_the_node_in_the_upid_and_accepts_warnings():
    seq = iter([{"status": "running"}, {"status": "stopped", "exitstatus": "OK"}])
    upid = "UPID:n1:0001:0002:0003:qmclone:3101:lab@pve!tklab:"
    quoted = upid.replace(":", "%3A").replace("!", "%21").replace("@", "%40")
    path = f"/nodes/n1/tasks/{quoted}/status"
    c, op = client({("GET", path): lambda req: next(seq)})
    c.wait_task(upid)  # the node comes from the UPID, never from the caller
    assert len(op.calls) == 2
    warn = {"status": "stopped", "exitstatus": "WARNINGS: 1"}
    c2, _ = client({("GET", "/nodes/n2/tasks/UPID%3An2%3Ay/status"): warn})
    c2.wait_task("UPID:n2:y")
    failed = {"status": "stopped", "exitstatus": "clone failed: disk full"}
    c3, _ = client({("GET", "/nodes/n1/tasks/UPID%3An1%3Ay/status"): failed})
    with pytest.raises(pve.PveError, match="disk full"):
        c3.wait_task("UPID:n1:y")


def test_agent_ipv4_picks_first_real_address_and_times_out():
    ifaces = {
        "result": [
            {
                "name": "lo",
                "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}],
            },
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
                    {"ip-address": "192.168.1.77", "ip-address-type": "ipv4"},
                ],
            },
        ]
    }
    c, _ = client({("GET", "/nodes/n1/qemu/3101/agent/network-get-interfaces"): ifaces})
    assert c.agent_ipv4("n1", 3101) == "192.168.1.77"
    err = http_error(500, b'{"errors":"QEMU guest agent is not running"}')
    c2, _ = client({("GET", "/nodes/n1/qemu/3101/agent/network-get-interfaces"): err})
    assert c2.agent_ipv4("n1", 3101, timeout=10) is None


def test_pool_vms_and_tags():
    res = [
        {
            "vmid": 3101,
            "type": "qemu",
            "pool": "lab",
            "name": "lab-x-01",
            "node": "n1",
            "status": "running",
            "tags": "lab;class-x",
        },
        {"vmid": 100, "type": "qemu", "pool": "other", "tags": ""},
    ]
    c, _ = client({("GET", "/cluster/resources"): res})
    vms = c.pool_vms("lab")
    assert [v["vmid"] for v in vms] == [3101]
    assert pve.tags(vms[0]) == {"lab", "class-x"}


def test_certificate_error_points_at_ca_file():
    err = urllib.error.URLError(
        ssl.SSLCertVerificationError("certificate verify failed: self-signed")
    )
    c, _ = client({("GET", "/cluster/resources"): err})
    with pytest.raises(pve.PveError, match=r"pve\.ca_file"):
        c.resources("vm")


def test_vm_node_finds_the_template_host():
    res = [
        {"vmid": 3900, "type": "qemu", "node": "n1"},
        {"vmid": 100, "type": "qemu", "node": "n2"},
    ]
    c, _ = client({("GET", "/cluster/resources"): res})
    assert c.vm_node(3900) == "n1"
    with pytest.raises(pve.PveError, match="VM 4000 not found"):
        c.vm_node(4000)


def test_create_resize_template_and_wait_status():
    seq = iter(["running", "stopped"])
    routes = {
        ("POST", "/nodes/n1/qemu"): "UPID:cr",
        ("PUT", "/nodes/n1/qemu/3900/resize"): None,
        ("POST", "/nodes/n1/qemu/3900/template"): None,
        ("GET", "/nodes/n1/qemu/3900/status/current"): lambda req: {"status": next(seq)},
    }
    c, op = client(routes)
    assert c.create_vm("n1", 3900, name="t", agent=1) == "UPID:cr"
    assert c.resize("n1", 3900, "scsi0", "60G") is None  # older PVE answers null, newer a UPID
    c.wait_status("n1", 3900, "stopped", timeout=60)
    c.make_template("n1", 3900)
    bodies = [b for (_, b, _) in op.calls]
    assert bodies[0] == "vmid=3900&name=t&agent=1" and bodies[1] == "disk=scsi0&size=60G"


def test_set_config_is_synchronous_put_and_vm_config_reads():
    routes = {
        ("PUT", "/nodes/n1/qemu/3101/config"): None,
        ("GET", "/nodes/n1/qemu/3101/config"): {"cores": 8, "scsi0": "x,size=60G"},
    }
    c, op = client(routes)
    c.set_config("n1", 3101, ciuser="student", tags="lab;class-x")
    assert c.vm_config("n1", 3101)["cores"] == 8
    ((key, body, _), _) = op.calls
    assert key == ("PUT", "/nodes/n1/qemu/3101/config") and "tags=lab%3Bclass-x" in body
    c2, _ = client({("GET", "/nodes/n1/qemu/3900/status/current"): {"status": "running"}})
    with pytest.raises(pve.PveError, match="did not reach stopped"):
        c2.wait_status("n1", 3900, "stopped", timeout=20)


def test_default_tls_context_verifies_but_is_not_rfc5280_strict(tmp_path):
    # Python 3.13+ turns on VERIFY_X509_STRICT, which rejects PVE's own CA (no Authority Key
    # Identifier). Verification of the chain and the hostname stays on; only strictness goes.
    ctx = pve.tls_context(None)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert not ctx.verify_flags & ssl.VERIFY_X509_STRICT
    c = pve.Pve("https://pve:8006", "t", "s")  # builds the default opener without error
    assert c.opener is not None


def admin(routes):
    op = opener_with(routes)
    return pve.PveAdmin("https://pve:8006", "root@pam", "hunter2", opener=op), op


TICKET = {
    ("POST", "/access/ticket"): {"ticket": "PVE:root@pam:ABC", "CSRFPreventionToken": "CSRF:1"}
}


def test_ticket_login_sets_cookie_and_csrf_on_writes():
    c, op = admin(TICKET | {("GET", "/access/roles"): [], ("POST", "/access/roles"): None})
    assert c.roles() == {}
    c.role_add("TklabX", ["VM.Audit", "VM.Clone"])
    (_, login_body, _), (_, _, get_h), (_, post_body, post_h) = op.calls
    assert login_body == "username=root%40pam&password=hunter2"
    assert get_h["Cookie"] == "PVEAuthCookie=PVE:root@pam:ABC"
    assert "Csrfpreventiontoken" not in get_h
    assert post_h["Csrfpreventiontoken"] == "CSRF:1"
    assert post_body == "roleid=TklabX&privs=VM.Audit%2CVM.Clone"


def test_ticket_login_reports_tfa():
    need = {"ticket": "PVE:!tfa!abc", "CSRFPreventionToken": "x", "NeedTFA": 1}
    c, _ = admin({("POST", "/access/ticket"): need})
    with pytest.raises(pve.PveError) as e:
        c.login()
    assert "TFA" in str(e.value) and "hunter2" not in str(e.value)
    seq = iter([need, {"ticket": "PVE:root@pam:OK", "CSRFPreventionToken": "y"}])
    c2, op = admin({("POST", "/access/ticket"): lambda req: next(seq)})
    c2.login(totp="123456")
    body = op.calls[1][1]
    assert "tfa-challenge=PVE%3A%21tfa%21abc" in body and "password=totp%3A123456" in body


def test_admin_inventory_shapes():
    routes = TICKET | {
        ("GET", "/access/roles"): [
            {"roleid": "TklabA", "privs": "VM.Audit,VM.Clone"},
            {"roleid": "Administrator", "privs": "", "special": 1},
        ],
        ("GET", "/access/users"): [{"userid": "root@pam"}, {"userid": "lab@pve"}],
        ("GET", "/pools"): [{"poolid": "lab"}],
        ("GET", "/access/users/lab@pve/token"): [{"tokenid": "tklab"}],
        ("GET", "/access/acl"): [
            {
                "path": "/pool/lab",
                "roleid": "TklabClass",
                "type": "token",
                "ugid": "lab@pve!tklab",
                "propagate": 1,
            }
        ],
        ("GET", "/storage"): [
            {"storage": "nas-nfs", "type": "nfs", "content": "images,snippets,import", "shared": 1}
        ],
        ("GET", "/nodes/n1/network"): [
            {"iface": "vmbr0", "type": "bridge"},
            {"iface": "eno1", "type": "eth"},
        ],
        ("GET", "/nodes"): [
            {"node": "n2", "status": "online"},
            {"node": "n1", "status": "online"},
            {"node": "n3", "status": "offline"},
        ],
        ("GET", "/nodes/n1/certificates/info"): [
            {"filename": "pve-root-ca.pem", "pem": "-----BEGIN CERTIFICATE-----\nAA\n"},
            {"filename": "pve-ssl.pem", "pem": "x"},
        ],
        ("GET", "/cluster/nextid"): lambda req: (
            http_error(400, b'{"errors":{"vmid":"VM 3900 already exists"}}')
            if "vmid=3900" in req.full_url
            else 3901
        ),
    }
    c, _ = admin(routes)
    assert c.roles() == {"TklabA": {"VM.Audit", "VM.Clone"}, "Administrator": set()}
    assert c.users() == {"root@pam", "lab@pve"} and c.pools() == {"lab"}
    assert c.tokens("lab@pve") == {"tklab"}
    assert c.acl()[0]["ugid"] == "lab@pve!tklab"
    assert c.storages()[0]["storage"] == "nas-nfs"
    assert c.bridges("n1") == ["vmbr0"] and c.nodes() == ["n1", "n2"]
    assert c.ca_pem("n1").startswith("-----BEGIN CERTIFICATE-----")
    assert c.vmid_free(3900) is False and c.vmid_free(3901) is True


def test_admin_writes_shapes():
    routes = TICKET | {
        ("PUT", "/access/roles/TklabA"): None,
        ("POST", "/access/users"): None,
        ("POST", "/pools"): None,
        ("POST", "/access/users/lab@pve/token/tklab"): {
            "full-tokenid": "lab@pve!tklab",
            "value": "SECRET-1",
        },
        ("DELETE", "/access/users/lab@pve/token/old"): None,
        ("PUT", "/access/acl"): None,
    }
    c, op = admin(routes)
    c.role_set("TklabA", ["VM.Audit"])
    c.user_add("lab@pve", "tklab service account")
    c.pool_add("lab", "tklab classes")
    assert c.token_add("lab@pve", "tklab") == "SECRET-1"
    c.token_remove("lab@pve", "old")
    c.acl_add("/pool/lab", "TklabClass", token="lab@pve!tklab")
    c.acl_add("/pool/lab", "TklabClass", user="lab@pve")
    bodies = {k: b for (k, b, _) in op.calls}
    assert bodies[("PUT", "/access/roles/TklabA")] == "privs=VM.Audit"
    assert bodies[("POST", "/access/users")] == "userid=lab%40pve&comment=tklab+service+account"
    assert bodies[("POST", "/access/users/lab@pve/token/tklab")] == "privsep=1"
    acl_bodies = [b for (k, b, _) in op.calls if k == ("PUT", "/access/acl")]
    assert acl_bodies == [
        "path=%2Fpool%2Flab&roles=TklabClass&propagate=1&tokens=lab%40pve%21tklab",
        "path=%2Fpool%2Flab&roles=TklabClass&propagate=1&users=lab%40pve",
    ]


def test_admin_vm_is_template():
    res = [{"vmid": 3900, "type": "qemu", "template": 1}, {"vmid": 3000, "type": "qemu"}]
    c, _ = admin(TICKET | {("GET", "/cluster/resources"): res})
    assert c.vm_is_template(3900) is True and c.vm_is_template(3000) is False
    assert c.vm_is_template(4000) is False


def test_clone_full_to_another_storage_and_storage_avail():
    routes = {
        ("POST", "/nodes/n1/qemu/3900/clone"): "UPID:n1:x",
        ("GET", "/nodes/n2/storage/local-lvm/status"): {"avail": 5 * 2**30, "total": 10 * 2**30},
    }
    c, op = client(routes)
    c.clone("n1", 3900, 3101, "lab-x-01", "lab", "n2", full=True, storage="local-lvm")
    assert op.calls[0][1] == "newid=3101&name=lab-x-01&pool=lab&full=1&target=n2&storage=local-lvm"
    assert c.storage_avail("n2", "local-lvm") == 5 * 2**30
    c.clone("n1", 3900, 3102, "lab-x-02", "lab", "n2")  # default stays a linked clone
    assert op.calls[-1][1] == "newid=3102&name=lab-x-02&pool=lab&full=0&target=n2"


def test_migrate_offline_with_local_disks():
    c, op = client({("POST", "/nodes/n1/qemu/3101/migrate"): "UPID:n1:mig"})
    assert c.migrate("n1", 3101, "n2") == "UPID:n1:mig"
    assert op.calls[0][1] == "target=n2&with-local-disks=1"


def test_agent_exec_posts_the_command_as_a_list_and_reads_status():
    routes = {
        ("POST", "/nodes/n1/qemu/3101/agent/exec"): {"pid": 42},
        ("GET", "/nodes/n1/qemu/3101/agent/exec-status"): {"exited": 1, "exitcode": 3},
    }
    c, op = client(routes)
    assert c.agent_exec("n1", 3101, ["bash", "-c", "echo hi > /tmp/x"]) == 42
    assert c.agent_exec_status("n1", 3101, 42) == {"exited": 1, "exitcode": 3}
    (_, body, _), (_, _, _) = op.calls
    # PVE takes array parameters as the same key repeated
    assert body == "command=bash&command=-c&command=echo+hi+%3E+%2Ftmp%2Fx"
    assert op.calls[1][0] == ("GET", "/nodes/n1/qemu/3101/agent/exec-status")
