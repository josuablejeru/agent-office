#!/usr/bin/env bash
# Builds the shared base image all agent VMs boot from:
#
#   ~/.config/agent-office/images/debian-desktop.qcow2
#
# It downloads the official Debian ARM64 cloud image (a minimal install with
# cloud-init), boots it once under QEMU/HVF with a seed that installs an XFCE
# desktop, Google Chrome, Playwright and the guest daemon service, then powers
# off and marks the result read-only.
#
# Usage: scripts/create-base-image.sh [--force]
#   --force   rebuild an existing base image (refused while agent disks use it)
set -euo pipefail

HOME_DIR="${AGENT_OFFICE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/agent-office}"
IMAGE_NAME="${IMAGE_NAME:-debian-desktop}"
DEBIAN_RELEASE="${DEBIAN_RELEASE:-trixie}"
DEBIAN_VERSION="${DEBIAN_VERSION:-13}"
DISK_SIZE="${DISK_SIZE:-20G}"
BUILD_MEMORY_MB="${BUILD_MEMORY_MB:-4096}"
BUILD_CPUS="${BUILD_CPUS:-4}"
BUILD_TIMEOUT_SECONDS="${BUILD_TIMEOUT_SECONDS:-3600}"
GUEST_USER="agent"
PACKAGES=(python3 python3-venv git curl sudo ca-certificates qemu-guest-agent
  # Desktop: XFCE on Xorg, logged in automatically as the guest user.
  xserver-xorg xfce4 xfce4-terminal lightdm lightdm-gtk-greeter dbus-x11
  fonts-liberation fonts-noto-core fonts-noto-color-emoji)
# The agent's browser. Google publishes Chrome for Linux ARM64.
CHROME_DEB_URL="https://dl.google.com/linux/direct/google-chrome-stable_current_arm64.deb"
# Python packages for the guest daemon's virtualenv. Keep websockets in step
# with the host version in pyproject.toml.
GUEST_VENV="/opt/agent-office/venv"
GUEST_PIP_PACKAGES=("websockets==17.2" "playwright==1.63.0")

# "generic" rather than "genericcloud": the latter's kernel has no display
# drivers, which the VM desktop (VNC) needs.
MIRROR="https://cloud.debian.org/images/cloud/${DEBIAN_RELEASE}/latest"
CLOUD_IMAGE="debian-${DEBIAN_VERSION}-generic-arm64.qcow2"
OK_MARKER="AGENT_OFFICE_PROVISION_OK"

IMAGES_DIR="$HOME_DIR/images"
SECRETS_DIR="$HOME_DIR/secrets"
BASE_IMAGE="$IMAGES_DIR/$IMAGE_NAME.qcow2"
CACHE_DIR="$IMAGES_DIR/cache"
PASSWORD_FILE="$SECRETS_DIR/$IMAGE_NAME.login-password"

force=false
[ "${1:-}" = "--force" ] && force=true

die() { echo "error: $*" >&2; exit 1; }

command -v qemu-system-aarch64 >/dev/null || die "qemu-system-aarch64 not found (brew install qemu)"
command -v qemu-img >/dev/null || die "qemu-img not found (brew install qemu)"
FIRMWARE="$(dirname "$(command -v qemu-system-aarch64)")/../share/qemu/edk2-aarch64-code.fd"
[ -f "$FIRMWARE" ] || die "UEFI firmware not found at $FIRMWARE"

if [ -e "$BASE_IMAGE" ]; then
  if compgen -G "$HOME_DIR/agents/*/disk.qcow2" >/dev/null; then
    die "$BASE_IMAGE is the backing file of existing agent disks; replacing it would corrupt them"
  fi
  $force || die "$BASE_IMAGE already exists (use --force to rebuild)"
fi

mkdir -p "$IMAGES_DIR" "$CACHE_DIR"
mkdir -p -m 700 "$SECRETS_DIR"

# One build at a time: two would write the same download and image files.
LOCK_DIR="$IMAGES_DIR/.build.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  other="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
    die "another image build is already running (pid $other)"
  fi
  rm -rf "$LOCK_DIR" && mkdir "$LOCK_DIR"   # left behind by a build that was killed
fi
echo $$ >"$LOCK_DIR/pid"
rm -rf "$IMAGES_DIR"/build.??????          # work directories of killed builds

