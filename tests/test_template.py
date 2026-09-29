from tkctl_lab import template
from tkctl_lab.config import Config, GuacConfig, PveConfig, TemplateConfig, VmConfig

from .fakes import FakePve

QUIET = {"log": lambda *_: None}


def test_user_data_installs_desktop_tk8s_and_prepulls_image():
    ud = template.user_data("v2026.10.0", "1.37.0")
    assert ud.startswith("#cloud-config\n")
    for pkg in ("qemu-guest-agent", "podman", "xfce4", "xrdp", "xorgxrdp", "firefox-esr"):
        assert f"- {pkg}" in ud
    assert "TK_VERSION=v2026.10.0 sh" in ud
    assert "ghcr.io/tarokolabs/tk8s/node:v1.37.0" in ud
    assert "name: student" in ud and "NOPASSWD:ALL" in ud
    assert "PasswordAuthentication yes" in ud and "ssh_pwauth: true" in ud
    assert "swapoff" in ud and "mode: poweroff" in ud
    # the build script stops at the first error and only a finished build powers the VM off
    assert "set -e" in ud and template.BUILD_OK in ud
    assert f"condition: test -f {template.BUILD_OK}" in ud
    assert "cloud-init clean --logs --machine-id" in ud
    # a fresh cloud image boots with ip_forward=0 and node netns inherit that at creation
    assert "net.ipv4.ip_forward = 1" in ud and "/etc/sysctl.d/80-tkctl-lab.conf" in ud
    assert "systemctl disable lightdm" in ud


def cfg(node="pve-node7", k8s="1.37.0"):
    return Config(
        PveConfig("u", "t", node, "lab", "nas-nfs", 3900, (3100, 3199), "vmbr0"),
        GuacConfig("g", "s"),
        VmConfig(),
        TemplateConfig(
            "https://x/debian-13-genericcloud-amd64.qcow2", "deadbeef", "v2026.10.0", k8s
        ),
    )


def test_build_needs_snippet_on_storage_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    pve = FakePve()
    lines = []
    rc = template.build(cfg(), pve, k8s=None, node=None, log=lines.append)
    assert rc == 1
    written = tmp_path / "tkctl" / "lab" / template.SNIPPET
    assert written.read_text().startswith("#cloud-config")
    joined = "\n".join(lines)
    assert str(written) in joined and "/mnt/pve/nas-nfs/snippets/" in joined
    assert not any(c[0] in ("download_url", "create_vm") for c in pve.calls)


def test_build_writes_snippet_into_a_mounted_snippets_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    mounted = tmp_path / "snippets"
    mounted.mkdir()
    c = cfg()
    c = Config(
        c.pve,
        c.guacamole,
        c.vm,
        TemplateConfig(
            c.template.image_url,
            c.template.image_sha512,
            c.template.tk8s_version,
            c.template.k8s,
            snippets_dir=str(mounted),
        ),
    )
    pve = FakePve()
    pve.storage_content = lambda node, storage, content: (
        [{"volid": f"{storage}:snippets/{template.SNIPPET}"}]
        if content == "snippets" and (mounted / template.SNIPPET).exists()
        else []
    )
    pve.status = lambda node, vmid: "stopped"
    pve.download_url = lambda *a: "UPID:pve-node7:dl"
    pve.create_vm = lambda node, vmid, **kv: "UPID:pve-node7:cr"
    pve.wait_status = lambda *a, **kw: None
    pve.make_template = lambda *a: None
    pve.vms[3900] = {
        "vmid": 3900,
        "name": "t",
        "node": "pve-node7",
        "pool": "",
        "status": "stopped",
        "tags": "",
        "cores": 8,
        "memory": 1,
        "balloon": 1,
        "disk": "2G",
    }
    lines = []
    assert template.build(c, pve, k8s=None, node=None, log=lines.append) == 0
    assert (mounted / template.SNIPPET).read_text().startswith("#cloud-config")
    assert "scp" not in "\n".join(lines)


