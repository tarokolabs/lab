import pytest

from tkctl_lab import config, prompt, setup
from tkctl_lab.pve import PveError

from .fakes import FakePveAdmin

SUMS = "abc123  debian-13-genericcloud-amd64.qcow2\nzzz  other.qcow2\n"
IMAGE = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
TEXT = ["https://p", "", "", "", "", "", "", "", "https://g", ""]


def answers(**kw):
    base = dict(
        pve_url="https://192.168.1.253:8006",
        pve_admin="root@pam",
        node="auto",
        pool="lab",
        storage="nas-nfs",
        bridge="vmbr0",
        template=3900,
        vmid_range=(3100, 3199),
        guacamole_url="https://guac",
        guacamole_admin="guacadmin",
        image_sha512="abc123",
    )
    return setup.Answers(**(base | kw))


def ensure(**kw):
    args = dict(flags={}, file=None, force=False, interactive=None, fetch=lambda u: SUMS)
    return setup.ensure_config(**(args | kw))


def test_fetch_sha512_reads_sums_next_to_the_image():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return SUMS

    assert setup.fetch_sha512(IMAGE, fetch=fetch) == "abc123"
    assert fetched == ["https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"]
    with pytest.raises(setup.SetupError, match="not listed"):
        setup.fetch_sha512("https://x/nope.qcow2", fetch=lambda u: SUMS)


def test_render_config_loads_and_points_ca_file_into_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    text = setup.render_config(answers(node="pve-node7"))
    p = tmp_path / "lab.toml"
    p.write_text(text)
    c = config.load(p)
    assert (c.pve.url, c.pve.node, c.pve.storage) == (
        "https://192.168.1.253:8006",
        "pve-node7",
        "nas-nfs",
    )
    assert c.pve.vmid_range == (3100, 3199)
    assert c.pve.ca_file == str(tmp_path / "tkctl" / "pve-root-ca.pem")
    assert c.template.image_sha512 == "abc123" and c.guacamole.url == "https://guac"
    assert "root@pam" not in text and "guacadmin" not in text  # admin identities never land here


def test_ensure_config_from_flags(tmp_path):
    p = tmp_path / "lab.toml"
    flags = {"pve_url": "https://p", "storage": "nas-nfs", "guacamole_url": "https://g"}
    c, state = ensure(path=p, flags=flags)
    assert state == "created" and p.exists() and c.pve.pool == "lab" and c.pve.node == "auto"
    c2, _ = ensure(path=tmp_path / "b.toml", flags=flags | {"vmid_range": "3200-3210"})
    assert c2.pve.vmid_range == (3200, 3210)


def test_ensure_config_flags_need_the_three_required_values(tmp_path):
    with pytest.raises(setup.SetupError, match="--storage"):
        ensure(path=tmp_path / "lab.toml", flags={"pve_url": "https://p"})


def test_ensure_config_from_file_copies_and_refuses_to_overwrite_a_different_one(tmp_path):
    src = tmp_path / "given.toml"
    src.write_text(setup.render_config(answers()))
    p = tmp_path / "lab.toml"
    _, state = ensure(path=p, file=src, fetch=None)
    assert state == "copied" and p.read_text() == src.read_text()
    _, state = ensure(path=p, file=src, fetch=None)
    assert state == "kept"
    src.write_text(setup.render_config(answers(pool="other")))
    with pytest.raises(setup.SetupError, match="--force"):
        ensure(path=p, file=src, fetch=None)
    _, state = ensure(path=p, file=src, force=True, fetch=None)
    assert state == "copied" and "other" in p.read_text()


def test_ensure_config_uses_existing_file_and_validates_it(tmp_path):
    p = tmp_path / "lab.toml"
    p.write_text("[pve]\nurl = 1\n")
    with pytest.raises(config.ConfigError):
        ensure(path=p, fetch=None)


def test_ensure_config_without_input_needs_interactive(tmp_path):
    with pytest.raises(setup.SetupError, match="interactive"):
        ensure(path=tmp_path / "lab.toml", fetch=None)


