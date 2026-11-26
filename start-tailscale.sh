#!/usr/bin/env bash
# Start Tailscale on this machine and join the tailnet as an exit node.
# Auth key sources, first one found wins:
#   1. $TAILSCALE_AUTHKEY environment variable (e.g. a GitHub Codespace secret)
#   2. $HOME/.config/tailscale-authkey file (chmod 600, never committed)
# With a key the node joins tagged as tag:codespace, so its exit node is
# auto-approved by the tailnet ACL and no browser approval is needed.
# Without a key it falls back to interactive login.

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

AUTHKEY="${TAILSCALE_AUTHKEY:-}"
if [ -z "$AUTHKEY" ] && [ -f "$HOME/.config/tailscale-authkey" ]; then
  AUTHKEY="$(cat "$HOME/.config/tailscale-authkey")"
fi

if [ -n "$AUTHKEY" ]; then
  echo "Connecting to Tailscale with auth key (tag:codespace)..."
  sudo tailscale up --authkey="$AUTHKEY" --advertise-tags=tag:codespace --ssh --advertise-exit-node
else
  echo "No auth key found (set TAILSCALE_AUTHKEY); connecting interactively..."
  sudo tailscale up --ssh --advertise-exit-node
fi

echo "Advertising this Codespace as an exit node..."
sudo tailscale set --advertise-exit-node

echo
echo "Tailscale is ready."
echo "Codespace IP: $(tailscale ip -4)"
tailscale status
