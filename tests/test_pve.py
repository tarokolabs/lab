import json
import ssl
import urllib.error
from io import BytesIO

import pytest

from tkctl_lab import pve

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
    c = pve.Pve("https://pve:8006", "lab@pve!tkctl", "SECRET", opener=op, sleep=lambda s: None)
    return c, op


def http_error(status, body: bytes):
    return urllib.error.HTTPError("u", status, "err", {}, BytesIO(body))


def test_auth_header_and_form_body():
    c, op = client({("POST", "/nodes/n1/qemu/3900/clone"): "UPID:x"})
    assert c.clone("n1", 3900, 3101, "lab-x-01", "lab", "n2") == "UPID:x"
    ((_key, body, headers),) = op.calls
    assert headers["Authorization"] == "PVEAPIToken=lab@pve!tkctl=SECRET"
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
    c, _ = client({("GET", "/cluster/resources"): res})
    assert c.next_vmid(3100, 3199) == 3102


def test_next_vmid_exhausted():
    c, _ = client({("GET", "/cluster/resources"): [{"vmid": 3100, "type": "qemu"}]})
    with pytest.raises(pve.PveError, match="no free VMID"):
        c.next_vmid(3100, 3100)


def test_pick_node_most_free_memory_online():
    res = [
        {"node": "n1", "status": "online", "maxmem": 100, "mem": 90},
        {"node": "n2", "status": "online", "maxmem": 100, "mem": 10},
        {"node": "n3", "status": "offline", "maxmem": 100, "mem": 0},
    ]
    c, _ = client({("GET", "/cluster/resources"): res})
    assert c.pick_node() == "n2"


def test_wait_task_polls_until_stopped_and_raises_on_failure():
    seq = iter([{"status": "running"}, {"status": "stopped", "exitstatus": "OK"}])
    c, op = client({("GET", "/nodes/n1/tasks/UPID%3Ax/status"): lambda req: next(seq)})
    c.wait_task("n1", "UPID:x")
    assert len(op.calls) == 2
    failed = {"status": "stopped", "exitstatus": "clone failed: disk full"}
    c2, _ = client({("GET", "/nodes/n1/tasks/UPID%3Ay/status"): failed})
    with pytest.raises(pve.PveError, match="disk full"):
        c2.wait_task("n1", "UPID:y")


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
