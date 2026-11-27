#!/usr/bin/env bash
# start-fresh.sh — first boot on a brand-new machine.
#
# Starts muse2api (Docker where available, otherwise uvicorn) and publishes
# it with a cloudflared quick tunnel, so the first-run setup wizard can be
# opened from any browser without touching a terminal:
#
#   bash start-fresh.sh            # port 18610 (default)
#   PORT=18699 bash start-fresh.sh # test on another port
#   bash start-fresh.sh --no-tunnel  # local only, no public URL
#
# The trycloudflare URL changes on EVERY restart — it is for first setup
# only, not stable public access. For stable access use a Codespace public
# port (see AGENT_GUIDE.md §7) or a named Cloudflare tunnel.
#
# The tunnel is optional: with a terminal attached, the script asks
# "Expose a public URL via trycloudflare? [Y/n]" (default Y after 15 s).
# Non-interactive runs always start the tunnel, unless --no-tunnel is
# passed or MUSE2API_NO_TUNNEL=1 is set.
#
# On the Muse VM trycloudflare is BLOCKED (api.trycloudflare.com returns
# 403 through the VM egress proxy). This script detects that and says so
# plainly instead of hanging; the server keeps running locally.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-18610}"
LOG="/tmp/muse2api-fresh.log"
TUNNEL_LOG="/tmp/muse2api-fresh-tunnel.log"

info() { echo "$*"; }
die() { echo "error: $*" >&2; exit 1; }

: > "$LOG"
: > "$TUNNEL_LOG"

# ---------- 1. Start the server ----------
started=""
if command -v docker >/dev/null 2>&1 && timeout 10 docker compose version >/dev/null 2>&1; then
  info "starting muse2api with Docker Compose on port $PORT..."
  (cd "$REPO_ROOT" && PORT="$PORT" docker compose up -d --build >>"$LOG" 2>&1) || die "docker compose up failed (see $LOG)"
  started="docker"
elif [ -x "$REPO_ROOT/.venv/bin/uvicorn" ]; then
  info "starting muse2api with uvicorn (.venv) on port $PORT..."
  (cd "$REPO_ROOT" && MUSE2API_HOME="$REPO_ROOT" MUSE2API_PORT="$PORT" \
    exec setsid "$REPO_ROOT/.venv/bin/uvicorn" app:app --host 0.0.0.0 --port "$PORT" \
    >>"$LOG" 2>&1 < /dev/null) &
  disown # otherwise bash waits for the backgrounded server when this script exits
  started="uvicorn-venv"
elif python3 -c "import uvicorn" 2>/dev/null; then
  info "starting muse2api with uvicorn (system python) on port $PORT..."
  (cd "$REPO_ROOT" && MUSE2API_HOME="$REPO_ROOT" MUSE2API_PORT="$PORT" \
    exec setsid python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT" \
    >>"$LOG" 2>&1 < /dev/null) &
  disown # otherwise bash waits for the backgrounded server when this script exits
  started="uvicorn-system"
else
  die "no Docker and no uvicorn found — on a Codespace/Docker host use docker compose up, otherwise: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

info "waiting for the admin page on http://127.0.0.1:$PORT/ ..."
ok=0
for _ in $(seq 1 30); do
  if curl -fs -o /dev/null --max-time 5 "http://127.0.0.1:$PORT/" 2>/dev/null; then ok=1; break; fi
  sleep 2
done
[ "$ok" -eq 1 ] || die "admin page did not return 200 on port $PORT (started via $started; see $LOG)"
info "server is up (via $started)."

# ---------- 2. Ask about the tunnel (interactive only) ----------
# --no-tunnel / MUSE2API_NO_TUNNEL=1 skips the public URL entirely.
# With a terminal attached and no flag, ask first (default Y after 15 s
# so a scripted run never hangs on the prompt). Non-interactive runs
# keep the old behaviour: always start the tunnel.
NO_TUNNEL="${MUSE2API_NO_TUNNEL:-0}"
for arg in "$@"; do
  case "$arg" in
    --no-tunnel) NO_TUNNEL=1 ;;
    *) die "unknown argument: $arg (only --no-tunnel is supported)" ;;
  esac
