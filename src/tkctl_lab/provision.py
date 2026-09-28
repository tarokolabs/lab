"""create / destroy / list / describe: the flows that drive the PVE and Guacamole clients."""

from __future__ import annotations

import secrets
import string
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import roster
from .classdef import ClassDef, Student, vm_name
from .config import Config
from .guac import RDP_PARAMS, SSH_PARAMS, GuacError
from .pve import PveError, tags

STUDENT_USER = "student"
ALPHABET = string.ascii_letters + string.digits
AGENT_TIMEOUT = 300


class ProvisionError(Exception):
    pass


def _password(n: int = 16) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def guac_user(class_name: str, student_name: str) -> str:
    """Numbered students get a class-prefixed login; named students log in as themselves."""
    return f"{class_name}-{student_name}" if student_name.isdigit() else student_name


def _tags(cd: ClassDef) -> str:
    t = ["lab", f"class-{cd.name}"]
    if cd.expires:
        t.append(f"expires-{cd.expires.isoformat()}")
    return ";".join(t)


def _tag_value(vm: dict, prefix: str) -> str | None:
    return next((x.removeprefix(prefix) for x in tags(vm) if x.startswith(prefix)), None)


class _Cloner:
    """Allocates a VMID and posts the clone under one lock, so parallel students never collide."""

    def __init__(self, cfg: Config, pve, template_node: str):
        self.cfg = cfg
        self.pve = pve
        self.template_node = template_node
        self.lock = threading.Lock()

    def clone(self, name: str, target: str) -> tuple[int, str]:
        lo, hi = self.cfg.pve.vmid_range
        with self.lock:
            # A VMID taken by someone else between next_vmid and clone gets one retry.
            for attempt in range(2):
                vmid = self.pve.next_vmid(lo, hi)
                try:
                    upid = self.pve.clone(
                        self.template_node,
                        self.cfg.pve.template,
                        vmid,
                        name,
                        self.cfg.pve.pool,
                        target,
                    )
                    return vmid, upid
                except PveError as e:
                    if "already exists" in str(e) and attempt == 0:
                        continue
                    raise
        raise AssertionError("unreachable")


def _provision_one(cd: ClassDef, s: Student, cfg: Config, pve, guac, cloner, group_id, log):
    name = vm_name(cd.name, s)
    node = cd.node or cfg.pve.node
    if node == "auto":
        node = pve.pick_node()
    login = guac_user(cd.name, s.name)
    try:
        vmid, upid = cloner.clone(name, node)
    except PveError as e:
        return roster.Entry(s.name, 0, node, "", "", "", f"clone: {e}")
    try:
        pve.wait_task(node, upid)
        vm_password = _password()
        pve.set_config(
            node,
            vmid,
            ciuser=STUDENT_USER,
            cipassword=vm_password,
            ipconfig0="ip=dhcp",
            cores=s.cores,
            memory=s.memory,
            balloon=s.balloon,
            tags=_tags(cd),
        )
        pve.wait_task(node, pve.start(node, vmid))
    except PveError as e:
        return roster.Entry(s.name, vmid, node, "", "", "", f"vm setup: {e}")
    log(f"{name}: VM {vmid} on {node} started, waiting for an address")
    ip = pve.agent_ipv4(node, vmid, timeout=AGENT_TIMEOUT)
    if not ip:
        why = (
            f"guest agent reported no IPv4 address within {AGENT_TIMEOUT}s; "
            "check the VM console, then `tkctl lab describe class --roster`"
        )
        return roster.Entry(s.name, vmid, node, "unknown", "", "", why)
    guac_password = _password()
    try:
        guac.create_user(login, guac_password)
        ssh = guac.create_connection(
            group_id, f"{cd.name}-{s.name} SSH", "ssh", SSH_PARAMS(ip, STUDENT_USER, vm_password)
        )
        rdp = guac.create_connection(
            group_id,
            f"{cd.name}-{s.name} Desktop",
            "rdp",
            RDP_PARAMS(ip, STUDENT_USER, vm_password),
        )
        guac.grant(login, connections=[ssh, rdp], groups=[group_id])
    except GuacError as e:
        return roster.Entry(s.name, vmid, node, ip, login, "", f"guacamole: {e}")
    log(f"{name}: {ip}, Guacamole user {login}")
    return roster.Entry(s.name, vmid, node, ip, login, guac_password, "")