WORK_DIR="$(mktemp -d "$IMAGES_DIR/build.XXXXXX")"
qemu_pid=""
cleanup() {
  [ -n "$qemu_pid" ] && kill "$qemu_pid" 2>/dev/null || true
  rm -rf "$WORK_DIR" "$LOCK_DIR"
}
trap cleanup EXIT
trap 'exit 143' TERM INT

echo "==> Downloading $CLOUD_IMAGE"
curl -fL --retry 3 -C - -o "$CACHE_DIR/$CLOUD_IMAGE" "$MIRROR/$CLOUD_IMAGE"
expected="$(curl -fsSL "$MIRROR/SHA512SUMS" | awk -v f="$CLOUD_IMAGE" '$2 == f {print $1}')"
[ -n "$expected" ] || die "no checksum published for $CLOUD_IMAGE"
actual="$(shasum -a 512 "$CACHE_DIR/$CLOUD_IMAGE" | awk '{print $1}')"
if [ "$expected" != "$actual" ]; then
  rm -f "$CACHE_DIR/$CLOUD_IMAGE"
  die "checksum mismatch for $CLOUD_IMAGE; the cached download was removed, run again"
fi

echo "==> Preparing ${DISK_SIZE} disk"
qemu-img convert -O qcow2 "$CACHE_DIR/$CLOUD_IMAGE" "$WORK_DIR/disk.qcow2"
qemu-img resize "$WORK_DIR/disk.qcow2" "$DISK_SIZE" >/dev/null

password="$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 16 || true)"
mkdir "$WORK_DIR/seed"
cat >"$WORK_DIR/seed/meta-data" <<EOF
instance-id: agent-office-base-build
local-hostname: $IMAGE_NAME
EOF
cat >"$WORK_DIR/seed/user-data" <<EOF
#cloud-config
users:
  - name: $GUEST_USER
    gecos: Agent
    shell: /bin/bash
    groups: [sudo]
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: false
chpasswd:
  expire: false
  users:
    - name: $GUEST_USER
      password: $password
      type: text
package_update: true
packages:
$(printf '  - %s\n' "${PACKAGES[@]}")
write_files:
  # The host puts the daemon code and the agent's secret on the seed disk and
  # rewrites it on every VM start, so a daemon update only needs a VM restart.
  - path: /usr/local/sbin/agent-office-load-seed
    permissions: "0755"
    content: |
      #!/bin/sh
      set -eu
      mnt=/run/agent-office/seed
      dev="\$(blkid -L cidata || blkid -L CIDATA)"
      mkdir -p "\$mnt" /etc/agent-office
      mount -o ro "\$dev" "\$mnt"
      trap 'umount "\$mnt"' EXIT
      rm -rf /opt/agent-office/guest
      cp -r "\$mnt/guest" /opt/agent-office/guest
      install -m 0600 -o $GUEST_USER -g $GUEST_USER "\$mnt/agent-secret" /etc/agent-office/agent-secret
  - path: /etc/systemd/system/agent-office-seed.service
    content: |
      [Unit]
      Description=agent-office: load agent daemon and secret from the seed disk
      After=local-fs.target

      [Service]
      Type=oneshot
      RemainAfterExit=yes
      ExecStart=/usr/local/sbin/agent-office-load-seed
      StandardOutput=journal+console
      StandardError=journal+console

      [Install]
      WantedBy=multi-user.target
  - path: /etc/systemd/system/agent-office-agentd.service
    content: |
      [Unit]
      Description=agent-office guest agent daemon
      Requires=agent-office-seed.service
      After=agent-office-seed.service network.target

      [Service]
      User=$GUEST_USER
      WorkingDirectory=/home/$GUEST_USER
      Environment=PYTHONPATH=/opt/agent-office PYTHONUNBUFFERED=1
      # The desktop session the daemon opens the browser in.
      Environment=DISPLAY=:0 XAUTHORITY=/home/$GUEST_USER/.Xauthority
      ExecStart=$GUEST_VENV/bin/python -m guest.agentd
      Restart=on-failure
      RestartSec=2
      StandardOutput=journal+console
      StandardError=journal+console

      [Install]
      WantedBy=multi-user.target
  - path: /etc/lightdm/lightdm.conf.d/50-agent-office.conf
    content: |
      [Seat:*]
      autologin-user=$GUEST_USER
      autologin-user-timeout=0
      user-session=xfce
  # The seed disk is plumbing, not something to show on the desktop.
  - path: /etc/udev/rules.d/90-agent-office.rules
    content: |
      SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="cidata", ENV{UDISKS_IGNORE}="1"
      SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="CIDATA", ENV{UDISKS_IGNORE}="1"
  # Keep Chrome from interrupting the agent with prompts it cannot act on.
  - path: /etc/opt/chrome/policies/managed/agent-office.json
    content: |
      {
        "DefaultBrowserSettingEnabled": false,
        "MetricsReportingEnabled": false,
        "PromotionalTabsEnabled": false,
        "PrivacySandboxPromptEnabled": false,
        "TranslateEnabled": false
      }
  # Never blank or lock: takeover must land on the live desktop.
  - path: /etc/X11/xorg.conf.d/10-agent-office.conf
    content: |
      Section "ServerFlags"
        Option "BlankTime" "0"
        Option "StandbyTime" "0"
        Option "SuspendTime" "0"
        Option "OffTime" "0"
      EndSection
