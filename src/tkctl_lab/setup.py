"""The one-time setup: look at what exists on PVE and Guacamole, create or fix what is missing."""

from __future__ import annotations

import secrets as _secrets
import shutil
import string
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config as configmod
from . import guac as guacmod
from .config import Config


class SetupError(Exception):
    """A setup step cannot proceed; the message says what to give or fix."""


USER = "lab@pve"
TOKENS = ("tkctl", "tkctl-build")
TOKEN_ENV = {"tkctl": "TK_LAB_PVE_TOKEN", "tkctl-build": "TK_LAB_PVE_BUILD_TOKEN"}
Result = tuple[str, str]  # (item, created | updated | kept | removed | foreign)

# Least privilege. Two tokens on one user: the class token can clone the template and manage
# VMs in the lab pool only; the build token, used by `create template` alone, may also write
# images to the storage and make the node fetch a URL. Nothing is granted on /, /vms or /nodes
# for VMs, so VMs outside the pool stay out of reach. Both the user and each token need the
# ACLs because the tokens are privilege-separated.
ROLES = {
    "TkctlLabClass": [
        "VM.Audit",
        "VM.Allocate",
        "VM.PowerMgmt",
        "VM.Config.CPU",
        "VM.Config.Memory",
        "VM.Config.Options",
        "VM.Config.Cloudinit",
        "VM.Config.Disk",
        "VM.GuestAgent.Audit",
        "Pool.Audit",  # else /cluster/resources omits the pool field and classes are invisible
        "VM.Migrate",  # a full clone is made next to the template, then moved to its node
    ],
    "TkctlLabTemplateUse": ["VM.Audit", "VM.Clone"],
    "TkctlLabTemplateBuild": [
        "VM.Audit",
        "VM.Allocate",
        "VM.PowerMgmt",
        "VM.Config.CPU",
        "VM.Config.Memory",
        "VM.Config.Options",
        "VM.Config.Cloudinit",
        "VM.Config.Disk",
        "VM.Config.HWType",
        "VM.Config.Network",
    ],
    "TkctlLabDisk": ["Datastore.AllocateSpace", "Datastore.Audit"],
    # Datastore.Allocate: PVE only lets a caller see or reference snippets with it. It also allows
    # deleting volumes on the storage, so it sits on the build token alone; remove that token
    # once the template exists.
    "TkctlLabImage": [
        "Datastore.AllocateSpace",
        "Datastore.AllocateTemplate",
        "Datastore.Allocate",
        "Datastore.Audit",
    ],
    "TkctlLabBridge": ["SDN.Use"],
    "TkctlLabFetch": ["Sys.AccessNetwork"],
}


def _principals(cfg: Config) -> tuple[str, str]:
    return cfg.pve.token_id, cfg.pve.build_token_id or f"{USER}!tkctl-build"


def acl_plan(cfg: Config, nodes: list[str]) -> list[tuple[str, str, str]]:
    """(path, role, principal) for the user and both tokens; nodes matter for pve.node = auto."""
    class_token, build_token = _principals(cfg)
    p = cfg.pve
    bridge = f"/sdn/zones/localnetwork/{p.bridge}"
    build_nodes = nodes if p.node == "auto" else [p.node]
    plan: list[tuple[str, str, str]] = []
    for who in (USER, class_token):
        plan += [
            (f"/pool/{p.pool}", "TkctlLabClass", who),
            (f"/vms/{p.template}", "TkctlLabTemplateUse", who),
            (f"/storage/{p.storage}", "TkctlLabDisk", who),
            (bridge, "TkctlLabBridge", who),
        ]
        if p.clone_storage:
            plan.append((f"/storage/{p.clone_storage}", "TkctlLabDisk", who))
    for who in (USER, build_token):
        plan += [
            (f"/vms/{p.template}", "TkctlLabTemplateBuild", who),
            (f"/storage/{p.storage}", "TkctlLabImage", who),
            (bridge, "TkctlLabBridge", who),
        ]
        plan += [(f"/nodes/{n}", "TkctlLabFetch", who) for n in build_nodes]
    return plan


