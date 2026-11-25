#!/usr/bin/env bash
# bash start-tailscale.sh
# curl ipinfo.io

set -euo pipefail

if ! command -v tailscale >/dev/null 2>&1; then
  echo "Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
fi

if ! pgrep -f '[t]ailscaled.*tun=userspace-networking' >/dev/null 2>&1; then
  echo "Starting tailscaled..."
  sudo nohup tailscaled --tun=userspace-networking \
    >/tmp/tailscaled.log 2>&1 &
fi

echo "Waiting for tailscaled..."
for attempt in {1..15}; do
  if tailscale status >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! tailscale status >/dev/null 2>&1; then
  echo "tailscaled did not become ready. Log: /tmp/tailscaled.log" >&2
  exit 1
fi

echo "Connecting to Tailscale..."
sudo tailscale up --ssh --advertise-exit-node

echo "Advertising this Codespace as an exit node..."
sudo tailscale set --advertise-exit-node

echo
echo "Tailscale is ready."
echo "Codespace IP: $(tailscale ip -4)"
echo "Approve the exit node at: https://login.tailscale.com/admin/machines"
tailscale status

