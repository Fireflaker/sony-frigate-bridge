#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_VERSION="$(uname -r)"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  dkms \
  ffmpeg \
  git \
  iw \
  linux-headers-"${KERNEL_VERSION}" \
  linux-modules-extra-"${KERNEL_VERSION}" \
  network-manager \
  python3-pip \
  python3-venv \
  rfkill \
  rtl8812au-dkms \
  unzip \
  usbutils \
  wireless-tools \
  wpasupplicant

systemctl enable NetworkManager
systemctl restart NetworkManager

modprobe cfg80211 || true
modprobe mac80211 || true
modprobe 8812au || true

python3 -m venv "${ROOT_DIR}/.venv"
"${ROOT_DIR}/.venv/bin/pip" install --upgrade pip
"${ROOT_DIR}/.venv/bin/pip" install requests

cat <<EOF
Bootstrap complete.

Suggested verification:
  lsusb | grep -i 0bda:8812
  lsmod | egrep '8812au|cfg80211|mac80211'
  iw dev
  nmcli device status

Next step:
  cp "${ROOT_DIR}/imagingedge-liveview.service" /etc/systemd/system/
  cp "${ROOT_DIR}/imagingedge-liveview.env.example" /etc/default/imagingedge-liveview
  systemctl daemon-reload
  systemctl enable --now imagingedge-liveview
EOF
