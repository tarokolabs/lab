import re

import pytest

from tklab import cli, config, setup
from tklab.pve import PveError

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
    from tklab.pve import PveError

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
    assert rc == 2 and "tklab init" in capsys.readouterr().err


def test_progress_lines_flush_when_stdout_is_a_file():
    # `nohup tklab create … > log` must show progress as it happens, not at exit
    assert cli.log.keywords == {"flush": True}


class TokenClient:
    """Stands in for Pve when init checks a kept token: 'stale' is the one secret PVE rejects."""

    def __init__(self, url, token_id, secret, *, ca_file=None):
        self.secret = secret

    def request(self, method, path, params=None):
        if self.secret == "stale":
            raise PveError(401, "invalid token")
        return {"version": "9.2"}


def init(argv, *, pve_admin=None, guac_admin=None, text=(), secrets=(), tty=True):
    pve_admin = pve_admin or FakePveAdmin(nodes=["n1"])
    guac_admin = guac_admin or FakeGuacAdmin()
    t, s = iter(text), iter(secrets)
    rc = cli.main(
        ["init", *argv],
        make_token_client=TokenClient,
        make_admin=lambda url, user, pw, ca_file=None: pve_admin,
        make_guac_admin=lambda url, user, pw, totp: guac_admin,
        make_service_client=lambda url, u, p, secret: ServiceClient(guac_admin, u, p, secret),
        make_clients=lambda cfg, sec: (FakePve(), FakeGuac()),
        ask_text=(lambda q, d: next(t) or d) if tty else None,
        ask_secret=(lambda q: next(s)) if tty else None,
        fetch=lambda url: SUMS,
        tty=tty,
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
    assert "config: created" in out and "TklabClass: created" in out
    assert "user tklab: created" in out
    from tklab import envfile

    e = envfile.read(envfile.path())
    assert set(e) >= {"TK_LAB_PVE_TOKEN", "TK_LAB_PVE_BUILD_TOKEN", "TK_LAB_GUAC_PASSWORD"}
    assert oct(envfile.path().stat().st_mode & 0o777) == "0o600"
    assert "next: tklab create template" in out
    assert "p" not in e.values() and "g" not in e.values()  # admin passwords never land here
    assert (config.config_path().parent / "pve-root-ca.pem").exists()


def test_init_flags_without_admin_password_is_a_usage_error_when_not_a_tty(capsys):
    rc, *_ = init(FLAGS, tty=False)
    assert rc == 2 and "TK_LAB_PVE_ADMIN_PASSWORD" in capsys.readouterr().err


def test_init_rerun_with_a_config_asks_for_the_admin_passwords_on_a_tty(capsys):
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text(setup.render_config(answers(node="n1")))
    # no admin passwords in the environment, but a terminal: ask for them
    rc, pve_admin, guac_admin = init([], text=["", ""], secrets=["pve-pw", "guac-pw"])
    out = capsys.readouterr().out
    assert rc == 0 and "config: kept" in out
    assert ("login", None) in pve_admin.calls and ("create_user", "tklab") in guac_admin.calls


def test_init_interactive_when_nothing_is_given(capsys):
    text = ["https://p", "", "", "", "", "", "", "", "", "https://g", ""]
    rc, *_ = init([], text=text, secrets=["pve-pw", "guac-pw"])
    assert rc == 0 and config.load().guacamole.url == "https://g"
    assert "next: tklab create template" in capsys.readouterr().out


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
    assert rc == 0 and pve_admin.calls == [] and ("create_user", "tklab") in guac_admin.calls


def test_init_manual_prints_script_and_checklist_without_connecting(capsys):
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text(setup.render_config(answers(node="pve-node7")))
    rc, pve_admin, guac_admin = init(["--manual"])
    out = capsys.readouterr().out
    assert rc == 0 and pve_admin.calls == [] and guac_admin.calls == []
    assert "pveum role add TklabClass" in out
    assert "grant /nodes/pve-node7 TklabFetch" in out
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

    text = iter(["https://p", "", "", "", "", "", "", "", "", "https://g", ""])
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
    from tklab.guac import GuacError

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


def test_create_k8s_flag_builds_clusters_and_is_exclusive_with_file(env, tmp_path, capsys):
    rc, pve, _ = run(["create", "class", "k8s-101", "--students", "2", "--k8s"])
    assert rc == 0
    assert sorted(c[1] for c in pve.calls if c[0] == "exec") == [3100, 3101]
    assert "tk8s cluster ready" in capsys.readouterr().out
    f = tmp_path / "c.toml"
    f.write_text('name = "k8s-102"\ncount = 1\n')
    rc, _, _ = run(["create", "class", "-f", str(f), "--k8s"])
    assert rc == 2 and "cannot be combined" in capsys.readouterr().err


def test_create_k8s_from_file_and_describe_toml_keeps_it(env, tmp_path, capsys):
    f = tmp_path / "c.toml"
    f.write_text('name = "k8s-103"\ncount = 1\nk8s = true\n')
    rc, pve, _ = run(["create", "class", "-f", str(f)])
    assert rc == 0 and [c[1] for c in pve.calls if c[0] == "exec"] == [3100]
    capsys.readouterr()
    rc, _, _ = run(["describe", "class", "k8s-103", "-o", "toml"], pve=pve)
    assert rc == 0 and "k8s = true" in capsys.readouterr().out


def test_init_without_admin_password_fails_before_writing_the_config(capsys):
    rc, *_ = init(FLAGS, tty=False)
    assert rc == 2 and "TK_LAB_PVE_ADMIN_PASSWORD" in capsys.readouterr().err
    assert not config.config_path().exists()


def test_init_guacamole_only_without_its_password_fails_before_writing_the_config(capsys):
    rc, *_ = init(["guacamole", *FLAGS], tty=False)
    assert rc == 2 and "TK_LAB_GUAC_ADMIN_PASSWORD" in capsys.readouterr().err
    assert not config.config_path().exists()


def test_init_abort_message_names_what_was_written(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    asked = []

    def guac_password(q):
        asked.append(q)
        raise KeyboardInterrupt

    pve_admin = FakePveAdmin(nodes=["n1"])
    rc = cli.main(
        ["init", *FLAGS, "--node", "n1"],
        make_token_client=TokenClient,
        make_admin=lambda url, user, pw, ca_file=None: pve_admin,
        make_guac_admin=lambda url, user, pw, totp: FakeGuacAdmin(),
        ask_text=lambda q, d: d,
        ask_secret=guac_password,
        fetch=lambda url: SUMS,
        tty=True,
    )
    err = capsys.readouterr().err
    assert rc == 1 and asked == ["Password for guacadmin"]  # interrupted at the Guacamole step
    assert "nothing written" not in err
    assert str(config.config_path()) in err and "secrets.env" in err


def test_init_ctrl_d_aborts_like_ctrl_c(capsys):
    def eof(q, d):
        raise EOFError

    rc = cli.main(["init"], ask_text=eof, ask_secret=lambda q: "x")
    assert rc == 1 and not config.config_path().exists()
    assert "aborted; nothing written" in capsys.readouterr().err


def test_init_keeps_secrets_issued_before_a_later_pve_failure(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    pve_admin = FakePveAdmin(nodes=["n1"])

    def acl_add(*a, **k):
        raise PveError(403, "Permission check failed")

    pve_admin.acl_add = acl_add
    rc, *_ = init([*FLAGS, "--node", "n1"], pve_admin=pve_admin)
    assert rc == 1
    from tklab import envfile

    assert set(envfile.read(envfile.path())) == {"TK_LAB_PVE_TOKEN", "TK_LAB_PVE_BUILD_TOKEN"}


def test_init_replaces_a_kept_token_that_pve_rejects(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")
    from tklab import envfile

    envfile.write(envfile.path(), {"TK_LAB_PVE_TOKEN": "stale", "TK_LAB_PVE_BUILD_TOKEN": "fine"})
    pve_admin = FakePveAdmin(
        users=["lab@pve"], tokens={"lab@pve": {"tklab", "tklab-build"}}, nodes=["n1"]
    )
    rc, *_ = init([*FLAGS, "--node", "n1"], pve_admin=pve_admin)
    out = capsys.readouterr().out
    assert rc == 0 and "token lab@pve!tklab: updated" in out
    assert "token lab@pve!tklab-build: kept" in out
    assert ("token_remove", "lab@pve", "tklab") in pve_admin.calls
    assert envfile.read(envfile.path())["TK_LAB_PVE_TOKEN"] != "stale"


def test_init_logs_the_guacamole_admin_in_for_real(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")
    rc, _, guac_admin = init([*FLAGS, "--node", "n1"])
    assert rc == 0 and guac_admin.calls[0] == ("login", None)


def test_init_without_an_online_node_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    rc, *_ = init([*FLAGS, "--node", "n1"], pve_admin=FakePveAdmin(nodes=[]))
    assert rc == 2 and "no online PVE node" in capsys.readouterr().err


def test_init_names_a_stale_token_exported_by_the_shell_instead_of_rotating(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("TK_LAB_PVE_TOKEN", "stale")
    pve_admin = FakePveAdmin(
        users=["lab@pve"], tokens={"lab@pve": {"tklab", "tklab-build"}}, nodes=["n1"]
    )
    rc, *_ = init([*FLAGS, "--node", "n1"], pve_admin=pve_admin)
    err = capsys.readouterr().err
    assert rc == 2 and "TK_LAB_PVE_TOKEN is set in the environment" in err
    assert not any(c[0] == "token_remove" for c in pve_admin.calls)


def test_init_rerun_keeps_tokens_whose_file_secrets_still_work(monkeypatch, capsys):
    monkeypatch.setenv("TK_LAB_PVE_ADMIN_PASSWORD", "p")
    monkeypatch.setenv("TK_LAB_GUAC_ADMIN_PASSWORD", "g")
    rc, pve_admin, guac_admin = init([*FLAGS, "--node", "n1"])
    assert rc == 0
    capsys.readouterr()
    pve_admin.calls.clear()
    rc, *_ = init(["pve"], pve_admin=pve_admin, guac_admin=guac_admin)
    out = capsys.readouterr().out
    assert rc == 0
    assert "token lab@pve!tklab: kept" in out and "token lab@pve!tklab-build: kept" in out
    assert not any(c[0] in ("token_add", "token_remove") for c in pve_admin.calls)