def test_interactive_writes_nothing_until_all_answers_are_in(tmp_path):
    p = tmp_path / "lab.toml"
    calls = []

    def interactive():
        calls.append("asked")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ensure(path=p, interactive=interactive)
    assert calls == ["asked"] and not p.exists()


def test_prompt_walks_the_questions_and_uses_pve_for_choices():
    admin = FakePveAdmin(nodes=["pve-node7", "pve-node8"])
    script = iter(
        [
            "https://192.168.1.253:8006",  # pve url
            "",  # admin user (default root@pam)
            "",  # storage (default: the first eligible)
            "",  # bridge (default vmbr0)
            "",  # node (default auto)
            "",  # pool
            "",  # template vmid
            "",  # vmid range
            "https://guac",  # guacamole url
            "",  # guacamole admin (default guacadmin)
        ]
    )
    secrets = iter(["pve-pass", "guac-pass"])
    seen = []

    def ask_text(question, default):
        seen.append(question)
        return next(script) or default

    made = {}

    def admin_factory(url, user, password):
        made.update(url=url, user=user, password=password)
        return admin

    a, got_admin, guac_pw = prompt.ask(
        admin_factory=admin_factory,
        ask_text=ask_text,
        ask_secret=lambda q: next(secrets),
        guac_login=lambda url, user, pw, totp: None,
    )
    assert made == {"url": "https://192.168.1.253:8006", "user": "root@pam", "password": "pve-pass"}
    assert got_admin is admin and guac_pw == "guac-pass"
    assert (a.storage, a.bridge, a.node, a.pool) == ("nas-nfs", "vmbr0", "auto", "lab")
    assert (a.template, a.vmid_range) == (3900, (3100, 3199))
    assert a.guacamole_url == "https://guac" and a.guacamole_admin == "guacadmin"
    assert any("nas-nfs" in q for q in seen)  # the eligible storage is offered in the question


def test_prompt_retries_pve_login_and_asks_totp_when_challenged():
    attempts = []

    def admin_factory(url, user, password):
        attempts.append(password)
        if len(attempts) == 1:
            raise PveError(401, "authentication failure")
        if len(attempts) == 2:
            raise PveError(401, "root@pam needs a TFA code")
        return FakePveAdmin(nodes=["n1"])

    text = iter(TEXT)
    secrets = iter(["wrong", "right", "123456", "right", "guac-pw"])
    _, admin, _ = prompt.ask(
        admin_factory=admin_factory,
        ask_text=lambda q, d: next(text) or d,
        ask_secret=lambda q: next(secrets),
        guac_login=lambda url, user, pw, totp: None,
    )
    assert attempts == ["wrong", "right", "right"] and admin.nodes() == ["n1"]
    assert ("login", "123456") in admin.calls


def test_interactive_asks_totp_when_challenged_by_guacamole():
    from tkctl_lab.guac import GuacError

    tries = []

    def guac_login(url, user, pw, totp):
        tries.append(totp)
        if totp is None:
            body = {"expected": [{"name": "guac-totp"}]}
            raise GuacError(403, "Verification code required", body)

    text = iter(TEXT)
    secrets = iter(["pve-pw", "guac-pw", "654321"])
    prompt.ask(
        admin_factory=lambda u, us, p: FakePveAdmin(nodes=["n1"]),
        ask_text=lambda q, d: next(text) or d,
        ask_secret=lambda q: next(secrets),
        guac_login=guac_login,
    )
    assert tries == [None, "654321"]


def test_prompt_refuses_when_no_storage_qualifies():
    class NoStorage(FakePveAdmin):
        def storages(self):
            return [{"storage": "local-lvm", "type": "lvmthin", "content": "images", "shared": 0}]

    text = iter(["https://p", ""])
    with pytest.raises(setup.SetupError, match="snippets"):
        prompt.ask(
            admin_factory=lambda u, us, p: NoStorage(nodes=["n1"]),
            ask_text=lambda q, d: next(text) or d,
            ask_secret=lambda q: "pw",
            guac_login=lambda *a: None,
        )
