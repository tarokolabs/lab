import json
import urllib.error
from io import BytesIO

import pytest

from tkctl_lab import guac

BASE = "https://guac/api"
DS = "/session/data/postgresql"


class FakeResp(BytesIO):
    def __init__(self, obj):
        super().__init__(json.dumps(obj).encode() if obj is not None else b"")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_with(routes):
    calls = []

    def open_(req, timeout=None, context=None):
        key = (req.get_method(), req.full_url.replace(BASE, ""))
        body = req.data.decode() if req.data else None
        calls.append((key, body, dict(req.header_items())))
        if key not in routes:
            payload = BytesIO(b'{"message":"no route"}')
            raise urllib.error.HTTPError(req.full_url, 501, f"no route {key}", {}, payload)
        r = routes[key]
        r = r(req, body) if callable(r) else r
        if isinstance(r, Exception):
            raise r
        return FakeResp(r)

    open_.calls = calls
    return open_


TOKEN = {("POST", "/tokens"): {"authToken": "T", "dataSource": "postgresql"}}


def client(routes):
    op = opener_with(TOKEN | routes)
    return guac.Guac("https://guac", "svc", "pw", opener=op), op


def bodies(op):
    return {k: json.loads(b) for (k, b, _) in op.calls if b and k != ("POST", "/tokens")}


def test_login_form_and_token_header_on_later_calls():
    c, op = client({("GET", f"{DS}/connectionGroups/ROOT/tree"): {"childConnectionGroups": []}})
    assert c.find_group("k8s-101") is None
    (k1, body1, _), (_k2, _, h2) = op.calls
    assert k1 == ("POST", "/tokens") and body1 == "username=svc&password=pw"
    assert h2["Guacamole-token"] == "T"


def test_ensure_group_reuses_or_creates():
    tree = {"childConnectionGroups": [{"identifier": "7", "name": "k8s-101"}]}
    c, _ = client({("GET", f"{DS}/connectionGroups/ROOT/tree"): tree})
    assert c.ensure_group("k8s-101") == "7"
    c2, op2 = client(
        {
            ("GET", f"{DS}/connectionGroups/ROOT/tree"): {"childConnectionGroups": []},
            ("POST", f"{DS}/connectionGroups"): {"identifier": "9"},
        }
    )
    assert c2.ensure_group("k8s-102") == "9"
    body = bodies(op2)[("POST", f"{DS}/connectionGroups")]
    expected = {
        "parentIdentifier": "ROOT",
        "name": "k8s-102",
        "type": "ORGANIZATIONAL",
        "attributes": {},
    }
    assert body == expected


def test_create_user_connection_and_grant():
    routes = {
        ("POST", f"{DS}/users"): None,
        ("POST", f"{DS}/connections"): {"identifier": "42"},
        ("PATCH", f"{DS}/users/alice/permissions"): None,
    }
    c, op = client(routes)
    c.create_user("alice", "s3cret")
    params = guac.SSH_PARAMS("192.168.1.77", "student", "pw")
    assert c.create_connection("7", "k8s-101-alice SSH", "ssh", params) == "42"
    c.grant("alice", connections=["42"], groups=["7"])
    b = bodies(op)
    assert b[("POST", f"{DS}/users")] == {
        "username": "alice",
        "password": "s3cret",
        "attributes": {},
    }
    conn = b[("POST", f"{DS}/connections")]
    assert conn["protocol"] == "ssh" and conn["parentIdentifier"] == "7"
    assert conn["parameters"]["hostname"] == "192.168.1.77"
    patch = b[("PATCH", f"{DS}/users/alice/permissions")]
    assert {"op": "add", "path": "/connectionPermissions/42", "value": "READ"} in patch
    assert {"op": "add", "path": "/connectionGroupPermissions/7", "value": "READ"} in patch


def test_rdp_params_resize_and_ignore_cert():
    p = guac.RDP_PARAMS("h", "student", "pw")
    assert p["port"] == "3389" and p["ignore-cert"] == "true"
    assert p["resize-method"] == "display-update"