def create(
    cd: ClassDef, cfg: Config, pve, guac, *, parallel: int = 5, log=print
) -> list[roster.Entry]:
    """Provision every student; failures are recorded per entry and never stop the others."""
    group_id = guac.ensure_group(cd.name)
    cloner = _Cloner(cfg, pve, pve.vm_node(cfg.pve.template))
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        entries = list(
            ex.map(
                lambda s: _provision_one(cd, s, cfg, pve, guac, cloner, group_id, log), cd.students
            )
        )
    roster.write(roster.path(cd.name), entries)
    return entries


def _class_vms(cfg: Config, pve, class_name: str) -> list[dict]:
    vms = [v for v in pve.pool_vms(cfg.pve.pool) if f"class-{class_name}" in tags(v)]
    return sorted(vms, key=lambda v: v["vmid"])


def _student_of(vm: dict, class_name: str) -> str:
    return vm["name"].removeprefix(f"lab-{class_name}-")


def destroy(class_name: str, cfg: Config, pve, guac, *, log=print) -> list[str]:
    """Remove the class's VMs, Guacamole objects and roster; returns what could not be removed."""
    failures: list[str] = []
    vms = _class_vms(cfg, pve, class_name)
    if not vms:
        return [f"no VMs tagged class-{class_name} in pool {cfg.pve.pool}"]
    for v in vms:
        try:
            if pve.status(v["node"], v["vmid"]) == "running":
                pve.wait_task(v["node"], pve.stop(v["node"], v["vmid"]))
            pve.wait_task(v["node"], pve.delete(v["node"], v["vmid"]))
            log(f"{v['name']}: VM {v['vmid']} deleted")
        except PveError as e:
            failures.append(f"{v['name']}: {e}")
    gid = guac.find_group(class_name)
    if gid:
        for c in guac.group_connections(gid):
            try:
                guac.delete_connection(c["identifier"])
            except GuacError as e:
                failures.append(f"connection {c['name']}: {e}")
        for v in vms:
            login = guac_user(class_name, _student_of(v, class_name))
            try:
                guac.delete_user(login)
            except GuacError as e:
                if e.status != 404:
                    failures.append(f"user {login}: {e}")
        try:
            guac.delete_group(gid)
        except GuacError as e:
            failures.append(f"group {class_name}: {e}")
    p = roster.path(class_name)
    if p.exists():
        p.unlink()
    return failures


def list_classes(cfg: Config, pve) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for v in sorted(pve.pool_vms(cfg.pve.pool), key=lambda v: v["vmid"]):
        cls = _tag_value(v, "class-")
        if not cls:
            continue
        row = {
            "vmid": v["vmid"],
            "name": v["name"],
            "node": v["node"],
            "status": v["status"],
            "expires": _tag_value(v, "expires-") or "",
        }
        out.setdefault(cls, []).append(row)
    return out


def expired(cfg: Config, pve, today: date) -> list[str]:
    names = []
    for cls, vms in list_classes(cfg, pve).items():
        exp = vms[0]["expires"]
        if exp and date.fromisoformat(exp) < today:
            names.append(cls)
    return names


def _disk_size(config: dict, default: str) -> str:
    parts = str(config.get("scsi0", "")).split(",")
    return next((p.removeprefix("size=") for p in parts if p.startswith("size=")), default)


def describe(class_name: str, cfg: Config, pve) -> ClassDef:
    """Rebuild the class definition from the VMs that carry its tag."""
    vms = _class_vms(cfg, pve, class_name)
    if not vms:
        raise ProvisionError(f"no VMs tagged class-{class_name} in pool {cfg.pve.pool}")
    students = []
    exp = None
    node = None
    for v in vms:
        c = pve.request("GET", f"/nodes/{v['node']}/qemu/{v['vmid']}/config")
        students.append(
            Student(
                _student_of(v, class_name),
                int(c.get("cores", cfg.vm.cores)),
                int(c.get("memory", cfg.vm.memory)),
                int(c.get("balloon", cfg.vm.balloon)),
                _disk_size(c, cfg.vm.disk),
            )
        )
        e = _tag_value(v, "expires-")
        exp = date.fromisoformat(e) if e else exp
        node = v["node"]
    return ClassDef(class_name, tuple(students), exp, node)
