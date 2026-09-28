"""Build the student VM template from the Debian cloud image, entirely through the PVE API.

The image is imported on pve.storage, a cloud-init snippet installs tk8s, the desktop and xrdp
on first boot and powers the VM off, then the VM becomes the template that classes clone.
"""

from __future__ import annotations

from .config import Config

SNIPPET = "tkctl-lab-template.yaml"
VM_NAME = "tkctl-lab-template"
IMPORT_TIMEOUT = 1800
FIRST_BOOT_TIMEOUT = 2400


def user_data(tk8s_version: str, k8s: str) -> str:
    install = (
        f"curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/{tk8s_version}/install.sh"
        f" | TK_VERSION={tk8s_version} sh"
    )
    return f"""#cloud-config
# tkctl lab template: tk8s preinstalled, node image pre-pulled, XFCE desktop over xrdp.
# The student user gets its password per clone (PVE cloud-init cipassword).
package_update: true
package_upgrade: true
packages:
  - qemu-guest-agent
  - podman
  - git
  - curl
  - xfce4
  - xfce4-terminal
  - xrdp
  - xorgxrdp
  - firefox-esr
  - dbus-x11
ssh_pwauth: true
users:
  - name: student
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: false
write_files:
  - path: /etc/ssh/sshd_config.d/90-lab.conf
    content: "PasswordAuthentication yes\\n"
  - path: /home/student/.xsession
    owner: student:student
    defer: true
    content: "xfce4-session\\n"
runcmd:
  - systemctl enable --now qemu-guest-agent
  - systemctl enable xrdp
  - [sh, -c, "swapoff -a; sed -i '/ swap / s/^/#/' /etc/fstab"]
  - [su, -, student, -c, "{install}"]
  - [podman, pull, "ghcr.io/tarokolabs/tk8s/node:v{k8s}"]
  - cloud-init clean --logs
power_state:
  mode: poweroff
  timeout: 60
  condition: true
"""


def build(cfg: Config, pve, *, k8s: str | None, log=print) -> int:
    t = cfg.template
    k8s = k8s or t.k8s
    if not k8s:
        log("template.k8s is not set and --k8s not given")
        return 2
    storage = cfg.pve.storage
    node = cfg.pve.node if cfg.pve.node != "auto" else pve.pick_node()
    image = t.image_url.rsplit("/", 1)[-1]
    vmid = cfg.pve.template

    log(f"downloading {image} to {storage} (import)")
    upid = pve.download_url(node, storage, "import", t.image_url, image, t.image_sha512, "sha512")
    pve.wait_task(node, upid, timeout=IMPORT_TIMEOUT)
    log("uploading cloud-init user-data")
    pve.upload_snippet(node, storage, SNIPPET, user_data(t.tk8s_version, k8s).encode())

    log(f"creating VM {vmid} on {node}")
    upid = pve.create_vm(
        node,
        vmid,
        name=VM_NAME,
        ostype="l26",
        cpu="x86-64-v2-AES",
        cores=cfg.vm.cores,
        memory=cfg.vm.memory,
        balloon=cfg.vm.balloon,
        scsihw="virtio-scsi-single",
        # qcow2 explicitly: the API default is raw, and linked clones on NFS need qcow2
        scsi0=f"{storage}:0,import-from={storage}:import/{image},format=qcow2,discard=on",
        ide2=f"{storage}:cloudinit",
        boot="order=scsi0",
        serial0="socket",
        vga="serial0",
        net0=f"virtio,bridge={cfg.pve.bridge}",
        agent=1,
        cicustom=f"user={storage}:snippets/{SNIPPET}",
        ipconfig0="ip=dhcp",
        tags="lab;template",
    )
    pve.wait_task(node, upid, timeout=IMPORT_TIMEOUT)
    pve.resize(node, vmid, "scsi0", cfg.vm.disk)

    log("first boot: installing packages and tk8s (10-15 minutes), then the VM powers off")
    pve.wait_task(node, pve.start(node, vmid))
    pve.wait_status(node, vmid, "stopped", timeout=FIRST_BOOT_TIMEOUT)
    # Clones must get PVE's generated user-data (ciuser/cipassword), not the build snippet.
    pve.set_config(node, vmid, delete="cicustom")
    pve.make_template(node, vmid)
    log(f"template {vmid} ready")
    return 0
