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


def test_size_parsing():
    assert provision.size_bytes("60G") == 60 * 1024**3
    assert provision.size_bytes("512M") == 512 * 1024**2 and provision.size_bytes("1T") == 1024**4
    with pytest.raises(ValueError):
        provision.size_bytes("big")


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
    # the clone task is waited on the template's node (the UPID carries it), before set_config
    waits = [c[1] for c in pve.calls if c[0] == "wait"]
    assert waits[0] == "UPID:pve-node6:clone-3101"
    assert pve.calls.index(("wait", waits[0])) < next(
        i for i, c in enumerate(pve.calls) if c[0] == "config"
    )
    kv = next(c[2] for c in pve.calls if c[0] == "config" and c[1] == 3101)
    assert kv["ciuser"] == "student" and kv["ipconfig0"] == "ip=dhcp" and kv["ciupgrade"] == 0
    assert e.vm_password == kv["cipassword"]
    assert not any(c[0] == "resize" for c in pve.calls)  # 60G equals the template disk
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


def test_create_skips_vmids_the_pool_token_cannot_see():
    # 3100 is a VM outside the pool: invisible in /cluster/resources, known to /cluster/nextid
    pve, guac = FakePve(hidden={3100}), FakeGuac()
    (e,) = provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    assert e.vmid == 3101 and e.error == ""
    assert [c for c in pve.calls if c[0] == "clone"] == [
        ("clone", "pve-node6", 3101, "lab-k8s-101-alice", "pve-node7")
    ]


def test_create_never_reuses_a_vmid_across_parallel_students():
    # next_vmid never reserves and in-flight clones are invisible to the token, so the
    # allocator must remember what it handed out; a taken id from the API is also remembered
    pve, guac = FakePve(), FakeGuac()
    real_clone = pve.clone
    seen = []

    def clone(node, template, newid, name, pool, target):
        seen.append(newid)
        if newid == 3101 and seen.count(3101) == 1:
            raise PveError(500, "newid: unable to create VM 3101: config file already exists")
        return real_clone(node, template, newid, name, pool, target)

    pve.clone = clone
    names = ["a", "b", "c", "d", "e"]
    entries = provision.create(cd(names), CFG, pve, guac, parallel=5, **QUIET)
    assert [e.error for e in entries] == [""] * 5
    assert sorted(e.vmid for e in entries) == [3100, 3102, 3103, 3104, 3105]


def test_create_spreads_auto_node_round_robin():
    pve, guac = FakePve(nodes=("pve-node8", "pve-node7")), FakeGuac()
    entries = provision.create(cd(["a", "b", "c"]), CFG, pve, guac, parallel=1, **QUIET)
    assert [e.node for e in entries] == ["pve-node8", "pve-node7", "pve-node8"]


def test_create_grows_disk_when_the_class_asks_for_more_and_refuses_shrink():
    pve, guac = FakePve(), FakeGuac()
    big = classdef.ClassDef("k8s-101", (classdef.Student("a", 8, 24576, 8192, "100G"),), None, None)
    (e,) = provision.create(big, CFG, pve, guac, parallel=1, **QUIET)
    assert e.error == "" and ("resize", e.vmid, "scsi0", "100G") in pve.calls
    small = classdef.ClassDef(
        "k8s-102", (classdef.Student("a", 8, 24576, 8192, "20G"),), None, None
    )
    (e,) = provision.create(small, CFG, pve, guac, parallel=1, **QUIET)
    assert "smaller than the template" in e.error and e.vmid == 0
    assert not any(v["name"] == "lab-k8s-102-a" for v in pve.vms.values())


def test_create_refuses_an_existing_class_without_roster():
    pve, guac = FakePve(), FakeGuac()
    provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    roster.path("k8s-101").unlink()
    with pytest.raises(provision.ProvisionError, match="already has 1 VM"):
        provision.create(cd(["alice", "bob"]), CFG, pve, guac, parallel=1, **QUIET)


def test_create_resumes_failed_students_from_the_roster():
    pve = FakePve(fail_clone_for={"lab-k8s-101-bob"}, agent_timeout_for={"lab-k8s-101-carol"})
    guac = FakeGuac(fail_user_for={"dave"})
    names = ["alice", "bob", "carol", "dave"]
    first = provision.create(cd(names), CFG, pve, guac, parallel=1, **QUIET)
    assert sum(1 for e in first if e.error) == 3
    # the world got better: bob's clone works, carol's agent answers, dave's user can be created
    pve.fail_clone_for.clear()
    pve.agent_timeout_for.clear()
    guac.fail_user_for.clear()
    again = provision.create(cd(names), CFG, pve, guac, parallel=1, **QUIET)
    by = {e.student: e for e in again}
    assert [e.error for e in again] == [""] * 4
    assert by["alice"] == first[0]  # untouched: same VM, same passwords
    assert by["carol"].vmid == first[2].vmid and by["carol"].ip == "192.168.1.100"
    assert by["carol"].vm_password == first[2].vm_password
    assert by["dave"].vmid == first[3].vmid and "dave" in guac.users
    assert sorted(v["name"] for v in pve.vms.values()) == [f"lab-k8s-101-{n}" for n in names]
    assert len([c for c in pve.calls if c[0] == "clone"]) == 5  # 4 first + 1 for bob
    assert roster.read(roster.path("k8s-101")) == again


