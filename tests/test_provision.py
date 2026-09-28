from datetime import date

import pytest

from tkctl_lab import classdef, provision, roster
from tkctl_lab.config import Config, GuacConfig, PveConfig, TemplateConfig, VmConfig
from tkctl_lab.pve import PveError

from .fakes import FakeGuac, FakePve

CFG = Config(
    pve=PveConfig(
        "https://pve", "lab@pve!t", "auto", "lab", "nas-iscsi-lvm", 3900, (3100, 3199), "vmbr0"
    ),
    guacamole=GuacConfig("https://guac", "svc"),
    vm=VmConfig(),
    template=TemplateConfig("u", "s"),
)
QUIET = {"log": lambda *_: None}


def student(name):
    return classdef.Student(name, 8, 24576, 8192, "60G")


def cd(names, expires="2026-10-20", name="k8s-101"):
    exp = date.fromisoformat(expires) if expires else None
    return classdef.ClassDef(name, tuple(student(n) for n in names), exp, None)


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path


def test_guac_user_rule():
    assert provision.guac_user("k8s-101", "alice") == "alice"
    assert provision.guac_user("k8s-101", "07") == "k8s-101-07"


def test_create_provisions_vm_and_two_connections_per_student():
    pve = FakePve(used={3100}, ips={"lab-k8s-101-alice": "192.168.1.77"})
    guac = FakeGuac()
    entries = provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    (e,) = entries
    assert (e.vmid, e.ip, e.guac_user, e.error) == (3101, "192.168.1.77", "alice", "")
    # clone is posted to the template's node and lands on the picked node
    assert ("clone", "pve-node6", 3101, "lab-k8s-101-alice", "pve-node7") in pve.calls
    kv = next(c[2] for c in pve.calls if c[0] == "config" and c[1] == 3101)
    assert kv["ciuser"] == "student" and kv["ipconfig0"] == "ip=dhcp"
    assert len(kv["cipassword"]) >= 16
    assert kv["tags"] == "lab;class-k8s-101;expires-2026-10-20"
    assert kv["cores"] == 8 and kv["memory"] == 24576 and kv["balloon"] == 8192
    assert pve.vms[3101]["status"] == "running"
    assert guac.users["alice"] == e.guac_password
    names = sorted(c["name"] for c in guac.connections.values())
    assert names == ["k8s-101-alice Desktop", "k8s-101-alice SSH"]
    rdp = next(c for c in guac.connections.values() if c["protocol"] == "rdp")
    assert rdp["parameters"]["hostname"] == "192.168.1.77"
    assert rdp["parameters"]["username"] == "student"
    assert rdp["parameters"]["password"] == kv["cipassword"]
    assert guac.grants["alice"] == set(guac.connections)
    p = roster.path("k8s-101")
    assert roster.read(p) == entries
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_create_uses_class_prefixed_guac_user_for_numbered_students():
    pve, guac = FakePve(), FakeGuac()
    (e,) = provision.create(cd(["01"]), CFG, pve, guac, parallel=1, **QUIET)
    assert e.guac_user == "k8s-101-01" and "k8s-101-01" in guac.users
    assert sorted(c["name"] for c in guac.connections.values()) == [
        "k8s-101-01 Desktop",
        "k8s-101-01 SSH",
    ]


def test_create_continues_past_failures_and_reports_them():
    pve = FakePve(fail_clone_for={"lab-k8s-101-bob"}, agent_timeout_for={"lab-k8s-101-carol"})
    guac = FakeGuac(fail_user_for={"dave"})
    names = ["alice", "bob", "carol", "dave"]
    entries = provision.create(cd(names), CFG, pve, guac, parallel=2, **QUIET)
    by = {e.student: e for e in entries}
    assert [e.student for e in entries] == names
    assert by["alice"].error == ""
    assert "clone failed" in by["bob"].error and by["bob"].vmid == 0
    assert by["carol"].ip == "unknown" and "guest agent" in by["carol"].error
    assert "carol" not in guac.users
    assert by["dave"].ip and "Username already exists" in by["dave"].error
    assert by["dave"].vmid in pve.vms
    assert len(roster.read(roster.path("k8s-101"))) == 4


