from tkctl_lab import template
from tkctl_lab.config import Config, GuacConfig, PveConfig, TemplateConfig, VmConfig


def test_user_data_installs_desktop_tk8s_and_prepulls_image():
    ud = template.user_data("v2026.10.0", "1.37.0")
    assert ud.startswith("#cloud-config\n")
    for pkg in ("qemu-guest-agent", "podman", "xfce4", "xrdp", "xorgxrdp", "firefox-esr"):
        assert f"- {pkg}" in ud
    assert "TK_VERSION=v2026.10.0 sh" in ud
    assert "ghcr.io/tarokolabs/tk8s/node:v1.37.0" in ud
    assert "name: student" in ud and "NOPASSWD:ALL" in ud
    assert "PasswordAuthentication yes" in ud and "ssh_pwauth: true" in ud
    assert "swapoff" in ud and "cloud-init clean" in ud and "mode: poweroff" in ud


class Rec:
    def __init__(self):
        self.calls = []
        self.statuses = iter(["running", "running", "stopped"])

    def __getattr__(self, name):
        def m(*a, **kw):
            self.calls.append((name, a, kw))
            ret = {
                "download_url": "UPID:dl",
                "create_vm": "UPID:cr",
                "upload_snippet": "UPID:up",
                "start": "UPID:st",
                "pick_node": "pve-node8",
            }
            if name == "status":
                return next(self.statuses)
            return ret.get(name)

        return m


def cfg(node="pve-node7", k8s="1.37.0"):
    return Config(
        PveConfig("u", "t", node, "lab", "nas-nfs", 3900, (3100, 3199), "vmbr0"),
        GuacConfig("g", "s"),
        VmConfig(),
        TemplateConfig(
            "https://x/debian-13-genericcloud-amd64.qcow2", "deadbeef", "v2026.10.0", k8s
        ),
    )


def test_build_sequence():
    pve = Rec()
    assert template.build(cfg(), pve, k8s=None, log=lambda *_: None) == 0
    names = [c[0] for c in pve.calls]
    assert names[:3] == ["download_url", "wait_task", "upload_snippet"]
    assert names.index("create_vm") < names.index("resize") < names.index("start")
    assert names[-3:] == ["wait_status", "set_config", "make_template"]
    dl = next(c for c in pve.calls if c[0] == "download_url")
    assert dl[1] == (
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
    assert kv["agent"] == 1 and kv["ide2"] == "nas-nfs:cloudinit"
    assert "import-from=nas-nfs:import/debian-13-genericcloud-amd64.qcow2" in kv["scsi0"]
    assert "format=qcow2" in kv["scsi0"]  # linked clones on NFS need qcow2; the API default is raw
    assert kv["cicustom"] == "user=nas-nfs:snippets/tkctl-lab-template.yaml"
    assert kv["net0"] == "virtio,bridge=vmbr0" and kv["cpu"] == "x86-64-v2-AES"
    assert next(c for c in pve.calls if c[0] == "resize")[1] == ("pve-node7", 3900, "scsi0", "60G")
    # clones must get PVE's generated user-data (ciuser/cipassword), not the build snippet
    assert next(c for c in pve.calls if c[0] == "set_config")[2] == {"delete": "cicustom"}


def test_build_picks_node_when_auto_and_needs_k8s():
    pve = Rec()
    assert template.build(cfg("auto", k8s=None), pve, k8s=None, log=lambda *_: None) == 2
    assert pve.calls == []
    pve = Rec()
    assert template.build(cfg("auto", k8s=None), pve, k8s="1.36.2", log=lambda *_: None) == 0
    assert pve.calls[0][0] == "pick_node"
    ud = next(c for c in pve.calls if c[0] == "upload_snippet")[1][3].decode()
    assert "node:v1.36.2" in ud
