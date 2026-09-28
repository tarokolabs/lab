from pathlib import Path

import pytest

from tkctl_lab import config

GOOD = """
[pve]
url = "https://pve-node1:8006"
token_id = "lab@pve!tkctl"
build_token_id = "lab@pve!tkctl-build"
node = "pve-node7"
pool = "lab"
storage = "nas-nfs"
template = 3900
vmid_range = [3100, 3199]
bridge = "vmbr0"

[guacamole]
url = "https://guac.example"
username = "tkctl-lab"

[vm]
cores = 8
memory = 24576
balloon = 8192
disk = "60G"

[template]
image_url = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
image_sha512 = "abc"
tk8s_version = "v2026.10.0"
k8s = "1.37.0"
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "lab.toml"
    p.write_text(text)
    return p


def test_load_good(tmp_path):
    cfg = config.load(write(tmp_path, GOOD))
    assert cfg.pve.template == 3900
    assert cfg.pve.vmid_range == (3100, 3199)
    assert cfg.pve.ca_file is None
    assert cfg.vm.memory == 24576 and cfg.vm.disk == "60G"
    assert cfg.template.tk8s_version == "v2026.10.0"


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(config.ConfigError, match=r"lab\.toml"):
        config.load(tmp_path / "lab.toml")


def test_all_problems_listed_at_once(tmp_path):
    bad = (
        GOOD.replace("template = 3900", 'template = "3900"')
        .replace("vmid_range = [3100, 3199]", "vmid_range = [3199, 3100]")
        .replace('storage = "nas-nfs"', "")
    )
    with pytest.raises(config.ConfigError) as e:
        config.load(write(tmp_path, bad))
    msg = str(e.value)
    assert "pve.template must be an integer" in msg
    assert "pve.vmid_range" in msg and "low <= high" in msg
    assert "pve.storage is required" in msg


def test_ca_file_must_be_a_string(tmp_path):
    bad = GOOD.replace('bridge = "vmbr0"', 'bridge = "vmbr0"\nca_file = 7')
    with pytest.raises(config.ConfigError, match=r"pve\.ca_file"):
        config.load(write(tmp_path, bad))


def test_secrets_from_env(monkeypatch):
    monkeypatch.setenv("TK_LAB_PVE_TOKEN", "t")
    monkeypatch.setenv("TK_LAB_GUAC_PASSWORD", "p")
    assert config.secrets() == config.Secrets(pve_token="t", guac_password="p")


def test_secrets_missing_named(monkeypatch):
    monkeypatch.delenv("TK_LAB_PVE_TOKEN", raising=False)
    monkeypatch.setenv("TK_LAB_GUAC_PASSWORD", "p")
    with pytest.raises(config.ConfigError, match="TK_LAB_PVE_TOKEN"):
        config.secrets()


def test_xdg_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    assert config.config_path() == tmp_path / "c" / "tkctl" / "lab.toml"
    assert config.state_dir() == tmp_path / "s" / "tkctl" / "lab"


def test_init_template_loads(tmp_path):
    # The generated example must itself be a valid config once the placeholders are kept.
    cfg = config.load(write(tmp_path, config.INIT_TEMPLATE))
    assert cfg.pve.pool == "lab"


def test_build_token_id_is_optional_and_read(tmp_path):
    p = tmp_path / "lab.toml"
    p.write_text(GOOD)
    assert config.load(p).pve.build_token_id == "lab@pve!tkctl-build"
    p.write_text(GOOD.replace('build_token_id = "lab@pve!tkctl-build"', ""))
    assert config.load(p).pve.build_token_id is None
    with pytest.raises(config.ConfigError, match="TK_LAB_PVE_BUILD_TOKEN"):
        config.build_secret({})
    assert config.build_secret({"TK_LAB_PVE_BUILD_TOKEN": "x"}) == "x"


def test_snippets_dir_is_optional(tmp_path):
    p = tmp_path / "lab.toml"
    p.write_text(GOOD)
    assert config.load(p).template.snippets_dir is None
    p.write_text(
        GOOD + '\n[template]\nsnippets_dir = "/mnt/snippets"\n'
        if "[template]" not in GOOD
        else GOOD.replace("[template]\n", '[template]\nsnippets_dir = "/mnt/snippets"\n')
    )
    assert config.load(p).template.snippets_dir == "/mnt/snippets"


def test_guac_totp_secret_is_optional(monkeypatch):
    monkeypatch.setenv("TK_LAB_PVE_TOKEN", "t")
    monkeypatch.setenv("TK_LAB_GUAC_PASSWORD", "p")
    monkeypatch.delenv("TK_LAB_GUAC_TOTP_SECRET", raising=False)
    assert config.secrets().guac_totp is None
    monkeypatch.setenv("TK_LAB_GUAC_TOTP_SECRET", "GEZD")
    assert config.secrets().guac_totp == "GEZD"