def test_build_sequence():
    pve = FakePve(snippets=[template.SNIPPET])
    statuses = iter(["running", "running", "stopped"])
    pve.status = lambda node, vmid: next(statuses)
    pve.download_url = lambda *a: pve.calls.append(("download_url", a)) or "UPID:pve-node7:dl"
    pve.create_vm = lambda node, vmid, **kv: (
        pve.calls.append(("create_vm", (node, vmid), kv)) or ("UPID:pve-node7:cr")
    )
    pve.wait_status = lambda *a, **kw: pve.calls.append(("wait_status", a))
    pve.make_template = lambda *a: pve.calls.append(("make_template", a)) or "UPID:pve-node7:tpl"
    pve.vms[3900] = {
        "vmid": 3900,
        "name": "t",
        "node": "pve-node7",
        "pool": "",
        "status": "stopped",
        "tags": "",
        "cores": 8,
        "memory": 1,
        "balloon": 1,
        "disk": "2G",
    }
    assert template.build(cfg(), pve, k8s=None, node=None, **QUIET) == 0
    names = [c[0] for c in pve.calls]
    assert names[:2] == ["download_url", "wait"]
    assert names.index("create_vm") < names.index("resize") < names.index("wait_status")
    # every async step is waited on: create, resize, start, delete cicustom, template
    assert names[-4:] == ["config", "make_template", "wait", "wait"] or names[-3:] == [
        "config",
        "make_template",
        "wait",
    ]
    dl = next(c for c in pve.calls if c[0] == "download_url")[1]
    assert dl == (
        "pve-node7",
        "nas-nfs",
        "import",
        "https://x/debian-13-genericcloud-amd64.qcow2",
        "debian-13-genericcloud-amd64.qcow2",
        "deadbeef",
        "sha512",
    )
    cv = next(c for c in pve.calls if c[0] == "create_vm")
    kv = cv[2]
    assert cv[1] == ("pve-node7", 3900)
    assert kv["agent"] == 1 and kv["ide2"] == "nas-nfs:cloudinit" and kv["ciupgrade"] == 0
    assert "import-from=nas-nfs:import/debian-13-genericcloud-amd64.qcow2" in kv["scsi0"]
    assert "format=qcow2" in kv["scsi0"]  # linked clones on NFS need qcow2; the API default is raw
    assert kv["cicustom"] == "user=nas-nfs:snippets/tkctl-lab-template.yaml"
    assert kv["net0"] == "virtio,bridge=vmbr0" and kv["cpu"] == "x86-64-v2-AES"
    assert ("resize", 3900, "scsi0", "60G") in pve.calls
    # clones must get PVE's generated user-data (ciuser/cipassword), not the build snippet
    assert next(c for c in pve.calls if c[0] == "config")[2] == {"delete": "cicustom"}


def test_build_skips_download_when_image_is_present_and_needs_a_node():
    pve = FakePve(snippets=[template.SNIPPET], imports=["debian-13-genericcloud-amd64.qcow2"])
    pve.status = lambda node, vmid: "stopped"
    pve.create_vm = lambda node, vmid, **kv: (
        pve.calls.append(("create_vm", (node, vmid), kv)) or ("UPID:pve-node9:cr")
    )
    pve.wait_status = lambda *a, **kw: None
    pve.make_template = lambda *a: None
    pve.vms[3900] = {
        "vmid": 3900,
        "name": "t",
        "node": "pve-node9",
        "pool": "",
        "status": "stopped",
        "tags": "",
        "cores": 8,
        "memory": 1,
        "balloon": 1,
        "disk": "2G",
    }
    lines = []
    assert template.build(cfg("auto"), pve, k8s=None, node=None, log=lines.append) == 2
    assert "--node" in "\n".join(lines)
    assert template.build(cfg("auto"), pve, k8s=None, node="pve-node9", **QUIET) == 0
    assert not any(c[0] == "download_url" for c in pve.calls)
    assert next(c for c in pve.calls if c[0] == "create_vm")[1] == ("pve-node9", 3900)


def test_build_needs_k8s_version():
    lines = []
    assert template.build(cfg(k8s=None), FakePve(), k8s=None, node=None, log=lines.append) == 2
    assert "--k8s" in "\n".join(lines)


def test_build_403_points_at_init_pve():
    from tkctl_lab.pve import PveError

    pve = FakePve(snippets=[template.SNIPPET], imports=["debian-13-genericcloud-amd64.qcow2"])

    def forbidden(node, vmid, **kv):
        raise PveError(403, "POST /nodes/pve-node7/qemu: Permission check failed")

    pve.create_vm = forbidden
    lines = []
    template.build(cfg(), pve, k8s=None, node=None, log=lines.append)
    text = "\n".join(lines)
    assert "tkctl lab init pve" in text and "grant /vms/3900" not in text
