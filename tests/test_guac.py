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
