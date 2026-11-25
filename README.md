# MUSE2API

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker" alt="Docker" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-green" alt="OpenAI API Compatible" />
  <img src="https://img.shields.io/badge/License-MIT-orange" alt="License" />
</p>

Reverse-engineers the cutting-edge multimodal capabilities of the **[muse.ai](https://muse.ai/)** web app into a standard **OpenAI-compatible RESTful API**. Via headless-browser CDP penetration, warm standby WebSocket tunnel reuse, and dynamic session management, it natively supports text chat (2-3 second streaming first-token response), text-to-image, image-to-image editing, text-to-video, and first-frame image-to-video, plus a multi-account pool with affinity rotation, fully automatic 48-hour session renewal and cloud VM wake-up keep-alive, and a companion Chrome one-click account-import extension.

---

## 🎬 Recommended: COOL AI — One-Stop AI Video & Image Creation for E-Commerce

Upload a product photo and create spokesperson sales videos, story-driven sales skits, and a complete set of e-commerce detail pages.

- **Spokesperson sales videos**: Automatically writes scripts, storyboards, and lines, and generates a real-person spokesperson sales video in one click
- **Story-driven sales videos**: Plot-twist skits, dialogue skits, and story product placement, with automatic scripting, storyboarding, and production
- **E-commerce detail images**: From a single white-background photo, generate a complete set in one click — hero image, scene image, selling-point image, detail image, and comparison image
- **AI images & tools**: Product retouching, GIF animations, digital humans, subtitle removal, and image quality enhancement

👉 Try it now: [www.coolai.mov](https://www.coolai.mov/?utm_source=github&utm_medium=readme&utm_campaign=muse2api)

### 🎬 Recommended: COOL AI — AI video & image studio for e-commerce

Upload one product photo and get talking-head sales videos, story-style ad shorts and a complete product detail-page image set.

- **Talking-head sales videos**: script, shots and lines written for you, generated in one click
- **Story ads**: twist, dialogue and product-placement shorts, scripted and storyboarded automatically
- **Product detail images**: hero, lifestyle, selling-point, detail and comparison images from one white-background photo
- **More tools**: product retouching, GIF animations, digital humans, subtitle removal, upscaling

👉 Try it: [www.coolai.mov](https://www.coolai.mov/?utm_source=github&utm_medium=readme&utm_campaign=muse2api)


---


## v1.6.12 Upgrade Notes

- **Generated images restored to original size (#62)**: Since 2026-11-19, muse.ai chat only shows ~498px thumbnails; the original image only loads when you open the image viewer. Image tasks now automatically open the viewer to fetch the original (verified 1344x1792); if it cannot be fetched, the thumbnail is still returned and a note is written to the service log. Just update the service; the extension does not need an upgrade.

Generated images are full size again (#62): image tasks open muse.ai's image viewer to fetch the original instead of the ~498px chat thumbnail. Update the service only; the extension is unchanged.


## v1.6.11 Upgrade Notes

- **The extension must be manually upgraded to 1.0.3**: Update the service first, download this instance's extension package from the admin page, replace the original extension directory, then reload it on the browser extensions page; upgrade each browser profile separately. For Incognito/InPrivate use, allow the extension to run in incognito windows. Importing only means the login state was saved — please continue testing the account.
- **Update checks do not automatically change code or restart**: The panel shows the real installed version and remote status, and an admin manually confirms the upgrade; upgrades are refused while tasks are queued or running. Image-based deployments can set MUSE2API_MANAGED_DEPLOYMENT=1 and update by rebuilding the image.
- **Queueing and generation are timed separately**: The admin page can set an independent queue timeout, default 3600 seconds, 0 means unlimited; long image tasks are still recommended to use the async task API.
- **The video library uses thumbnails**: Source deployments need FFmpeg installed; Docker users should rebuild the image. Updating only the Python source will not install system dependencies in an existing container.

Extension 1.0.3 requires a manual reload in every browser profile. Update checks are read-only; confirm upgrades explicitly. Rebuild Docker images for FFmpeg thumbnail support.


## 🌟 Core Features

- 💬 **Standard Chat API (Chat Completions & Responses API)**
  - Fully compatible with the `/v1/chat/completions` standard protocol and `/v1/responses`, which the new Codex uses by default.
  - Natively supports SSE streaming typewriter output (`stream=True`) and synchronous full responses, with warm-tab and Noise WebSocket tunnel affinity reuse, **with only a 2-3 second first-token delay in continuous conversation**.
  - Supports multi-turn conversation context and System Prompt settings.
  - Built-in smart model alias mapping: common model names such as `gpt-4o`, `gpt-5`, `claude-sonnet-4`, and `deepseek-chat` are automatically routed.
- 🎨 **High-Quality Image Generation & Editing (Images Generations & Edits)**
  - Fully compatible with the `/v1/images/generations` (text-to-image) and `/v1/images/edits` (image-to-image / reference-image editing) APIs.
  - Supports multiple aspect ratios such as `1:1`, `16:9`, `9:16`, `4:3`, and `3:4`, with single- or multi-reference direct upload and watermark-free clean output.
  - Supports both `url` direct links and `b64_json` return formats, with built-in second-level fast detection of plain-text refusals to prevent queue deadlocks.
  - Image-to-image returns only media newly generated in this run: upload previews and history attachments are excluded, and downloads are bound to the selected result; if a reference-image upload or prompt send is not confirmed, it errors explicitly instead of silently returning the original image.
- 🎬 **Text-to-Video / Image-to-Video (Videos)**
  - Natively connects to Muse's top-tier video generation model, accepting duration requests of 5 / 6 / 8 / 10 seconds (the actual finished video length follows Muse's upstream output) and `9:16` portrait / `16:9` landscape video generation.
  - Supports uploading a single first-frame reference image, or uploading multiple content and style reference images via the `images` array; in multi-image mode, the first image is used as the main starting frame.
  - Async task architecture (`/v1/videos` to create a task + `/v1/videos/{task_id}` status polling).
  - Built-in media service `/v1/media/{filename}`, automatically persisting generated MP4 / WebP assets.
- ⚡ **True Multi-Account Parallel Execution**
  - Based on Chromium's native `Target.createBrowserContext` incognito isolation containers, multiple tasks run concurrently in separate sandboxes.
  - Multiple accounts generating images, videos, and streaming chats at the same time never block each other; Cookie and Session are 100% isolated with no cross-talk.
  - Built-in smart account-conflict scheduler: the same account is automatically mutually exclusive to prevent stampedes, while multiple accounts automatically run in parallel at full capacity.
  - Use `MUSE2API_CONCURRENCY` to flexibly raise the concurrency limit to match machine performance (default 2 concurrent).
- 🔄 **Multi-Account Pool & Warm-Connection Affinity Scheduling**
  - Supports importing an unlimited matrix of Muse accounts.
  - Smart distribution based on warm-connection affinity (Warm-Tab Affinity) and LRU strategy, balancing 2-second ultra-fast responses with balanced consumption across accounts.
  - When a single account's quota is exhausted or its session fails, it is automatically marked and fails over to a healthy standby account in 0 seconds, seamlessly.
- 🛡️ **Session State Detection & Cloud VM Keep-Alive**
  - The background requests `/api/session` to check the session, and calls `/api/hatch/vm/wake` as needed to wake the workspace; after configuring an account proxy, these requests use the same egress as the browser.
  - Only Cookies and expiry times actually returned by upstream are saved; a successful check will not inflate the validity period. If upstream does not renew, the session is revoked, or the account is restricted, you still need to confirm on the official site and re-import.
- 🌐 **Per-Account Independent HTTP / SOCKS5 Proxy**
  - Each account can be configured with a different HTTP or SOCKS5 proxy and authentication info; browser contexts and session requests for concurrent tasks are isolated per account; proxy failures do not silently fall back to a direct connection.
- 🧩 **Companion Chrome One-Click Account-Import Extension**
  - No manual F12 packet capture needed — click the extension icon once to instantly extract the current browser login state (including the `HttpOnly` core Cookie and real expiry time) and securely push it to the account pool.
- 🖥️ **Modern Dark Ops Panel (Web Console) & Real-Time Online Hot Upgrade**
  - Built-in out-of-the-box Web UI supporting real-time service health, account-pool quota and status, one-click keep-alive for the whole pool, task progress replay, media library management, and online API debugging.
  - The video list in the media library only lazily loads JPEG thumbnails; a video is only loaded when you click its cover or "Open", and closing the preview releases the player. Thumbnails are generated on demand by FFmpeg and cached in `data/thumbnails/`; they are cleaned up together when media is deleted; old videos do not need to be regenerated. The Docker image already includes FFmpeg; manual deployments need to install `ffmpeg`; if the dependency is missing or a video is corrupted, a playback placeholder is shown and it will not fall back to preloading the original video.
  - **Real-time update broadcasts across all nodes & one-click upgrade**: When the official GitHub repository releases a new version or fix, an update notification banner automatically pops up at the top of the admin backend on all deployed nodes; click **"⚡ One-Click Online Upgrade & Restart"** to automatically pull the latest code and restart smoothly (local `.env` configuration and account data are automatically preserved).

---

## 🚀 Quick Deployment

### Option 1: Docker Compose (Recommended, one command out of the box)

1. **Clone the code and enter the directory**:
   ```bash
   git clone https://github.com/czg86389-hub/muse2api.git
   cd muse2api
   ```

2. **Configure environment variables (optional)**:
   ```bash
   cp .env.example .env
   # Edit .env as needed; it is recommended to change MUSE2API_KEY to your own admin key
   ```

   Docker Compose reads `MUSE2API_KEY` from `.env` and will no longer be overridden by the example value in the Compose file.

3. **Start the containers**:
   ```bash
   docker compose up -d
   ```

4. **Open the admin panel**:
   Open your browser and visit: `http://<your-server-IP>:18610/admin?key=<your-MUSE2API_KEY>`

---

### Option 2: Bare Metal / VPS Deployment (Ubuntu / Debian)

1. **Install system dependencies & Chromium**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y chromium ffmpeg fonts-wqy-zenhei python3 python3-pip python3-venv
   ```

2. **Configure the Python virtual environment**:
   ```bash
   cd /opt/muse2api
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure the systemd service**:
   ```bash
   sudo cp deploy/muse2api.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now muse2api
   ```

4. **Nginx reverse proxy configuration (Important: prevent 502 timeouts and streaming stalls)**:
   If you use Nginx / BaoTa / 1Panel as a reverse proxy, be sure to increase `proxy_read_timeout` to `600s` and disable `proxy_buffering` (see `deploy/nginx.example.conf` for details):
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:18610;
       proxy_read_timeout 600s;      # Prevent Nginx from returning 502 Bad Gateway when image/video generation takes a long time
       proxy_send_timeout 600s;
       proxy_buffering off;          # Ensure SSE chat streaming types out with 0 delay
       client_max_body_size 200M;    # Support uploading large reference media such as video/audio
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   }
   ```

---

## 🧩 Account Import (Chrome Extension / Tampermonkey Script)

> **v1.6.6 users must manually upgrade the extension: Chrome/Edge extension minimum 1.0.2, Tampermonkey script minimum 1.1.2.**
> Server updates do not automatically replace an already-loaded extension. Download the ZIP from this Release, overwrite the files in the original extension directory, and click "Reload" at chrome://extensions / edge://extensions; upgrade each profile separately. **v1.0.2** should be shown next to the popup title.
> [Download extension 1.0.2](https://github.com/czg86389-hub/muse2api/releases/download/v1.6.6/muse2api-chrome-extension-v1.6.6.zip). Only hatch_sess is required for import; missing hatch_vml and hatch_gw cannot be used to conclude you are not logged in. Re-import and then click "Test" on the account.
>
> **Manual extension upgrade required:** extension 1.0.2+ / userscript 1.1.2+. A server update does not upgrade unpacked extensions. Replace files in the original folder and click Reload, separately for every browser profile.


### Option A: Chrome / Edge Extension
The project includes a dedicated Chrome account-import extension (located in the `extension/` directory), ending complex F12 Cookie extraction:

1. Open Chrome / Edge, visit `chrome://extensions`, and turn on **"Developer mode"** in the top right.
2. Click **"Load unpacked"**, and select the `extension` directory in this project.
3. In that browser, log in to [muse.ai](https://muse.ai/) to reach the main chat interface.
4. Click the extension icon in the top right of the browser, and enter your service address (e.g. `http://1.2.3.4:18610`) and `MUSE2API_KEY`.
5. Click **"Read & Import"** for second-level sync into the pool!

### Option B: Tampermonkey Script (cross-browser, no extension install needed)
For users who do not want to enable Developer mode or who use Firefox / Safari, we recommend installing the Tampermonkey script in one click:

👉 **[⚡ Click to install the Tampermonkey script (Raw UserJS)](https://raw.githubusercontent.com/czg86389-hub/muse2api/main/tools/muse2api_cookie_importer.user.js)**

1. Install the [Tampermonkey](https://www.tampermonkey.net/) extension in your browser.
2. Click the **"⚡ Click to install the Tampermonkey script"** link above; Tampermonkey will automatically pop up the install window — click "Install" (no need to manually copy and paste code).
3. Open the [muse.ai](https://muse.ai/) website; a **"⚡"** floating icon will appear in the bottom right — click it to expand the configuration panel and push to the pool in one click (clipboard auto-copy fallback is also supported).

---

## 📖 API Usage Examples

### Per-Account Proxy

Click **Proxy** next to an account in the account pool:

1. Select **Use a proxy**, and paste the full link directly, e.g. `socks5://your-user:your-password@proxy.example.com:1080`. The username and password are parsed automatically; you can also expand the optional manual authentication fields and fill them in separately.
2. Click **Test connection** immediately, without saving or reopening the window first. The result shows proxy connectivity, egress IP, and Muse session status separately; testing a different unsaved configuration does not change the account's saved proxy, health status, or proxy detection record; if the tested content matches the saved proxy, that proxy's detection record is updated. Click **Save** after confirming, and it takes effect on the next request.
3. Continue using the account's **Test** button to confirm the browser can load the chat input box. A successful session verification does not guarantee that upstream will allow a given image or video generation.

Supports HTTP proxy endpoints (optional Basic auth) and SOCKS5 proxy endpoints (optional username/password auth), e.g. `http://proxy.example.com:8080`, `socks5://proxy.example.com:1080`, or `socks5h://proxy.example.com:1080`. SOCKS5 target hostnames are resolved via the proxy; HTTPS sites work as usual. **HTTPS proxy endpoints, VLESS, VMess, and subscription links are not yet supported for direct entry**; when using tools such as Clash/Mihomo, enter the HTTP or SOCKS5 proxy entry point accessible to the deployment server. `127.0.0.1:7890` refers to the deployment server itself, not the computer that imported the Cookie. Old accounts without a configured proxy keep their original network behavior.

Chat, image generation, video, browser downloads, account detection, quota queries, session renewal, and VM wake-up use the account proxy. Proxy disconnection, authentication failure, or browser context creation failure will report an error and **will not silently fall back to a direct connection**. Switching accounts also switches the proxy. External reference images are prefetched before account selection under the existing security policy, continuing to enforce public-address, redirect, and DNS rebinding protections; there is no promise that every reference-image URL goes through the account proxy.

Modifying the proxy while an account is busy returns HTTP 409; please wait for the current task to finish. After modification, the next request rebuilds the connection and does not reuse the old node. Proxy connectivity and account session are different states: HTTP 407 and connection timeouts do not directly mark a Cookie as invalid; proxy tests also do not overwrite an account's existing authentication result.

Authentication info in a full link takes precedence over manual authentication fields; special characters such as `@`, `#`, and `/` in a username or password should be URL percent-encoded. After saving, the address is normalized to a form without authentication info, and reopening it will not echo the password.

Proxy passwords are stored locally together with Cookies in `data/accounts.json`; protect this directory and its backups. APIs, logs, and pages do not echo passwords. When editing, if the address and username are unchanged, leaving the password blank means keep it; if the address or username changes, re-enter the authentication password. Select **Default network** or **Remove proxy** to clear the configuration and restore the default network.

Admin API examples (authentication required):

```http
PATCH /admin/accounts/{account_id}
Content-Type: application/json
Authorization: Bearer <MUSE2API_KEY>

{
  "proxy": {
    "server": "http://proxy.example.com:8080",
    "username": "your-proxy-user",
    "password": "your-proxy-password"
  }
}
```

Omitting `proxy` leaves the setting unchanged; submitting `{"proxy": null}` removes the proxy. `POST /admin/accounts` also accepts the same `proxy` object, including setting the same proxy for this bulk import. The response only includes the server, username, and `has_password`, and does not return the password.

```http
POST /admin/accounts/{account_id}/proxy/test
Authorization: Bearer <MUSE2API_KEY>
```

Testing without a request body tests the saved proxy. To test a proxy that has not been saved yet, send `Content-Type: application/json` and `{"proxy":{"server":"socks5://your-user:your-password@proxy.example.com:1080"}}`; `server` also supports full HTTP/SOCKS5h links, and the legacy split-field authentication remains compatible. A response of `tested_saved_proxy=false` means the tested content differs from the currently saved proxy and the account's saved content is unchanged; when `true`, the current proxy's detection record is updated.

After upgrading from 1.6.8, accounts that previously had no saved detection result need to click "Test connection" once again. An empty record shows "Proxy not tested"; only after an actual test is completed does it show "Proxy connected" or "Proxy failed", and an unknown status is not automatically marked as successful.

When testing before saving, the server may reuse a measured record from the current process within 5 minutes; it must be the same account, same node and authentication, and same login Cookie. Changing the configuration, changing the Cookie, expiry, or a service restart means the old success is not reused. Only actual results are reused; a failed test is not turned into a success; this does not mean the account's browser or generation capability has been verified.

This test accesses a cookie-free HTTPS egress check via the selected proxy, then verifies the Muse session; the returned `proxy_reachable`, `exit_ip`, `session_ok`, and `workspace_status` represent the results at each layer. If there is neither a temporary configuration nor a saved proxy, it returns 400; if the account is busy, it returns 409. This operation does not generate content and does not consume image/video generation quota.

**English:** Each account can use its own HTTP proxy with optional Basic authentication or a SOCKS5 proxy with optional username/password authentication. Select **Use a proxy**, paste a full URL such as `socks5://your-user:your-password@proxy.example.com:1080`, and click **Test connection** immediately. Saving and reopening are not required. Tests of a different unsaved route leave the saved route, account health and saved test status unchanged. Testing the exact saved configuration updates its connectivity record; click **Save** separately to apply the route. Manual credential fields remain optional. Credentials in the URL take precedence; percent-encode special characters. Both `socks5://` and `socks5h://` proxy origins are accepted, and target hostnames are resolved by the proxy. HTTPS sites are supported; HTTPS proxy endpoints, VLESS/VMess and subscription URLs are not supported directly. The proxy address must be reachable from the deployment server. A proxy failure never falls back to direct access. Unconfigured accounts retain their existing network behavior. Active accounts cannot change proxies (HTTP 409); the next request recreates the context after a saved change. Keep the password blank only to preserve it with an unchanged server and username. Neither API responses nor logs expose it. Saving the exact configuration may adopt a server-observed result from the last five minutes in the same process, bound to the account, route, credentials and cookies. Changed/expired inputs never inherit success, and failed results remain failed. The test API accepts an optional `{"proxy":{"server":"socks5://your-user:your-password@proxy.example.com:1080"}}` body for a draft test; an empty body retains saved-proxy testing. The proxy test reports connectivity, egress IP and Muse session status separately; it does not prove generation capability or replace the account's browser test.

---

All protected endpoints require this in the Header:
```http
Authorization: Bearer <your-MUSE2API_KEY>
```

### 1. Chat Completions

```bash
curl -X POST "http://localhost:18610/v1/chat/completions" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [
      {"role": "user", "content": "Write a seven-character quatrain about cyberpunk and neon night rain"}
    ],
    "stream": false
  }'
```

### 2. Text-to-Image (Image Generation)

```bash
curl -X POST "http://localhost:18610/v1/images/generations" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-image",
    "prompt": "A mechanical Shiba Inu wearing an anti-gravity spacesuit, cinematic lighting, 8k resolution",
    "size": "16:9",
    "response_format": "url"
  }'
```

**Response example**:
```json
{
  "created": 1790148495,
  "data": [
    {
      "revised_prompt": "A mechanical Shiba Inu wearing an anti-gravity spacesuit, cinematic lighting, 8k resolution",
      "url": "http://localhost:18610/v1/media/img_abc123.webp",
      "kind": "image",
      "bytes": 54210
    }
  ]
}
```

Use the `images` array for multi-image references (up to 10 assets; each no more than 40 MiB, 100 MiB in total — see "Reference Materials" below); the service uploads them one by one in array order, and only sends the prompt after all attachments have been confirmed by the web page. `images` cannot be mixed with legacy single-image fields such as `image` / `reference_image`:

```bash
curl -X POST "http://localhost:18610/v1/images/generations" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-image",
    "prompt": "Combine the subject of the first image with the color palette of the second image to generate a poster",
    "images": [
      {"url": "https://example.com/subject.png"},
      {"url": "https://example.com/style.png"}
    ],
    "response_format": "url"
  }'
```

A multipart request to `/v1/images/edits` can repeatedly submit the `image` field with the same name:

```bash
curl -X POST "http://localhost:18610/v1/images/edits" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -F "prompt=Generate a new image combining the two reference images" \
  -F "image=@subject.png" \
  -F "image=@style.png"
```

### 3. Text-to-Video / Image-to-Video (Videos)

- **Step 1: Create a generation task**
  ```bash
  curl -X POST "http://localhost:18610/v1/videos" \
    -H "Authorization: Bearer m2a_your_secret_key" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "Golden maple leaves drift lightly in the breeze, sunlight filtering through the treetops",
      "duration": 5,
      "size": "16:9"
    }'
  ```
  Returns the task ID: `{"id": "task_xyz789", "status": "queued"}`

  Multi-image reference videos use an ordered `images` array in the same request; this supports multiple images as video references — uploading video files as references is not supported:
  ```bash
  curl -X POST "http://localhost:18610/v1/videos" \
    -H "Authorization: Bearer m2a_your_secret_key" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "Let the subject of the first image move in the scene and color palette of the second image",
      "images": [
        {"url": "https://example.com/start.png"},
        {"url": "https://example.com/style.png"}
      ],
      "duration": 6,
      "size": "16:9"
    }'
  ```

- **Step 2: Poll task progress**
  ```bash
  curl "http://localhost:18610/v1/videos/task_xyz789" \
    -H "Authorization: Bearer m2a_your_secret_key"
  ```
  Returns when complete:
  ```json
  {
    "id": "task_xyz789",
    "status": "succeeded",
    "progress": 100,
    "result": {
      "url": "http://localhost:18610/v1/media/vid_xyz789.mp4"
    }
  }
  ```

---

## 🎞️ Reference Materials: Images / Videos / Audio / Documents

Image generation, image editing, image tasks, and video endpoints can all include reference materials, with limits matching the muse.ai web app:

| Item | Limit |
|---|---|
| Count | Up to 10 per request (all fields combined) |
| Single size | 40 MiB; 100 MiB in total |
| Images | jpg / png / gif / webp / heic / heif / avif |
| Videos | mp4 / mov / webm / 3gp, no longer than 120 seconds (mp4/mov are pre-validated) |
| Audio | mp3 / wav / m4a / ogg / flac / aac / opus |
| Documents | pdf / doc / docx |

- JSON fields: `images` (any material can be mixed in), `videos`, `audios`, `files`, uploaded in this order; each item can be a URL, Data URL, Base64, or an object such as `{"url": ...}` / `{"video_url": ...}` / `{"audio_url": ...}`.
- The type is taken from the Data URL / response header / multipart-declared MIME; when the declaration is empty or `application/octet-stream`, it is identified from the file header.
- Multipart for `/v1/images/edits` can repeatedly submit the `image`, `video`, `audio`, and `file` fields.
- For a video task, if the first asset is an image, it is used as the main starting frame; the finished product will never be one of the assets uploaded in this run (excluded by content hash).

```bash
curl -X POST "http://localhost:18610/v1/videos"   -H "Authorization: Bearer m2a_your_secret_key"   -H "Content-Type: application/json"   -d '{
    "prompt": "Based on the camera movement of the reference video and the mood of the audio, generate a new city nightscape",
    "videos": [{"url": "https://example.com/ref.mp4"}],
    "audios": [{"url": "https://example.com/bgm.mp3"}],
    "duration": 8,
    "size": "16:9"
  }'
```

Nginx reverse proxy needs `client_max_body_size` set to around 200M, otherwise large assets will be rejected with 413.

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MUSE2API_KEY` | Auto-generated | Admin console and API authentication key (starting with `m2a_`) |
| `MUSE2API_HOST` | `127.0.0.1` | Listen IP (inside Docker, `0.0.0.0` is recommended) |
| `MUSE2API_PORT` | `18610` | Service port |
| `MUSE2API_PUBLIC_BASE` | `Empty` | Full public URL prefix; when empty, the frontend automatically detects the access origin |
| `MUSE2API_CHROMIUM` | `chromium` | Full path to the browser executable |
| `MUSE2API_CDP_PORT` | `19210` | Internal CDP debug communication port |
| `MUSE2API_CONCURRENCY` | `2` | Maximum number of concurrent generation tasks (can be freely raised to match server resources, default 2) |
| `MUSE2API_IMAGE_TIMEOUT` | `240` | Image generation timeout limit (seconds) |
| `MUSE2API_VIDEO_TIMEOUT` | `600` | Video generation timeout limit (seconds) |
| `MUSE2API_CHAT_TIMEOUT` | `300` | Chat generation timeout limit (seconds) |
| `MUSE2API_BUSY_RETRIES` | `3` | Extra retries when muse.ai replies "video/image service overloaded, 503, temporarily unavailable" (0 = no retry); generation refusals, unsupported regions, and insufficient quota are not retried |
| `MUSE2API_BUSY_RETRY_DELAY` | `20` | Wait in seconds before the above retries |
| `MUSE2API_BUSY_RETRY_MIN_REMAINING` | `180` | Stop retrying and return failure directly when the remaining generation time limit is less than this many seconds (plus the wait time) |
| `MUSE2API_VIDEO_HEDGE` | `1` | When muse.ai has recently returned "overloaded/503", generate a video on 2 idle accounts at the same time; the first to finish wins and the other is cancelled immediately; set to `0` to disable. Only affects videos, and requires at least 2 idle accounts and 1 idle concurrency slot |
| `MUSE2API_VIDEO_HEDGE_WINDOW` | `1200` | How many seconds after the most recent overload reply to keep parallel mode (restored from the most recent task records after a restart) |
| `MUSE2API_VIDEO_HEDGE_LEASE_TIMEOUT` | `5` | Maximum seconds the second lane waits for an idle account; if none becomes available, only one lane runs |
| `MUSE2API_REF_IMAGE_LOCAL_FILES` | `0` | Reading server-local paths is disabled by default; only trusted single-user deployments may explicitly set this to `1` to restore it |
| `MUSE2API_REF_IMAGE_PRIVATE_FETCH` | `0` | Only public URLs are allowed by default, with per-hop DNS/redirect checks, pinning the checked IP, and no environment proxy; explicitly set to `1` to allow private/local URLs |

Both switches default to `0`: all image generation, image editing, image tasks, and video endpoints prohibit server-local paths and private/local URLs. Data URLs, valid Base64, and public URLs work normally. Only set them explicitly to `1` to restore the corresponding legacy capability when you have confirmed the caller is trusted and it is actually needed.
Policy rejections, download failures, and non-compliant MIME or size will return HTTP 400 before a task is submitted, and will not create a background task that only fails later.

Reference-image security regression check (simulated DNS and HTTP, no real network or account):

```bash
python tests/test_ref_image_safety.py
```

---

## 🔒 Security & Privacy Statement

- **Zero data leaks**: All data in this software (including account credentials, task queues, and media files) is persisted locally in the `data/` directory, with no reliance on any third-party telemetry or external relay service.
- **Open-source compliance**: This project is for technical exchange, system automation operations research, and automated testing only. Do not use this project for any purpose that violates Meta's platform Terms of Service or any national laws or regulations.

---

## 👥 Contributor Thanks (Contributors)

Thank the following developers for their code contributions and feature improvements to MUSE2API (in PR merge order):

- 🌟 **[@cpt-kenvie](https://github.com/cpt-kenvie)** (PR [#2](https://github.com/czg86389-hub/muse2api/pull/2)) — Fixed Docker Compose reading `.env` environment variables and the example configuration override issue
- 🌟 **[@djs-91](https://github.com/djs-91)** (PR [#9](https://github.com/czg86389-hub/muse2api/pull/9)) — Fixed Base URL collapsing when `MUSE2API_PUBLIC_BASE` is not configured, causing admin page address parsing errors
- 🌟 **[@toby-bridges](https://github.com/toby-bridges)** (PR [#8](https://github.com/czg86389-hub/muse2api/pull/8)) — Adapted to muse.ai's live Chinese UI, fixed attachment upload confirmation and Stop status misjudgement, and strengthened video output and cover anti-misselection
- 🌟 **[@LF2023](https://github.com/LF2023)** (PR [#18](https://github.com/czg86389-hub/muse2api/pull/18)) — Fixed video generation matching a cover still frame by tid alone, and standardized expected matching and anti-misselection mechanisms
- 🌟 **[@wuliaolaozi](https://github.com/wuliaolaozi)** (PR [#27](https://github.com/czg86389-hub/muse2api/pull/27)) — Adapted bilingual (Chinese/English) quota queries, and fixed quota reading and admin page error messages
- 🌟 **[@CarloCPP](https://github.com/CarloCPP)** (PR [#6](https://github.com/czg86389-hub/muse2api/pull/6), [#26](https://github.com/czg86389-hub/muse2api/pull/26)) — Contributed the Tampermonkey one-click account-import script, relaxed the required Cookie check to session credentials, and one-click installation
- 🌟 **[@gr219](https://github.com/gr219)** (PR [#31](https://github.com/czg86389-hub/muse2api/pull/31)) — Added bilingual (Chinese/English) i18n dynamic switching for the admin backend and Chrome extension
- 🌟 **[@Yeadon8888](https://github.com/Yeadon8888)** (PR [#32](https://github.com/czg86389-hub/muse2api/pull/32)) — Added ordered multi-reference image generation, editing, and image-to-video support
- 🌟 **[@pandalaohe](https://github.com/pandalaohe)** (PR [#24](https://github.com/czg86389-hub/muse2api/pull/24)) — Provided reference-image local file and private URL security policies, redirect re-checks, and DNS rebinding protection
- 🌟 **[@QuartzWarrior](https://github.com/QuartzWarrior)** (PR [#25](https://github.com/czg86389-hub/muse2api/pull/25)) — Contributed per-account independent HTTP proxy basics, authentication handover, configuration UI, and isolation regression tests
- [@Roins-hub](https://github.com/Roins-hub): New Muse input-area compatibility, video/audio/document reference materials (PR #40).

- [@WXJJ0520](https://github.com/WXJJ0520): Scoped the send button to the current input area, fixed sidebar history mismatches, and added real Chromium regression tests (PR [#48](https://github.com/czg86389-hub/muse2api/pull/48)).

- [@Yeadon8888](https://github.com/Yeadon8888): Kept Cookie rotation in unassigned session responses, avoiding continued use of expired old values (PR [#60](https://github.com/czg86389-hub/muse2api/pull/60)).
- [@ALight777](https://github.com/ALight777): Current-window Cookie storage isolation, independent queue budgets, real version reporting and read-only update checks, video thumbnails (PR [#58](https://github.com/czg86389-hub/muse2api/pull/58), [#55](https://github.com/czg86389-hub/muse2api/pull/55), [#57](https://github.com/czg86389-hub/muse2api/pull/57), [#51](https://github.com/czg86389-hub/muse2api/pull/51)).

Everyone is welcome to submit PRs and Issues — let's make the project even stronger and better together!

---

## 📄 Open Source License

This project is released as open source under the [MIT License](LICENSE).


## Media Selection Regression Tests

Fixed reference images being misidentified as generated results, and downloads being overwritten by other media on the page.
Tests use an independent temporary Chromium profile and a locally synthesized DOM, do not access Muse, do not read accounts, and do not consume generation quota.
After installing the project dependencies and Chromium, run from the repository root:

```bash
python tests/test_media_selection.py
```

If the browser is not in PATH, set `MUSE2API_CHROMIUM` to the executable path.
Covers upload preview exclusion, result deduplication, history attachment exclusion, precise source downloads, video source preservation,
prohibition of global download fallback, and aborting when reference-image reading fails. The final image-editing result still needs to be verified through the real API.


## v1.5.2: Long Image Generation Behind a Reverse Proxy / CDN

The synchronous OpenAI image API remains compatible, but a CDN/client may truncate long requests before generation finishes.
Production clients should use short submission + polling, rather than extending the HTTP timeout and generating again:

1. `POST /v1/images/tasks`: The JSON is the same as `/v1/images/generations`; you can pass a single reference image with `reference_image`, or multiple reference images with the `images` array; adding a stable `Idempotency-Key` request header is recommended.
2. Receive HTTP 202 and `id`, and call `GET /v1/images/tasks/{id}` every 3 seconds.
3. When `status=completed`, read `url` or `data[0]`; when `status=failed`, show `error`. If a query fails, only retry the query — do not POST a new image generation.
4. The same idempotency key and same input return the original task; the same key with different input returns 409; a full queue returns 429. One process shares one browser and accepts at most 8 unfinished image tasks.

The original `/v1/images/generations` JSON and `/v1/images/edits` JSON/multipart can also pass `async=true` to switch to the same async handling. The default synchronous behavior is unchanged.
`timeout` is the queue and generation wait budget (1-600 seconds); browser initialization and retrieval have their own timeouts; clients should keep polling for the task's final state.
Task metadata is persisted; when the process restarts, unfinished image tasks are marked as failed and are not silently generated again.
The admin page image API test has also been changed to task polling.

Regression checks (do not consume real generation quota):

```bash
python tests/test_async_images.py
python tests/test_vm_wait.py engine.py --assert
```

Fixed a race where shared-browser exception handling reset other tasks after unlocking; no longer treats leftover sidebar text such as
`Connecting...` / `Still sending` as evidence that the current image generation failed within 16 seconds.


## v1.5.3: Abnormal Account Status & Self-Hosted Instance Troubleshooting

Keep-alive requests come from the **deployment server's egress**, not from the browser that exported the Cookie. That the author's server works does not prove that another server's network, region, proxy configuration, or the same account's session is also fine.

- `/api/session HTTP 401`: Upstream rejected authentication. First confirm the account can log in on the muse.ai official site, then re-import the Cookie.
- `HTTP 403`: Access denied, possibly involving account permissions, server egress, or region/access restrictions; **this is not the same as an expired Cookie**. Check the full error and deployment network, and do not blindly re-import repeatedly.
- `HTTP 429`, `5xx`, timeouts, non-JSON, or no `assigned` returned: This keep-alive was not confirmed; it no longer falsely reports success or overwrites the last confirmed account status; the note shows the diagnosis. If the original status was "Not tested" or "Abnormal", it is not forcibly changed to "Available" either.
- A browser page load timeout is no longer treated directly as authentication failure; an account is only marked abnormal on explicit authentication failure.
- "Available" is the most recent confirmed result and does not guarantee the network is reachable this time; "Remaining validity" is a Cookie time/estimate, not proof that the server session is valid. Successful detection no longer extends the validity by 48 hours out of thin air; renewal follows the Cookie actually returned.

After updating to v1.5.3 and restarting the service, click "Test" on the account to re-confirm. Exceptions already written by the old version are not unconditionally cleared to normal. Hover over the note to see the full error; when reporting an issue, provide the HTTP status, version, and deployment environment, and **do not publish Cookies, API Keys, or admin page links containing a key**.

Bare-metal Git deployment: run `git pull --ff-only` and then restart the corresponding service; Docker Compose: `git pull --ff-only && docker compose up -d --build`. Save local custom changes first, and keep `.env` and `data/`.

Regression tests that do not use real accounts or consume generation quota:

```bash
python tests/test_session_health.py
```
# MUSE2API 1.6.1

## Update Notes (2026-10-13)

- Fixed cross-account quota query mix-ups: quota queries are bound to the corresponding account's browser page, preventing different accounts from showing wrong balances and usage.
- When quota reading fails or no valid data is read, the old cache is kept and the failure is clearly reported, avoiding treating invalid results as real-time quota.
- Fixed false keep-alive success reports: distinguish between a wake-up request being accepted and the VM actually running; wake-up success is only reported when the re-check status is RUNNING.
- The admin page shows quota query time and expiry reminders; account tests separately report session validity and quota reading status.
- Added offline regression tests covering account quota isolation and VM wake-up status confirmation.

Reference material validation notes: Videos must contain parsable duration information of no more than 120 seconds (MP4/MOV/3GP/WebM);
Missing or damaged duration information will be rejected. PDF page counts, document parsing, and account permissions are still validated by Muse upstream,
Attachment confirmation in this project only means a file has entered the input area; it does not guarantee upstream can read every file type.
Chromium and the service must be able to access the same material file; temporary materials are located in the browser profile directory and are cleaned up after the task ends.
