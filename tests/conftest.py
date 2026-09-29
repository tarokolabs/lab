import pytest


@pytest.fixture(autouse=True)
def clean_xdg(tmp_path, monkeypatch):
    """Every test starts from empty XDG dirs and no tkctl lab variables in the environment."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    for v in (
        "TK_LAB_PVE_TOKEN",
        "TK_LAB_PVE_BUILD_TOKEN",
        "TK_LAB_GUAC_PASSWORD",
        "TK_LAB_GUAC_TOTP_SECRET",
        "TK_LAB_PVE_ADMIN_PASSWORD",
        "TK_LAB_GUAC_ADMIN_PASSWORD",
        "TK_ASSUME_YES",
    ):
        monkeypatch.delenv(v, raising=False)
