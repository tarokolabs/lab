"""The class roster: who got which VM, IP and Guacamole login.

Written with mode 0600; it is the only place the generated passwords land.
"""

from __future__ import annotations

import csv
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

FIELDS = ("student", "vmid", "node", "ip", "guac_user", "guac_password", "vm_password", "error")


@dataclass(frozen=True)
class Entry:
    student: str
    vmid: int
    node: str
    ip: str
    guac_user: str
    guac_password: str
    vm_password: str = ""
    error: str = ""


def path(class_name: str) -> Path:
    return config.state_dir() / f"{class_name}.csv"


def write(p: Path, entries: list[Entry]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for e in entries:
            w.writerow(asdict(e))
    os.chmod(p, 0o600)


def read(p: Path) -> list[Entry]:
    with p.open(newline="") as f:
        rows = csv.DictReader(f)
        return [
            Entry(**{k: (int(v) if k == "vmid" else v) for k, v in row.items()}) for row in rows
        ]
