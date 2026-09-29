from tkctl_lab import setup
from tkctl_lab.guac import GuacError

from .fakes import FakeGuacAdmin
from .test_setup_pve import cfg


class ServiceClient:
    """What make_client returns: a Guac-like object whose login goes through the fake admin."""

    def __init__(self, fake, username, password, totp_secret):
        self.fake, self.username, self.password, self.totp_secret = (
            fake,
            username,
            password,
            totp_secret,
        )

    def login(self):
        r = self.fake.service_login(self.username, self.password, self.totp_secret)
        if r["secret"]:
            # the real client raises the challenge; setup reads the secret from it
            body = {"expected": [{"name": "guac-totp", "secret": r["secret"]}]}
            raise GuacError(403, "Verification code required", body)

    def enrol(self, code):
        self.fake.calls.append(("enrol", self.username, code))

    def logout(self):
        pass


def run(fake, env):
    return setup.reconcile_guacamole(
        cfg(), fake, env, make_client=lambda u, p, s: ServiceClient(fake, u, p, s)
    )


def test_creates_account_permissions_and_no_totp():
    fake = FakeGuacAdmin()
    results, new_env = run(fake, {})
    by = dict(results)
    assert by["user tkctl-lab"] == "created" and by["permissions"] == "created"
    assert by["totp"] == "kept"
    assert fake.perms["tkctl-lab"] == set(setup.SERVICE_PERMS)
    assert new_env["TK_LAB_GUAC_PASSWORD"] == fake.users["tkctl-lab"]
    assert len(new_env["TK_LAB_GUAC_PASSWORD"]) == 24
    assert "TK_LAB_GUAC_TOTP_SECRET" not in new_env
    assert ("logout",) in fake.calls


def test_idempotent_when_everything_exists():
    fake = FakeGuacAdmin(users={"tkctl-lab": "pw"}, perms={"tkctl-lab": set(setup.SERVICE_PERMS)})
    results, new_env = run(fake, {"TK_LAB_GUAC_PASSWORD": "pw"})
    assert {r for _, r in results} == {"kept"} and new_env == {}
    assert [c for c in fake.calls if c[0] != "logout"] == []


def test_resets_password_when_env_lacks_it_and_warns_on_extra_permissions():
    fake = FakeGuacAdmin(
        users={"tkctl-lab": "old"}, perms={"tkctl-lab": {"CREATE_USER", "ADMINISTER"}}
    )
    results, new_env = run(fake, {})
    by = dict(results)
    assert by["user tkctl-lab"] == "updated" and new_env["TK_LAB_GUAC_PASSWORD"] != "old"
    assert by["permissions"] == "updated" and ("permission ADMINISTER", "foreign") in results
    assert fake.perms["tkctl-lab"] >= set(setup.SERVICE_PERMS)


def test_enrols_totp_from_a_fresh_challenge():
    fake = FakeGuacAdmin(totp="fresh")
    results, new_env = run(fake, {})
    assert dict(results)["totp"] == "created"
    assert new_env["TK_LAB_GUAC_TOTP_SECRET"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert any(c[0] == "enrol" and len(c[2]) == 6 for c in fake.calls)


def test_clears_and_reenrols_when_secret_is_unknown():
    fake = FakeGuacAdmin(
        users={"tkctl-lab": "pw"}, perms={"tkctl-lab": set(setup.SERVICE_PERMS)}, totp="enrolled"
    )
    fake.enrolled_secret = "OLDSECRET"
    results, new_env = run(fake, {"TK_LAB_GUAC_PASSWORD": "pw"})
    assert dict(results)["totp"] == "updated"
    assert ("clear_totp", "tkctl-lab") in fake.calls
    assert new_env["TK_LAB_GUAC_TOTP_SECRET"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_keeps_known_secret():
    fake = FakeGuacAdmin(
        users={"tkctl-lab": "pw"}, perms={"tkctl-lab": set(setup.SERVICE_PERMS)}, totp="enrolled"
    )
    fake.enrolled_secret = "KNOWN"
    env = {"TK_LAB_GUAC_PASSWORD": "pw", "TK_LAB_GUAC_TOTP_SECRET": "KNOWN"}
    results, new_env = run(fake, env)
    assert dict(results)["totp"] == "kept" and new_env == {}


def test_drops_stale_secret_when_totp_is_gone():
    fake = FakeGuacAdmin(
        users={"tkctl-lab": "pw"}, perms={"tkctl-lab": set(setup.SERVICE_PERMS)}, totp="off"
    )
    env = {"TK_LAB_GUAC_PASSWORD": "pw", "TK_LAB_GUAC_TOTP_SECRET": "STALE"}
    results, new_env = run(fake, env)
    assert dict(results)["totp"] == "removed" and new_env == {"TK_LAB_GUAC_TOTP_SECRET": ""}


def test_manual_guacamole_lists_the_three_permissions():
    text = setup.manual_guacamole(cfg())
    for p in ("Create new users", "Create new connections", "Create new connection groups"):
        assert p in text
    assert "tkctl-lab" in text and "TK_LAB_GUAC_PASSWORD" in text
    assert "TK_LAB_GUAC_TOTP_SECRET" in text


def test_new_password_is_24_alnum():
    p = setup.new_password()
    assert len(p) == 24 and p.isalnum() and setup.new_password() != p


def test_rotates_a_stale_password_instead_of_dying():
    fake = FakeGuacAdmin(users={"tkctl-lab": "real"}, perms={"tkctl-lab": set(setup.SERVICE_PERMS)})
    results, new_env = run(fake, {"TK_LAB_GUAC_PASSWORD": "stale"})
    by = dict(results)
    assert by["user tkctl-lab"] == "updated" and ("set_password", "tkctl-lab") in fake.calls
    assert new_env["TK_LAB_GUAC_PASSWORD"] == fake.users["tkctl-lab"] != "stale"
    assert by["totp"] == "kept"
