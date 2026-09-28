"""create / destroy / list / describe: the flows that drive the PVE and Guacamole clients."""

from __future__ import annotations

import re
import secrets
import string
import threading
import time
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
SIZE_RE = re.compile(r"^(\d+)([KMGT])$")
UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


class ProvisionError(Exception):
    pass


def _password(n: int = 16) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def guac_user(class_name: str, student_name: str) -> str:
    """Numbered students get a class-prefixed login; named students log in as themselves."""
    return f"{class_name}-{student_name}" if student_name.isdigit() else student_name


def size_bytes(size: str) -> int:
    m = SIZE_RE.match(size)
    if not m:
        raise ValueError(f"disk size must look like 60G, got {size!r}")
    return int(m.group(1)) * UNITS[m.group(2)]


def _tags(cd: ClassDef) -> str:
    t = ["lab", f"class-{cd.name}"]
    if cd.expires:
        t.append(f"expires-{cd.expires.isoformat()}")
    return ";".join(t)


def _tag_value(vm: dict, prefix: str) -> str | None:
    return next((x.removeprefix(prefix) for x in tags(vm) if x.startswith(prefix)), None)


def _disk_size(config: dict, default: str) -> str:
    parts = str(config.get("scsi0", "")).split(",")
    return next((p.removeprefix("size=") for p in parts if p.startswith("size=")), default)


class _Cloner:
    """Hands out VMIDs and posts clones under one lock.

    The token only sees pool VMs, and a clone joins the pool at the end of its task, so the
    allocator must remember every id it handed out or saw rejected; /cluster/nextid confirms
    each candidate against the whole cluster.
    """

    def __init__(self, cfg: Config, pve, template_node: str):
        self.cfg = cfg
        self.pve = pve
        self.template_node = template_node
        self.lock = threading.Lock()
        self.taken: set[int] = set()

    def clone(self, name: str, target: str) -> tuple[int, str]:
        lo, hi = self.cfg.pve.vmid_range
        with self.lock:
            while True:
                vmid = self.pve.next_vmid(lo, hi, exclude=self.taken)
                self.taken.add(vmid)
                try:
                    upid = self.pve.clone(
                        self.template_node,
                        self.cfg.pve.template,
                        vmid,
                        name,
                        self.cfg.pve.pool,
                        target,
                    )
                except PveError as e:
                    if "already exists" in str(e):
                        continue
                    raise
                return vmid, upid


class _Class:
    """Everything one create() run shares: config, clients, node choice, template facts."""

    def __init__(self, cd: ClassDef, cfg: Config, pve, guac, log, sleep=None):
        self.cd, self.cfg, self.pve, self.guac, self.log = cd, cfg, pve, guac, log
        self.sleep = sleep or time.sleep  # resolved late so tests can patch time.sleep
        self.group_id = guac.ensure_group(cd.name)
        template_node = pve.vm_node(cfg.pve.template)
        self.cloner = _Cloner(cfg, pve, template_node)
        tpl = pve.vm_config(template_node, cfg.pve.template)
        self.template_disk = size_bytes(_disk_size(tpl, cfg.vm.disk))
        fixed = cd.node or cfg.pve.node
        self.nodes = pve.online_nodes() if fixed == "auto" else [fixed]

    def node_for(self, index: int) -> str:
        return self.nodes[index % len(self.nodes)]


def _entry(s: Student, vmid: int, node: str, ip: str, login: str, err: str, **kw) -> roster.Entry:
    return roster.Entry(s.name, vmid, node, ip, login, kw.get("gp", ""), kw.get("vp", ""), err)


def _clone_and_start(c: _Class, s: Student, node: str) -> roster.Entry:
    """Clone, then configure and start the VM. The returned entry carries the VM password."""
    name = vm_name(c.cd.name, s)
    try:
        want = size_bytes(s.disk)
    except ValueError as e:
        return _entry(s, 0, node, "", "", str(e))
    if want < c.template_disk:
        return _entry(s, 0, node, "", "", f"disk {s.disk} is smaller than the template disk")
    try:
        vmid, upid = c.cloner.clone(name, node)
        c.pve.wait_task(upid)
    except PveError as e:
        return _entry(s, 0, node, "", "", f"clone: {e}")
    return _setup(c, s, _entry(s, vmid, node, "", "", "", vp=_password()))


