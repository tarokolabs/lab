from tklab import setup
from tklab.config import Config, GuacConfig, PveConfig, TemplateConfig, VmConfig

from .fakes import FakePveAdmin


def cfg(node="auto"):
    return Config(
        PveConfig(
            "https://pve",
            "lab@pve!tklab",
            node,
            "lab",
            "nas-nfs",
            3900,
            (3100, 3199),
            "vmbr0",
            build_token_id="lab@pve!tklab-build",
        ),
        GuacConfig("https://guac", "tklab"),
        VmConfig(),
        TemplateConfig(
            "https://x/debian-13-genericcloud-amd64.qcow2", "deadbeef", "v2026.10.1", "1.37.0"
        ),
    )


def test_acl_plan_matches_the_spec():
    plan = setup.acl_plan(cfg("pve-node7"), ["n1", "n2"])
    assert ("/pool/lab", "TklabClass", "lab@pve") in plan
    assert ("/pool/lab", "TklabClass", "lab@pve!tklab") in plan
    assert ("/vms/3900", "TklabTemplateUse", "lab@pve!tklab") in plan
    assert ("/vms/3900", "TklabTemplateBuild", "lab@pve!tklab-build") in plan
    assert ("/storage/nas-nfs", "TklabDisk", "lab@pve!tklab") in plan
    assert ("/storage/nas-nfs", "TklabImage", "lab@pve!tklab-build") in plan
    assert ("/sdn/zones/localnetwork/vmbr0", "TklabBridge", "lab@pve!tklab-build") in plan
    assert ("/nodes/pve-node7", "TklabFetch", "lab@pve!tklab-build") in plan
    assert not any(p[0] == "/nodes/n1" for p in plan)
    auto = setup.acl_plan(cfg("auto"), ["n1", "n2"])
    assert {p[0] for p in auto if p[1] == "TklabFetch"} == {"/nodes/n1", "/nodes/n2"}


def test_reconcile_pve_from_scratch(tmp_path):
    admin = FakePveAdmin(nodes=["n1"])
    results, new_env = setup.reconcile_pve(cfg("n1"), admin, {}, ca_path=tmp_path / "ca.pem")
    by = dict(results)
    assert all(by[r] == "created" for r in setup.ROLES)
    assert by["user lab@pve"] == "created" and by["pool lab"] == "created"
    assert by["token lab@pve!tklab"] == "created"
    assert by["token lab@pve!tklab-build"] == "created"
    assert by["ca"] == "created" and (tmp_path / "ca.pem").read_text().startswith("-----BEGIN")
    assert new_env == {
        "TK_LAB_PVE_TOKEN": "secret-tklab-1",
        "TK_LAB_PVE_BUILD_TOKEN": "secret-tklab-build-2",
    }
    plan = setup.acl_plan(cfg("n1"), ["n1"])
    assert sum(1 for c in admin.calls if c[0] == "acl_add") == len(plan)
    assert all(by[f"acl {p} {r} {who}"] == "created" for p, r, who in plan)


def test_reconcile_pve_is_idempotent(tmp_path):
    admin = FakePveAdmin(nodes=["n1"])
    _, env = setup.reconcile_pve(cfg("n1"), admin, {}, ca_path=tmp_path / "ca.pem")
    admin.calls.clear()
    results, new_env = setup.reconcile_pve(cfg("n1"), admin, env, ca_path=tmp_path / "ca.pem")
    assert admin.calls == [] and new_env == {}
    assert {r for _, r in results} == {"kept"}


def test_reconcile_pve_updates_a_role_whose_privs_drifted(tmp_path):
    admin = FakePveAdmin(roles={"TklabBridge": {"SDN.Use", "Sys.Audit"}}, nodes=["n1"])
    results, _ = setup.reconcile_pve(cfg("n1"), admin, {}, ca_path=tmp_path / "ca.pem")
    assert dict(results)["TklabBridge"] == "updated"
    assert ("role_set", "TklabBridge", ["SDN.Use"]) in admin.calls


def test_reconcile_pve_recreates_token_whose_secret_is_unknown(tmp_path):
    admin = FakePveAdmin(
        users=["lab@pve"], tokens={"lab@pve": {"tklab", "tklab-build"}}, nodes=["n1"]
    )
    env = {"TK_LAB_PVE_TOKEN": "known"}
    results, new_env = setup.reconcile_pve(cfg("n1"), admin, env, ca_path=tmp_path / "ca.pem")
    by = dict(results)
    assert by["token lab@pve!tklab"] == "kept" and by["token lab@pve!tklab-build"] == "updated"
    assert ("token_remove", "lab@pve", "tklab-build") in admin.calls
    assert list(new_env) == ["TK_LAB_PVE_BUILD_TOKEN"]


def test_reconcile_pve_keeps_foreign_acls(tmp_path):
    foreign = {
        "path": "/pool/lab",
        "roleid": "PVEVMAdmin",
        "type": "user",
        "ugid": "lab@pve",
        "propagate": 1,
    }
    admin = FakePveAdmin(acl=[foreign], nodes=["n1"])
    results, _ = setup.reconcile_pve(cfg("n1"), admin, {}, ca_path=tmp_path / "ca.pem")
    assert admin.acl().count(foreign) == 1
    assert ("acl /pool/lab PVEVMAdmin lab@pve", "foreign") in results


def test_reconcile_pve_keeps_ca_when_unchanged(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nCA\n")
    results, _ = setup.reconcile_pve(cfg("n1"), FakePveAdmin(nodes=["n1"]), {}, ca_path=ca)
    assert dict(results)["ca"] == "kept"


def test_manual_pve_script():
    text = setup.manual_pve(cfg("pve-node7"), None)
    assert 'pveum role add TklabClass --privs "' in text
    assert "grant /vms/3900 TklabTemplateBuild 'lab@pve!tklab-build'" in text
    assert "grant /nodes/pve-node7 TklabFetch 'lab@pve!tklab-build'" in text
    auto = setup.manual_pve(cfg("auto"), ["n1", "n2"])
    assert "grant /nodes/n1 TklabFetch" in auto and "grant /nodes/n2 TklabFetch" in auto
    offline = setup.manual_pve(cfg("auto"), None)
    assert "grant /nodes/<node given to create template --node> TklabFetch" in offline


def test_acl_plan_and_manual_cover_the_clone_storage():
    c = cfg("n1")
    local = Config(
        PveConfig(
            c.pve.url,
            c.pve.token_id,
            c.pve.node,
            c.pve.pool,
            c.pve.storage,
            c.pve.template,
            c.pve.vmid_range,
            c.pve.bridge,
            build_token_id=c.pve.build_token_id,
            clone_storage="local-lvm",
        ),
        c.guacamole,
        c.vm,
        c.template,
    )
    plan = setup.acl_plan(local, ["n1"])
    assert ("/storage/local-lvm", "TklabDisk", "lab@pve!tklab") in plan
    assert ("/storage/local-lvm", "TklabDisk", "lab@pve") in plan
    assert not any(p[0] == "/storage/local-lvm" and p[2].endswith("tklab-build") for p in plan)
    assert "grant /storage/local-lvm TklabDisk 'lab@pve!tklab'" in setup.manual_pve(local, None)
    assert not any(p[0] == "/storage/local-lvm" for p in setup.acl_plan(c, ["n1"]))
