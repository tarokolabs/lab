"""tkctl-lab: the tkctl plugin that provisions per-student VMs on PVE with Guacamole access."""

from __future__ import annotations

import argparse
import functools
import os
import sys
import tomllib
from datetime import date
from pathlib import Path

from . import __version__, classdef, config, provision, roster
from .guac import Guac, GuacError
from .pve import Pve, PveError

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
PVE_SETUP = """# Run once as a PVE administrator.
{roles}
pveum user add lab@pve --comment "tkctl lab service account"
pveum pool add {pool} --comment "tkctl lab classes"
pveum user token add lab@pve tkctl --privsep 1        # -> {token_env}
pveum user token add lab@pve tkctl-build --privsep 1  # -> {build_env}
grant() {{  # path role token: the user and the privilege-separated token both need the ACL
  pveum acl modify "$1" --users lab@pve --roles "$2"
  pveum acl modify "$1" --tokens "$3" --roles "$2"
}}
grant /pool/{pool} TkctlLabClass 'lab@pve!tkctl'
grant /vms/{template} TkctlLabTemplateUse 'lab@pve!tkctl'
grant /storage/{storage} TkctlLabDisk 'lab@pve!tkctl'
grant /sdn/zones/localnetwork/{bridge} TkctlLabBridge 'lab@pve!tkctl'
grant /vms/{template} TkctlLabTemplateBuild 'lab@pve!tkctl-build'
grant /storage/{storage} TkctlLabImage 'lab@pve!tkctl-build'
grant /sdn/zones/localnetwork/{bridge} TkctlLabBridge 'lab@pve!tkctl-build'
grant /nodes/{build_node} TkctlLabFetch 'lab@pve!tkctl-build'
"""


def _role_lines() -> str:
    return "\n".join(f'pveum role add {r} --privs "{" ".join(p)}"' for r, p in ROLES.items())


# Progress lines go out immediately even when stdout is a log file.
log = functools.partial(print, flush=True)


def _clients(cfg: config.Config, sec: config.Secrets):
    pve = Pve(cfg.pve.url, cfg.pve.token_id, sec.pve_token, ca_file=cfg.pve.ca_file)
    guac = Guac(
        cfg.guacamole.url,
        cfg.guacamole.username,
        sec.guac_password or "",
        totp_secret=sec.guac_totp,
    )
    return pve, guac


def _build_client(cfg: config.Config, token: str):
    token_id = cfg.pve.build_token_id or cfg.pve.token_id
    return Pve(cfg.pve.url, token_id, token, ca_file=cfg.pve.ca_file)


def _fail(msg: str, rc: int = 2) -> int:
    print(f"tkctl lab: {msg}", file=sys.stderr)
    return rc


def _confirm(prompt: str, yes: bool) -> bool:
    if yes or os.environ.get("TK_ASSUME_YES") == "1":
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def _print_roster(entries: list[roster.Entry]) -> None:
    head = ("STUDENT", "VMID", "NODE", "IP", "GUACAMOLE USER", "PASSWORD", "ERROR")
    print(f"{head[0]:10} {head[1]:6} {head[2]:12} {head[3]:16} {head[4]:16} {head[5]:18} {head[6]}")
    for e in entries:
        print(
            f"{e.student:10} {e.vmid:<6} {e.node:12} {e.ip:16} {e.guac_user:16} "
            f"{e.guac_password:18} {e.error}"
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tkctl lab",
        description="Per-student tk8s VMs on Proxmox VE with Guacamole access.",
    )
    p.add_argument("--version", action="version", version=__version__)
    verbs = p.add_subparsers(dest="verb", required=True)
    verbs.add_parser(
        "init", help="write the config template (kept if present) and print the PVE setup commands"
    )

    create = verbs.add_parser("create", help="create a class or the template")
    create_nouns = create.add_subparsers(dest="noun", required=True)
    c = create_nouns.add_parser("class", help="one VM and one Guacamole login per student")
    c.add_argument("name", nargs="?")
    c.add_argument(
        "-f", "--file", type=Path, help="class definition file (TOML); excludes the topology flags"
    )
    c.add_argument("--students", type=int, help="number of students (01, 02, ...)")
    c.add_argument("--expires", help="date the class may be deleted with --expired (YYYY-MM-DD)")
    c.add_argument("--node", help="PVE node for every VM (default: pve.node from the config)")
    c.add_argument("--cores", type=int)
    c.add_argument("--memory", type=int, help="MiB")
    c.add_argument("--parallel", type=int, default=5, help="students provisioned at once")
    t = create_nouns.add_parser("template", help="build the node template from the cloud image")
    t.add_argument("--k8s", help="Kubernetes version of the node image to pre-pull")
    t.add_argument("--node", help="PVE node to build on (required when pve.node is auto)")

    get = verbs.add_parser("get", help="list classes")
    get.add_subparsers(dest="noun", required=True).add_parser("classes").add_argument(
        "name", nargs="?"
    )

    desc = verbs.add_parser("describe", help="show a class")
    d = desc.add_subparsers(dest="noun", required=True).add_parser("class")
    d.add_argument("name")
    d.add_argument("--roster", action="store_true", help="print the roster with passwords")
    d.add_argument("-o", choices=["toml"], help="print a class file that create -f accepts")

    dele = verbs.add_parser("delete", help="delete a class")
    x = dele.add_subparsers(dest="noun", required=True).add_parser("class")
    x.add_argument("name", nargs="?")
    x.add_argument(
        "--expired", action="store_true", help="every class whose expires tag is in the past"
    )
    x.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return p