def test_group_connections_and_deletes():
    tree = {
        "childConnections": [
            {"identifier": "1", "name": "a SSH"},
            {"identifier": "2", "name": "a Desktop"},
        ]
    }
    routes = {
        ("GET", f"{DS}/connectionGroups/7/tree"): tree,
        ("DELETE", f"{DS}/connections/1"): None,
        ("DELETE", f"{DS}/users/alice"): None,
        ("DELETE", f"{DS}/connectionGroups/7"): None,
    }
    c, op = client(routes)
    assert [x["identifier"] for x in c.group_connections("7")] == ["1", "2"]
    c.delete_connection("1")
    c.delete_user("alice")
    c.delete_group("7")
    assert [k for (k, _, _) in op.calls if k[0] == "DELETE"] == [
        ("DELETE", f"{DS}/connections/1"),
        ("DELETE", f"{DS}/users/alice"),
        ("DELETE", f"{DS}/connectionGroups/7"),
    ]


def test_error_message_from_guacamole():
    payload = BytesIO(b'{"message":"Username already exists"}')
    err = urllib.error.HTTPError("u", 400, "Bad Request", {}, payload)
    c, _ = client({("POST", f"{DS}/users"): err})
    with pytest.raises(guac.GuacError, match="Username already exists"):
        c.create_user("alice", "x")


def test_totp_matches_rfc6238_vector():
    # RFC 6238 appendix B, SHA-1, secret "12345678901234567890" at T=59 → 94287082 (8 digits)
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert guac.totp(secret, at=59) == "287082"
    assert guac.totp(secret, at=1111111109) == "081804"
    assert guac.totp("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", at=59) == "287082"  # spaced/lower


def test_login_answers_a_totp_challenge_when_a_secret_is_configured():
    challenge = {
        "message": "Verification code required",
        "expected": [{"name": "guac-totp", "type": "GUACAMOLE_TOTP_CODE"}],
        "type": "INSUFFICIENT_CREDENTIALS",
    }
    calls = []

    def tokens(req, body):
        calls.append(body)
        if "guac-totp=" not in body:
            payload = BytesIO(json.dumps(challenge).encode())
            return urllib.error.HTTPError("u", 403, "Forbidden", {}, payload)
        return {"authToken": "T", "dataSource": "postgresql"}

    op = opener_with({("POST", "/tokens"): tokens})
    c = guac.Guac(
        "https://guac", "svc", "pw", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", opener=op
    )
    c.login()
    assert len(calls) == 2 and calls[0] == "username=svc&password=pw"
    assert calls[1].startswith("username=svc&password=pw&guac-totp=")
    assert len(calls[1].rsplit("=", 1)[1]) == 6


def test_login_without_secret_reports_the_totp_requirement():
    challenge = {"message": "Verification code required", "expected": [{"name": "guac-totp"}]}
    payload = BytesIO(json.dumps(challenge).encode())
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, payload)
    op = opener_with({("POST", "/tokens"): err})
    with pytest.raises(guac.GuacError, match="TK_LAB_GUAC_TOTP_SECRET"):
        guac.Guac("https://guac", "svc", "pw", opener=op).login()


def test_login_retries_a_rejected_totp_code_on_the_next_period():
    # Guacamole refuses a code that was already used in the same 30 s period (replay protection),
    # which happens when two commands run back to back; wait for the next period and try once more
    challenge = {"expected": [{"name": "guac-totp"}]}
    rejected = {"message": "Provided TOTP code is not valid.", "type": "INVALID_CREDENTIALS"}
    calls, slept = [], []

    def tokens(req, body):
        calls.append(body)
        if "guac-totp=" not in body:
            return urllib.error.HTTPError(
                "u", 403, "F", {}, BytesIO(json.dumps(challenge).encode())
            )
        if not slept:
            return urllib.error.HTTPError("u", 400, "B", {}, BytesIO(json.dumps(rejected).encode()))
        return {"authToken": "T", "dataSource": "mysql"}

    op = opener_with({("POST", "/tokens"): tokens})
    c = guac.Guac(
        "https://guac",
        "svc",
        "pw",
        totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
        opener=op,
        sleep=slept.append,
    )
    c.login()
    assert len(calls) == 3 and c.data_source == "mysql"
    assert 0 < slept[0] <= 31


