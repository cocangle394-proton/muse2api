# Shared data for muse2api

muse2api runs on disposable compute platforms (a GitHub Codespace today,
a VPS tomorrow). The **code** lives on GitHub (the fork). The **runtime
data** lives in one central place outside any compute platform:
a Backblaze B2 bucket (S3-compatible, 10 GB free, no card required).
Any platform can restore the same data and take over. A platform is
never the only copy of the data. (Cloudflare R2 is supported as an
alternative backend, but it requires a card on file even for its free
tier, so B2 is the default.)

The scripts in this repo are the whole mechanism:
`scripts/muse2api-data.sh` (`setup` / `backup` / `restore` / `status`).

## What is synced

Only config / system data — never images, videos or chat history.

| Item | Location | Notes |
| --- | --- | --- |
| Muse accounts + cookies | `data/accounts.json` | Sensitive. Encrypted in the backup bundle. |
| Generation queue | `data/generation-queue.json` | Pending generations. |
| Mail accounts | `data/mail_accounts.txt` | Contains plaintext passwords for receiving muse.ai login codes. Sensitive. |
| API key | `.env` (`MUSE2API_KEY`) | Sensitive. Encrypted in the backup bundle. The file is gitignored and chmod 600. |

A backup is one dated bundle,
`muse2api-data-<YYYYMMDD-HHMMSS>.tar.gz.age`, plus a manifest
`<bundle>.manifest.json` (sha256, size, date, file list, accounts count —
count only, never cookie values). Bundles live in the bucket under
`backups/`. A copy of the latest bundle is also kept in `/tmp` on the
machine that made the backup — never inside the repo.

## What is NOT synced, and why

- **Chat / image / video task history** (`data/tasks.json`).
  Deliberately not backed up: it is history, not config, and it
  grows with every generation. A new platform starts with an empty
  task history.
- **Generated media** (`data/media/*`) and **downloads**
  (`data/downloads/*`). Images, videos and downloaded files are not
  config data; they stay on the platform that created them and are
  not restored elsewhere.
- **Chromium profiles** (`~/muse2api-profiles`, default `profile_root`).
  They live under `$HOME`, outside the repo and outside the `data/`
  volume, and they are tied to one machine / one Chromium build. A new
  platform rebuilds its profile from the cookies in `accounts.json`.
  Do not copy profiles between platforms (and never between x86 and ARM).
- **Docker images, container logs, `__pycache__`.** Rebuilt on the new
  platform with `docker compose up -d --build`.
- **The repo code itself.** It is synced through git (the fork), not
  through backups.

## Create the B2 bucket and application key (one time)

Backblaze B2: 10 GB free forever, no credit card required to sign up.