def _init() -> int:
    p = config.config_path()
    if p.exists():
        print(f"config: {p} (kept)")
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(config.INIT_TEMPLATE)
        envs = f"{config.PVE_TOKEN_ENV} and {config.GUAC_PASSWORD_ENV}"
        print(f"wrote {p}; fill it in, then export {envs}")
    # The pveum commands only need the resource names, so a half-filled config is fine here.
    try:
        pve = tomllib.loads(p.read_text()).get("pve", {})
    except tomllib.TOMLDecodeError, OSError:
        pve = {}
    node = pve.get("node", "auto")
    print(
        PVE_SETUP.format(
            roles=_role_lines(),
            pool=pve.get("pool", "lab"),
            template=pve.get("template", 3900),
            storage=pve.get("storage", "nas-nfs"),
            bridge=pve.get("bridge", "vmbr0"),
            build_node=node if node != "auto" else "<node given to create template --node>",
            token_env=config.PVE_TOKEN_ENV,
            build_env=config.PVE_BUILD_TOKEN_ENV,
        ),
        end="",
    )
    return 0


def _create_class(args, cfg, pve, guac) -> int:
    topo = [
        k
        for k in ("students", "expires", "node", "cores", "memory")
        if getattr(args, k) is not None
    ]
    try:
        if args.file:
            if topo or args.name:
                return _fail("-f cannot be combined with a name or the topology flags")
            cd = classdef.from_file(args.file, vm=cfg.vm)
        else:
            if not args.name or args.students is None:
                return _fail("create class needs a name and --students N, or -f FILE")
            cd = classdef.from_flags(
                args.name,
                args.students,
                vm=cfg.vm,
                expires=args.expires,
                node=args.node,
                cores=args.cores,
                memory=args.memory,
            )
    except classdef.ClassDefError as e:
        return _fail(str(e))
    try:
        entries = provision.create(cd, cfg, pve, guac, parallel=args.parallel, log=log)
    except provision.ProvisionError as e:
        return _fail(str(e), 1)
    _print_roster(entries)
    print(f"roster: {roster.path(cd.name)}")
    return 1 if any(e.error for e in entries) else 0


def _get_classes(args, cfg, pve) -> int:
    for cls, vms in provision.list_classes(cfg, pve).items():
        if args.name and cls != args.name:
            continue
        print(f"{cls}   ({len(vms)} VMs, expires {vms[0]['expires'] or 'never'})")
        for v in vms:
            print(f"  {v['vmid']:<6} {v['name']:28} {v['node']:12} {v['status']}")
    return 0


def _describe_class(args, cfg, pve) -> int:
    if args.roster:
        p = roster.path(args.name)
        if not p.exists():
            return _fail(f"no roster for {args.name} ({p})", 1)
        _print_roster(roster.read(p))
        return 0
    try:
        cd = provision.describe(args.name, cfg, pve)
    except provision.ProvisionError as e:
        return _fail(str(e), 1)
    if args.o == "toml":
        print(classdef.to_toml(cd), end="")
        return 0
    print(
        f"{cd.name}: {len(cd.students)} students, expires {cd.expires or 'never'}, node {cd.node}"
    )
    for s in cd.students:
        print(f"  {s.name:12} {s.cores} cpu  {s.memory} MiB  {s.disk}")
    return 0


def _delete_class(args, cfg, pve, guac) -> int:
    if args.expired:
        names = provision.expired(cfg, pve, date.today())
        if not names:
            print("tkctl lab: nothing to delete", file=sys.stderr)
            return 0
    elif args.name:
        names = [args.name]
    else:
        return _fail("delete class needs a name or --expired")
    rc = 0
    for n in names:
        if not _confirm(f"Delete class {n}: its VMs, Guacamole users and connections?", args.yes):
            print("aborted")
            return 1
        failures = provision.destroy(n, cfg, pve, guac, log=log)
        for f in failures:
            print(f"  failed: {f}", file=sys.stderr)
        rc = rc or (1 if failures else 0)
        print(f"class {n} deleted" + (f" with {len(failures)} failure(s)" if failures else ""))
    return rc


def _dispatch(args, *, make_clients, make_build_client) -> int:
    cfg = config.load()
    if args.verb == "create" and args.noun == "template":
        from . import template

        pve = (make_build_client or _build_client)(cfg, config.build_secret())
        return template.build(cfg, pve, k8s=args.k8s, node=args.node, log=log)
    needs_guac = args.verb in ("create", "delete")
    pve, guac = (make_clients or _clients)(cfg, config.secrets(guacamole=needs_guac))
    if args.verb == "create":
        return _create_class(args, cfg, pve, guac)
    if args.verb == "get":
        return _get_classes(args, cfg, pve)
    if args.verb == "describe":
        return _describe_class(args, cfg, pve)
    if args.verb == "delete":
        return _delete_class(args, cfg, pve, guac)
    return 2


def main(argv: list[str] | None = None, *, make_clients=None, make_build_client=None) -> int:
    args = build_parser().parse_args(argv)
    if args.verb == "init":
        return _init()
    try:
        return _dispatch(args, make_clients=make_clients, make_build_client=make_build_client)
    except config.ConfigError as e:
        return _fail(str(e))
    except (PveError, GuacError) as e:
        return _fail(str(e), 1)
