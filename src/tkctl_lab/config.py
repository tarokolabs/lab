"""Instructor configuration: a TOML file under XDG paths plus two secrets from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """One or more configuration problems; the message lists all of them."""


@dataclass(frozen=True)
class PveConfig:
    url: str
    token_id: str
    node: str
    pool: str
    storage: str
    template: int
    vmid_range: tuple[int, int]
    bridge: str
    ca_file: str | None = None


@dataclass(frozen=True)
class GuacConfig:
    url: str
    username: str


@dataclass(frozen=True)
class VmConfig:
    cores: int = 8
    memory: int = 24576
    balloon: int = 8192
    disk: str = "60G"


@dataclass(frozen=True)
class TemplateConfig:
    image_url: str
    image_sha512: str
    tk8s_version: str = "main"
    k8s: str | None = None


@dataclass(frozen=True)
class Config:
    pve: PveConfig
    guacamole: GuacConfig
    vm: VmConfig
    template: TemplateConfig


@dataclass(frozen=True)
class Secrets:
    pve_token: str
    guac_password: str


PVE_TOKEN_ENV = "TK_LAB_PVE_TOKEN"
GUAC_PASSWORD_ENV = "TK_LAB_GUAC_PASSWORD"

INIT_TEMPLATE = """# tkctl lab configuration. Secrets never go here:
#   TK_LAB_PVE_TOKEN      the PVE API token secret
#   TK_LAB_GUAC_PASSWORD  the Guacamole service account password
[pve]
url = "https://pve-node1:8006"
token_id = "lab@pve!tkctl"      # user@realm!tokenid; `tkctl lab init` prints the pveum commands
node = "auto"                    # node for new VMs; "auto" picks the one with the most free memory
pool = "lab"                     # every VM this tool touches lives in this pool
storage = "nas-iscsi-lvm"        # shared storage holding the template and its linked clones
template = 3900                  # template VMID (built by `tkctl lab create template`)
vmid_range = [3100, 3199]        # VMIDs for student VMs
bridge = "vmbr0"
# ca_file = "/path/to/pve-root-ca.pem"   # PVE's CA (/etc/pve/pve-root-ca.pem); default: system CAs

[guacamole]
url = "https://guac.example"
username = "tkctl-lab"           # service account with CREATE_USER and CREATE_CONNECTION only

[vm]
cores = 8
memory = 24576                   # MiB; enough for a 3-control-plane + 2-worker HA exercise
balloon = 8192
disk = "60G"

[template]
image_url = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
image_sha512 = "replace-with-the-value-from-SHA512SUMS"
tk8s_version = "v2026.10.0"     # tag or branch install.sh checks out
k8s = "1.37.0"                   # node image pre-pulled into the template
"""


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tkctl" / "lab.toml"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "tkctl" / "lab"


def secrets() -> Secrets:
    missing = [v for v in (PVE_TOKEN_ENV, GUAC_PASSWORD_ENV) if not os.environ.get(v)]
    if missing:
        raise ConfigError("missing environment variable(s): " + ", ".join(missing))
    return Secrets(os.environ[PVE_TOKEN_ENV], os.environ[GUAC_PASSWORD_ENV])


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(section: dict, key: str, typ: type, errors: list[str], where: str):
    if key not in section:
        errors.append(f"{where}.{key} is required")
        return None
    value = section[key]
    if typ is int and not _is_int(value):
        errors.append(f"{where}.{key} must be an integer")
        return None
    if typ is str and not isinstance(value, str):
        errors.append(f"{where}.{key} must be a string")
        return None
    return value


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config not found: {path} (run: tkctl lab init)") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    errors: list[str] = []
    pve = raw.get("pve", {})
    guac = raw.get("guacamole", {})
    vm = raw.get("vm", {})
    tpl = raw.get("template", {})
    str_keys = ("url", "token_id", "node", "pool", "storage", "bridge")
    p = {k: _require(pve, k, str, errors, "pve") for k in str_keys}
    template = _require(pve, "template", int, errors, "pve")
    rng = pve.get("vmid_range")
    if not (isinstance(rng, list) and len(rng) == 2 and all(_is_int(x) for x in rng)):
        errors.append("pve.vmid_range must be [low, high]")
        rng = None
    elif rng[0] > rng[1]:
        errors.append("pve.vmid_range must satisfy low <= high")
    ca_file = pve.get("ca_file")
    if ca_file is not None and not isinstance(ca_file, str):
        errors.append("pve.ca_file must be a path string")
    g = {k: _require(guac, k, str, errors, "guacamole") for k in ("url", "username")}
    v = {k: vm.get(k, getattr(VmConfig, k)) for k in ("cores", "memory", "balloon", "disk")}
    for k in ("cores", "memory", "balloon"):
        if not _is_int(v[k]):
            errors.append(f"vm.{k} must be an integer (MiB for memory and balloon)")
    if not isinstance(v["disk"], str):
        errors.append('vm.disk must be a string like "60G"')
    t = {k: _require(tpl, k, str, errors, "template") for k in ("image_url", "image_sha512")}
    if errors:
        raise ConfigError(f"{path}:\n  " + "\n  ".join(errors))
    assert template is not None and rng is not None  # every error path raised above
    return Config(
        pve=PveConfig(**p, template=template, vmid_range=(rng[0], rng[1]), ca_file=ca_file),
        guacamole=GuacConfig(**g),
        vm=VmConfig(**v),
        template=TemplateConfig(
            **t, tk8s_version=tpl.get("tk8s_version", "main"), k8s=tpl.get("k8s")
        ),
    )