1. Go to [backblaze.com](https://www.backblaze.com) and sign up with
   an email address (verify the email). In the web console, open
   **B2 Cloud Storage > Buckets** and create a bucket named
   `muse2api-data` (keep it **Private**).
2. On the bucket page, note the **S3 Endpoint**, e.g.
   `https://s3.us-west-004.backblazeb2.com` — the region part varies
   per bucket, so copy exactly what the page shows.
3. Go to **B2 Cloud Storage > Application Keys > Add a New
   Application Key**: name it `muse2api-backup`, allow access to
   the `muse2api-data` bucket only, with **Read and Write** access.
   Copy two values: **keyID** and **applicationKey**. The
   applicationKey is shown once — save it in a password manager.
4. Pick a backup passphrase (long, random). This encrypts every backup
   bundle with `age`. Keep it in a password manager too. **Losing it
   means the backups cannot be decrypted.** The passphrase is never
   written to a file by the scripts and never printed.

Alternative backend: Cloudflare R2 works the same way
(`STORAGE_PROVIDER=r2` with `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` /
`R2_SECRET_ACCESS_KEY`), but enabling R2 requires a card or PayPal on
file even for its free tier, so it is not the default.

## Configure (on each platform, once)

Set the environment variables, or put them in `scripts/.env.storage`
(gitignored — never commit it):

```sh
export STORAGE_PROVIDER="b2"
export STORAGE_BUCKET="muse2api-data"
export B2_ENDPOINT="https://s3.us-west-004.backblazeb2.com"
export B2_KEY_ID="<keyID>"
export B2_APPLICATION_KEY="<applicationKey>"
export BACKUP_PASSPHRASE="<your backup passphrase>"
```

Then generate the rclone config (stored at
`~/.config/rclone/rclone.conf`, chmod 600, outside the repo):

```sh
scripts/muse2api-data.sh setup
```

`setup` only prints masked values (first 4 characters + length).
It refuses to write the rclone config inside the repo.

## Commands

```sh
# What is here, and what is in the bucket?
scripts/muse2api-data.sh status

# Back up the current platform.
# Stops the container, tars only the config files listed above,
# encrypts with age, uploads to B2, restarts the container,
# verifies the admin page returns 200.
scripts/muse2api-data.sh backup

# Restore on a new platform (latest bundle, or name one explicitly).
# Downloads, verifies the sha256 from the manifest, decrypts, extracts
# the config files (.env chmod 600). Does NOT start the container.
# Task history, media and downloads are not part of a backup, so a
# restored platform starts with an empty task history and no media.
scripts/muse2api-data.sh restore
scripts/muse2api-data.sh restore muse2api-data-20261125-103000.tar.gz.age
```

After a restore, start the service and verify with the **old** API key
(the key is preserved by the backup — a new key is never generated):

```sh
docker compose up -d --build
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18610/   # expect 200
# /v1/models with Authorization: Bearer <old key> — expect 200
# /v1/models without a key — expect 401
```

## Migration workflow (when a platform runs out of quota)

The person managing the move (currently Muse) does this, in order:

1. **On the old platform:** run `backup`. Check the output: bundle
   name, sha256 prefix, accounts count. Run `status` to confirm the
   bundle is listed on the remote.
2. **On the new platform:** clone the fork, install Docker, rclone and
   age, copy the same `scripts/.env.storage` values (or set the same env
   vars), run `setup`, then run `restore`.
3. **Verify on the new platform:** accounts count matches the old
   platform, `.env` contains `MUSE2API_KEY`, `docker compose up -d
   --build` succeeds, admin page returns 200, `/v1/models` returns
   200 with the old key and 401 without a key.
4. **Only then** stop and delete the old platform. Never delete the old
   platform before step 3 passes — it is the only other copy until the
   restore is verified.

## Security notes

- Secrets (`B2_APPLICATION_KEY`, `R2_SECRET_ACCESS_KEY`,
  `BACKUP_PASSPHRASE`, `MUSE2API_KEY`)
  are never hardcoded, never committed, and never printed by the
  scripts. The only values shown are sha256 prefixes and lengths.
- The backup bundle is encrypted with `age -p`. `age` refuses to read
  a passphrase from a pipe, and the usual `script(1)` PTY workaround
  echoes the passphrase into the log, so the script feeds the
  passphrase through a small Python PTY helper that waits briefly and
  only sends it after `age` has disabled terminal echo. The helper
  discards `age`'s output on success; if `age` fails, the output is
  shown only after scrubbing the passphrase out of it.
- The B2 application key is scoped to one bucket with Read and Write
  only. Rotate it if it ever leaks. (Same for the R2 API token, if
  R2 is used instead.)
- `data/` is gitignored (`accounts.json`, `tasks.json`, media,
  downloads, mail accounts). Backups are the only way this data
  leaves a platform — never `git add` it.
- If a platform is deleted without a backup, its data is gone:
  a stopped Codespace is deleted after 30 days of inactivity, and
  a deleted Codespace cannot be recovered.

## Troubleshooting

- `BACKUP_PASSPHRASE is not set` — set it in the environment or in
  `scripts/.env.storage` before `backup` / `restore`.
- `rclone remote 'storage' is not configured` — run `setup` first.
- `sha256 mismatch` on restore — the bundle is damaged or was
  replaced. Do not extract it; restore the previous bundle instead.
- `age: incorrect passphrase` on restore — the passphrase does not
  match the one used for that backup. Check the password manager;
  there is no way to recover a bundle without it.
- For a local test without B2, point `STORAGE_REMOTE` at a local rclone
  remote (for example a `type = local` remote) and set `STORAGE_BUCKET`
  to a local path. The script treats any rclone remote the same way.
