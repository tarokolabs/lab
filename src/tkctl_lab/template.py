"""Build the student VM template from the Debian cloud image through the PVE API.

The image is imported on pve.storage, a cloud-init snippet (placed on the storage by the
operator: PVE's upload endpoint does not accept snippets) installs tk8s, the desktop and
xrdp on first boot and powers the VM off, then the VM becomes the template classes clone.
"""

from __future__ import annotations

from pathlib import Path

from . import config
from .config import Config
from .pve import PveError

SNIPPET = "tkctl-lab-template.yaml"
VM_NAME = "tkctl-lab-template"
BUILD_OK = "/var/lib/tkctl-lab/build-ok"
IMPORT_TIMEOUT = 1800
FIRST_BOOT_TIMEOUT = 2400


def user_data(tk8s_version: str, k8s: str) -> str:
    raw = f"https://raw.githubusercontent.com/tarokolabs/tk8s/{tk8s_version}/install.sh"
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
  - path: /etc/ssh/sshd_config.d/00-lab.conf
    content: "PasswordAuthentication yes\\n"
  - path: /etc/sysctl.d/80-tkctl-lab.conf
    # node network namespaces copy the host's IPv4 forwarding flag when they are created,
    # and a fresh cloud image boots with it off; kubeadm's preflight then fails inside the node
    content: "net.ipv4.ip_forward = 1\\n"
  - path: /home/student/.xsession
    owner: student:student
    defer: true
    content: "xfce4-session\\n"
  - path: /usr/local/sbin/tkctl-lab-build
    permissions: "0755"
    content: |
      #!/bin/sh
      # Runs once at first boot; any failure leaves the VM up for inspection.
      set -e
      systemctl enable --now qemu-guest-agent
      systemctl enable xrdp
      systemctl disable lightdm || true
      swapoff -a
      sed -i '/ swap / s/^/#/' /etc/fstab
      curl -fsSL -o /tmp/install.sh {raw}
      su - student -c "TK_VERSION={tk8s_version} sh /tmp/install.sh"
      podman pull ghcr.io/tarokolabs/tk8s/node:v{k8s}
      install -d /var/lib/tkctl-lab
      touch {BUILD_OK}
      cloud-init clean --logs --machine-id
runcmd:
  - /usr/local/sbin/tkctl-lab-build
power_state:
  mode: poweroff
  timeout: 60
  condition: test -f {BUILD_OK}
"""


def _has(pve, node: str, storage: str, content: str, filename: str) -> bool:
    volid = f"{storage}:{content}/{filename}"
    return any(v.get("volid") == volid for v in pve.storage_content(node, storage, content))


def build(cfg: Config, pve, *, k8s: str | None, node: str | None, log=print) -> int:
    t = cfg.template
    k8s = k8s or t.k8s
    if not k8s:
        log("template.k8s is not set and --k8s not given")
        return 2
    node = node or (cfg.pve.node if cfg.pve.node != "auto" else None)
    if not node:
        log("pve.node is auto: pass --node NAME to say where the template is built")
        return 2
    storage = cfg.pve.storage
    image = t.image_url.rsplit("/", 1)[-1]
    vmid = cfg.pve.template

    if not _has(pve, node, storage, "snippets", SNIPPET):
        data = user_data(t.tk8s_version, k8s)
        if t.snippets_dir:
            target = Path(t.snippets_dir) / SNIPPET
            target.write_text(data)
            log(f"wrote {target}")
            if not _has(pve, node, storage, "snippets", SNIPPET):
                log(f"{target} is not visible as {storage}:snippets/{SNIPPET}; check the mount")
                return 1
        else:
            local = config.state_dir() / SNIPPET
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(data)
            log(f"wrote {local}")
            log("PVE cannot receive snippets over the API; copy it onto the storage once, e.g.")
            log(f"  scp {local} root@<pve-node>:/mnt/pve/{storage}/snippets/")
            log("then run `tkctl lab create template` again")
            return 1

    if _has(pve, node, storage, "import", image):
        log(f"{image} already on {storage}, not downloading")
    else:
        log(f"downloading {image} to {storage} (import)")
        upid = pve.download_url(
            node, storage, "import", t.image_url, image, t.image_sha512, "sha512"
        )
        pve.wait_task(upid, timeout=IMPORT_TIMEOUT)

    log(f"creating VM {vmid} on {node}")
    try:
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
            ciupgrade=0,
            tags="lab;template",
        )
    except PveError as e:
        if "already exists" in str(e):
            log(f"VM {vmid} exists; delete it (or pick another pve.template) before rebuilding")
            return 1
        raise
    pve.wait_task(upid, timeout=IMPORT_TIMEOUT)
    resize = pve.resize(node, vmid, "scsi0", cfg.vm.disk)
    if resize:
        pve.wait_task(resize)

    log("first boot: installing packages and tk8s (10-15 minutes), then the VM powers off")
    pve.wait_task(pve.start(node, vmid))
    try:
        pve.wait_status(node, vmid, "stopped", timeout=FIRST_BOOT_TIMEOUT)
    except PveError as e:
        log(
            f"{e}; the build script failed, open the console of VM {vmid} and read "
            "/var/log/cloud-init-output.log"
        )
        return 1
    # Clones must get PVE's generated user-data (ciuser/cipassword), not the build snippet.
    pve.set_config(node, vmid, delete="cicustom")
    made = pve.make_template(node, vmid)
    if made:
        pve.wait_task(made)
    log(f"template {vmid} ready")
    return 0
