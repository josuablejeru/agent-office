# Base image

Agents boot from a qcow2 overlay backed by one shared base image:

    ~/.config/agent-office/images/debian-desktop.qcow2   (shared, read-only backing file)
    ~/.config/agent-office/agents/<name>/disk.qcow2      (per-agent overlay, persistent)

Build it with `scripts/create-base-image.sh`. The script:

1. downloads the Debian 13 ARM64 cloud image (a minimal install with
   cloud-init) and verifies its SHA-512;
2. converts it to qcow2 and grows it to 20 GB;
3. boots it once under QEMU/HVF with a cloud-init seed that
   - creates the `agent` user,
   - installs an XFCE desktop on Xorg with LightDM logging `agent` in
     automatically, with screen blanking and locking disabled,
   - installs Google Chrome (Google publishes it for Linux ARM64),
   - creates the guest daemon's virtualenv with `websockets` and `playwright`,
   - enables the daemon's systemd services;
4. powers off, marks the image read-only (about 4 GB) and stores the generated
   login password in `~/.config/agent-office/secrets/debian-desktop.login-password`.

The "generic" Debian image is used rather than "genericcloud", whose kernel
has no display drivers and would leave the VM desktop blank.

## What is not in the image

The guest daemon's code and the agent's secret are not baked in. The host
writes them to a small seed disk on every VM start, and a systemd unit in the
guest installs them at boot. Updating the daemon therefore only needs a VM
restart, not a new base image.

Chrome is not started by the image either: the daemon opens it on the desktop
with a persistent profile (`~/.config/agent-office-browser`) and reopens it if it
is closed. Before a VM is stopped the daemon quits Chrome cleanly so cookies and
site storage are written to disk.

## Rebuilding

The script refuses to replace a base image that agent disks already depend on,
because an overlay is only valid against the exact backing file it was created
from. To change the base for new agents, build under another name
(`IMAGE_NAME=debian-desktop-2 scripts/create-base-image.sh`).

Settings are environment variables at the top of the script (`DISK_SIZE`,
`DEBIAN_RELEASE`, `BUILD_MEMORY_MB`, ...). Image files are never committed.
