"""The one-time setup: look at what exists on PVE and Guacamole, create or fix what is missing."""

from __future__ import annotations

from pathlib import Path

from .config import Config

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