def _start(c: _Class, node: str, vmid: int) -> None:
    """Start once more when PVE trips over its own mkdir on NFS (parallel clones)."""
    try:
        c.pve.wait_task(c.pve.start(node, vmid))
    except PveError as e:
        if "File exists" not in str(e):
            raise
        c.sleep(2)
        c.pve.wait_task(c.pve.start(node, vmid))


def _setup(c: _Class, s: Student, e: roster.Entry) -> roster.Entry:
    """Apply the student's cloud-init, sizing and tags to a cloned VM and start it."""
    name = vm_name(c.cd.name, s)
    want = size_bytes(s.disk)
    try:
        c.pve.set_config(
            e.node,
            e.vmid,
            ciuser=STUDENT_USER,
            cipassword=e.vm_password,
            ipconfig0="ip=dhcp",
            ciupgrade=0,
            cores=s.cores,
            memory=s.memory,
            balloon=s.balloon,
            tags=_tags(c.cd),
        )
        if want > c.template_disk:
            resize = c.pve.resize(e.node, e.vmid, "scsi0", s.disk)
            if resize:
                c.pve.wait_task(resize)
        _start(c, e.node, e.vmid)
    except PveError as err:
        return _entry(s, e.vmid, e.node, "", "", f"vm setup: {err}", vp=e.vm_password)
    c.log(f"{name}: VM {e.vmid} on {e.node} started, waiting for an address")
    return _entry(s, e.vmid, e.node, "", "", "", vp=e.vm_password)


def _connect(c: _Class, s: Student, e: roster.Entry, *, resume: bool) -> roster.Entry:
    """Fetch the address and create the Guacamole user and connections for a started VM."""
    name = vm_name(c.cd.name, s)
    login = guac_user(c.cd.name, s.name)
    ip = (
        e.ip
        if e.ip and e.ip != "unknown"
        else c.pve.agent_ipv4(e.node, e.vmid, timeout=AGENT_TIMEOUT)
    )
    if not ip:
        why = (
            f"guest agent reported no IPv4 address within {AGENT_TIMEOUT}s; check the VM console, "
            f"then rerun `tkctl lab create class` for {c.cd.name} to finish this student"
        )
        return _entry(s, e.vmid, e.node, "unknown", "", why, vp=e.vm_password)
    guac_password = _password()
    try:
        try:
            c.guac.create_user(login, guac_password)
        except GuacError as err:
            # on a rerun the user may exist from the failed attempt; keep whatever password
            # the roster has for it (a fresh create treats the clash as the error it is)
            if not resume or "already exists" not in str(err):
                raise
            guac_password = e.guac_password
        existing = {x["name"] for x in c.guac.group_connections(c.group_id)}
        ids = []
        for kind, params in (("SSH", SSH_PARAMS), ("Desktop", RDP_PARAMS)):
            conn_name = f"{c.cd.name}-{s.name} {kind}"
            if conn_name in existing:
                continue
            proto = "ssh" if kind == "SSH" else "rdp"
            ids.append(
                c.guac.create_connection(
                    c.group_id, conn_name, proto, params(ip, STUDENT_USER, e.vm_password)
                )
            )
        c.guac.grant(login, connections=ids, groups=[c.group_id])
    except GuacError as err:
        return _entry(s, e.vmid, e.node, ip, login, f"guacamole: {err}", vp=e.vm_password)
    c.log(f"{name}: {ip}, Guacamole user {login}")
    return _entry(s, e.vmid, e.node, ip, login, "", gp=guac_password, vp=e.vm_password)


def _provision_one(
    c: _Class, index: int, s: Student, previous: roster.Entry | None
) -> roster.Entry:
    current = previous
    try:
        if previous is not None and not previous.error:
            return previous
        if previous is not None and previous.vmid:
            e = previous
            if previous.error.startswith("vm setup"):
                current = e = _setup(c, s, previous)
                if e.error:
                    return e
        else:
            current = e = _clone_and_start(c, s, c.node_for(index))
            if e.error:
                return e
        return _connect(c, s, e, resume=previous is not None)
    except Exception as err:  # one student's surprise must not sink the class
        vmid = current.vmid if current else 0
        node = current.node if current else c.node_for(index)
        vp = current.vm_password if current else ""
        return _entry(s, vmid, node, "", "", f"unexpected {type(err).__name__}: {err}", vp=vp)


