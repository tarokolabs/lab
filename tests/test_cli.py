import re

import pytest

from tkctl_lab import cli, config

from .fakes import FakeGuac, FakePve

GOOD = config.INIT_TEMPLATE.replace(
    'image_sha512 = "replace-with-the-value-from-SHA512SUMS"', 'image_sha512 = "abc"'
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("TK_LAB_PVE_TOKEN", "t")
    monkeypatch.setenv("TK_LAB_GUAC_PASSWORD", "p")
    monkeypatch.delenv("TK_ASSUME_YES", raising=False)
    p = config.config_path()
    p.parent.mkdir(parents=True)
    p.write_text(GOOD)
    return tmp_path


def run(argv, pve=None, guac=None):
    pve, guac = pve or FakePve(), guac or FakeGuac()
    rc = cli.main(argv, make_clients=lambda cfg, sec: (pve, guac))
    return rc, pve, guac


def test_create_with_flags_prints_roster(env, capsys):
    argv = ["create", "class", "k8s-101", "--students", "2", "--expires", "2026-10-20"]
    rc, pve, _ = run(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert re.search(r"01\s+3100\s+pve-node7\s+192\.168\.1\.100\s+k8s-101-01\s+\S{16}", out)
    assert "lab-k8s-101-02" in [v["name"] for v in pve.vms.values()]
    assert "roster:" in out


def test_create_flags_and_file_are_exclusive(env, tmp_path, capsys):
    f = tmp_path / "c.toml"
    f.write_text('name = "k8s-101"\ncount = 1\n')
    rc, *_ = run(["create", "class", "-f", str(f), "--students", "3"])
    assert rc == 2 and "cannot be combined" in capsys.readouterr().err


def test_create_needs_name_and_students_without_file(env, capsys):
    rc, *_ = run(["create", "class", "k8s-101"])
    assert rc == 2 and "--students" in capsys.readouterr().err


def test_create_from_file(env, tmp_path):
    f = tmp_path / "c.toml"
    f.write_text(
        'name = "k8s-101"\nstudents = ["alice"]\n'
        '[[students_override]]\nname = "alice"\nmemory = 32768\n'
    )
    rc, pve, _ = run(["create", "class", "-f", str(f)])
    assert rc == 0 and pve.vms[3100]["memory"] == 32768


def test_create_invalid_definition_is_exit_2(env, capsys):
    rc, *_ = run(["create", "class", "K8S", "--students", "0"])
    err = capsys.readouterr().err
    assert rc == 2 and "name must match" in err and "count" in err


def test_create_reports_failures_with_exit_1(env, capsys):
    pve = FakePve(fail_clone_for={"lab-k8s-101-02"})
    rc, *_ = run(["create", "class", "k8s-101", "--students", "2"], pve=pve)
    assert rc == 1 and "clone failed" in capsys.readouterr().out


def test_describe_toml_round_trip(env, tmp_path, capsys):
    pve, guac = FakePve(), FakeGuac()
    run(["create", "class", "k8s-101", "--students", "1", "--expires", "2026-10-20"], pve, guac)
    capsys.readouterr()
    rc, *_ = run(["describe", "class", "k8s-101", "-o", "toml"], pve, guac)
    out = capsys.readouterr().out
    assert rc == 0 and 'name = "k8s-101"' in out and "expires = 2026-10-20" in out
    f = tmp_path / "again.toml"
    f.write_text(out.replace('name = "k8s-101"', 'name = "k8s-102"'))
    rc, *_ = run(["create", "class", "-f", str(f)], pve, guac)
    assert rc == 0
    assert "lab-k8s-102-01" in [v["name"] for v in pve.vms.values()]


def test_describe_plain_and_roster(env, capsys):
    pve, guac = FakePve(), FakeGuac()
    run(["create", "class", "k8s-101", "--students", "1"], pve, guac)
    capsys.readouterr()
    rc, *_ = run(["describe", "class", "k8s-101"], pve, guac)
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("k8s-101: 1 students") and "8 cpu" in out
    rc, *_ = run(["describe", "class", "k8s-101", "--roster"], pve, guac)
    out = capsys.readouterr().out
    assert rc == 0 and re.search(r"k8s-101-01\s+\S{16}", out)


def test_describe_unknown_class_is_exit_1(env, capsys):
    rc, *_ = run(["describe", "class", "nope"])
    assert rc == 1 and "no class nope" in capsys.readouterr().err
    rc, *_ = run(["describe", "class", "nope", "--roster"])
    assert rc == 1 and "no roster" in capsys.readouterr().err


def test_delete_needs_yes(env, capsys, monkeypatch):
    pve, guac = FakePve(), FakeGuac()
    run(["create", "class", "k8s-101", "--students", "1"], pve, guac)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    rc, *_ = run(["delete", "class", "k8s-101"], pve, guac)
    assert rc == 1 and 3100 in pve.vms
    rc, *_ = run(["delete", "class", "k8s-101", "--yes"], pve, guac)
    assert rc == 0 and 3100 not in pve.vms


def test_delete_expired(env, monkeypatch):
    pve, guac = FakePve(), FakeGuac()
    run(["create", "class", "old", "--students", "1", "--expires", "2020-01-01"], pve, guac)
    run(["create", "class", "new", "--students", "1", "--expires", "2999-01-01"], pve, guac)
    monkeypatch.setenv("TK_ASSUME_YES", "1")
    rc, *_ = run(["delete", "class", "--expired"], pve, guac)
    assert rc == 0 and [v["name"] for v in pve.vms.values()] == ["lab-new-01"]


def test_delete_without_target_is_usage_error(env, capsys):
    rc, *_ = run(["delete", "class"])
    assert rc == 2 and "--expired" in capsys.readouterr().err


def test_get_classes_groups_by_class(env, capsys):
    pve, guac = FakePve(), FakeGuac()
    run(["create", "class", "k8s-101", "--students", "2"], pve, guac)
    capsys.readouterr()
    rc, *_ = run(["get", "classes"], pve, guac)
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("k8s-101") and "lab-k8s-101-02" in out


def test_init_writes_config_once_and_prints_pveum(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["init"]) == 0
    assert config.config_path().exists()
    first = capsys.readouterr().out
    assert "wrote" in first and "pveum role add TkctlLab" in first
    config.config_path().write_text(GOOD)
    assert cli.main(["init"]) == 0  # existing config is kept; only the pveum commands are printed
    again = capsys.readouterr().out
    assert "wrote" not in again
    assert "grant /pool/lab TkctlLabClass 'lab@pve!tkctl'" in again
    assert 'pveum acl modify "$1" --tokens "$3" --roles "$2"' in again
    assert 'pveum acl modify "$1" --users lab@pve --roles "$2"' in again
    assert "-privs" not in again.replace("--privs", "")  # long options only, spelled as pveum wants
    assert "pveum user token add lab@pve tkctl --privsep 1" in again
    assert "pveum user token add lab@pve tkctl-build --privsep 1" in again
    # least privilege: no Sys.Audit anywhere, no VM.Monitor, no Pool.Audit; the runtime token
    # may only clone the template, and only the build token may write images or fetch URLs
    assert "Sys.Audit" not in again and "VM.Monitor" not in again and "Pool.Audit" not in again
    assert "Datastore.AllocateTemplate" in again and "Sys.AccessNetwork" in again
    # snippets are only visible with Datastore.Allocate (check_volume_access); build token only
    assert re.search(r"role add TkctlLabImage .*Datastore\.Allocate ", again + " ")
    assert not re.search(r"role add TkctlLabDisk .*Datastore\.Allocate ", again + " ")
    assert "'lab@pve!tkctl'" in again and "'lab@pve!tkctl-build'" in again  # no history expansion
    assert re.search(r"role add TkctlLabTemplateUse .*VM\.Clone", again)
    assert (
        "/vms/3900" in again
        and "/storage/nas-nfs" in again
        and "/sdn/zones/localnetwork/vmbr0" in again
    )
    assert config.config_path().read_text() == GOOD


def test_pve_error_is_one_line_and_exit_1(env, capsys):
    from tkctl_lab.pve import PveError

    class Broken(FakePve):
        def pool_vms(self, pool):
            raise PveError(0, "GET /cluster/resources: certificate verify failed; set pve.ca_file")

    rc = cli.main(["get", "classes"], make_clients=lambda c, s: (Broken(), FakeGuac()))
    err = capsys.readouterr().err
    assert rc == 1 and err.count("\n") == 1 and "set pve.ca_file" in err


def test_create_template_uses_the_build_token_and_node(env, monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_BUILD_TOKEN", "b")
    seen = {}

    def build_client(cfg, token):
        seen["token"] = token
        return FakePve()

    rc = cli.main(["create", "template", "--node", "pve-node7"], make_build_client=build_client)
    out = capsys.readouterr().out
    assert rc == 1 and seen["token"] == "b" and "/mnt/pve/nas-nfs/snippets/" in out
    monkeypatch.delenv("TK_LAB_PVE_BUILD_TOKEN")
    rc = cli.main(["create", "template"], make_build_client=build_client)
    assert rc == 2 and "TK_LAB_PVE_BUILD_TOKEN" in capsys.readouterr().err


def test_get_and_describe_do_not_need_the_guacamole_password(env, monkeypatch, capsys):
    monkeypatch.delenv("TK_LAB_GUAC_PASSWORD")
    seen = {}

    def clients(cfg, sec):
        seen["guac"] = sec.guac_password
        return FakePve(), FakeGuac()

    assert cli.main(["get", "classes"], make_clients=clients) == 0 and seen["guac"] is None
    rc = cli.main(["create", "class", "x", "--students", "1"], make_clients=clients)
    assert rc == 2 and "TK_LAB_GUAC_PASSWORD" in capsys.readouterr().err


def test_missing_secret_is_named(env, monkeypatch, capsys):
    monkeypatch.delenv("TK_LAB_PVE_TOKEN", raising=False)
    rc = cli.main(["get", "classes"], make_clients=lambda c, s: (FakePve(), FakeGuac()))
    assert rc == 2 and "TK_LAB_PVE_TOKEN" in capsys.readouterr().err


def test_missing_config_points_at_init(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rc = cli.main(["get", "classes"], make_clients=lambda c, s: (FakePve(), FakeGuac()))
    assert rc == 2 and "tkctl lab init" in capsys.readouterr().err


def test_progress_lines_flush_when_stdout_is_a_file():
    # `nohup tkctl-lab create … > log` must show progress as it happens, not at exit
    assert cli.log.keywords == {"flush": True}