def reconcile_pve(
    cfg: Config, admin, env: dict[str, str], *, ca_path: Path
) -> tuple[list[Result], dict[str, str]]:
    """Bring PVE to the state the config describes; returns what happened and new secrets."""
    results: list[Result] = []
    new_env: dict[str, str] = {}

    existing = admin.roles()
    for role, privs in ROLES.items():
        if role not in existing:
            admin.role_add(role, privs)
            results.append((role, "created"))
        elif existing[role] != set(privs):
            admin.role_set(role, privs)
            results.append((role, "updated"))
        else:
            results.append((role, "kept"))

    if USER in admin.users():
        results.append((f"user {USER}", "kept"))
    else:
        admin.user_add(USER, "tkctl lab service account")
        results.append((f"user {USER}", "created"))

    if cfg.pve.pool in admin.pools():
        results.append((f"pool {cfg.pve.pool}", "kept"))
    else:
        admin.pool_add(cfg.pve.pool, "tkctl lab classes")
        results.append((f"pool {cfg.pve.pool}", "created"))

    have = admin.tokens(USER)
    for tokenid in TOKENS:
        key = TOKEN_ENV[tokenid]
        label = f"token {USER}!{tokenid}"
        if tokenid in have and env.get(key):
            results.append((label, "kept"))
            continue
        if tokenid in have:
            # Its secret is gone for good; a new token is the only way back.
            admin.token_remove(USER, tokenid)
        new_env[key] = admin.token_add(USER, tokenid)
        results.append((label, "updated" if tokenid in have else "created"))

    nodes = admin.nodes()
    current = {(a["path"], a["roleid"], a["ugid"]) for a in admin.acl()}
    wanted = acl_plan(cfg, nodes)
    for path, role, who in wanted:
        label = f"acl {path} {role} {who}"
        if (path, role, who) in current:
            results.append((label, "kept"))
        else:
            if "!" in who:
                admin.acl_add(path, role, token=who)
            else:
                admin.acl_add(path, role, user=who)
            results.append((label, "created"))
    ours = set(wanted)
    for a in admin.acl():
        key = (a["path"], a["roleid"], a["ugid"])
        mine = a["ugid"] == USER or a["ugid"].startswith(f"{USER}!")
        if key not in ours and mine:
            results.append((f"acl {a['path']} {a['roleid']} {a['ugid']}", "foreign"))

    pem = admin.ca_pem(nodes[0] if cfg.pve.node == "auto" else cfg.pve.node)
    existed = ca_path.exists()
    if existed and ca_path.read_text() == pem:
        results.append(("ca", "kept"))
    else:
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_text(pem)
        results.append(("ca", "updated" if existed else "created"))
    return results, new_env


MANUAL_PVE = """# Run once as a PVE administrator.
{roles}
pveum user add lab@pve --comment "tkctl lab service account"
pveum pool add {pool} --comment "tkctl lab classes"
pveum user token add lab@pve tkctl --privsep 1        # -> TK_LAB_PVE_TOKEN
pveum user token add lab@pve tkctl-build --privsep 1  # -> TK_LAB_PVE_BUILD_TOKEN
grant() {{  # path role token: the user and the privilege-separated token both need the ACL
  pveum acl modify "$1" --users lab@pve --roles "$2"
  pveum acl modify "$1" --tokens "$3" --roles "$2"
}}
{grants}
"""


def manual_pve(cfg: Config, nodes: list[str] | None) -> str:
    """The pveum script that does what reconcile_pve does, for an administrator to paste."""
    build_nodes = nodes or ["<node given to create template --node>"]
    plan = acl_plan(cfg, build_nodes)
    class_token, build_token = _principals(cfg)
    grants = "\n".join(
        f"grant {path} {role} '{who}'"
        for path, role, who in plan
        if who in (class_token, build_token)
    )
    roles = "\n".join(f'pveum role add {r} --privs "{" ".join(p)}"' for r, p in ROLES.items())
    return MANUAL_PVE.format(roles=roles, pool=cfg.pve.pool, grants=grants)


# --- Guacamole
SERVICE_PERMS = ("CREATE_USER", "CREATE_CONNECTION", "CREATE_CONNECTION_GROUP")
ALPHABET = string.ascii_letters + string.digits


def new_password() -> str:
    return "".join(_secrets.choice(ALPHABET) for _ in range(24))


