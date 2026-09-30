"""tkctl-lab: the tkctl plugin that provisions per-student VMs on PVE with Guacamole access."""

from __future__ import annotations

import argparse
import dataclasses
import functools
import getpass
import os
import sys
from datetime import date
from pathlib import Path

from . import __version__, classdef, config, envfile, prompt, provision, roster, setup
from . import guac as guacmod
from .guac import Guac, GuacError
from .pve import Pve, PveAdmin, PveError

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
    init_p = verbs.add_parser(
        "init", help="set up the config, PVE and Guacamole (all, or one side)"
    )
    init_p.add_argument("noun", nargs="?", choices=["pve", "guacamole"], help="only this side")
    init_p.add_argument(
        "--manual", action="store_true", help="print what to do by hand instead of doing it"
    )
    init_p.add_argument("-f", "--file", type=Path, help="use this config file")
    init_p.add_argument("--force", action="store_true", help="replace an existing config with -f")
    for flag in (
        "pve-url",
        "pve-admin",
        "node",
        "pool",
        "storage",
        "clone-storage",
        "nodes",
        "bridge",
        "guacamole-url",
        "guacamole-admin",
        "vmid-range",
    ):
        init_p.add_argument(f"--{flag}")
    init_p.add_argument("--template", type=int)

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


FLAG_KEYS = (
    "pve_url",
    "pve_admin",
    "node",
    "pool",
    "storage",
    "clone_storage",
    "nodes",
    "bridge",
    "template",
    "vmid_range",
    "guacamole_url",
    "guacamole_admin",
)
PVE_ADMIN_PRIVS = "Permissions.Modify, Realm.AllocateUser, Pool.Allocate, Sys.Modify"


def _report(section: str, results: list[tuple[str, str]]) -> None:
    for item, result in results:
        print(f"{section}: {item}: {result}")


