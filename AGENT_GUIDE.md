# Agent Guide — muse2api handoff

> For a fresh AI agent (Muse or any agent) that has just cloned this repo
> and has been given the owner's secrets. Read this file first, then
> `DATA_SYNC.md`. Do not guess the setup — it is all written down here.

## 1. What this repo is

muse2api is an OpenAI-compatible API over Muse (chat / image / video),
running in Docker on a Codespace — and, since 2026-11-27, as the
primary host directly on a Muse VM via uvicorn (no Docker there;
see §13). The rest of this guide describes the Codespace/Docker
path unless it says "VM".

- Repo (the fork — this is the only repo you push to):
  `https://github.com/cocangle394-proton/muse2api`, branch `main`
- Service: container `muse2api`, port **18610**, CDP port 19210,
  volume `./data:/app/data`
- Endpoints: `/v1/chat/completions`, `/v1/images/generations`,
  `/v1/videos`, `/v1/models`
- Models: `muse-spark` / `muse-image` / `muse-video`, plus auto-mapped
  names like `gpt-4o`
- Video is async: `POST /v1/videos` returns a task id
  (`task_...`, `queued` / `processing` / `completed`); poll
  `GET /v1/videos/{task_id}`; a completed result has
  `url: /v1/media/<file>.mp4`
- Admin UI: open the service root `/` in a browser, log in with the
  API key. Tabs: Account Pool, Get Cookies, Tasks, Media Library,
  API Playground, Integration Docs.

## 2. Golden rules

- **Push and commit only to the fork** `cocangle394-proton/muse2api`.
  Never push to the upstream `czg86389-hub/muse2api` — its push URL is
  disabled on purpose. Check `git remote -v` before you push.
- **Never commit secrets or runtime data.** `data/`, `.env`,
  `scripts/.env.storage` and `~/.config/rclone/rclone.conf` are
  gitignored. Never `git add` them. Never print a secret value in
  chat, in a log, or in a file. Record only where a secret lives.
