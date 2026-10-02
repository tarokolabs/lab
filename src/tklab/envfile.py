"""The instructor's secrets file: KEY=value lines, mode 0600, only the four keys the tool owns."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

KEYS = (
    "TK_LAB_PVE_TOKEN",
    "TK_LAB_PVE_BUILD_TOKEN",
    "TK_LAB_GUAC_PASSWORD",
    "TK_LAB_GUAC_TOTP_SECRET",
)


def path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tklab" / "secrets.env"


def read(p: Path) -> dict[str, str]:
    try:
        text = p.read_text()
    except FileNotFoundError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[k] = v
    return out


def write(p: Path, values: dict[str, str]) -> None:
    """Replace or append the given keys; every other line stays as it was."""
    foreign = [k for k in values if k not in KEYS]
    if foreign:
        raise ValueError(f"not a tklab secret: {', '.join(foreign)}")
    try:
        lines = p.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    pending = dict(values)
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in pending:
                lines[i] = f"{k}={pending.pop(k)}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the file and rename over it: a crash midway never leaves a half-written
    # secrets file, and the previous one stays readable until the new one is complete.
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