def test_challenge_parses_totp_field():
    body = {"expected": [{"name": "guac-totp", "secret": "GEZD", "digits": 6}]}
    err = guac.GuacError(403, "x", body)
    assert guac.challenge(err) == {"name": "guac-totp", "secret": "GEZD", "digits": 6}
    other = guac.GuacError(403, "x", {"expected": [{"name": "username"}]})
    assert guac.challenge(other) is None
    assert guac.challenge(guac.GuacError(400, "x", {})) is None


def test_login_accepts_a_one_time_code():
    challenge = {"expected": [{"name": "guac-totp"}]}
    calls = []

    def tokens(req, body):
        calls.append(body)
        if "guac-totp=" not in body:
            payload = BytesIO(json.dumps(challenge).encode())
            return urllib.error.HTTPError("u", 403, "F", {}, payload)
        return {"authToken": "T", "dataSource": "mysql"}

    c = guac.Guac("https://guac", "admin", "pw", opener=opener_with({("POST", "/tokens"): tokens}))
    c.login(totp="654321")
    assert calls[1].endswith("&guac-totp=654321")


def test_admin_user_methods_shapes():
    missing = urllib.error.HTTPError(
        "u", 404, "Not Found", {}, BytesIO(b'{"message":"no such user"}')
    )
    routes = {
        ("GET", f"{DS}/users/nobody"): missing,
        ("GET", f"{DS}/users/tkctl-lab"): {"username": "tkctl-lab", "attributes": {}},
        ("PUT", f"{DS}/users/tkctl-lab"): None,
        ("GET", f"{DS}/users/tkctl-lab/permissions"): {
            "systemPermissions": ["CREATE_USER"],
            "connectionPermissions": {},
        },
        ("PATCH", f"{DS}/users/tkctl-lab/permissions"): None,
        ("DELETE", "/tokens/T"): None,
    }
    c, op = client(routes)
    assert c.get_user("nobody") is None
    assert c.get_user("tkctl-lab")["username"] == "tkctl-lab"
    c.set_password("tkctl-lab", "new-pw")
    assert c.system_permissions("tkctl-lab") == {"CREATE_USER"}
    c.grant_system("tkctl-lab", ["CREATE_CONNECTION", "CREATE_CONNECTION_GROUP"])
    c.clear_totp("tkctl-lab")
    c.logout()
    b = bodies(op)
    puts = [json.loads(x) for (k, x, _) in op.calls if k == ("PUT", f"{DS}/users/tkctl-lab") and x]
    assert puts[0] == {"username": "tkctl-lab", "password": "new-pw", "attributes": {}}
    assert puts[1]["attributes"] == {"guac-totp-key-secret": "", "guac-totp-key-confirmed": "false"}
    assert b[("PATCH", f"{DS}/users/tkctl-lab/permissions")] == [
        {"op": "add", "path": "/systemPermissions", "value": "CREATE_CONNECTION"},
        {"op": "add", "path": "/systemPermissions", "value": "CREATE_CONNECTION_GROUP"},
    ]
    assert ("DELETE", "/tokens/T") in [k for (k, _, _) in op.calls]
    assert c.token is None


def test_enrol_posts_the_code_and_drops_the_session():
    calls = []

    def tokens(req, body):
        calls.append(body)
        return {"authToken": "T2", "dataSource": "mysql"}

    op = opener_with({("POST", "/tokens"): tokens, ("DELETE", "/tokens/T2"): None})
    c = guac.Guac("https://guac", "svc", "pw", opener=op)
    c.enrol("123456")
    assert calls == ["username=svc&password=pw&guac-totp=123456"]
    assert ("DELETE", "/tokens/T2") in [k for (k, _, _) in op.calls] and c.token is None