def _totp_state(client) -> tuple[str, str | None]:
    """Log the service account in once: 'none' (no TOTP), 'fresh' (secret offered), 'enrolled'."""
    try:
        client.login()
    except guacmod.GuacError as e:
        field = guacmod.challenge(e)
        if field is None:
            raise
        return ("fresh", field["secret"]) if field.get("secret") else ("enrolled", None)
    client.logout()
    return "none", None


def _secret_works(client) -> bool:
    try:
        client.login()
    except guacmod.GuacError:
        return False
    client.logout()
    return True


def reconcile_guacamole(
    cfg: Config, admin, env: dict[str, str], *, make_client
) -> tuple[list[Result], dict[str, str]]:
    """Bring the Guacamole service account to the state the tool needs; returns new secrets.

    make_client(username, password, totp_secret) builds a client that logs in as the service
    account; it is how the TOTP state is discovered and enrolled.
    """
    results: list[Result] = []
    new_env: dict[str, str] = {}
    user = cfg.guacamole.username
    password = env.get("TK_LAB_GUAC_PASSWORD", "")

    if admin.get_user(user) is None:
        password = new_password()
        admin.create_user(user, password)
        new_env["TK_LAB_GUAC_PASSWORD"] = password
        results.append((f"user {user}", "created"))
    elif not password:
        password = new_password()
        admin.set_password(user, password)
        new_env["TK_LAB_GUAC_PASSWORD"] = password
        results.append((f"user {user}", "updated"))
    else:
        results.append((f"user {user}", "kept"))

    have = admin.system_permissions(user)
    missing = [p for p in SERVICE_PERMS if p not in have]
    if missing:
        admin.grant_system(user, missing)
        results.append(("permissions", "updated" if have else "created"))
    else:
        results.append(("permissions", "kept"))
    for extra in sorted(have - set(SERVICE_PERMS)):
        results.append((f"permission {extra}", "foreign"))

    secret = env.get("TK_LAB_GUAC_TOTP_SECRET") or None
    # Probe without the secret first: no challenge means TOTP is off, a challenge with a secret
    # means the account is not enrolled yet, a bare challenge means it is.
    try:
        state, offered = _totp_state(make_client(user, password, None))
    except guacmod.GuacError as e:
        if e.status != 403 or "TK_LAB_GUAC_PASSWORD" not in env:
            raise
        # The password we hold no longer opens the account: it is ours, so rotate it.
        password = new_password()
        admin.set_password(user, password)
        new_env["TK_LAB_GUAC_PASSWORD"] = password
        slot = next(i for i, (item, _) in enumerate(results) if item == f"user {user}")
        results[slot] = (f"user {user}", "updated")
        state, offered = _totp_state(make_client(user, password, None))
    if state == "none":
        if secret:
            new_env["TK_LAB_GUAC_TOTP_SECRET"] = ""
            results.append(("totp", "removed"))
        else:
            results.append(("totp", "kept"))
    else:
        if state == "enrolled" and secret and _secret_works(make_client(user, password, secret)):
            results.append(("totp", "kept"))
        else:
            cleared = False
            if state == "enrolled":
                admin.clear_totp(user)
                cleared = True
                state, offered = _totp_state(make_client(user, password, None))
                if state != "fresh":
                    raise guacmod.GuacError(
                        500, f"{user}: TOTP reset did not take; clear it in the admin UI and rerun"
                    )
            assert offered
            make_client(user, password, offered).enrol(guacmod.totp(offered))
            new_env["TK_LAB_GUAC_TOTP_SECRET"] = offered
            results.append(("totp", "updated" if (secret or cleared) else "created"))
    admin.logout()
    return results, new_env


MANUAL_GUAC = """# Guacamole: do this once as an administrator (Settings -> Users -> New User).
Username:      {user}
Password:      choose one, then put it in TK_LAB_GUAC_PASSWORD
Permissions:   tick only "Create new users", "Create new connections",
               "Create new connection groups"
TOTP:          if the TOTP extension is installed and cannot be disabled per user, log in as
               {user} once, copy the secret shown under Details into TK_LAB_GUAC_TOTP_SECRET,
               and finish the enrolment with an authenticator app
Guacamole URL: {url}
"""


def manual_guacamole(cfg: Config) -> str:
    return MANUAL_GUAC.format(user=cfg.guacamole.username, url=cfg.guacamole.url)