def create(
    cd: ClassDef, cfg: Config, pve, guac, *, parallel: int = 5, log=print
) -> list[roster.Entry]:
    """Provision every student; failures are recorded per entry and never stop the others.

    Rerunning for a class that already has a roster resumes: finished students are kept,
    failed ones are redone from where they stopped.
    """
    existing = _class_vms(cfg, pve, cd.name)
    path = roster.path(cd.name)
    previous: dict[str, roster.Entry] = {}
    if path.exists():
        previous = {e.student: e for e in roster.read(path)}
    elif existing:
        raise ProvisionError(
            f"class {cd.name} already has {len(existing)} VM(s) in pool {cfg.pve.pool} and no "
            f"roster at {path}; delete the class first"
        )
    c = _Class(cd, cfg, pve, guac, log)
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        entries = list(
            ex.map(
                lambda item: _provision_one(c, item[0], item[1], previous.get(item[1].name)),
                enumerate(cd.students),
            )
        )
    roster.write(path, entries)
    return entries


def _class_vms(cfg: Config, pve, class_name: str) -> list[dict]:
    """VMs of a class: tagged, or merely named (a clone whose set_config never ran)."""
    prefix = f"lab-{class_name}-"
    tag = f"class-{class_name}"
    vms = [
        v
        for v in pve.pool_vms(cfg.pve.pool)
        if tag in tags(v) or str(v.get("name", "")).startswith(prefix)
    ]
    return sorted(vms, key=lambda v: v["vmid"])


def _student_of(vm: dict, class_name: str) -> str:
    return vm["name"].removeprefix(f"lab-{class_name}-")


def destroy(class_name: str, cfg: Config, pve, guac, *, log=print) -> list[str]:
    """Remove the class's VMs, Guacamole objects and roster; returns what could not be removed."""
    failures: list[str] = []
    vms = _class_vms(cfg, pve, class_name)
    gid = guac.find_group(class_name)
    p = roster.path(class_name)
    if not vms and not gid and not p.exists():
        return [
            f"no class {class_name}: no VMs in pool {cfg.pve.pool}, no Guacamole group, no roster"
        ]
    for v in vms:
        try:
            if pve.status(v["node"], v["vmid"]) == "running":
                pve.wait_task(pve.stop(v["node"], v["vmid"]))
            pve.wait_task(pve.delete(v["node"], v["vmid"]))
            log(f"{v['name']}: VM {v['vmid']} deleted")
        except PveError as e:
            failures.append(f"{v['name']}: {e}")
    if gid:
        for c in guac.group_connections(gid):
            try:
                guac.delete_connection(c["identifier"])
            except GuacError as e:
                failures.append(f"connection {c['name']}: {e}")
        students = {_student_of(v, class_name) for v in vms}
        if p.exists():
            students |= {e.student for e in roster.read(p)}
        for student in sorted(students):
            login = guac_user(class_name, student)
            try:
                guac.delete_user(login)
            except GuacError as e:
                if e.status != 404:
                    failures.append(f"user {login}: {e}")
        try:
            guac.delete_group(gid)
        except GuacError as e:
            failures.append(f"group {class_name}: {e}")
    if p.exists():
        p.unlink()
    return failures


def list_classes(cfg: Config, pve) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for v in sorted(pve.pool_vms(cfg.pve.pool), key=lambda v: v["vmid"]):
        cls = _tag_value(v, "class-")
        if not cls:
            m = re.match(r"^lab-([a-z][a-z0-9-]{0,15})-", str(v.get("name", "")))
            cls = m.group(1) if m else None
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
        exp = next((v["expires"] for v in vms if v["expires"]), "")
        if exp and date.fromisoformat(exp) < today:
            names.append(cls)
    return names


def describe(class_name: str, cfg: Config, pve) -> ClassDef:
    """Rebuild the class definition from the VMs that carry its tag or name."""
    vms = _class_vms(cfg, pve, class_name)
    if not vms:
        raise ProvisionError(f"no class {class_name} in pool {cfg.pve.pool}")
    students = []
    exp = None
    nodes = set()
    for v in vms:
        c = pve.vm_config(v["node"], v["vmid"])
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
        nodes.add(v["node"])
    node = nodes.pop() if len(nodes) == 1 else None
    return ClassDef(class_name, tuple(students), exp, node)
