#!/usr/bin/env bash
# muse2api one-command startup: Tailscale SSH + data sync + API.
#
#   bash start-muse2api.sh [--build]
#
# Runs everything muse2api needs, in order:
#   1. Tailscale (SSH + exit node) via start-tailscale.sh (idempotent).
#   2. Data sync: restores the latest B2 backup ONLY when local config
#      data is missing (.env or data/accounts.json absent). It never
#      overwrites local data — on a normal restart the disk already holds
#      the newest data, and a backup could be older.
#   3. API: docker compose up -d. If the container does not come up
#      (e.g. its read-write layer got corrupted by a Codespace restart
#      and it exits 255), it is force-recreated — the image and ./data
#      are intact, so nothing is lost.
#   4. Waits for the admin page and checks the API key.
#
# Idempotent and safe to run on every Codespace start (it is the
# devcontainer postStartCommand) or by hand in the VS Code terminal.
# A second concurrent run exits quietly (flock). Pass --build to force
# a Docker image rebuild (e.g. after pulling code changes).
set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

echo "=== muse2api start: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# One start at a time (postStartCommand and the shell fallback can overlap).
if command -v flock >/dev/null 2>&1; then
  exec 9>/tmp/muse2api-start.lock
  if ! flock -n 9; then echo "another muse2api start is already running, exiting"; exit 0; fi
fi

BUILD=0
[ "${1:-}" = "--build" ] && BUILD=1

# 1. Tailscale SSH + exit node.
if [ -f "$REPO_ROOT/start-tailscale.sh" ]; then
  echo "-- tailscale --"
  bash "$REPO_ROOT/start-tailscale.sh" || echo "warning: start-tailscale.sh failed (continuing)"
else
  echo "warning: start-tailscale.sh not found, skipping tailscale"
fi

# 2. Data sync (restore only if local config data is missing).
NEED_RESTORE=0
[ -f "$REPO_ROOT/.env" ] || NEED_RESTORE=1
[ -f "$REPO_ROOT/data/accounts.json" ] || NEED_RESTORE=1
if [ "$NEED_RESTORE" = 1 ]; then
  if [ -f "$REPO_ROOT/scripts/.env.storage" ] && command -v rclone >/dev/null 2>&1 && command -v age >/dev/null 2>&1; then
    echo "-- local data missing, restoring latest B2 backup --"
    bash "$REPO_ROOT/scripts/muse2api-data.sh" restore \
      || echo "warning: restore failed (starting with whatever local data exists)"
  else
    echo "warning: local data missing but no B2 config (scripts/.env.storage) or rclone/age — skipping restore"
  fi
else
  echo "-- local data present, no restore needed --"
fi

# 3. API.
if [ "$BUILD" = 1 ]; then
  echo "-- starting container (with rebuild) --"
  docker compose up -d --build
else
  echo "-- starting container --"
  docker compose up -d
fi

# If it still is not running, force a recreate (fixes a corrupt
# read-write layer after a Codespace restart, exit 255).
for i in 1 2 3 4 5 6; do
  sleep 5
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx muse2api; then break; fi
  if [ "$i" = 3 ]; then
    echo "-- container not running, force-recreating --"
    docker compose up -d --force-recreate \
      || { docker compose down; docker compose up -d; }
  fi
done

# Last resort: the image itself can be corrupt after a Codespace restart
# (missing layer blobs — `up` fails with "failed to get reader from
# content store"). Rebuild it from scratch, then start.
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx muse2api; then
  echo "-- container still not running, rebuilding image from scratch (slow) --"
  docker compose build --no-cache && docker compose up -d
fi

# 4. Wait for the admin page, then report.
UP=0
for _ in $(seq 1 24); do
  if curl -fsS -o /dev/null --max-time 5 http://127.0.0.1:18610/ 2>/dev/null; then UP=1; break; fi
  sleep 5
done
if [ "$UP" = 1 ]; then
  echo "-- muse2api is up: http://127.0.0.1:18610/ --"
else
  echo "warning: admin page did not answer within 2 minutes — check: docker logs muse2api"
fi
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'muse2api' \
  || echo "warning: muse2api container is not running"
if [ -f "$REPO_ROOT/.env" ]; then
  KEY="$(grep MUSE2API_KEY "$REPO_ROOT/.env" | cut -d= -f2-)"
  if [ -n "$KEY" ]; then
    if curl -fsS -o /dev/null --max-time 5 -H "Authorization: Bearer $KEY" \
        http://127.0.0.1:18610/v1/models; then
      echo "API key works (/v1/models 200)"
    else
      echo "warning: /v1/models did not return 200 with the key in .env"
    fi
  fi
fi
echo "=== muse2api start done ==="