runcmd:
  - mkdir -p /etc/agent-office /opt/agent-office
  - python3 -m venv $GUEST_VENV
  - $GUEST_VENV/bin/pip install --no-cache-dir ${GUEST_PIP_PACKAGES[*]}
  - curl -fsSL -o /tmp/chrome.deb $CHROME_DEB_URL
  - DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/chrome.deb
  - rm -f /tmp/chrome.deb
  - DEBIAN_FRONTEND=noninteractive apt-get purge -y light-locker xfce4-screensaver xscreensaver xfce4-power-manager || true
  - groupadd -f autologin
  - usermod -aG autologin $GUEST_USER
  - systemctl set-default graphical.target
  - systemctl enable qemu-guest-agent agent-office-seed.service agent-office-agentd.service lightdm.service
  - apt-get clean
  - dpkg -s ${PACKAGES[*]} >/dev/null && $GUEST_VENV/bin/python -c "import websockets, playwright" && test -x /usr/bin/google-chrome && echo $OK_MARKER >/dev/ttyAMA0
  # Each agent overlay must generate its own machine-id on first boot.
  - truncate -s 0 /etc/machine-id
  - rm -f /var/lib/dbus/machine-id
power_state:
  mode: poweroff
  timeout: 120
EOF
hdiutil makehybrid -quiet -iso -joliet -default-volume-name cidata \
  -o "$WORK_DIR/seed.iso" "$WORK_DIR/seed"

echo "==> Provisioning (console log: $WORK_DIR/console.log)"
qemu-system-aarch64 \
  -machine virt -accel hvf -cpu host -smp "$BUILD_CPUS" -m "$BUILD_MEMORY_MB" \
  -bios "$FIRMWARE" \
  -drive "if=none,id=hd0,file=$WORK_DIR/disk.qcow2,format=qcow2" \
  -device virtio-blk-pci,drive=hd0,bootindex=0 \
  -drive "if=none,id=seed,file=$WORK_DIR/seed.iso,format=raw,readonly=on" \
  -device virtio-blk-pci,drive=seed \
  -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
  -display none -serial "file:$WORK_DIR/console.log" &
qemu_pid=$!

waited=0
while kill -0 "$qemu_pid" 2>/dev/null; do
  if [ "$waited" -ge "$BUILD_TIMEOUT_SECONDS" ]; then
    cp "$WORK_DIR/console.log" "$IMAGES_DIR/$IMAGE_NAME.build-failed.log" 2>/dev/null || true
    die "provisioning timed out; see $IMAGES_DIR/$IMAGE_NAME.build-failed.log"
  fi
  sleep 5
  waited=$((waited + 5))
done
wait "$qemu_pid" || true
qemu_pid=""

if ! grep -q "$OK_MARKER" "$WORK_DIR/console.log"; then
  cp "$WORK_DIR/console.log" "$IMAGES_DIR/$IMAGE_NAME.build-failed.log"
  die "provisioning did not complete; see $IMAGES_DIR/$IMAGE_NAME.build-failed.log"
fi

rm -f "$BASE_IMAGE"
mv "$WORK_DIR/disk.qcow2" "$BASE_IMAGE"
chmod 444 "$BASE_IMAGE"
rm -f "$PASSWORD_FILE"
(umask 077 && printf '%s\n' "$password" >"$PASSWORD_FILE")

echo "==> Built $BASE_IMAGE"
echo "    Guest login: user '$GUEST_USER', password stored in $PASSWORD_FILE"