done
if [ "$NO_TUNNEL" = "0" ] && [ -t 0 ]; then
  printf 'Expose a public URL via trycloudflare? [Y/n] '
  answer=""
  IFS= read -r -t 15 answer || true
  case "$answer" in
    [nN]|[nN][oO]) NO_TUNNEL=1 ;;
  esac
fi
if [ "$NO_TUNNEL" = "1" ]; then
  info ""
  info "no tunnel: server is local only at http://127.0.0.1:$PORT/"
  setup="$(curl -s --max-time 5 "http://127.0.0.1:$PORT/admin/setup-status" 2>/dev/null || echo '')"
  if echo "$setup" | grep -q '"setup_needed": *true'; then
    info "setup needed: open http://127.0.0.1:$PORT/ — the first-run setup form appears instead of the login."
    info "Enter the 4 B2 values there; the server restores the latest backup and the setup page then locks itself."
  else
    info "setup already done: open http://127.0.0.1:$PORT/ and log in with your API key."
  fi
  exit 0
fi

# ---------- 3. cloudflared quick tunnel ----------
CLOUDFLARED=""
for cand in cloudflared "$HOME/workspace/cloudflared" "$REPO_ROOT/cloudflared" /tmp/cloudflared; do
  if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then CLOUDFLARED="$cand"; break; fi
done
if [ -z "$CLOUDFLARED" ]; then
  arch="$(uname -m)"
  case "$arch" in
    x86_64) bin="cloudflared-linux-amd64" ;;
    aarch64|arm64) bin="cloudflared-linux-arm64" ;;
    *) die "unsupported architecture $arch for cloudflared download — install cloudflared manually" ;;
  esac
  info "downloading cloudflared ($bin)..."
  if curl -fsSL --max-time 120 -o /tmp/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/$bin"; then
    chmod +x /tmp/cloudflared
    CLOUDFLARED="/tmp/cloudflared"
  else
    die "could not download cloudflared — install it manually (https://developers.cloudflare.com/cloudflare-one/connections/connect/downloads/) and re-run this script. The server is still running at http://127.0.0.1:$PORT/"
  fi
fi

info "starting cloudflared quick tunnel to localhost:$PORT ..."
setsid "$CLOUDFLARED" tunnel --url "http://localhost:$PORT" >>"$TUNNEL_LOG" 2>&1 < /dev/null &
TUNNEL_PID=$!
disown # the tunnel must not keep this script from exiting either
url=""
for _ in $(seq 1 30); do
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then break; fi
  url="$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -n 1 || true)"
  [ -n "$url" ] && break
  sleep 1
done

if [ -z "$url" ]; then
  kill "$TUNNEL_PID" 2>/dev/null || true
  api_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://api.trycloudflare.com/ 2>/dev/null || echo 000)"
  if [ "$api_code" = "403" ]; then
    die "trycloudflare is BLOCKED on this machine (api.trycloudflare.com returned 403 — this happens on the Muse VM, whose egress proxy blocks it). The server is still running at http://127.0.0.1:$PORT/ — open it directly, or publish it another way (Codespace public port, named Cloudflare tunnel). Tunnel log: $TUNNEL_LOG"
  fi
  die "cloudflared did not produce a public URL (log: $TUNNEL_LOG). The server is still running at http://127.0.0.1:$PORT/"
fi

# ---------- 4. Setup status ----------
info ""
info "public URL: $url"
info "(this trycloudflare URL changes on every restart — first setup only, not stable access)"
setup="$(curl -s --max-time 5 "http://127.0.0.1:$PORT/admin/setup-status" 2>/dev/null || echo '')"
if echo "$setup" | grep -q '"setup_needed": *true'; then
  info "setup needed: open $url/ — the first-run setup form appears instead of the login."
  info "Enter the 4 B2 values there; the server restores the latest backup and the setup page then locks itself."
else
  info "setup already done: open $url/ and log in with your API key."
fi
info "tunnel pid: $TUNNEL_PID (log: $TUNNEL_LOG)"