def test_create_retries_next_vmid_when_id_is_taken():
    pve, guac = FakePve(), FakeGuac()
    real_clone = pve.clone
    attempts = []

    def clone(node, template, newid, name, pool, target):
        attempts.append(newid)
        if len(attempts) == 1:
            raise PveError(500, f"newid: VM {newid} already exists")
        return real_clone(node, template, newid, name, pool, target)

    pve.clone = clone
    (e,) = provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    assert attempts == [3100, 3101] and e.vmid == 3101 and e.error == ""


def test_create_uses_fixed_node_when_configured():
    pve, guac = FakePve(), FakeGuac()
    fixed = classdef.ClassDef("k8s-101", (student("alice"),), None, "pve-node9")
    (e,) = provision.create(fixed, CFG, pve, guac, parallel=1, **QUIET)
    assert e.node == "pve-node9"
    assert ("clone", "pve-node6", 3100, "lab-k8s-101-alice", "pve-node9") in pve.calls
    assert next(c[2] for c in pve.calls if c[0] == "config")["tags"] == "lab;class-k8s-101"


def test_destroy_removes_only_this_class_and_tolerates_partial_state():
    pve, guac = FakePve(), FakeGuac()
    (alice,) = provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    (zed,) = provision.create(cd(["zed"], None, "k8s-102"), CFG, pve, guac, parallel=1, **QUIET)
    # a half-provisioned student: VM exists, no Guacamole objects
    pve.vms[3150] = {
        "vmid": 3150,
        "name": "lab-k8s-101-ghost",
        "node": "pve-node7",
        "pool": "lab",
        "status": "running",
        "tags": "lab;class-k8s-101",
        "cores": 8,
        "memory": 1,
        "balloon": 1,
    }
    failures = provision.destroy("k8s-101", CFG, pve, guac, **QUIET)
    assert failures == []
    assert sorted(pve.deleted) == [alice.vmid, 3150] and zed.vmid in pve.vms
    assert "alice" not in guac.users and "zed" in guac.users
    assert "k8s-101" not in guac.groups and "k8s-102" in guac.groups
    assert [c["name"] for c in guac.connections.values()] == [
        "k8s-102-zed SSH",
        "k8s-102-zed Desktop",
    ]
    assert not roster.path("k8s-101").exists()


def test_destroy_unknown_class_is_an_error():
    failures = provision.destroy("nope", CFG, FakePve(), FakeGuac(), **QUIET)
    assert failures == ["no VMs tagged class-nope in pool lab"]


def test_destroy_reports_pve_failures_and_keeps_going():
    pve, guac = FakePve(), FakeGuac()
    provision.create(cd(["alice", "bob"]), CFG, pve, guac, parallel=1, **QUIET)
    real_delete = pve.delete

    def delete(node, vmid):
        if vmid == 3100:
            raise PveError(500, "storage busy")
        return real_delete(node, vmid)

    pve.delete = delete
    failures = provision.destroy("k8s-101", CFG, pve, guac, **QUIET)
    assert failures == ["lab-k8s-101-alice: PVE 500: storage busy"]
    assert pve.deleted == [3101] and not guac.users and not guac.groups


def test_list_and_expired_and_describe():
    pve, guac = FakePve(), FakeGuac()
    provision.create(cd(["alice", "bob"], "2026-01-01"), CFG, pve, guac, parallel=1, **QUIET)
    pve.vms[100] = {
        "vmid": 100,
        "name": "other",
        "node": "n",
        "pool": "lab",
        "status": "running",
        "tags": "",
    }
    classes = provision.list_classes(CFG, pve)
    assert list(classes) == ["k8s-101"]
    assert [v["name"] for v in classes["k8s-101"]] == ["lab-k8s-101-alice", "lab-k8s-101-bob"]
    assert classes["k8s-101"][0]["expires"] == "2026-01-01"
    assert provision.expired(CFG, pve, date(2026, 9, 28)) == ["k8s-101"]
    assert provision.expired(CFG, pve, date(2026, 1, 1)) == []
    back = provision.describe("k8s-101", CFG, pve)
    assert [s.name for s in back.students] == ["alice", "bob"]
    assert back.expires == date(2026, 1, 1) and back.node == "pve-node7"
    assert back.students[0] == student("alice")


def test_describe_unknown_class_raises():
    with pytest.raises(provision.ProvisionError, match="class-nope"):
        provision.describe("nope", CFG, FakePve())