# --- the config file
@dataclass(frozen=True)
class Answers:
    pve_url: str
    pve_admin: str
    node: str
    pool: str
    storage: str
    bridge: str
    template: int
    vmid_range: tuple[int, int]
    guacamole_url: str
    guacamole_admin: str
    image_sha512: str
    clone_storage: str | None = None
    nodes: tuple[str, ...] = ()


DEFAULT_IMAGE = (
    "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
)
DEFAULTS: dict = dict(
    pve_admin="root@pam",
    node="auto",
    pool="lab",
    bridge="vmbr0",
    template=3900,
    vmid_range=(3100, 3199),
    guacamole_admin="guacadmin",
)
REQUIRED_FLAGS = ("pve_url", "storage", "guacamole_url")


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode()


def fetch_sha512(image_url: str, fetch=_http_get) -> str:
    base, name = image_url.rsplit("/", 1)
    for line in fetch(f"{base}/SHA512SUMS").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0]
    raise SetupError(f"{name} is not listed in {base}/SHA512SUMS")


def render_config(a: Answers) -> str:
    """The commented INIT_TEMPLATE with the answers filled in; admin identities never land here."""
    text = configmod.INIT_TEMPLATE
    ca = configmod.config_path().parent / "pve-root-ca.pem"
    subs = {
        'url = "https://pve-node1:8006"': f'url = "{a.pve_url}"',
        'node = "auto"': f'node = "{a.node}"',
        'pool = "lab"': f'pool = "{a.pool}"',
        'storage = "nas-nfs"': f'storage = "{a.storage}"',
        "template = 3900": f"template = {a.template}",
        "vmid_range = [3100, 3199]": f"vmid_range = [{a.vmid_range[0]}, {a.vmid_range[1]}]",
        'bridge = "vmbr0"': f'bridge = "{a.bridge}"',
        '# ca_file = "/path/to/pve-root-ca.pem"': f'ca_file = "{ca}"',
        'url = "https://guac.example"': f'url = "{a.guacamole_url}"',
        'image_sha512 = "replace-with-the-value-from-SHA512SUMS"': (
            f'image_sha512 = "{a.image_sha512}"'
        ),
    }
    if a.clone_storage:
        subs['# clone_storage = "local-lvm"'] = f'clone_storage = "{a.clone_storage}"'.ljust(30)
    if a.nodes:
        listed = ", ".join(f'"{n}"' for n in a.nodes)
        subs['# nodes = ["pve-node6", "pve-node7"]'] = f"nodes = [{listed}]".ljust(35)
    for old, new in subs.items():
        assert old in text, old
        text = text.replace(old, new, 1)
    return text


def _parse_nodes(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return tuple(n.strip() for n in value.split(",") if n.strip())
    return tuple(value)


def _parse_range(value) -> tuple[int, int]:
    if isinstance(value, tuple):
        return value
    lo, hi = value.split("-")
    return int(lo), int(hi)


def ensure_config(
    *, path: Path, flags: dict, file: Path | None, force: bool, interactive, fetch
) -> tuple[Config, str]:
    """Make sure the config exists and loads; returns it and created | copied | kept."""
    if file is not None:
        if path.exists() and path.read_text() == file.read_text():
            return configmod.load(path), "kept"
        if path.exists() and not force:
            raise SetupError(f"{path} exists and differs from {file}; pass --force to replace it")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, path)
        return configmod.load(path), "copied"
    if path.exists():
        return configmod.load(path), "kept"
    given = {k: v for k, v in flags.items() if v is not None}
    if given:
        missing = [f"--{k.replace('_', '-')}" for k in REQUIRED_FLAGS if k not in given]
        if missing:
            raise SetupError("missing " + ", ".join(missing))
        values = DEFAULTS | given
        values["vmid_range"] = _parse_range(values["vmid_range"])
        values["template"] = int(values["template"])
        values["nodes"] = _parse_nodes(values.get("nodes"))
        a = Answers(**values, image_sha512=fetch_sha512(DEFAULT_IMAGE, fetch=fetch))
    elif interactive is not None:
        a = interactive()
    else:
        raise SetupError(
            "no config yet: run `tkctl lab init` interactively, pass flags, or -f FILE"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(a))
    return configmod.load(path), "created"