- **Backup scope is config/system data only** (the owner's rule):
  `.env`, `data/accounts.json`, `data/mail_accounts.txt`,
  `data/generation-queue.json`. Never back up `data/tasks.json`,
  `data/media/*`, or `data/downloads/*`.
- **Stay with free hosting** unless the owner explicitly approves a
  paid one.
- **Do not delete the old platform** until the new platform is
  verified (see §9).

## 3. What you need from the owner before you start

The owner provides these — you cannot invent them, and the repo does
not contain them.

### Required to run anywhere: 4 Backblaze B2 values

All four live in `scripts/.env.storage` (gitignored, chmod 600) or as
environment variables. All four are required; one alone is not enough.

| Value | What it is |
| --- | --- |
| `B2_ENDPOINT` | S3 endpoint of the bucket, e.g. `https://s3.us-west-004.backblazeb2.com` (region varies per bucket — copy exactly) |
| `B2_KEY_ID` | B2 application key id |
| `B2_APPLICATION_KEY` | B2 application key (shown once when created — if lost, create a new key) |
| `BACKUP_PASSPHRASE` | Passphrase that encrypts every backup bundle with `age`. **Without it a bundle cannot be decrypted, even with the B2 key.** |

Notes:

- The B2 application key must be scoped to the bucket `muse2api-data`,
  capabilities **Read and Write**, and the file-name prefix must be
  left **empty**. A key with a prefix, or a read-only key, looks like
  it works (list succeeds) but every upload fails with 403.
- The backup bundle is `muse2api-data-<YYYYMMDD-HHMMSS>.tar.gz.age`
  plus a `.manifest.json` (sha256, size, date, file list, accounts
  count only), under `backups/` in the bucket.
- If the owner hands you a full `scripts/.env.storage` file, put it in
  place with chmod 600 and do not print it. Do not put these values in
  chat.

### Needed only in some setups

- **`TAILSCALE_AUTHKEY`** — a reusable Tailscale auth key tagged
  `tag:codespace`. Needed only if you must join the owner's Tailscale
  tailnet: to SSH into a Codespace from outside it, or to reach the
  Codespace SOCKS proxy from a Muse VM (see §8). Sources, first one
  found wins: the `$TAILSCALE_AUTHKEY` environment variable (e.g. a
  GitHub Codespace secret) or the file `~/.config/tailscale-authkey`
  (chmod 600). A single-use key will not work twice — it must be
  reusable.
- **Proxy SOCKS address** — needed only if you run the service on a
  Muse VM (see §8). The owner gives you a SOCKS5 address by IP, e.g.
  `socks5://<IP>:1080` (with `user:pass@` in the URL if the proxy
  needs auth). It must be an IP, not a hostname.

If any of the 4 B2 values is missing, stop and ask the owner for it.
Do not start the service with an empty data set and call it a restore.

## 4. Quick start

This is the normal path. It works on a GitHub Codespace and on any
machine with Docker, rclone and age installed.

1. Clone the fork:
   `git clone https://github.com/cocangle394-proton/muse2api.git`
   (the fork is public — cloning needs no login).
2. Put the 4 B2 values in `scripts/.env.storage` (or export them as
   environment variables). On a Codespace created from this repo they
   may already arrive as Codespace secrets — check whether
   `scripts/.env.storage` or the environment already has them before
   asking the owner again.
3. Run the one-command startup:
   `bash start-muse2api.sh`
   (or `bash start-muse2api.sh --build` after code changes).
   It runs, in order: Tailscale via `start-tailscale.sh`, data sync
   (restores the latest B2 backup **only** when local `.env` or
   `data/accounts.json` is missing — it never overwrites local data),
   `docker compose up -d`, then waits for the admin page and checks
   `/v1/models` with the key.
4. Verify (see §6). If verification passes, you are done.

On a fresh Codespace, `rclone` and `age` are installed at creation by
`.devcontainer/devcontainer.json` (`postCreateCommand`), and
`start-muse2api.sh` is the `postStartCommand`, so step 3 may already
have run. Check the service before running it again.

## 5. Manual steps (if the one-command startup fails)

Run these only if `start-muse2api.sh` did not bring the service up.
Do them in order, and verify after each step.

1. Install tools if missing: `rclone` and `age`.
   (On Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y rclone age`.)
2. Configure the B2 remote: `scripts/muse2api-data.sh setup`
   (writes `~/.config/rclone/rclone.conf`, chmod 600).
3. Restore the latest backup: `scripts/muse2api-data.sh restore`
   (or name a bundle explicitly). It downloads, verifies the sha256
   from the manifest, decrypts with the passphrase, and extracts the
   config files (`.env` chmod 600). It does **not** start the
   container.
4. Start the service: `docker compose up -d --build`
5. Verify (see §6).

Full backup/restore details, including the migration workflow, are in
`DATA_SYNC.md`. That file is the authority on backup scope and on the
`setup` / `backup` / `restore` / `status` commands.

## 6. Verify — all of these must pass

- `docker ps` shows container `muse2api` Up.
- Admin page: `GET http://127.0.0.1:18610/` returns **200**.
- `GET /v1/models` with `Authorization: Bearer <key from .env>`
  returns **200** (3 models). Without a key it must return **401**.
  The key is preserved by the backup — a restore never generates a
  new key. After editing `.env`, always run `docker compose up -d`
  to recreate the container, verify the key, then run a fresh backup.
- `GET /admin/status` with the key returns 200 and shows the accounts
  and stats. The restored accounts count must match the old platform.
  (Last known state: 13 accounts in `data/accounts.json`, 4 enabled:
  redstone32, acc1, acc2, acc7 — merged from the Codespace on
  2026-11-27, see §13.)
- Real generation test: `POST /v1/chat/completions` with model
  `muse-spark` must return 200 with real content. If it fails, check
  §8 first — on a Muse VM the usual cause is a missing/bad proxy,
  not a bad key.

## 7. Public URL (Codespace)

On a Codespace, make port 18610 **Public** in the VS Code Ports panel.
The public admin URL is then:

`https://<codespace-name>-18610.app.github.dev/`

Anyone with the link can open the page, but the API key is still
required to use it. Do not share the key.

**VM service published through the Codespace (live since 2026-11-27):**
the VM runs a persistent reverse tunnel
`ssh -N -R 18611:localhost:18610 codespace@codespaces-2da4bc`, so
Codespace port **18611** serves the VM's muse2api, and that port is
Public: `https://<codespace-name>-18611.app.github.dev/`. Use the
18611 URL for the VM service and the 18610 URL for the Codespace's
own container — they are different services with the same API key
and (since the 2026-11-27 merge) the same accounts.

Note (2026-11-27): port 18610 was seen showing as Private in the
Ports panel even though it had been made Public on 2026-11-25 —
visibility may reset on a Codespace restart. Re-check visibility
after any restart; do not assume it stuck.

## 8. Proxy — when you need one and when you don't

- **Running on a Codespace or any VPS with direct internet: no proxy
  needed.** The container reaches muse.ai directly. Skip this section.
- **Running on a Muse VM: a proxy is required.** The VM's egress
  proxy blocks muse.ai (403), so direct calls fail even though the
  admin page loads and the key checks pass. The 4 B2 values are
  enough to *start* the service on a Muse VM, but not enough to
  *generate* content there.

How the proxy works in this project:

- muse2api supports a separate proxy per Muse account (Account Pool
  tab → Proxy button per account, or the admin API). It supports
  `http://`, `socks5://` and `socks5h://`, with username/password.
  Both the browser and the HTTP session checks go through the proxy.
  If the proxy dies, the account errors — it does not fall back to
  a direct connection.
- On a Muse VM, enter the proxy **by IP address, not by hostname**:
  the VM's DNS cannot resolve unfamiliar domains (muse.ai does not
  resolve), while direct TCP out to the internet works.
- The working setup used so far is the Codespace as a SOCKS5 proxy:
  `microsocks` on the Codespace listening on `127.0.0.1:1080`,
  exposed with `sudo tailscale serve --bg --tcp 1080 tcp://127.0.0.1:1080`
  (binding microsocks directly to the Tailscale IP fails — the
  Codespace runs Tailscale in userspace mode with no `tailscale0`
  interface), plus a bridge on the Muse VM forwarding
  `127.0.0.1:11080` to the Codespace proxy, so each account's proxy
  is `socks5://127.0.0.1:11080`. A real chat completion through that
  path has been verified. See the `proxy-station` repo
  (`https://github.com/cocangle394-proton/proxy-station`, `run.sh`)
  for the proxy installer. Note: `microsocks` on the Codespace does
  **not** auto-start after a restart — it must be started manually
  each time.
- Without Tailscale, the same thing works with any public SOCKS5
  proxy by IP: run `microsocks` (or equivalent) on a machine with a
  public IP, or use a proxy service, and set that address on each
  enabled account. Tailscale is a convenience here, not a
  requirement — but losing it means you must arrange another way to
  manage the machine remotely and another proxy.
- **trycloudflare does not work from a Muse VM** (verified
  2026-11-27): `cloudflared` downloads fine, but creating a quick
  tunnel fails because `api.trycloudflare.com` returns 403 via the
  VM egress proxy — the same block as muse.ai. A trycloudflare URL
  is HTTPS-only and cannot be used as a direct SOCKS5 endpoint
  anyway. Do not retry it from the VM.

## 9. Backup, restore, migration

- Back up from the old platform before a move:
  `scripts/muse2api-data.sh backup`, then `scripts/muse2api-data.sh status`
  to confirm the bundle is listed on B2. The script stops the
  container, encrypts, uploads, restarts it, and verifies the admin
  page returns 200. If a backup fails, the script restarts the
  container on exit — a failed upload must never leave the service down.
- Restore on the new platform as in §5. Then verify as in §6.
- Only after verification passes, stop and delete the old platform.
- Container-owned files (`data/accounts.json`, `data/tasks.json`)
  are root-owned mode 600 — the backup script stages them with sudo
  before tarring. Do not try to read them directly as a normal user.
- Data formats that have crashed startup before: `data/generation-queue.json`
  must be a JSON object (not a list); `data/tasks.json` is loaded as
  a dict; `data/accounts.json` as `[]` is fine.

What a restore does **not** bring back (by design — see §2):

- Chat / image / video task history (`data/tasks.json`) — a restored
  platform starts with an empty task history.
- Generated media (`data/media/*`) and downloads (`data/downloads/*`).
- Browser profiles (`~/muse2api-profiles/generate`) — they live
  outside `data/`, are tied to one machine / Chromium build, and are
  rebuilt from the cookies in `accounts.json` on a new platform.
  Do not copy profiles between platforms (and never between x86
  and ARM).

## 10. Importing a Muse account (only if `accounts.json` is empty)

The restored accounts normally need no re-import. If they are gone
or expired:

1. Admin → Get Cookies tab → download `muse2api-extension.zip`.
2. Chrome → `chrome://extensions` → Developer mode → Load unpacked.
3. Open https://muse.ai/ and log in.
4. Click the extension icon, fill in the BASE URL and the API key
   (shown at the top of the Account Pool tab), then "Read and import".
5. Cookies are httpOnly — `document.cookie` in DevTools cannot read
   them. The extension is required. Its popup also shows the manual
   paste format: `hatch_sess=...; hatch_vml=...`.

## 11. Known bugs and workarounds (as of 2026-11-27)

Do not re-discover these. If you hit one, use the workaround and,
if you fix it, fix it in this repo and push to the fork.

- **`start-tailscale.sh` cannot self-recover from logged-out.**
  While logged out it waits for `tailscale ip -4` to succeed and
  then exits ("tailscaled did not become ready") before reaching its
  own `tailscale up` — but `tailscale ip` can only succeed after
  login. Workaround: run `tailscale up` directly with the auth key
  read from the file / environment variable (with `--ssh` and
  `--advertise-exit-node`).
- **`start-muse2api.sh` flock early-exit.** If a previous start is
  still holding the lock (or a stale lock is held), a new run prints
  "another muse2api start is already running, exiting" and stops —
  including on Codespace restart, when Tailscale was never
  re-connected. If the service is down and the log shows that line,
  check for a stale lock / stuck start before assuming the service
  is fine.
- **Codespace secret update from inside a Codespace fails with 403.**
  `gh secret set` from inside a Codespace uses a `GITHUB_TOKEN`
  without secret-management permission. Update Codespace secrets
  (e.g. a rotated `TAILSCALE_AUTHKEY`) via the GitHub web UI or a
  PAT outside the Codespace. Otherwise the next restart picks up
  the old key from the environment variable first and breaks again.
- **Full Tailscale on a Muse VM does not work.** The real Tailscale
  client fails to join (the VM's egress proxy does TLS interception),
  and the runtime's own Tailscale connector is client-only
  (outgoing only, no exit-node selection). The VM reaches the
  tailnet only through its CONNECT proxy — enough for SSH to the
  Codespace and for the proxy bridge in §8, not for a full join.
- **Muse VM is localhost-only by itself.** There is no `tailscale0`
  interface on the VM, and the old `http://100.95.59.119:18610/`
  address is not usable (502). The VM service is published through
  the Codespace reverse tunnel on port 18611 (§7, §13) — give the
  owner the 18611 public URL, never the Tailscale IP.
- **rclone on the VM fails with the runtime CA env vars set.**
  `LoadCustomCABundleError ... unsupported transport` — unset
  `SSL_CERT_FILE`, `SSL_CERT_DIR`, `CURL_CA_BUNDLE`,
  `REQUESTS_CA_BUNDLE` and `AWS_CA_BUNDLE` for every rclone call
  (see §13). Do not work around it by disabling TLS verification.
- **`tailscale status` hangs when run from a systemd unit.** It
  works fine from an agent shell but never returns under systemd,
  so a unit that calls it never starts its real process. Resolve
  the Codespace IP in the start script (agent context) and pass it
  via a file (`~/workspace/bridge-target.txt`).
- **Free public SOCKS5 lists do not work from a Muse VM** (verified
  2026-11-27 with monosans/proxy-list: top 5 of 219 SOCKS5 proxies
  all failed). TCP to the proxy opens, but the SOCKS5 handshake
  gets an invalid-version response — the VM's egress path does not
  carry raw SOCKS5 to the public internet. Only the Tailscale
  bridge path (§8) works from the VM. Do not put free-list proxies
  on real accounts.
- **SSH sessions to the Codespace break after ~35–40s** ("Broken
  pipe"). Keep each SSH command under ~30s, or run longer work
  detached on the Codespace (output to a log) and poll the log.
  Upload files with `scp`, not by piping a script through stdin in
  the same command that backgrounds a job (that produced a 0-byte
  file once).

## 12. Troubleshooting

- `BACKUP_PASSPHRASE is not set` — set it in the environment or in
  `scripts/.env.storage` before `backup` / `restore`.
- `rclone remote 'storage' is not configured` — run `setup` first.
- `sha256 mismatch` on restore — do not extract that bundle;
  restore the previous bundle instead.
- `age: incorrect passphrase` on restore — the passphrase does not
  match the one used for that backup. There is no way to recover a
  bundle without it.
- `/v1/models` returns 401 with the key — the key in `.env` does not
  match the container's `MUSE2API_KEY`. After editing `.env`, run
  `docker compose up -d` to recreate the container, then verify
  again. Verify by hash: the host `.env` hash must equal the
  container env hash.
- Container exits 255 after a Codespace restart — its read-write
  layer is corrupt. `start-muse2api.sh` force-recreates it; by hand,
  `docker compose up -d --force-recreate`, and as a last resort a
  full `docker compose build --no-cache`.
- Chat / image / video fails but admin and `/v1/models` pass — on a
  Muse VM this is almost always the proxy (§8). Check the proxy is
  up, the account has it set, and the proxy test passes.
- Codespace limits (free tier): 120 core-hours/month and 15 GB/month
  (60 hours/month for this 2-core Codespace), auto-stop after
  30 minutes idle, 12-hour max session, stopped Codespace deleted
  after 30 days without reopening. Opening more Codespaces does not
  increase quota — it is per account, aggregate.

## 13. Running on a Muse VM (primary host, since 2026-11-27)

The Muse VM is the primary host. The Codespace still runs its own
container and hosts the muse.ai proxy, but the service the owner
uses is the VM's, published through the Codespace (see §7).

How the VM differs from the Codespace path:

- **No Docker on the VM.** The service runs directly from the repo
  checkout at `~/workspace/muse2api` with its own venv:
  `MUSE2API_HOME=<repo>`, `MUSE2API_CHROMIUM=~/workspace/chrome-linux64/chrome`,
  `~/workspace/muse2api/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 18610`.
  The stock `start-muse2api.sh` (Docker) does **not** work on the VM.
  Use `~/workspace/start-muse2api-vm.sh` instead — it is idempotent
  (flock), starts only what is missing, and verifies admin 200 and
  `/v1/models` 200 with the key.
- **Three components must be up:** (1) uvicorn on localhost:18610,
  (2) the bridge `~/workspace/tailscale_bridge.py` on 127.0.0.1:11080
  (see §8), (3) the reverse tunnel `ssh -N -R 18611:localhost:18610`
  to the Codespace (see §7). The start script reconciles all three.
- **Auto-restart, two layers.** Systemd units (canonical copies in
  `~/workspace/systemd/`, installed in `/etc/systemd/system/` —
  `/etc` is ephemeral across VM replacement, so the canonical copies
  are the source of truth): `muse2api-vm`, `muse2api-bridge`,
  `muse2api-tunnel`, all `Restart=always`, `WantedBy=default.target`
  (the guest boots `default.target`, not `multi-user.target`).
  `systemctl --user` does not work on the VM (no bus) — use the
  system manager. Plus two runtime crons: `muse2api-vm-health-check`
  (every 15m, runs the start script when something is down, silent
  when healthy) and `muse2api-vm-backup-watch` (every 5m, see below).
  No secrets in any unit file: the bridge CONNECTs to the tunnel
  proxy without auth (verified), and the tunnel uses Tailscale SSH
  with no key or password.
- **Bridge target is a file, not a command.** The Codespace
  Tailscale IP changes on restart. The start script resolves it via
  `tailscale status` and writes `~/workspace/bridge-target.txt`;
  the bridge unit reads that file. Do not call `tailscale status`
  from a unit — it hangs under systemd (see §11).
- **The VM still depends on the Codespace proxy for muse.ai** (§8).
  If the Codespace is down or microsocks is not running there, the
  VM can serve admin pages but cannot generate. Microsocks on the
  Codespace does not auto-start — start it manually after a restart.
- **Public access is only via the Codespace tunnel (§7).** The VM
  itself has no public URL: trycloudflare is blocked, and the
  Tailscale IP is not reachable (502). The VM admin is localhost-only.

### Automatic B2 backup on the VM

- Watcher: `~/workspace/muse2api-vm-backup-watch.sh`, run every
  5 minutes by the `muse2api-vm-backup-watch` cron. State:
  `~/workspace/muse2api-vm-backup.state` (epoch + accounts.json
  sha256 of the last successful backup). Log:
  `~/workspace/muse2api-vm-backup.log` (every decision is logged).
- Triggers: (1) periodic — 20 minutes since the last successful
  backup, and only if the server is up (admin 200 and `/v1/models`
  200 with the key); if the server is down, skip. (2) on change —
  `data/accounts.json` hash differs from the last successful backup.
  `--force` backs up regardless (test path only).
- The backup itself (`~/workspace/muse2api-vm-backup.sh`) reuses the
  stock script with a docker shim in `~/workspace/bin/docker` (it
  can only report "no containers" and no-op — it cannot start a
  real container). It does **not** stop uvicorn; the stock script
  stages the 4 allowlisted files to a temp dir and encrypts/uploads
  from the staging copy. A failed backup leaves the server running
  and is logged; the state is only updated after the bundle is
  verified present in B2.
- Manifest: the stock script now writes `"platform": "vm"` when
  `MUSE2API_PLATFORM=vm` is set (default `"codespace"`); bundle
  naming and age encryption are unchanged. `created_utc` is the
  timestamp. On the VM, only `.env` and `data/accounts.json` exist
  to back up — `data/mail_accounts.txt` and
  `data/generation-queue.json` are not present on the VM, so they
  are simply absent from VM bundles.
- **rclone on the VM needs its CA env vars unset.** With
  `SSL_CERT_FILE` / `SSL_CERT_DIR` / `CURL_CA_BUNDLE` /
  `REQUESTS_CA_BUNDLE` / `AWS_CA_BUNDLE` set (they point at the
  runtime egress CA bundle), rclone 1.60 fails with
  `LoadCustomCABundleError ... unsupported transport`. Unsetting
  all five makes rclone use the system CA pool, which already
  contains the Hatch egress CA. The backup wrapper and watcher do
  this for every rclone call — do the same for any manual rclone
  command on the VM.
- **rclone/age must come from `~/workspace/bin`, not apt.**
  Apt-installed tools die on VM replacement (`/usr` does not
  survive it). `~/workspace/bin/rclone` (1.60.1) and
  `~/workspace/bin/age` (1.1.1, plus `age-keygen`) are persistent
  binaries extracted from the cached .debs with `dpkg-deb -x`
  (which needs no apt/dpkg lock). The watcher and the backup
  wrapper prepend `~/workspace/bin` to PATH. If they are missing
  after a replacement, extract them again from
  `/var/cache/apt/archives/rclone_*.deb` and `age_*.deb` (the
  archive cache survives reboots) — or download static builds.
  This broke auto-backup on 2026-11-27 (the watch failed twice
  with `missing tool: rclone/age`) until the persistent binaries
  were installed; `muse2api-data-20261127-044028.tar.gz.age` was
  the first bundle after the fix.
- B2 credentials on the VM: `~/workspace/muse2api/scripts/.env.storage`
  (copied from the Codespace via scp, chmod 600) and
  `~/.config/rclone/rclone.conf` (written by `setup`, chmod 600).

### Merging accounts between Codespace and VM

Never do a blind restore of one platform's `accounts.json` onto the
other — the two copies diverge (different `enabled` flags and
per-account proxy settings). Merge instead (script:
`~/workspace/merge-accounts.py`):

- Base = the Codespace list (superset of ids), order preserved.
- Overlapping ids: keep the **VM's** `enabled` flag and its `proxy`
  / `proxy_status` fields (present only on VM-enabled accounts).
- New ids: keep the Codespace dict as-is; do not enable anything
  by hand.
- Stage the Codespace file via sudo to a temp file first (it is
  root-owned 600 there), scp it over, chmod 600, remove the temp
  file, and back up the VM's current file first. Never print cookie
  values. Stop uvicorn while writing the merged file, then restart
  via the start script and verify `/admin/status` (2026-11-27
  merge: 8 VM + 5 new = 13 total, 4 enabled: redstone32, acc1,
  acc2, acc7).
- Open gap (2026-11-27): acc7 is enabled but has **no proxy** set
  (it came from the Codespace). On the VM it cannot generate until
  `socks5://127.0.0.1:11080` is set on it, like the other three
  enabled accounts. Do not enable acc3–acc6 without being asked.

## 14. First-run setup wizard (added 2026-11-27)

Problem it solves: on a brand-new machine after cloning, there is no
API key to log in with, so a dashboard where the owner enters the 4
B2 values (§3) cannot sit behind API-key login. The wizard needs no
key — but only while the machine is fresh, and it locks itself after
setup.

### Fresh-state rule

The server is "fresh" when it has **no accounts in
`data/accounts.json` AND no `scripts/.env.storage`**. Note the
deliberate deviation from the naive rule ("no `.env` with a key"):
the startup handler auto-generates an API key into `.env` on first
boot, so requiring "no `.env`" would make the wizard unreachable
after any normal first start — exactly the state it exists for. The
auto-generated key counts as "not restored data" because nobody has
ever seen it. Never allow setup when data already exists: anyone
with the public link must not be able to overwrite a live server's
data.

### Endpoints (no auth, fresh-only — enforced server-side, not just in the UI)

- `GET /admin/setup-status` → `{"setup_needed": true/false}`.
- `POST /admin/setup` with JSON `{B2_ENDPOINT, B2_KEY_ID,
  B2_APPLICATION_KEY, BACKUP_PASSPHRASE}` → validates all four
  non-empty and `B2_ENDPOINT` starts with `https://` (400
  otherwise); writes them to `scripts/.env.storage` (chmod 600,
  gitignored, never logged or returned); runs
  `scripts/muse2api-data.sh setup` + `restore` (latest bundle)
  reusing the stock script — the restore logic is not rewritten.
  On success returns `{"ok": true, "accounts": N, "bundle": name}`
  (count only, no cookie values). On failure returns the error with
  secrets scrubbed and leaves the server fresh so setup can be
  retried. A failed attempt cleans up after itself: it removes the
  just-written `.env.storage`, any `accounts.json` the attempt
  created, and a just-added rclone remote — otherwise stale wrong
  credentials would poison every retry (`setup` is a no-op when the
  remote already exists). After success, setup-status is false and
  `POST /admin/setup` returns 403.
- Code: `app.py` (wizard section after `auth()`:
  `_setup_needed`, `_run_setup_restore`, `_reload_after_restore`,
  the two routes), `store.py` (`Store.reload()`).

### After a restore: hot reload, not restart

The restored `.env` (API key) and `accounts.json` are picked up in
the running process — a restart would drop the setup response in
flight and any in-progress generations. One subtlety that bit the
first test: `_reload_after_restore` reads the key from the **`.env`
file directly**, NOT via `_read_env_key`. That helper prefers
`os.environ`, and `os.environ` still holds the pre-restore key:
`config._load_dotenv` copies `.env` into the process env at import,
and on a fresh boot that is the auto-generated key nobody has ever
seen. Reading via the env would leave the server on a key nobody
knows and the restored key could never log in (this was reproduced
by the regression test in `tests/test_first_run_setup.py` and fixed
2026-11-27). `store.reload()` re-reads accounts from disk.

### Admin UI

`admin.html` calls `/admin/setup-status` first on load. If setup is
needed it shows a Setup card (4 password-type fields, short English
explanation, Restore button) and hides the key box and the 6 tabs;
on success it shows the accounts count and bundle and tells the
user to log in with the restored API key. When setup is not needed
nothing changes: normal API-key login and all 6 tabs work as
before. The setup form is fetched with plain `fetch`, not the
authed `api()` helper.

### `start-fresh.sh` — publish a fresh machine for first setup

`bash start-fresh.sh` (or `PORT=18699 bash start-fresh.sh`):
(a) starts the server the platform's normal way — Docker Compose if
`docker compose version` works, else `.venv/bin/uvicorn`, else
system `python3 -m uvicorn`; (b) starts a cloudflared quick tunnel
to localhost:18610 and prints the public
`https://*.trycloudflare.com` URL; (c) prints the setup status
from `/admin/setup-status`. The trycloudflare URL changes on every
restart — first setup only, not stable public access (for stable
access use a Codespace public port, §7, or a named Cloudflare
tunnel). **On the Muse VM trycloudflare is BLOCKED**
(`api.trycloudflare.com` returns 403 via the VM egress proxy, and
cloudflared itself fails with a TLS handshake error) — the script
detects the 403 and says so plainly instead of hanging; the server
keeps running locally. Implementation note: the backgrounded server
and tunnel are `disown`ed — without that, bash waits for the
backgrounded server when the script exits and the script hangs
instead of returning (found and fixed 2026-11-27).

### Tests

- Unit: `tests/test_first_run_setup.py` (stdlib-only ASGI client,
  no network) — 15 checks: fresh → setup needed; missing field /
  non-https endpoint → 400 and stays fresh; accounts or
  `.env.storage` present → setup locked (403); `store.reload()`
  re-reads accounts; hot-reload regression (restored key wins over
  the pre-restore auto-generated key in `os.environ`). Run with the
  repo venv python.
- Isolated integration test (2026-11-27, temp dir, live data
  untouched): setup-status true on a fresh server; wrong B2 values
  → 500 and the server stays fresh; real values restore bundle
  `muse2api-data-20261127-044028.tar.gz.age` with 13 accounts;
  afterwards setup-status false and a second setup → 403.
- `start-fresh.sh` tested on the VM 2026-11-27: prints the plain
  BLOCKED message and exits in ~4s, server stays up on the chosen
  port.
