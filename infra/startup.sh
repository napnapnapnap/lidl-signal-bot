#!/bin/bash
set -euo pipefail

# ── Docker ────────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  apt-get update -q
  apt-get install -y -q ca-certificates curl gnupg lsb-release git
  install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
  fi
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
  usermod -aG docker debian
fi

# ── Persistent disk ───────────────────────────────────────────────────────────
DATA_DEV="/dev/disk/by-id/google-data"
DATA_MOUNT="/data"

if [ -b "$DATA_DEV" ]; then
  # Format only if the disk has no filesystem yet
  if ! blkid "$DATA_DEV" | grep -q "TYPE="; then
    mkfs.ext4 -F "$DATA_DEV"
  fi
  mkdir -p "$DATA_MOUNT"
  if ! mountpoint -q "$DATA_MOUNT"; then
    mount "$DATA_DEV" "$DATA_MOUNT"
  fi
  # Add fstab entry only once
  if ! grep -q "$DATA_DEV" /etc/fstab; then
    echo "$DATA_DEV $DATA_MOUNT ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
fi

# ── Swap ──────────────────────────────────────────────────────────────────────
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ── Data directories ──────────────────────────────────────────────────────────
mkdir -p /data/n8n /data/signal-cli /data/receipts
chmod -R 777 /data

# ── Docker data root on persistent disk ───────────────────────────────────────
# Playwright + n8n images are ~1.5 GB total — too large for the 10 GB boot disk.
# Moving Docker's storage to /data keeps the OS disk free.
mkdir -p /data/docker
DAEMON_JSON=/etc/docker/daemon.json
if ! grep -q '\"data-root\"' "$DAEMON_JSON" 2>/dev/null; then
  echo '{"data-root": "/data/docker"}' > "$DAEMON_JSON"
  systemctl restart docker
fi
