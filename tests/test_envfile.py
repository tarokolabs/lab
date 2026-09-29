import os

import pytest

from tkctl_lab import envfile


def test_path_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert envfile.path() == tmp_path / "tkctl" / "lab.env"


def test_read_missing_is_empty(tmp_path):
    assert envfile.read(tmp_path / "none.env") == {}


def test_write_creates_0600_and_reads_back(tmp_path):
    p = tmp_path / "d" / "lab.env"
    envfile.write(p, {"TK_LAB_PVE_TOKEN": "a=b=c", "TK_LAB_GUAC_PASSWORD": "p w"})
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert envfile.read(p) == {"TK_LAB_PVE_TOKEN": "a=b=c", "TK_LAB_GUAC_PASSWORD": "p w"}


def test_write_keeps_foreign_lines(tmp_path):
    p = tmp_path / "lab.env"
    p.write_text("# mine\nEDITOR=vim\nTK_LAB_PVE_TOKEN=old\n\nTK_LAB_GUAC_PASSWORD=x\n")
    os.chmod(p, 0o644)
    envfile.write(p, {"TK_LAB_PVE_TOKEN": "new", "TK_LAB_GUAC_TOTP_SECRET": "S"})
    assert p.read_text() == (
        "# mine\nEDITOR=vim\nTK_LAB_PVE_TOKEN=new\n\nTK_LAB_GUAC_PASSWORD=x\n"
        "TK_LAB_GUAC_TOTP_SECRET=S\n"
    )
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_write_rejects_keys_it_does_not_own(tmp_path):
    with pytest.raises(ValueError, match="EDITOR"):
        envfile.write(tmp_path / "lab.env", {"EDITOR": "vim"})
