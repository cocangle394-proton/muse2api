#!/usr/bin/env bash
# muse2api-data.sh — shared-data backup / restore for muse2api.
#
# The repo code lives on GitHub (the fork). The runtime data lives in a
# central object store (Backblaze B2 by default, S3-compatible; Cloudflare
# R2 is an alternative) so any compute platform (Codespace, VPS, ...)
# can restore the same data and take over. Compute platforms are
# disposable; the bucket is the source of truth.
#
# Only config / system data is backed up — never images, videos or chat
# history. The backup bundle contains only these files (when they exist):
#   .env, data/accounts.json, data/mail_accounts.txt,
#   data/generation-queue.json
# Deliberately NOT backed up: data/tasks.json (chat/image/video task
# history), data/media/* (generated images/videos), data/downloads/*.
#
# Usage:
#   scripts/muse2api-data.sh setup    # check tools, write rclone config
#   scripts/muse2api-data.sh backup   # stop container, bundle+encrypt+upload, restart
#   scripts/muse2api-data.sh restore [bundle-name]  # download+decrypt+extract (does NOT start the container)
#   scripts/muse2api-data.sh status   # local data size, accounts count, remote backups
#
# Configuration comes ONLY from the environment or from
# scripts/.env.storage (gitignored, never committed; the old name
# scripts/.env.r2 is still sourced if present). Nothing secret is
# hardcoded here and nothing secret is ever printed by this script
# (only sha256 prefixes and lengths are shown).
#
#   STORAGE_PROVIDER      "b2" (default) or "r2"
#   STORAGE_BUCKET        bucket name (default: muse2api-data)
#   STORAGE_REMOTE        rclone remote name (default: storage).
#                         Override for tests, e.g. a local remote
#                         pointing at /tmp/b2-test.
#   B2_ENDPOINT           Backblaze B2 S3 endpoint, e.g.
#                         https://s3.us-west-004.backblazeb2.com
#                         (shown on the bucket page; the region varies)
#   B2_KEY_ID             B2 application key id (keyID)
#   B2_APPLICATION_KEY    B2 application key (shown once — never printed)
#   B2_REGION             optional override; otherwise derived from
#                         B2_ENDPOINT (the part after "s3.")
#   R2_ACCOUNT_ID         Cloudflare account ID (R2 endpoint host part)
#   R2_ACCESS_KEY_ID      R2 API token access key id
#   R2_SECRET_ACCESS_KEY  R2 API token secret (never printed)
#   BACKUP_PASSPHRASE     age passphrase for the backup bundle (never printed, never written to a file)
#   RCLONE_CONFIG         rclone config path (default: ~/.config/rclone/rclone.conf)
#
# Why age needs a helper below: `age -p` refuses to read a passphrase
# from a pipe (it needs a TTY) and the `script(1)` PTY workaround echoes
# the passphrase into the log. _age_pty runs age under a real PTY with
# python3 and feeds the passphrase only after age has disabled echo,
# so the passphrase never appears in output or in any file.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Local env file (gitignored). Sourced first; real environment wins afterwards
# only if it is set after sourcing, so keep this file out of git.
for _env_file in "$SCRIPT_DIR/.env.storage" "$SCRIPT_DIR/.env.r2"; do
  if [ -f "$_env_file" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$_env_file"
    set +a
  fi
done

STORAGE_PROVIDER="${STORAGE_PROVIDER:-b2}"
# R2_BUCKET / R2_REMOTE are accepted as aliases for the generic names.
STORAGE_BUCKET="${STORAGE_BUCKET:-${R2_BUCKET:-muse2api-data}}"
STORAGE_REMOTE="${STORAGE_REMOTE:-${R2_REMOTE:-storage}}"
RCLONE_CONF="${RCLONE_CONFIG:-$HOME/.config/rclone/rclone.conf}"
BACKUP_PREFIX="backups"

die() { echo "error: $*" >&2; exit 1; }
info() { echo "$*"; }

masked() {
  # masked <value> — show first 4 chars + length, never the whole value.
  local v="$1"
  if [ -z "$v" ]; then echo "(empty)"; return; fi
  printf '%s*** (len %s)' "${v:0:4}" "${#v}"
}

check_tools() {
  local missing=0
  for t in rclone age python3 tar sha256sum curl; do
    command -v "$t" >/dev/null 2>&1 || { echo "missing tool: $t" >&2; missing=1; }
  done
  [ "$missing" -eq 0 ] || die "install the missing tools first (e.g. sudo apt-get install -y rclone age)"
}

remote_configured() {
  [ -f "$RCLONE_CONF" ] && grep -q "^\[$STORAGE_REMOTE\]" "$RCLONE_CONF"
}

require_remote() {
  remote_configured || die "rclone remote '$STORAGE_REMOTE' is not configured. Run '$0 setup' first (or set STORAGE_REMOTE to an existing remote)."
}

require_passphrase() {
  [ -n "${BACKUP_PASSPHRASE:-}" ] || die "BACKUP_PASSPHRASE is not set. Set it in the environment or in scripts/.env.storage (never commit it)."
}

accounts_count() {
  # Count accounts in data/accounts.json (a JSON list). Count only — never print content.
  local f="$REPO_ROOT/data/accounts.json"
  if [ ! -f "$f" ]; then echo 0; return; fi
  python3 - "$f" <<'PYEOF' 2>/dev/null || echo 0
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        print(len(data))
    elif isinstance(data, dict) and isinstance(data.get("accounts"), list):
        print(len(data["accounts"]))
    else:
        print(0)
except Exception:
    print(0)
PYEOF
}

# _age_pty <enc|dec> <age args...>
# Runs age under a PTY and feeds BACKUP_PASSPHRASE only after age
# disables terminal echo, so the passphrase never appears in output.
_age_pty() {
  local mode="$1"; shift
  AGE_PTY_MODE="$mode" python3 - "$@" <<'PYEOF'
import os, pty, select, signal, sys, time

mode = os.environ.get("AGE_PTY_MODE", "")
pw = os.environ.get("BACKUP_PASSPHRASE", "")
if not pw:
    sys.stderr.write("age-pty: BACKUP_PASSPHRASE is empty\n")
    sys.exit(2)
args = sys.argv[1:]
pid, fd = pty.fork()
if pid == 0:
    try:
        os.execvp(args[0], args)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("age-pty: exec failed: %s\n" % exc)
        os._exit(127)
out = b""
sent = 0
need = 2 if mode == "enc" else 1
deadline = time.time() + 120
code = None
while time.time() < deadline:
    r, _, _ = select.select([fd], [], [], 0.5)
    if r:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        if chunk:
            out += chunk
            prompts = out.count(b"passphrase")
            if sent < need and prompts > sent:
                # Give age a moment to disable terminal echo for this
                # prompt before sending; sending immediately can race
                # the echo-off and leak the passphrase into the log.
                time.sleep(0.3)
                os.write(fd, (pw + "\n").encode())
                sent += 1
    wpid, status = os.waitpid(pid, os.WNOHANG)
    if wpid == pid:
        code = os.waitstatus_to_exitcode(status)
        break
if code is None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        code = 124
# Only show age's output when it failed, with the passphrase scrubbed
# out of it. On success the output is discarded so a passphrase can
# never leak into a log through terminal echo.
if (code is None or code != 0) and out:
    sys.stderr.buffer.write(out.replace(pw.encode(), b"[REDACTED]"))
    sys.stderr.flush()
sys.exit(code if code is not None else 1)
PYEOF
}

cmd_setup() {
  check_tools
  case "$RCLONE_CONF" in
    "$REPO_ROOT"/*) die "refusing to write the rclone config inside the repo ($RCLONE_CONF)" ;;
  esac
  if remote_configured; then
    info "rclone remote '$STORAGE_REMOTE' is already configured in $RCLONE_CONF — nothing to do."
    info "provider: $STORAGE_PROVIDER"
    info "bucket: $STORAGE_BUCKET"
    return 0
  fi
  mkdir -p "$(dirname "$RCLONE_CONF")"
  chmod 700 "$(dirname "$RCLONE_CONF")" 2>/dev/null || true
  case "$STORAGE_PROVIDER" in
    b2)
      [ -n "${B2_ENDPOINT:-}" ] || die "B2_ENDPOINT is not set (e.g. https://s3.us-west-004.backblazeb2.com — shown on the bucket page)."
      [ -n "${B2_KEY_ID:-}" ] || die "B2_KEY_ID is not set."
      [ -n "${B2_APPLICATION_KEY:-}" ] || die "B2_APPLICATION_KEY is not set."
      local region="${B2_REGION:-}"
      if [ -z "$region" ]; then
        # https://s3.us-west-004.backblazeb2.com -> us-west-004
        region="${B2_ENDPOINT#https://s3.}"
        region="${region%%.backblazeb2.com}"
        case "$region" in
          *backblazeb2.com*|"$B2_ENDPOINT") die "cannot derive the B2 region from B2_ENDPOINT ($B2_ENDPOINT). Set B2_REGION explicitly." ;;
        esac
      fi
      {
        echo "[$STORAGE_REMOTE]"
        echo "type = s3"
        echo "provider = Other"
        echo "access_key_id = $B2_KEY_ID"
        echo "secret_access_key = $B2_APPLICATION_KEY"
        echo "endpoint = $B2_ENDPOINT"
        echo "region = $region"
        echo "acl = private"
        echo ""
      } >> "$RCLONE_CONF"
      chmod 600 "$RCLONE_CONF"
      info "wrote rclone remote '$STORAGE_REMOTE' to $RCLONE_CONF (chmod 600)"
      info "provider: b2"
      info "endpoint: $B2_ENDPOINT"
      info "region: $region"
      info "key id: $(masked "$B2_KEY_ID")"
      info "bucket: $STORAGE_BUCKET"
      info "application key: (not shown, len ${#B2_APPLICATION_KEY})"
      ;;
    r2)
      [ -n "${R2_ACCOUNT_ID:-}" ] || die "R2_ACCOUNT_ID is not set (and remote '$STORAGE_REMOTE' is not configured yet)."
      [ -n "${R2_ACCESS_KEY_ID:-}" ] || die "R2_ACCESS_KEY_ID is not set."
      [ -n "${R2_SECRET_ACCESS_KEY:-}" ] || die "R2_SECRET_ACCESS_KEY is not set."
      {
        echo "[$STORAGE_REMOTE]"
        echo "type = s3"
        echo "provider = Cloudflare"
        echo "access_key_id = $R2_ACCESS_KEY_ID"
        echo "secret_access_key = $R2_SECRET_ACCESS_KEY"
        echo "endpoint = https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com"
        echo "acl = private"
        echo ""
      } >> "$RCLONE_CONF"
      chmod 600 "$RCLONE_CONF"
      info "wrote rclone remote '$STORAGE_REMOTE' to $RCLONE_CONF (chmod 600)"
      info "provider: r2"
      info "account id: $(masked "$R2_ACCOUNT_ID")"
      info "access key id: $(masked "$R2_ACCESS_KEY_ID")"
      info "bucket: $STORAGE_BUCKET"
      info "secret access key: (not shown, len ${#R2_SECRET_ACCESS_KEY})"
      ;;
    *)
      die "unknown STORAGE_PROVIDER '$STORAGE_PROVIDER' (use 'b2' or 'r2')"
      ;;
  esac
}

cmd_backup() {
  check_tools
  require_passphrase
  require_remote
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  local ts bundle work sha size count was_running=0
  ts="$(date +%Y%m%d-%H%M%S)"
  bundle="muse2api-data-${ts}.tar.gz.age"
  work="$(mktemp -d /tmp/muse2api-backup.XXXXXX)"
  # Expand $work now: a trap runs after locals go out of scope, and
  # under `set -u` the unexpanded name would fail at exit.
  trap "rm -rf '$work'" EXIT

  # Stop the container first so accounts.json is not being written
  # while we tar it. Only if it is actually running.
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "muse2api"; then
    info "stopping container muse2api..."
    (cd "$REPO_ROOT" && docker compose stop >/dev/null)
    was_running=1
    # If the upload (or anything after this point) fails, the EXIT trap
    # restarts the container, so a failed backup never leaves the
    # service down. On success the container is already running and
    # the trap's restart is a no-op.
    trap "rm -rf '$work'; if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'muse2api'; then (cd '$REPO_ROOT' && docker compose up -d >/dev/null 2>&1 || true); fi" EXIT
  else
    info "container muse2api is not running — skipping stop."
  fi

  # Config / system data only — explicit allowlist. Never images,
  # videos or chat history: data/tasks.json, data/media/* and
  # data/downloads/* are deliberately NOT included.
  local tar_files=()
  for f in .env data/accounts.json data/mail_accounts.txt data/generation-queue.json; do
    if [ -f "$REPO_ROOT/$f" ]; then
      tar_files+=("$f")
    fi
  done
  if [ "${#tar_files[@]}" -eq 0 ]; then
    die "nothing to back up — none of .env, data/accounts.json, data/mail_accounts.txt, data/generation-queue.json exists"
  fi
  if [ ! -f "$REPO_ROOT/.env" ]; then
    echo "warning: $REPO_ROOT/.env not found — bundle will not contain the API key" >&2
  fi
  tar -czf "$work/bundle.tar.gz" -C "$REPO_ROOT" "${tar_files[@]}"
  tar -tzf "$work/bundle.tar.gz" > "$work/files.txt"
  count="$(accounts_count)"

  info "encrypting bundle with age (passphrase from BACKUP_PASSPHRASE, never printed)..."
  _age_pty enc age -p -o "$work/$bundle" "$work/bundle.tar.gz"
  sha="$(sha256sum "$work/$bundle" | awk '{print $1}')"
  size="$(stat -c%s "$work/$bundle")"

  # Manifest: sha256, date, file list, accounts count (count only).
  BACKUP_SHA="$sha" BACKUP_SIZE="$size" BACKUP_TS="$ts" BACKUP_BUNDLE="$bundle" \
    BACKUP_COUNT="$count" python3 - "$work/files.txt" "$work/$bundle.manifest.json" <<'PYEOF'
import json, os, sys
files = [line.rstrip("\n") for line in open(sys.argv[1], encoding="utf-8")]
manifest = {
    "bundle": os.environ["BACKUP_BUNDLE"],
    "sha256": os.environ["BACKUP_SHA"],
    "size_bytes": int(os.environ["BACKUP_SIZE"]),
    "created_utc": os.environ["BACKUP_TS"],
    "accounts_count": int(os.environ["BACKUP_COUNT"]),
    "files": files,
}
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2, sort_keys=True)
PYEOF

  info "uploading $bundle to $STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/ ..."
  rclone copyto "$work/$bundle" "$STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/$bundle"
  rclone copyto "$work/$bundle.manifest.json" "$STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/$bundle.manifest.json"

  # Local copy in /tmp only — never in the repo.
  cp "$work/$bundle" "/tmp/$bundle"
  cp "$work/$bundle" "/tmp/muse2api-data-latest.tar.gz.age"
  chmod 600 "/tmp/$bundle" "/tmp/muse2api-data-latest.tar.gz.age"

  if [ "$was_running" -eq 1 ]; then
    info "restarting container..."
    (cd "$REPO_ROOT" && docker compose up -d >/dev/null)
    local ok=0
    for _ in $(seq 1 30); do
      if curl -fs -o /dev/null --max-time 5 "http://127.0.0.1:18610/" 2>/dev/null; then ok=1; break; fi
      sleep 2
    done
    if [ "$ok" -eq 1 ]; then
      info "admin page: HTTP 200"
    else
      echo "warning: admin page did not return 200 after restart — check 'docker logs muse2api'" >&2
    fi
  fi

  info "backup done."
  info "bundle: $bundle"
  info "sha256: ${sha:0:12}... (len ${#sha})"
  info "size: $size bytes"
  info "accounts: $count"
  info "remote: $STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/$bundle"
  info "local copy: /tmp/$bundle"
}

cmd_restore() {
  check_tools
  require_passphrase
  require_remote
  local bundle="${1:-}"
  if [ -z "$bundle" ]; then
    info "no bundle given — picking the latest backup on the remote..."
    bundle="$(rclone lsf "$STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/" --files-only 2>/dev/null | grep '\.tar\.gz\.age$' | sort | tail -n 1)"
    [ -n "$bundle" ] || die "no backup bundle found at $STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/"
  fi
  info "restoring bundle: $bundle"
  local work sha_manifest sha_actual count
  work="$(mktemp -d /tmp/muse2api-restore.XXXXXX)"
  # Expand $work now: a trap runs after locals go out of scope, and
  # under `set -u` the unexpanded name would fail at exit.
  trap "rm -rf '$work'" EXIT

  rclone copyto "$STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/$bundle" "$work/$bundle"
  if rclone copyto "$STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/$bundle.manifest.json" "$work/$bundle.manifest.json" 2>/dev/null; then
    sha_manifest="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$work/$bundle.manifest.json")"
    sha_actual="$(sha256sum "$work/$bundle" | awk '{print $1}')"
    [ "$sha_manifest" = "$sha_actual" ] || die "sha256 mismatch for $bundle (manifest ${sha_manifest:0:12}... vs actual ${sha_actual:0:12}...)"
    info "sha256 verified: ${sha_actual:0:12}..."
  else
    echo "warning: no manifest found for $bundle — skipping hash verification" >&2
  fi

  info "decrypting bundle with age (passphrase from BACKUP_PASSPHRASE, never printed)..."
  _age_pty dec age -d -o "$work/bundle.tar.gz" "$work/$bundle"
  tar -xzf "$work/bundle.tar.gz" -C "$REPO_ROOT"
  if [ -f "$REPO_ROOT/.env" ]; then
    chmod 600 "$REPO_ROOT/.env"
  fi

  count="$(accounts_count)"
  if grep -q '^MUSE2API_KEY=' "$REPO_ROOT/.env" 2>/dev/null; then
    info ".env: MUSE2API_KEY present (value not shown)"
  else
    echo "warning: .env does not contain MUSE2API_KEY after restore" >&2
  fi
  info "restore done."
  info "bundle: $bundle"
  info "accounts: $count"
  info "next step (not done by restore): cd $REPO_ROOT && docker compose up -d --build"
  info "then verify: curl http://127.0.0.1:18610/ (expect 200) and /v1/models with the old API key (expect 200)."
}

cmd_status() {
  check_tools
  info "repo: $REPO_ROOT"
  if [ -d "$REPO_ROOT/data" ]; then
    info "local data size: $(du -sh "$REPO_ROOT/data" | awk '{print $1}')"
  else
    info "local data: data/ not found"
  fi
  info "accounts: $(accounts_count)"
  if [ -f "$REPO_ROOT/.env" ]; then
    info ".env: present (API key value not shown)"
  else
    info ".env: missing"
  fi
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "muse2api"; then
    info "container: muse2api running ($(docker ps --format '{{.Status}}' -f name=muse2api))"
  else
    info "container: muse2api not running"
  fi
  if remote_configured; then
    info "remote backups at $STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/:"
    rclone lsf "$STORAGE_REMOTE:$STORAGE_BUCKET/$BACKUP_PREFIX/" --files-only 2>/dev/null | sort || echo "(none found)"
  else
    info "remote: '$STORAGE_REMOTE' not configured — run '$0 setup' first."
  fi
}

case "${1:-}" in
  setup)   cmd_setup ;;
  backup)  cmd_backup ;;
  restore) shift; cmd_restore "${1:-}" ;;
  status)  cmd_status ;;
  *)
    echo "usage: $0 {setup|backup|restore [bundle-name]|status}" >&2
    echo "config: STORAGE_PROVIDER (b2| r2, default b2), STORAGE_BUCKET (default muse2api-data)," >&2
    echo "        B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY (for b2) or" >&2
    echo "        R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (for r2)," >&2
    echo "        BACKUP_PASSPHRASE, STORAGE_REMOTE (default storage)" >&2
    echo "        (or put them in scripts/.env.storage — gitignored, never commit it)" >&2
    exit 2
    ;;
esac
