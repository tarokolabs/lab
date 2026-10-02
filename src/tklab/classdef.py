"""A class definition: flags and the TOML class file expand to the same ClassDef."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import VmConfig

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
# A student is a name (same rule as class names) or the number a count expands to (01, 02, ...).
STUDENT_RE = re.compile(r"^([a-z][a-z0-9-]{0,15}|[0-9]{2,3})$")
VM_FIELDS = ("cores", "memory", "balloon", "disk")


class ClassDefError(Exception):
    """Invalid class definition; the message lists every problem."""


@dataclass(frozen=True)
class Student:
    name: str
    cores: int
    memory: int
    balloon: int
    disk: str


@dataclass(frozen=True)
class ClassDef:
    name: str
    students: tuple[Student, ...]
    expires: date | None = None
    node: str | None = None
    k8s: bool = False  # create a tk8s cluster in every VM after it is up


def vm_name(class_name: str, student: Student) -> str:
    return f"lab-{class_name}-{student.name}"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_date(value: object, errors: list[str]) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        errors.append(f"expires must be a date like 2026-10-20, got {value!r}")
        return None


def _vm_fields(section: dict, where: str, errors: list[str]) -> dict:
    out = {}
    for k in VM_FIELDS:
        if k not in section:
            continue
        v = section[k]
        if k == "disk":
            if not isinstance(v, str):
                errors.append(f'{where}.disk must be a string like "60G"')
                continue
        elif not _is_int(v):
            errors.append(f"{where}.{k} must be an integer (MiB for memory and balloon)")
            continue
        out[k] = v
    return out


def _build(
    name: object,
    names: list[str],
    expires: date | None,
    node: str | None,
    vm: VmConfig,
    class_vm: dict,
    overrides: dict[str, dict],
    errors: list[str],
    k8s: bool = False,
) -> ClassDef:
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(f"name must match {NAME_RE.pattern}, got {name!r}")
    seen: set[str] = set()
    for n in names:
        if not STUDENT_RE.match(n):
            errors.append(f'student "{n}" must match {STUDENT_RE.pattern}')
        elif n in seen:
            errors.append(f"duplicate student {n}")
        seen.add(n)
    for n in overrides:
        if n not in seen:
            errors.append(f'override for unknown student "{n}"')
    base = {k: getattr(vm, k) for k in VM_FIELDS} | class_vm
    specs = {n: base | overrides.get(n, {}) for n in names}
    for n, spec in specs.items():
        if not errors and spec["balloon"] > spec["memory"]:
            errors.append(
                f'balloon ({spec["balloon"]}) must not exceed memory ({spec["memory"]}) for "{n}"'
            )
    if errors:
        raise ClassDefError("invalid class definition:\n  " + "\n  ".join(errors))
    assert isinstance(name, str)
    students = tuple(Student(name=n, **specs[n]) for n in names)
    return ClassDef(name=name, students=students, expires=expires, node=node, k8s=k8s)


def from_flags(
    name: str,
    count: int,
    *,
    vm: VmConfig,
    expires: str | None,
    node: str | None,
    cores: int | None,
    memory: int | None,
    k8s: bool = False,
) -> ClassDef:
    errors: list[str] = []
    if not _is_int(count) or count < 1:
        errors.append(f"count must be a positive integer, got {count!r}")
        count = 0
    class_vm = {k: v for k, v in (("cores", cores), ("memory", memory)) if v is not None}
    names = [f"{i:02d}" for i in range(1, count + 1)]
    return _build(name, names, _parse_date(expires, errors), node, vm, class_vm, {}, errors, k8s)


def from_file(path: Path, *, vm: VmConfig) -> ClassDef:
    errors: list[str] = []
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise ClassDefError(f"class file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ClassDefError(f"{path}: {e}") from e
    name = raw.get("name")
    if name is None:
        errors.append("name is required")
    names: list[str] = []
    if "students" in raw and "count" in raw:
        errors.append("students and count are mutually exclusive")
    if "students" in raw:
        st = raw["students"]
        if not isinstance(st, list) or not all(isinstance(s, str) for s in st):
            errors.append("students must be a list of names")
        else:
            names = list(st)
    elif "count" in raw:
        c = raw["count"]
        if not _is_int(c) or c < 1:
            errors.append("count must be a positive integer")
        else:
            names = [f"{i:02d}" for i in range(1, c + 1)]
    else:
        errors.append("students or count is required")
    class_vm = _vm_fields(raw.get("vm", {}), "vm", errors)
    overrides: dict[str, dict] = {}
    for i, o in enumerate(raw.get("students_override", [])):
        n = o.get("name")
        if not isinstance(n, str):
            errors.append(f"students_override[{i}].name is required")
            continue
        overrides[n] = _vm_fields(o, f"students_override[{i}]", errors)
    node = raw.get("node")
    if node is not None and not isinstance(node, str):
        errors.append("node must be a string")
        node = None
    expires = _parse_date(raw.get("expires"), errors)
    k8s = raw.get("k8s", False)
    if not isinstance(k8s, bool):
        errors.append("k8s must be true or false")
        k8s = False
    return _build(name, names, expires, node, vm, class_vm, overrides, errors, k8s)


def to_toml(cd: ClassDef) -> str:
    """Render a class file that from_file() reads back to the same ClassDef."""
    lines = [f'name = "{cd.name}"']
    if cd.expires:
        lines.append(f"expires = {cd.expires.isoformat()}")
    if cd.node:
        lines.append(f'node = "{cd.node}"')
    if cd.k8s:
        lines.append("k8s = true")
    lines.append("students = [" + ", ".join(f'"{s.name}"' for s in cd.students) + "]")
    # Per-student specs are written in full so the file does not depend on the instructor config.
    for s in cd.students:
        lines += [
            "",
            "[[students_override]]",
            f'name = "{s.name}"',
            f"cores = {s.cores}",
            f"memory = {s.memory}",
            f"balloon = {s.balloon}",
            f'disk = "{s.disk}"',
        ]
    return "\n".join(lines) + "\n"
