"""Interactive questions for `tkctl lab init`; input and secrets are injected for tests."""

from __future__ import annotations

from .guac import GuacError, challenge
from .pve import PveError
from .setup import DEFAULTS, Answers, SetupError

ELIGIBLE = {"images", "snippets", "import"}


def _choose(ask_text, question: str, options: list[str], default: str) -> str:
    while True:
        answer = ask_text(f"{question} [{', '.join(options)}]", default)
        if answer in options:
            return answer
        print(f"  choose one of: {', '.join(options)}")


def _is_auth_error(e: PveError) -> bool:
    return e.status == 401


def pve_login(ask_text, ask_secret, admin_factory, pve_url: str):
    """Ask for the administrator once, then for the password until PVE accepts it (plus a TFA
    code when PVE asks for one). Anything but a rejected login (TLS, network, 5xx) propagates."""
    pve_admin = ask_text("PVE administrator", DEFAULTS["pve_admin"])
    while True:
        password = ask_secret(f"Password for {pve_admin}")
        try:
            return admin_factory(pve_url, pve_admin, password), pve_admin
        except PveError as e:
            if not _is_auth_error(e):
                raise
            if "TFA" not in str(e):
                print(f"  {e}")
                continue
            code = ask_secret("TFA code")
            try:
                admin = admin_factory(pve_url, pve_admin, password)
                admin.login(totp=code)
                return admin, pve_admin
            except PveError as e2:
                if not _is_auth_error(e2):
                    raise
                print(f"  {e2}")


def guac_admin_login(ask_text, ask_secret, guac_login, guac_url: str) -> tuple[str, str]:
    guac_admin = ask_text("Guacamole administrator", DEFAULTS["guacamole_admin"])
    while True:
        guac_pw = ask_secret(f"Password for {guac_admin}")
        try:
            guac_login(guac_url, guac_admin, guac_pw, None)
            return guac_admin, guac_pw
        except GuacError as e:
            if e.status != 403:
                raise
            if challenge(e) is None:
                print(f"  {e}")
                continue
            code = ask_secret("Guacamole TOTP code")
            try:
                guac_login(guac_url, guac_admin, guac_pw, code)
                return guac_admin, guac_pw
            except GuacError as e2:
                if e2.status not in (400, 403):
                    raise
                print(f"  {e2}")


def ask(*, admin_factory, ask_text, ask_secret, guac_login) -> tuple[Answers, object, str]:
    """Walk the questions. Returns the answers (image_sha512 left empty for the caller), a
    logged-in PVE admin client and the Guacamole admin password, both used once and never stored.
    """
    pve_url = ask_text("PVE API URL (https://host:8006)", "")
    admin, pve_admin = pve_login(ask_text, ask_secret, admin_factory, pve_url)
    storages = [
        s["storage"]
        for s in admin.storages()
        if set((s.get("content") or "").split(",")) >= ELIGIBLE and s.get("shared")
    ]
    if not storages:
        raise SetupError(
            "no shared storage offers images, snippets and import content; add one in PVE first"
        )
    storage = _choose(ask_text, "Storage for the template and clones", storages, storages[0])
    nodes = admin.nodes()
    bridges = admin.bridges(nodes[0]) or [DEFAULTS["bridge"]]
    default_bridge = DEFAULTS["bridge"] if DEFAULTS["bridge"] in bridges else bridges[0]
    bridge = _choose(ask_text, "Bridge", bridges, default_bridge)
    node = _choose(ask_text, "Node to build the template on", ["auto", *nodes], "auto")
    pool = ask_text("Pool for class VMs", DEFAULTS["pool"])
    while True:
        template = int(ask_text("Template VMID", str(DEFAULTS["template"])))
        if admin.vmid_free(template):
            break
        if admin.vm_is_template(template):
            print(f"  VMID {template} is the existing template; kept")
            break
        print(f"  VMID {template} is in use by a VM")
    while True:
        raw = ask_text("Student VMID range (low-high)", "3100-3199")
        try:
            lo, hi = (int(x) for x in raw.split("-"))
        except ValueError:
            print("  expected two numbers like 3100-3199")
            continue
        if lo > hi:
            print("  the low end must not exceed the high end")
        elif lo <= template <= hi:
            print(f"  the range must not include the template VMID {template}")
        else:
            break
    guac_url = ask_text("Guacamole URL", "")
    guac_admin, guac_pw = guac_admin_login(ask_text, ask_secret, guac_login, guac_url)
    answers = Answers(
        pve_url=pve_url,
        pve_admin=pve_admin,
        node=node,
        pool=pool,
        storage=storage,
        bridge=bridge,
        template=template,
        vmid_range=(lo, hi),
        guacamole_url=guac_url,
        guacamole_admin=guac_admin,
        image_sha512="",
    )
    return answers, admin, guac_pw