class _Init:
    """One `tkctl lab init` run: config, then PVE, then Guacamole, with injectable dependencies."""

    def __init__(self, args, deps: dict):
        self.args = args
        self.make_admin = deps.get("make_admin") or (
            lambda url, user, pw, ca_file=None: PveAdmin(url, user, pw, ca_file=ca_file)
        )
        self.make_guac_admin = deps.get("make_guac_admin") or (
            lambda url, user, pw, code: Guac(url, user, pw)
        )
        self.make_service_client = deps.get("make_service_client") or (
            lambda url, user, pw, secret: Guac(url, user, pw, totp_secret=secret)
        )
        self.ask_text = deps.get("ask_text") or (
            lambda q, d: input(f"{q} [{d}]: " if d else f"{q}: ").strip() or d
        )
        self.ask_secret = deps.get("ask_secret") or (lambda q: getpass.getpass(f"{q}: "))
        # Questions are only possible on a terminal (or when a test injects the askers).
        tty = deps.get("tty")
        if tty is None:
            tty = deps.get("ask_secret") is not None or sys.stdin.isatty()
        self.can_ask = tty
        self.fetch = deps.get("fetch") or setup._http_get
        # The CA fetched by an earlier `init pve` (or copied there by hand) works before any config.
        well_known = config.config_path().parent / "pve-root-ca.pem"
        self.ca_file: str | None = str(well_known) if well_known.exists() else None
        self.pve_admin = None  # a logged-in PveAdmin gathered interactively
        self.guac_admin = None  # a logged-in Guac admin client gathered interactively

    # --- credentials
    def admin_factory(self, url: str, user: str, password: str):
        admin = self.make_admin(url, user, password, ca_file=self.ca_file)
        admin.login()
        self.pve_admin = admin
        return admin

    def guac_login(self, url: str, user: str, password: str, code: str | None) -> None:
        client = self.make_guac_admin(url, user, password, code)
        login = getattr(client, "login", None)
        if login is not None:
            login(totp=code) if code else login()
        self.guac_admin = client

    def interactive(self) -> setup.Answers:
        answers, _, _ = prompt.ask(
            admin_factory=self.admin_factory,
            ask_text=self.ask_text,
            ask_secret=self.ask_secret,
            guac_login=self.guac_login,
        )
        sha = setup.fetch_sha512(setup.DEFAULT_IMAGE, fetch=self.fetch)
        return dataclasses.replace(answers, image_sha512=sha)

    # --- the three sections
    def run(self) -> int:
        args = self.args
        only = args.noun
        flags = {k: getattr(args, k) for k in FLAG_KEYS}
        try:
            cfg, state = setup.ensure_config(
                path=config.config_path(),
                flags=flags,
                file=args.file,
                force=args.force,
                interactive=self.interactive if (self.can_ask and not args.manual) else None,
                fetch=self.fetch,
            )
        except setup.SetupError as e:
            return _fail(str(e))
        print(f"config: {state} ({config.config_path()})")
        self.ca_file = (
            cfg.pve.ca_file if cfg.pve.ca_file and Path(cfg.pve.ca_file).exists() else None
        )

        if args.manual:
            if only != "guacamole":
                print(setup.manual_pve(cfg, None), end="")
            if only != "pve":
                print(setup.manual_guacamole(cfg), end="")
            return 0

        env = config.merged_env()
        if only != "guacamole":
            rc = self.pve(cfg, flags, env)
            if rc:
                return rc
        if only != "pve":
            rc = self.guacamole(cfg, flags, env)
            if rc:
                return rc
        print("next: tkctl lab create template")
        return 0

    def pve(self, cfg, flags, env) -> int:
        admin = self.pve_admin
        if admin is None:
            pw = os.environ.get("TK_LAB_PVE_ADMIN_PASSWORD")
            user = flags.get("pve_admin") or setup.DEFAULTS["pve_admin"]
            try:
                if pw:
                    admin = self.admin_factory(cfg.pve.url, user, pw)
                elif self.can_ask:
                    admin, _ = prompt.pve_login(
                        self.ask_text, self.ask_secret, self.admin_factory, cfg.pve.url
                    )
                else:
                    return _fail(
                        "set TK_LAB_PVE_ADMIN_PASSWORD (or run `tkctl lab init` on a terminal)"
                    )
            except PveError as e:
                return _fail(f"pve: {e}", 1)
        ca_path = config.config_path().parent / "pve-root-ca.pem"
        try:
            results, new_env = setup.reconcile_pve(cfg, admin, env, ca_path=ca_path)
        except PveError as e:
            hint = f" (needs {PVE_ADMIN_PRIVS}; or use --manual)" if e.status == 403 else ""
            return _fail(f"pve: {e}{hint}", 1)
        _report("pve", results)
        self._store(new_env, env)
        return 0

    def guacamole(self, cfg, flags, env) -> int:
        gadmin = self.guac_admin
        if gadmin is None:
            pw = os.environ.get("TK_LAB_GUAC_ADMIN_PASSWORD")
            user = flags.get("guacamole_admin") or setup.DEFAULTS["guacamole_admin"]
            try:
                if pw:
                    self.guac_login(cfg.guacamole.url, user, pw, None)
                elif self.can_ask:
                    prompt.guac_admin_login(
                        self.ask_text, self.ask_secret, self.guac_login, cfg.guacamole.url
                    )
                else:
                    return _fail(
                        "set TK_LAB_GUAC_ADMIN_PASSWORD (or run `tkctl lab init` on a terminal)"
                    )
            except GuacError as e:
                if guacmod.challenge(e) is not None:
                    return _fail(
                        f"guacamole: administrator {user} needs a TOTP code; "
                        "run `tkctl lab init` interactively or use --manual",
                        1,
                    )
                return _fail(f"guacamole: {e}", 1)
            gadmin = self.guac_admin
        url = cfg.guacamole.url
        try:
            results, new_env = setup.reconcile_guacamole(
                cfg, gadmin, env, make_client=lambda u, p, s: self.make_service_client(url, u, p, s)
            )
        except GuacError as e:
            return _fail(f"guacamole: {e}", 1)
        _report("guacamole", results)
        self._store(new_env, env)
        return 0

    @staticmethod
    def _store(new_env: dict[str, str], env: dict[str, str]) -> None:
        if new_env:
            envfile.write(envfile.path(), new_env)
            env.update(new_env)
            print(f"wrote {envfile.path()}")


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


def main(
    argv: list[str] | None = None,
    *,
    make_clients=None,
    make_build_client=None,
    make_admin=None,
    make_guac_admin=None,
    make_service_client=None,
    ask_text=None,
    ask_secret=None,
    fetch=None,
    tty=None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.verb == "init":
        deps = dict(
            make_admin=make_admin,
            make_guac_admin=make_guac_admin,
            make_service_client=make_service_client,
            ask_text=ask_text,
            ask_secret=ask_secret,
            fetch=fetch,
            tty=tty,
        )
        try:
            return _Init(args, deps).run()
        except config.ConfigError as e:
            return _fail(str(e))
        except (PveError, GuacError) as e:
            return _fail(str(e), 1)
        except KeyboardInterrupt:
            print("\naborted; nothing written", file=sys.stderr)
            return 1
    try:
        return _dispatch(args, make_clients=make_clients, make_build_client=make_build_client)
    except config.ConfigError as e:
        return _fail(str(e))
    except (PveError, GuacError) as e:
        return _fail(str(e), 1)