def test_create_records_unexpected_errors_per_student():
    pve, guac = FakePve(), FakeGuac()

    def boom(node, vmid, timeout=300):
        raise TimeoutError("read timed out")

    pve.agent_ipv4 = boom
    (e,) = provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    assert "read timed out" in e.error and e.vmid == 3100
    assert roster.read(roster.path("k8s-101")) == [e]


def test_create_uses_fixed_node_when_configured():
    pve, guac = FakePve(), FakeGuac()
    fixed = classdef.ClassDef("k8s-101", (student("alice"),), None, "pve-node9")
    (e,) = provision.create(fixed, CFG, pve, guac, parallel=1, **QUIET)
    assert e.node == "pve-node9"
    assert ("clone", "pve-node6", 3100, "lab-k8s-101-alice", "pve-node9") in pve.calls
    assert next(c[2] for c in pve.calls if c[0] == "config")["tags"] == "lab;class-k8s-101"


def test_create_reports_a_pve_error_before_any_student(tmp_path):
    pve, guac = FakePve(), FakeGuac()

    def vm_node(vmid):
        raise PveError(403, "GET /cluster/resources: Permission check failed")

    pve.vm_node = vm_node
    with pytest.raises(PveError, match="Permission check failed"):
        provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    assert not roster.path("k8s-101").exists()


def test_destroy_removes_only_this_class_and_tolerates_partial_state():
    pve, guac = FakePve(), FakeGuac()
    (alice,) = provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    (zed,) = provision.create(cd(["zed"], None, "k8s-102"), CFG, pve, guac, parallel=1, **QUIET)
    # a half-provisioned student: the clone finished but set_config never ran, so no tags,
    # and no Guacamole objects; the name is the only mark
    pve.vms[3150] = {
        "vmid": 3150,
        "name": "lab-k8s-101-ghost",
        "node": "pve-node7",
        "pool": "lab",
        "status": "running",
        "tags": "",
        "cores": 8,
        "memory": 1,
        "balloon": 1,
        "disk": "60G",
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
    assert failures == ["no class nope: no VMs in pool lab, no Guacamole group, no roster"]


def test_destroy_cleans_guacamole_group_when_every_clone_failed():
    pve = FakePve(fail_clone_for={"lab-k8s-101-alice"})
    guac = FakeGuac()
    provision.create(cd(["alice"]), CFG, pve, guac, parallel=1, **QUIET)
    assert "k8s-101" in guac.groups and roster.path("k8s-101").exists()
    assert provision.destroy("k8s-101", CFG, pve, guac, **QUIET) == []
    assert "k8s-101" not in guac.groups and not roster.path("k8s-101").exists()


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
    pve.vms[3150] = {
        "vmid": 3150,
        "name": "lab-k8s-101-ghost",
        "node": "pve-node7",
        "pool": "lab",
        "status": "stopped",
        "tags": "",
        "cores": 8,
        "memory": 24576,
        "balloon": 8192,
        "disk": "60G",
    }
    classes = provision.list_classes(CFG, pve)
    assert list(classes) == ["k8s-101"]
    assert [v["name"] for v in classes["k8s-101"]] == [
        "lab-k8s-101-alice",
        "lab-k8s-101-bob",
        "lab-k8s-101-ghost",  # untagged but named: listed so it can be deleted
    ]
    assert classes["k8s-101"][0]["expires"] == "2026-01-01"
    assert provision.expired(CFG, pve, date(2026, 9, 28)) == ["k8s-101"]
    assert provision.expired(CFG, pve, date(2026, 1, 1)) == []
    back = provision.describe("k8s-101", CFG, pve)
    assert [s.name for s in back.students] == ["alice", "bob", "ghost"]
    assert back.expires == date(2026, 1, 1) and back.node == "pve-node7"
    assert back.students[0] == student("alice")


def test_describe_unknown_class_raises():
    with pytest.raises(provision.ProvisionError, match="no class nope"):
        provision.describe("nope", CFG, FakePve())


def test_describe_leaves_node_unset_when_a_class_spans_nodes():
    pve, guac = FakePve(nodes=("n1", "n2")), FakeGuac()
    provision.create(cd(["a", "b"]), CFG, pve, guac, parallel=1, **QUIET)
    assert provision.describe("k8s-101", CFG, pve).node is None
