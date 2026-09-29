import re

import pytest

from tkctl_lab import cli, config, setup
from tkctl_lab.pve import PveError

from .fakes import FakeGuac, FakeGuacAdmin, FakePve, FakePveAdmin
from .test_setup_config import SUMS, answers
from .test_setup_guac import ServiceClient

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


def init(argv, *, pve_admin=None, guac_admin=None, text=(), secrets=()):
    pve_admin = pve_admin or FakePveAdmin(nodes=["n1"])
    guac_admin = guac_admin or FakeGuacAdmin()
    t, s = iter(text), iter(secrets)
    rc = cli.main(
        ["init", *argv],
        make_admin=lambda url, user, pw, ca_file=None: pve_admin,
        make_guac_admin=lambda url, user, pw, totp: guac_admin,
        make_service_client=lambda url, u, p, secret: ServiceClient(guac_admin, u, p, secret),
        make_clients=lambda cfg, sec: (FakePve(), FakeGuac()),
        ask_text=lambda q, d: next(t) or d,
        ask_secret=lambda q: next(s),
        fetch=lambda url: SUMS,
    )
    return rc, pve_admin, guac_admin


FLAGS = ["--pve-url", "https://p", "--storage", "nas-nfs", "--guacamole-url", "https://g"]


def test_init_with_flags_does_all_three_and_writes_env(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")
    rc, *_ = init([*FLAGS, "--node", "n1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert config.config_path().exists() and config.load().pve.storage == "nas-nfs"
    assert "config: created" in out and "TkctlLabClass: created" in out
    assert "user tkctl-lab: created" in out
    from tkctl_lab import envfile

    e = envfile.read(envfile.path())
    assert set(e) >= {"TK_LAB_PVE_TOKEN", "TK_LAB_PVE_BUILD_TOKEN", "TK_LAB_GUAC_PASSWORD"}
    assert oct(envfile.path().stat().st_mode & 0o777) == "0o600"
    assert "next: tkctl lab create template" in out
    assert "p" not in e.values() and "g" not in e.values()  # admin passwords never land here
    assert (config.config_path().parent / "pve-root-ca.pem").exists()


def test_init_flags_without_admin_password_is_a_usage_error(capsys):
    rc, *_ = init(FLAGS)
    assert rc == 2 and "TK_LAB_PVE_ADMIN_PASSWORD" in capsys.readouterr().err


def test_init_interactive_when_nothing_is_given(capsys):
    text = ["https://p", "", "", "", "", "", "", "", "https://g", ""]
    rc, *_ = init([], text=text, secrets=["pve-pw", "guac-pw"])
    assert rc == 0 and config.load().guacamole.url == "https://g"
    assert "next: tkctl lab create template" in capsys.readouterr().out


def test_init_pve_only_and_rerun_is_kept(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")
    admin = FakePveAdmin(nodes=["n1"])
    init(FLAGS, pve_admin=admin)
    capsys.readouterr()
    rc, _, guac_admin = init(["pve"], pve_admin=admin)
    out = capsys.readouterr().out
    assert rc == 0 and "kept" in out and "created" not in out
    assert guac_admin.calls == []  # guacamole untouched


def test_init_guacamole_only(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text(setup.render_config(answers()))
    rc, pve_admin, guac_admin = init(["guacamole"])
    assert rc == 0 and pve_admin.calls == [] and ("create_user", "tkctl-lab") in guac_admin.calls


def test_init_manual_prints_script_and_checklist_without_connecting(capsys):
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text(setup.render_config(answers(node="pve-node7")))
    rc, pve_admin, guac_admin = init(["--manual"])
    out = capsys.readouterr().out
    assert rc == 0 and pve_admin.calls == [] and guac_admin.calls == []
    assert "pveum role add TkctlLabClass" in out
    assert "grant /nodes/pve-node7 TkctlLabFetch" in out
    assert "Create new connection groups" in out


def test_init_from_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")
    f = tmp_path / "given.toml"
    f.write_text(setup.render_config(answers()))
    rc, *_ = init(["-f", str(f)])
    assert rc == 0 and "config: copied" in capsys.readouterr().out


def test_init_pve_failure_stops_before_guacamole(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")

    class Forbidden(FakePveAdmin):
        def role_add(self, roleid, privs):
            raise PveError(403, "POST /access/roles: Permission check failed")

    rc, _, guac_admin = init(FLAGS, pve_admin=Forbidden(nodes=["n1"]))
    err = capsys.readouterr().err
    assert rc == 1 and "Permissions.Modify" in err and "--manual" in err
    assert guac_admin.calls == []


def test_init_ctrl_c_leaves_nothing_behind(capsys):
    def interrupted(q, d):
        raise KeyboardInterrupt

    rc = cli.main(["init"], ask_text=interrupted, ask_secret=lambda q: "x")
    assert rc == 1 and not config.config_path().exists()
    assert "aborted" in capsys.readouterr().err


def test_init_uses_the_well_known_ca_before_a_config_exists(capsys):
    ca = config.config_path().parent / "pve-root-ca.pem"
    ca.parent.mkdir(parents=True)
    ca.write_text("-----BEGIN CERTIFICATE-----\nCA\n")
    seen = {}

    def make_admin(url, user, pw, ca_file=None):
        seen["ca_file"] = ca_file
        return FakePveAdmin(nodes=["n1"])

    text = iter(["https://p", "", "", "", "", "", "", "", "https://g", ""])
    guac_admin = FakeGuacAdmin()
    rc = cli.main(
        ["init"],
        make_admin=make_admin,
        make_guac_admin=lambda url, user, pw, totp: guac_admin,
        make_service_client=lambda url, u, p, s: ServiceClient(guac_admin, u, p, s),
        ask_text=lambda q, d: next(text) or d,
        ask_secret=lambda q: "pw",
        fetch=lambda url: SUMS,
    )
    assert rc == 0 and seen["ca_file"] == str(ca)


def test_init_guacamole_totp_admin_message_does_not_point_at_the_service_secret(
    monkeypatch, capsys
):
    from tkctl_lab.guac import GuacError

    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text(setup.render_config(answers()))

    class NeedsCode(FakeGuacAdmin):
        def login(self, totp=None):
            raise GuacError(
                403, "Verification code required", {"expected": [{"name": "guac-totp"}]}
            )

    rc, *_ = init(["guacamole"], guac_admin=NeedsCode())
    err = capsys.readouterr().err
    assert rc == 1 and "TOTP" in err and "--manual" in err
    assert "TK_LAB_GUAC_TOTP_SECRET" not in err


def test_init_403_hint_names_sys_modify(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")

    class Forbidden(FakePveAdmin):
        def role_add(self, roleid, privs):
            raise PveError(403, "POST /access/roles: Permission check failed")

    rc, *_ = init([*FLAGS], pve_admin=Forbidden(nodes=["n1"]))
    assert rc == 1 and "Sys.Modify" in capsys.readouterr().err
