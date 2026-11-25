"""muse2api — Proxies the chat, image, and video quota of free Muse (muse.ai) web accounts as an API.

OpenAI-compatible:
  POST /v1/chat/completions          (text / code chat, stream supported, Codex-compatible)
  POST /v1/images/generations
  POST /v1/videos  +  GET /v1/videos/{task_id}
  GET  /v1/models
  GET  /v1/media/{name}

Admin (account-pool management UI at GET /):
  GET    /admin/status
  GET    /admin/accounts
  POST   /admin/accounts            (single / bulk text / bulk array)
  PATCH  /admin/accounts/{id}       (edit label, enable/disable)
  DELETE /admin/accounts/{id}
  POST   /admin/accounts/{id}/test  (actually opens muse.ai to verify the session is valid)
  POST   /admin/accounts/{id}/relogin
  GET    /admin/tasks               (task records)
  DELETE /admin/tasks/{id}  |  POST /admin/tasks/clear
  GET    /admin/media  |  POST /admin/media/delete  |  DELETE /admin/media/{name}
  GET    /admin/extension           (browser extension zip, for fetching cookies)
  GET    /admin/cookie-helper       (CLI cookie-fetching script, advanced)
"""
from __future__ import annotations

from typing import Any

import asyncio
import copy
from contextlib import contextmanager
import base64
import collections
import binascii
import io
import hashlib
import json
import logging
import os
import re
import queue
import threading
import time
import uuid
import zipfile

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import model_validator, BaseModel, Field, SecretStr, field_validator

from config import CFG
from engine import (ESSENTIAL_COOKIES, MAX_REFERENCE_IMAGE_BYTES,
                    MAX_REFERENCE_IMAGES, MAX_REFERENCE_TOTAL_BYTES,
                    REFERENCE_MIME_EXT, MuseAuthError, MuseEngine, MuseGenerationError,
                    MuseModelRefusalError, RefImageSafetyError, reference_kind)
from engine import MuseProxyError, MuseServiceBusyError, MuseSessionUnavailable
from store import Store, account_expiry, min_expiry
from generation_timing import QueuePolicy, QueueSettings, TaskTiming
from proxy import normalize_server, request_proxy_context, split_proxy_url
from thumbnails import (VIDEO_FORMATS, ThumbnailUnavailable, clear_thumbnail,
                        source_version, video_thumbnail)

import sys
log = logging.getLogger("muse2api")

# Muse media-backend outages ("video service overloaded (503)") are retried; see _run_generation_locked.
BUSY_RETRIES = max(0, int(os.environ.get("MUSE2API_BUSY_RETRIES", "3")))
BUSY_RETRY_DELAY = max(0.0, float(os.environ.get("MUSE2API_BUSY_RETRY_DELAY", "20")))
BUSY_RETRY_MIN_REMAINING = max(0.0, float(os.environ.get("MUSE2API_BUSY_RETRY_MIN_REMAINING", "180")))
# While muse.ai is overloaded a failed video turn costs 6-7 minutes before it says so.
# For a while after any overload reply, video tasks run on two accounts at once and the
# first finished video wins; the other attempt is cancelled.
VIDEO_HEDGE = os.environ.get("MUSE2API_VIDEO_HEDGE", "1") != "0"
VIDEO_HEDGE_WINDOW = max(0.0, float(os.environ.get("MUSE2API_VIDEO_HEDGE_WINDOW", "1200")))
VIDEO_HEDGE_LEASE_TIMEOUT = max(0.1, float(os.environ.get("MUSE2API_VIDEO_HEDGE_LEASE_TIMEOUT", "5")))
_OVERLOAD_LOCK = threading.Lock()
_last_overload = 0.0


def _note_overload():
    global _last_overload
    with _OVERLOAD_LOCK:
        _last_overload = time.monotonic()


def _overload_recent() -> bool:
    with _OVERLOAD_LOCK:
        return _last_overload > 0 and time.monotonic() - _last_overload <= VIDEO_HEDGE_WINDOW


def _seed_overload_from_tasks(tasks, now: float | None = None):
    """After a restart, remember an outage that recent tasks already ran into."""
    global _last_overload
    now = time.time() if now is None else now
    newest = 0.0
    for task in tasks:
        text = f"{task.get('error') or ''} {task.get('last_error') or ''}"
        if "media service temporarily unavailable" not in text:
            continue
        try:
            newest = max(newest, float(task.get("updated_at") or 0))
        except (TypeError, ValueError):
            continue
    age = now - newest
    if newest and 0 <= age <= VIDEO_HEDGE_WINDOW:
        with _OVERLOAD_LOCK:
            _last_overload = max(_last_overload, time.monotonic() - age)
        return True
    return False


class MuseCancelledError(MuseGenerationError):
    """The parallel attempt was stopped because the other one already delivered."""
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)

CFG.ensure_dirs()
app = FastAPI(title="muse2api", version="1.6.12")

# The cookie helper script sends import requests from muse.ai pages, so that origin must be allowed;
# Browser extensions send them from chrome-extension://, so allow those too.
#
# Allow all origins here: this service authenticates with Bearer Key for auth and does not rely on cookies,
# so allowing origins brings no privilege-escalation risk; if origins were locked down, browser-based agents
# (Open WebUI / LobeChat / various web clients) would be blocked by CORS.
_origins = [o.strip() for o in (CFG.cors_origins or "").split(",") if o.strip()]
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)

store = Store(CFG)
engine = MuseEngine(CFG)
engine.proxy_resolver = lambda aid: (store.get_account(aid) or {}).get("proxy")


class ConcurrencyLock:
    """Multi-account concurrency scheduling lock.
    CFG.max_concurrency caps the maximum concurrency slots;
    it also exposes acquire / release / locked interfaces compatible with the standard Lock,
    keeping backward compatibility with legacy single-lock assertions and admin operations."""
    def __init__(self, limit: int = 2):
        self.limit = max(1, limit)
        self._sem = threading.Semaphore(self.limit)
        self._count_lock = threading.Lock()
        self._active_count = 0

    def acquire(self, blocking: bool = True, timeout: float | None = -1) -> bool:
        if timeout is None:
            timeout = -1
        if timeout < 0:
            ok = self._sem.acquire(blocking=blocking)
        else:
            ok = self._sem.acquire(blocking=blocking, timeout=timeout)
        if ok:
            with self._count_lock:
                self._active_count += 1
        return ok

    def release(self):
        with self._count_lock:
            if self._active_count > 0:
                self._active_count -= 1
        self._sem.release()

    def locked(self) -> bool:
        with self._count_lock:
            return self._active_count > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class AccountPoolManager:
    """Account concurrency-conflict manager: ensures the same account is never used by two concurrent tasks at once, while multiple accounts run in parallel."""
    def __init__(self, store_instance):
        self.store = store_instance
        self.busy_accounts: set[str] = set()
        self.cond = threading.Condition()

    def acquire(self, account_id: str | None = None, preferred_id: str | None = None,
                rotate: bool = True, force_rotate: bool = False,
                timeout: float | None = 60.0, record_use: bool = True) -> dict:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise MuseGenerationError("Timed out waiting for an idle account; all accounts are currently busy")

                if account_id:
                    acc = self.store.get_account(account_id)
                    if not acc or not acc.get("enabled", True):
                        raise MuseAuthError(f"Specified account {account_id} does not exist or is disabled")
                    if account_id not in self.busy_accounts:
                        self.busy_accounts.add(account_id)
                        return acc
                else:
                    pref = preferred_id if preferred_id not in self.busy_accounts else None
                    acc = self.store.pick_account(
                        preferred_id=pref,
                        rotate=rotate,
                        force_rotate=force_rotate,
                        exclude_ids=self.busy_accounts,
                        record_use=record_use,
                    )
                    if acc:
                        self.busy_accounts.add(acc["id"])
                        return acc

                live = [a for a in self.store.accounts if a.get("enabled", True) and a.get("cookies")]
                if not live:
                    raise MuseAuthError("No accounts available; please import cookies on the admin page first")

                self.cond.wait(timeout=0.5 if remaining is None else min(remaining, 0.5))

    def release(self, account_id: str | None):
        if not account_id:
            return
        with self.cond:
            self.busy_accounts.discard(account_id)
            self.cond.notify_all()


GEN_LOCK = ConcurrencyLock(limit=CFG.max_concurrency)
ACCOUNT_POOL = AccountPoolManager(store)
IMAGE_TASK_LOCK = threading.Lock()
QUEUE_POLICY = QueuePolicy(os.path.join(CFG.data_dir, "generation-queue.json"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bind_account_proxy(account: dict):
    """Pin the selected account's route for the whole operation, including retries."""
    if hasattr(engine, "_ensure_local"):
        engine._ensure_local().proxy_snapshot = copy.deepcopy(account.get("proxy")) or None


def _clear_account_proxy():
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            del local.proxy_snapshot


def _account_proxy(account: dict):
    if hasattr(engine, "_ensure_local"):
        local = engine._ensure_local()
        if hasattr(local, "proxy_snapshot"):
            return local.proxy_snapshot
    return account.get("proxy")


@contextmanager
def _account_operation(aid: str):
    """Maintenance and settings share the same account lease as generation."""
    with ACCOUNT_POOL.cond:
        account = store.get_account(aid)
        if not account:
            raise HTTPException(404, "Account does not exist")
        if aid in ACCOUNT_POOL.busy_accounts:
            raise HTTPException(409, "Account is running a task or check; please try again later")
        ACCOUNT_POOL.busy_accounts.add(aid)
        account = copy.deepcopy(account)
    try:
        _bind_account_proxy(account)
        yield account
    finally:
        try:
            engine.close_current_page()
        finally:
            _clear_account_proxy()
            ACCOUNT_POOL.release(aid)


# ------------------------- OpenAI-style error responses -------------------------
# Most agents read the error message from OpenAI-style {"error": {"message": ...}};
# FastAPI returns {"detail": ...} by default, so clients cannot read the reason and only show "unknown error".
# So /v1/* is uniformly converted to OpenAI format, while admin endpoints keep their original shape (the frontend relies on detail).
def _err_type(status: int) -> str:
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    # Never echo user-supplied proxy credentials in validation errors.
    errors = [{k: v for k, v in error.items() if k not in ("input", "ctx")}
              for error in exc.errors()]
    if request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "message": "Request parameter validation failed: " + str(errors)[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": errors})

MODELS = [
    {"id": "muse-spark", "object": "model", "owned_by": "muse",
     "description": "Muse Spark — text / code chat (free web quota, streaming supported)"},
    {"id": "muse-image", "object": "model", "owned_by": "muse",
     "description": "Muse Image — text-to-image / image editing (free web quota)"},
    {"id": "muse-video", "object": "model", "owned_by": "muse",
     "description": "Muse Video — text-to-video / image-to-video (free web quota)"},
]

# Downstream clients (Codex / Cline / various clients) usually pass model names from OpenAI or Anthropic,
# so they are all mapped here to muse's real capabilities.
#
# Note: the muse.ai web page is an auto-routing agent with **no enumerable model list** exposed,
# and the only reliably callable capabilities are three real ones — Muse Spark (language/code),
# Muse Image (image generation), and Muse Video (video generation). Aliases just let downstream clients keep their existing config.
MODEL_ALIASES = {
    # ---- Text / code → muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    "gpt-3.5-turbo": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4.1": "muse-spark",
    "gpt-4.1-mini": "muse-spark", "gpt-5": "muse-spark", "gpt-5-codex": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o3": "muse-spark",
    "o3-mini": "muse-spark", "o4-mini": "muse-spark",
    "codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark",
    "claude-3-haiku": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- Image generation → muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    # ---- Video generation → muse-video ----
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    n = (name or "").strip().lower()
    return MODEL_ALIASES.get(n, n or default)


# ------------------------- Auth -------------------------
def auth(authorization: str | None = Header(default=None)):
    if not CFG.api_key:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Authorization: Bearer <key>")
    parts = authorization.split(None, 1)
    token = parts[1].strip() if len(parts) > 1 else ""
    if not token or token != CFG.api_key:
        raise HTTPException(401, "Invalid API key")
    return True


# ------------------------- Request models -------------------------

def _persist_session_cookies(acc_id: str, cookies: dict, cookies_exp: dict | None) -> None:
    """Merge cookies rotated by muse.ai into storage without confirming the session."""
    acc = store.get_account(acc_id)
    if not acc or not cookies:
        return
    merged = {**(acc.get("cookies") or {}), **{k: v for k, v in cookies.items() if v}}
    exps = {**(acc.get("cookies_exp") or {}),
            **{k: v for k, v in (cookies_exp or {}).items() if v}}
    if merged == (acc.get("cookies") or {}) and exps == (acc.get("cookies_exp") or {}):
        return
    # synced_at / last_keepalive mean "session confirmed"; rotation alone does not.
    store.update_account(acc_id, cookies=merged, cookies_exp=exps)


engine.cookie_sink = _persist_session_cookies


def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """Calls /api/session to renew the account cookies, writes them back to the store, and returns the latest account dict.
    An account renewed recently (within 10 minutes) and in good state is reused directly, avoiding a 2-3 second HTTP round trip on every request."""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm,
                                        **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"),
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"Session OK (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except MuseProxyError:
        raise
    except MuseSessionUnavailable as exc:
        _persist_session_cookies(acc_id, exc.cookies, exc.cookies_exp)
        # The page load right after must not renew again with the pre-rotation cookie.
        engine._last_http_renew[acc_id] = now
        log.warning("HTTP pre-renewal: account %s has no workspace assigned%s: %s", acc_id,
                    " (rotated cookies saved)" if exc.rotated else "", exc)
    except Exception as exc:
        log.warning("HTTP pre-renewal of account %s failed: %s", acc_id, exc)
    return store.get_account(acc_id)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None):
    """Acquire a generation slot before the account lease; release both after cleanup."""
    import queue
    q = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        leased_id = None
        slot_acquired = False
        try:
            slot_acquired = GEN_LOCK.acquire(timeout=timeout)
            if not slot_acquired:
                q.put(("error", MuseGenerationError("Timed out waiting for a chat concurrency slot")))
                return
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    if leased_id:
                        ACCOUNT_POOL.release(leased_id)
                        leased_id = None
                    try:
                        alt = ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=timeout)
                    except Exception:
                        break
                    if not alt or alt["id"] == cur_id:
                        if alt:
                            leased_id = alt["id"]
                        break
                    cur_id = alt["id"]
                    leased_id = cur_id
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("[Auto account switch] Switching to backup account %s (%s), retrying...", alt.get("label"), cur_id)
                elif cur_id:
                    ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)
                    leased_id = cur_id

                yielded = False
                try:
                    if cur_id:
                        _bind_account_proxy(store.get_account(cur_id) or {})
                        refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                        if refreshed:
                            cur_cookies = refreshed["cookies"]
                            cur_exp = refreshed.get("cookies_exp")
                    if stop_event.is_set():
                        return
                    engine.start()
                    for chunk in engine.chat_stream(
                        cur_cookies, prompt, cur_exp, timeout,
                        account_id=cur_id, stop_event=stop_event
                    ):
                        yielded = True
                        q.put(("data", chunk))
                        if stop_event.is_set():
                            return
                    if cur_id:
                        store.mark(cur_id, True, "")
                        _sync_cookies(cur_id)
                    return
                except MuseAuthError as exc:
                    last_exc = exc
                    if cur_id:
                        store.mark(cur_id, False, str(exc))
                    engine.close_current_page()
                    if yielded:
                        break
                except Exception as exc:
                    last_exc = exc
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                    if yielded:
                        break
            if last_exc is not None:
                q.put(("error", last_exc))
        except Exception as exc:
            q.put(("error", exc))
        finally:
            try:
                if leased_id:
                    ACCOUNT_POOL.release(leased_id)
                engine.close_current_page()
            except Exception as exc:
                log.warning("Chat cleanup failed: %s", type(exc).__name__)
            finally:
                try:
                    _clear_account_proxy()
                finally:
                    if slot_acquired:
                        GEN_LOCK.release()
                    q.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    reference_image: str | None = None
    async_: bool = Field(False, alias="async")


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    duration: int | None = None
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    timeout: int | None = None
    extra: str | None = None
    image: Any = None
    images: list | None = None
    videos: list | None = None
    audios: list | None = None
    files: list | None = None
    image_url: Any = None
    reference_image: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions request.

    Be as lenient as possible with third-party clients: ignore any unrecognized fields (Pydantic default behavior),
    and only read the ones we actually use — otherwise every agent passing its own parameters would trigger a 422.
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    prompt: str | None = None       # Compatible with clients that put prompt directly at the top level
    # These are declared only so they can be read; muse.ai does nothing with them
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API (used by default in newer Codex)."""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AccountProxy(BaseModel):
    server: str
    username: str = ""
    password: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_proxy_url(cls, value):
        return split_proxy_url(value) if isinstance(value, dict) else value

    @field_validator("server")
    @classmethod
    def validate_server(cls, value):
        return normalize_server(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        if ":" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Proxy username cannot contain a colon or control characters")
        try:
            value.encode("latin-1")
        except UnicodeError:
            raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value):
        if value is not None:
            try:
                value.get_secret_value().encode("latin-1")
            except UnicodeError:
                raise ValueError("Proxy credentials must use Latin-1 characters") from None
        return value

    def configuration(self, previous=None):
        previous = previous or {}
        password = self.password.get_secret_value() if self.password is not None else None
        if password is None:
            if previous.get("server") == self.server and previous.get("username", "") == self.username:
                password = previous.get("password", "")
            elif self.username:
                raise HTTPException(400, "Enter the proxy password when changing its server or username")
            else:
                password = ""
        return {"server": self.server, "username": self.username, "password": password}


class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # Multi-line text, one account per line
    expires: dict[str, int] = Field(default_factory=dict)   # cookie name -> expiry timestamp
    proxy: AccountProxy | None = None


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    proxy: AccountProxy | None = None


class ProxyTestRequest(BaseModel):
    proxy: AccountProxy


# ------------------------- Prompt construction -------------------------
def _image_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        candidate = (value.get("image_url") or value.get("video_url") or value.get("audio_url")
                     or value.get("file_url") or value.get("url") or value.get("b64_json")
                     or value.get("data"))
        if isinstance(candidate, dict):
            candidate = candidate.get("url")
        return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None
    return None


def _reference_list(images: list | None, legacy: list[Any], field: str,
                    extra: dict[str, list | None] | None = None) -> list[str]:
    """Validate ordered references and make legacy precedence explicit.

    Order: images (or the legacy single-image field) first, then videos / audios / files in turn.
    images itself may also mix videos, audio, and documents; types are identified during preprocessing by MIME/file header.
    """
    primary = next((ref for value in legacy if (ref := _image_value(value))), None)
    if primary and images:
        raise HTTPException(400, f"{field} cannot be used together with the legacy single-image field")
    groups: list[tuple[str, list]] = []
    if primary:
        groups.append((field, [primary]))
    elif images is not None:
        groups.append((field, images))
    for name, values in (extra or {}).items():
        if values is not None:
            if not isinstance(values, list):
                raise HTTPException(400, f"{name} must be an array")
            groups.append((name, values))
    total = sum(len(values) for _, values in groups)
    if total > MAX_REFERENCE_IMAGES:
        raise HTTPException(400, f"At most {MAX_REFERENCE_IMAGES} reference items (currently {total})")
    refs = []
    for name, values in groups:
        for index, value in enumerate(values):
            ref = _image_value(value)
            if not ref:
                raise HTTPException(400, f"{name}[{index}] is not a valid reference")
            refs.append(ref)
    return refs


def _extra_media(req) -> dict[str, list | None]:
    return {"videos": req.videos, "audios": req.audios, "files": req.files}


def _image_references(req: ImageRequest) -> list[str]:
    return _reference_list(req.images, [req.reference_image, req.image], "images",
                           _extra_media(req))


def _video_references(req: VideoRequest) -> list[str]:
    return _reference_list(
        req.images, [req.reference_image, req.image_url, req.image], "images",
        _extra_media(req))


def _reference_kinds(prepared: list[str]) -> list[str]:
    """Preprocessed data URLs -> image/video/audio/document list (order preserved)."""
    return [reference_kind(ref[5:].split(";", 1)[0]) for ref in prepared]


_KIND_LABELS = {"image": ("", "image"), "video": ("", "video"),
                "audio": ("", "audio"), "document": ("", "document")}


def _describe_kinds(kinds: list[str]) -> str:
    counts = collections.Counter(kinds)
    return "、".join(f"{counts[k]} {_KIND_LABELS[k][0]}{_KIND_LABELS[k][1]}"
                    for k in ("image", "video", "audio", "document") if counts[k])


# Workbench prompts refer to items as [ImageN] / [VideoN] / [AudioN], numbered per kind in upload order.
# muse does not recognize these markers, so the "marker -> which attachment" mapping is written into the prompt.
_KIND_TOKENS = {"image": "Image", "video": "Video", "audio": "Audio", "document": "Document"}
_REFERENCE_TOKEN = re.compile(r"\[(?:Image|Video|Audio|Document)\d+\]")


def _reference_legend(kinds: list[str]) -> str:
    seen: collections.Counter = collections.Counter()
    parts = []
    for index, kind in enumerate(kinds, 1):
        seen[kind] += 1
        parts.append(f"[{_KIND_TOKENS[kind]}{seen[kind]}] is attachment #{index} ({_KIND_LABELS[kind][1]})")
    return "Attachment key: " + ", ".join(parts)

def build_image_prompt(r: ImageRequest, kinds: list[str] | None = None) -> str:
    refs = _image_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if has_ref and any(k != "image" for k in kinds):
        parts = [f"Based on the reference items I uploaded with this request ({_describe_kinds(kinds)}; {_reference_legend(kinds)}; "
                 f"use only these attachments uploaded this time; each attachment's purpose follows the description below)"
                 f"generate/edit an image and output one image: {r.prompt.strip()}"]
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(r.prompt):
        parts = [f"Generate/edit an image based on the reference images I uploaded with this request ({_reference_legend(kinds)}): {r.prompt.strip()}"]
    elif has_ref:
        parts = [f"Generate/edit an image based on the reference images I uploaded with this request: {r.prompt.strip()}"]
    else:
        parts = [f"Create a brand-new text-to-image work (no reference image was provided; do not search past albums or ask the user for an original image - draw a completely new image from scratch based on the text description): {r.prompt.strip()}"]
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()

    if any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "vertical", "720x1280", "1080x1920")):
        parts.append("[Composition and aspect ratio]: strict 9:16 vertical full-screen portrait frame (9:16 vertical portrait aspect ratio, taller than wide, full-screen phone vertical image); never generate a horizontal image, keep a vertical composition")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "horizontal", "1280x720", "1920x1080")):
        parts.append("[Composition and aspect ratio]: 16:9 widescreen landscape frame (16:9 widescreen landscape aspect ratio)")
    elif any(k in ar or k in sz for k in ("1:1", "square", "square", "1024x1024")):
        parts.append("[Composition and aspect ratio]: 1:1 square frame (1:1 square aspect ratio)")
    elif any(k in ar or k in sz for k in ("4:3", "4/3")):
        parts.append("[Composition and aspect ratio]: 4:3 frame")
    elif any(k in ar or k in sz for k in ("3:4", "3/4")):
        parts.append("[Composition and aspect ratio]: 3:4 vertical frame")
    elif r.aspect_ratio:
        parts.append(f"[Composition and aspect ratio]: {r.aspect_ratio} aspect ratio")
    elif r.size:
        parts.append(f"Size/aspect ratio: {r.size}")

    if has_ref and "image" in kinds:
        parts.append("[Clean image requirement]: thoroughly remove all text, watermarks, signatures, badges, and logo marks from the reference image (clean image without any watermark, text, or logo), and output an absolutely clean image with no text")

    if r.extra:
        parts.append(r.extra)
    return "，".join(parts)


def _content_text(content) -> str:
    """Normalize OpenAI content into plain text (compatible with multimodal list form)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif t == "image_url":
                    parts.append("[Image]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def build_chat_prompt(messages: list[ChatMessage]) -> str:
    """Join messages into a single prompt to send to muse.ai.

    The muse.ai web page is itself a contextual conversation, but this API is stateless (each request may land on
    a different account/page), so folding history into the prompt is the most controllable approach - which fits clients like Codex that
    send the full history every turn.
    """
    system, turns = [], []
    for m in messages:
        role = (m.role or "").strip().lower()
        text = _content_text(m.content).strip()
        if role == "tool":
            # Tool result -> send it as "user-provided information"
            if text:
                turns.append(("user", "[Tool result]\n" + text))
            continue
        if not text:
            # Some clients send assistant messages with only tool_calls and no body text
            if role == "assistant" and m.tool_calls:
                turns.append(("assistant", "[Requesting tool call]" + json.dumps(
                    m.tool_calls, ensure_ascii=False)[:600]))
            continue
        if role in ("system", "developer"):
            system.append(text)
        else:
            turns.append((role, text))

    # Single turn with no system instructions -> send the original text directly, closest to natural conversation
    if len(turns) == 1 and not system and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    if system:
        sys_text = "\n\n".join(system)
        sys_text = sys_text.replace("danger-full-access", "standard-workspace-access")
        parts.append(f"Background and task setup:\n{sys_text}")
    for role, text in turns:
        label = "Assistant" if role == "assistant" else "User"
        parts.append(f"{label}：\n{text}")
    return "\n\n".join(parts)


# ------------------------- Tool calling (function calling) adaptation -------------------------
# The muse.ai web model **does not** return structured tool_calls, so this layer adapts the protocol:
#   1) When a request includes tools, translate the tool definitions into the [Tool Calling Protocol] in the prompt;
#   2) The model outputs ```json {"tool": "...", "arguments": {...}} ``` following the protocol;
#   3) We parse that back into OpenAI tool_calls for the downstream agent.
#
# Note: this is a "best-effort adaptation", not a guarantee - the target model is a general chat model, not fine-tuned
# for function calling, so how well it follows the protocol needs to be observed in practice.
_TOOL_PROTOCOL_HEAD = """You may call the following tools as needed to help the user complete the task.
If you need to call a tool, output a JSON code block in the following format directly (do not include any other explanation):
```json
{"name": "<tool name>", "arguments": {<parameters>}}
```
If you need to call multiple tools, output a JSON array containing multiple objects.
If you do not need to call any tool, answer directly in natural language.

Available tools:
"""


def _describe_params(params) -> str:
    """Describe JSON Schema parameters as readable multi-line text."""
    if not isinstance(params, dict):
        return "      (no parameters)"
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props:
        return "      (no parameters)"
    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append("      - %s (%s, %s)%s" % (
            name, spec.get("type") or "any",
            "required" if name in required else "optional",
            (" " + spec["description"]) if spec.get("description") else ""))
    return "\n".join(lines)


def build_tools_prompt(tools: list | None) -> str:
    """Translate the tools definitions into a prompt fragment (empty string when there are no tools).

    Also compatible with Chat Completions' `{"type":"function","function":{...}}`
    and the Responses API's `{"type":"function","name":...,"parameters":...}`.
    """
    if not tools:
        return ""
    items = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        desc = fn.get("description") or ""
        items.append("%d. %s%s\n   Parameters:\n%s" % (
            len(items) + 1, fn["name"], (" — " + desc) if desc else "",
            _describe_params(fn.get("parameters"))))
    if not items:
        return ""
    return _TOOL_PROTOCOL_HEAD + "\n".join(items) + "\n"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


def _as_tool_calls(obj) -> list[dict] | None:
    """Convert parsed JSON into OpenAI tool_calls; return None if it does not look like a tool call."""
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("tool") or item.get("name") or item.get("function")
        if not isinstance(name, str) or not name:
            return None
        args = item.get("arguments")
        if args is None:
            args = item.get("parameters") or item.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function",
                    "function": {"name": name, "arguments": args}})
    return out or None


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """Extract tool calls from the model output.

    Returns (tool_calls, remaining text); returns (None, original text) if nothing is found.
    Supports several forms:
    1) ```json ... ``` code block;
    2) Bare JSON (optionally with a leading json/JSON keyword);
    3) A complete JSON object or array embedded in text.
    """
    if not text:
        return None, text
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            calls = _as_tool_calls(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if calls:
            return calls, (text[:m.start()] + text[m.end():]).strip()

    stripped = text.strip()
    clean_stripped = re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", stripped).rstrip("`").strip()
    if clean_stripped.startswith(("{", "[")):
        try:
            calls = _as_tool_calls(json.loads(clean_stripped))
            if calls:
                return calls, ""
        except Exception:
            pass

    m_json = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m_json:
        try:
            calls = _as_tool_calls(json.loads(m_json.group(1).strip()))
            if calls:
                rest = (text[:m_json.start()] + text[m_json.end():]).strip()
                if re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", rest).strip("`").strip() == "":
                    rest = ""
                return calls, rest
        except Exception:
            pass

    return None, text


def build_video_prompt(r: VideoRequest, kinds: list[str] | None = None) -> str:
    user_prompt = r.prompt.strip()
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()
    dur = r.duration or 6
    refs = _video_references(r)
    has_ref = bool(refs)
    kinds = kinds or ["image"] * len(refs)
    if any(k != "image" for k in kinds):
        # No longer default to "first image as the opening frame": testing showed it pulls the model back to the image scene and makes it ignore the reference video
        reference_instruction = (
            f"Generate a video based on the reference items I uploaded with this request ({_describe_kinds(kinds)})"
            f"({_reference_legend(kinds)}; use only these attachments uploaded this time, never use historical material;"
            "each attachment's purpose follows the description below)")
    elif len(refs) > 1 and _REFERENCE_TOKEN.search(user_prompt):
        reference_instruction = (
            f"Generate a video based on the {len(refs)} reference images I uploaded with this request ({_reference_legend(kinds)};"
            "each image's purpose follows the description below; when unspecified, use the first image as the main opening frame; never use historical images or any other images)")
    elif len(refs) > 1:
        reference_instruction = (
            f"Generate a video based on the {len(refs)} reference images I uploaded with this request (using the first image as the main opening frame,"
            "with the remaining images as subject, scene, and style references; never use historical images or any other images)")
    else:
        reference_instruction = (
            "Use the reference image attachment I uploaded with this request as the first-frame reference image (never use historical images or any other images,"
            "the opening frame must strictly be this image I have just uploaded and attached here)")

    gen_word = "image-to-video" if all(k == "image" for k in kinds) else "video"
    is_vertical = any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "vertical", "720x1280", "1080x1920"))

    parts = []
    if is_vertical:
        if has_ref:
            parts.append(f"{reference_instruction}: generate a strictly 9:16 vertical full-screen phone{gen_word} (9:16 vertical portrait video, 720x1280, taller than wide, full-screen phone vertical image; never generate a horizontal image or black bars; duration strictly {dur} seconds): {user_prompt}")
        else:
            parts.append(f"Brand-new text-to-video creation (purely text-based, never reference any historical images or context): generate a strictly 9:16 vertical full-screen phone video (9:16 vertical portrait video, 720x1280, taller than wide, full-screen phone vertical image; never generate a horizontal image or side black bars; keep a vertical composition; duration strictly {dur} seconds): {user_prompt}")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "horizontal", "1280x720", "1920x1080")):
        if has_ref:
            parts.append(f"{reference_instruction}: generate a 16:9 widescreen landscape{gen_word} (16:9 widescreen landscape video; duration strictly {dur} seconds): {user_prompt}")
        else:
            parts.append(f"Brand-new text-to-video creation (purely text-based, never reference any historical images or context): generate a 16:9 horizontal widescreen video (16:9 widescreen landscape video; duration strictly {dur} seconds): {user_prompt}")
    else:
        if has_ref:
            parts.append(f"{reference_instruction}: generate a moving{gen_word} (duration strictly {dur} seconds): {user_prompt}")
        else:
            parts.append(f"Brand-new text-to-video creation (purely text-based, never reference any historical images or context): generate a video (duration strictly {dur} seconds): {user_prompt}")

    if r.resolution:
        parts.append(f"Quality spec: {r.resolution}")
    if r.extra:
        parts.append(r.extra)
    return "，".join(parts)


def media_url(name: str) -> str:
    """Media URL.

    If public_base is set, return an **absolute URL** - OpenAI-compatible clients (and various agent
    platforms) usually render or download data[].url directly; a relative path would resolve against the client's
    own domain and cause a 404. When public_base is empty, fall back to a relative path.
    """
    base = _public_base()
    return f"{base}/v1/media/{name}" if base else f"/v1/media/{name}"


# ------------------------- Cookie parsing -------------------------
def parse_cookie_text(text: str) -> dict[str, str]:
    """Parse `a=1; b=2` / JSON / Set-Cookie lines into a dict."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        import json
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def parse_batch(text: str) -> list[tuple[str, dict]]:
    """Bulk import: one `label | cookie-string` per line; the label may be omitted."""
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # `|` inside a cookie is not a separator
                label, body = head.strip(), tail.strip()
        cookies = parse_cookie_text(body)
        if cookies:
            out.append((label, cookies))
    return out


# ------------------------- Core generation -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """After generation, read cookies back from the browser and write them into the account pool.

    Observed result: **a core cookie's expires is not renewed by usage** (hatch_vml is fixed
    to expire in about 2 days); what gets synced back here is mainly the non-core
    cookies (_fbp / wd / dpr etc.) reissued on every visit, plus any new entries, so account info does not go staler than reality.

    Note: this function **must never fail the generation result** - generation already succeeded,
    syncing is just a bonus; any problem is only logged.
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # Expiry must be "merged", not "overwritten": a session cookie read back has expires -1,
        # and overwriting wholesale would wipe the existing expiry records (learned this the hard way in testing).
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()))
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("Cookie sync failed (does not affect this generation): %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _generation_callback(callback, *args):
    if callback:
        try:
            callback(*args)
        except Exception as exc:
            log.warning("Task metadata callback failed: %s", type(exc).__name__)


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    on_acquired=None, on_account=None, on_queue=None, on_error=None) -> tuple[dict, str | None]:
    if not any(a.get("enabled", True) and a.get("cookies") for a in store.accounts):
        raise MuseGenerationError("No generation accounts available; please check the account pool")
    queue_timeout = QUEUE_POLICY.settings()["queue_timeout"]
    t0 = time.monotonic()
    acquired = GEN_LOCK.acquire(timeout=queue_timeout or None)
    queued = time.monotonic() - t0
    _generation_callback(on_queue, queued)
    if not acquired:
        raise MuseGenerationError("Queue time limit reached; generation has not started yet. Please retry later or adjust the standalone queue time limit")
    if (kind == "video" and not account_id and VIDEO_HEDGE and _overload_recent()
            and _idle_account_count() >= 2 and GEN_LOCK.acquire(blocking=False)):
        # Both slots now belong to the two parallel attempts, which release them.
        return _run_generation_hedged(prompt, kind, timeout, on_progress, reference_image,
                                      reference_images, on_acquired, on_account, on_queue,
                                      on_error, queued, queue_timeout)
    try:
        return _run_generation_locked(prompt, kind, timeout, account_id,
                                      on_progress, reference_image, reference_images,
                                      on_acquired=on_acquired, on_account=on_account,
                                      on_queue=on_queue, on_error=on_error,
                                      queued=queued, queue_timeout=queue_timeout)
    finally:
        GEN_LOCK.release()


def _idle_account_count() -> int:
    with ACCOUNT_POOL.cond:
        busy = set(ACCOUNT_POOL.busy_accounts)
    return sum(1 for a in store.accounts
               if a.get("enabled", True) and a.get("cookies") and a.get("id") not in busy)


def _hedge_error(errors: list[BaseException]) -> BaseException:
    """Report the failure that tells the caller the most."""
    real = [e for e in errors if not isinstance(e, MuseCancelledError)] or errors
    for kind in (MuseModelRefusalError, MuseServiceBusyError):
        for exc in real:
            if isinstance(exc, kind):
                return exc
    return real[0]


def _run_generation_hedged(prompt, kind, timeout, on_progress, reference_image, reference_images,
                           on_acquired, on_account, on_queue, on_error, queued, queue_timeout):
    """Run one video on two accounts at once; the first delivered video wins.

    The caller already holds two GEN_LOCK slots; each branch releases one.
    """
    results: "queue.Queue[tuple[int, bool, Any]]" = queue.Queue()
    stops = [threading.Event(), threading.Event()]
    once = threading.Lock()
    acquired_sent = [False]
    best_progress = [0]

    def acquired_once():
        with once:
            if acquired_sent[0]:
                return
            acquired_sent[0] = True
        _generation_callback(on_acquired)

    def progress_max(value):
        with once:
            if value <= best_progress[0]:
                return
            best_progress[0] = value
        _generation_callback(on_progress, value)

    def branch(index: int):
        try:
            res = _run_generation_locked(
                prompt, kind, timeout, None, progress_max, reference_image, reference_images,
                on_acquired=acquired_once, on_account=on_account,
                on_queue=on_queue if index == 0 else None,
                on_error=on_error, queued=queued if index == 0 else 0.0,
                queue_timeout=queue_timeout if index == 0 else VIDEO_HEDGE_LEASE_TIMEOUT,
                stop_event=stops[index])
            results.put((index, True, res))
        except BaseException as exc:  # noqa: BLE001 - reported to the waiting caller
            results.put((index, False, exc))
        finally:
            GEN_LOCK.release()

    log.warning("[Parallel generation] muse.ai was overloaded within the last %.0f minutes; this video is being generated on 2 accounts at once, first delivered video wins",
                VIDEO_HEDGE_WINDOW / 60)
    for index in range(2):
        threading.Thread(target=branch, args=(index,), daemon=True,
                         name=f"muse-hedge-{index}").start()
    errors: list[BaseException] = []
    for _ in range(2):
        index, ok, value = results.get()
        if ok:
            stops[1 - index].set()
            log.info("[Parallel generation] Attempt %d delivered first (account %s); the other attempt was cancelled", index + 1, value[1])
            return value
        errors.append(value)
    raise _hedge_error(errors)


def _run_generation_locked(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None,
                    reference_images: list[str] | None = None,
                    deadline=None, on_acquired=None, on_account=None,
                    on_queue=None, on_error=None, queued=0.0, queue_timeout=None,
                    stop_event: threading.Event | None = None) -> tuple[dict, str | None]:
    def cancelled() -> bool:
        return stop_event is not None and stop_event.is_set()

    if queue_timeout is None:
        queue_timeout = QUEUE_POLICY.settings()["queue_timeout"]

    def lease(**kwargs):
        nonlocal queued, deadline
        remaining = max(0.001, queue_timeout - queued) if queue_timeout else None
        t0 = time.monotonic()
        try:
            return ACCOUNT_POOL.acquire(timeout=remaining, **kwargs)
        finally:
            waited = time.monotonic() - t0
            queued += waited
            # Retry account waits also use the queue budget, never the generation budget.
            if deadline is not None:
                deadline += waited
            _generation_callback(on_queue, queued)

    cur_acc = lease(account_id=account_id, preferred_id=None, rotate=True)
    if deadline is None:
        deadline = time.monotonic() + max(1, timeout)
    last_exc = None
    try:
        if on_acquired:
            on_acquired()
        # Two attempts for ordinary failures; a temporary Muse media outage
        # (MuseServiceBusyError) earns up to BUSY_RETRIES extra attempts after a pause.
        attempt_limit, busy_used, attempt, first_busy = 2, 0, -1, None
        while True:
            attempt += 1
            busy = isinstance(last_exc, MuseServiceBusyError)
            if busy:
                first_busy = first_busy or last_exc
            if busy and busy_used < BUSY_RETRIES:
                busy_used += 1
                attempt_limit += 1
            if attempt >= attempt_limit:
                break
            if cancelled():
                raise MuseCancelledError("The other parallel attempt already delivered; this attempt was cancelled")
            # Preserve the original failure, including a refusal at the deadline boundary.
            if isinstance(last_exc, MuseModelRefusalError) or time.monotonic() >= deadline:
                if last_exc is not None:
                    break
                raise MuseGenerationError("Generation time limit reached (excluding queue time); stopped waiting")
            if busy:
                # Each Muse turn already retries internally for minutes; only start
                # another one when it can still finish inside the generation budget.
                if deadline - time.monotonic() < BUSY_RETRY_DELAY + BUSY_RETRY_MIN_REMAINING:
                    break
                log.warning("Muse media service temporarily unavailable; retrying in %.0f seconds (attempt %d)", BUSY_RETRY_DELAY, attempt + 1)
                if stop_event is not None:
                    if stop_event.wait(BUSY_RETRY_DELAY):
                        raise MuseCancelledError("The other parallel attempt already delivered; this attempt was cancelled")
                else:
                    time.sleep(BUSY_RETRY_DELAY)
            if attempt > 0:
                old_id = cur_acc["id"] if cur_acc else None
                if old_id:
                    ACCOUNT_POOL.release(old_id)
                cur_acc = None
                try:
                    alt = lease(rotate=True, force_rotate=True)
                except (MuseAuthError, MuseGenerationError):
                    raise last_exc
                cur_acc = alt
                # An outage is not tied to the account, so the same one may retry it.
                if not alt or (old_id and alt["id"] == old_id and not busy):
                    break
            _generation_callback(on_account, cur_acc["id"], attempt + 1)
            try:
                _bind_account_proxy(cur_acc)
                refreshed = _renew_and_persist(cur_acc["id"], wake_vm=True, force=(attempt > 0))
                if refreshed:
                    cur_acc = refreshed
                engine.start()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MuseGenerationError("Generation preparation timed out (excluding queue time); the prompt for this run has not been sent yet")
                refs = list(reference_images or [])
                engine_kwargs = {}
                if len(refs) == 1 and not reference_image:
                    reference_image = refs[0]
                elif len(refs) > 1:
                    engine_kwargs["reference_images"] = refs
                if stop_event is not None:
                    engine_kwargs["stop_event"] = stop_event
                res = engine.generate(cur_acc["cookies"], prompt, expect=kind,
                                      timeout=remaining, expires=cur_acc.get("cookies_exp"),
                                      account_id=cur_acc["id"], on_progress=on_progress,
                                      reference_image=reference_image, **engine_kwargs)
                store.mark(cur_acc["id"], True, "")
                _sync_cookies(cur_acc["id"])
                return res, cur_acc["id"]
            except Exception as exc:  # noqa: BLE001 - dispatched below
                if cancelled():
                    # Stopped on purpose: not an account fault and never retried.
                    raise MuseCancelledError("The other parallel attempt already delivered; this attempt was cancelled") from exc
                if isinstance(exc, MuseServiceBusyError):
                    _note_overload()
                last_exc = _record_generation_failure(cur_acc, exc)
            if on_error:
                on_error(str(last_exc))
        # A retry that only ran out of time says less than the outage that caused it.
        if first_busy is not None and not isinstance(last_exc, (MuseServiceBusyError, MuseModelRefusalError,
                                                                 MuseAuthError)):
            raise first_busy
        raise last_exc
    finally:
        if cur_acc:
            ACCOUNT_POOL.release(cur_acc.get("id"))
        engine.close_current_page()
        _clear_account_proxy()


def _record_generation_failure(cur_acc: dict, exc: Exception) -> Exception:
    """Book one failed attempt against its account and return the error to keep."""
    if isinstance(exc, MuseAuthError):
        store.mark(cur_acc["id"], False, str(exc))
        engine.close_current_page()
        return exc
    if isinstance(exc, MuseProxyError):
        store.touch_keepalive(cur_acc["id"], None, f"Proxy error: {str(exc)[:160]}")
        engine.close_current_page()
        return exc
    if isinstance(exc, MuseGenerationError):
        store.mark(cur_acc["id"], True, f"Task error: {str(exc)[:200]}")
        try:
            engine.reset_thread()
        except Exception:
            pass
        return exc
    engine.close_current_page()
    return MuseGenerationError(f"Generation failed: {exc}")


# ------------------------- Basic endpoints -------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}


@app.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": MODELS}


# ------------------------- Image generation -------------------------
def _image_response(req: ImageRequest, res: dict) -> dict:
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"]),
            "size": req.size or "auto", "kind": res["kind"], "bytes": res["size"]}
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        with open(fpath, "rb") as f:
            item["b64_json"] = base64.b64encode(f.read()).decode()
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _generic_upload_mime(mime: str) -> bool:
    """Common generic multipart upload types (identified by file header) and alias spellings."""
    return mime in ("application/octet-stream", "binary/octet-stream") or mime in (
        "image/jpg", "audio/mp3", "audio/x-wav", "audio/wave", "audio/x-m4a",
        "audio/m4a", "audio/x-flac", "audio/x-aac", "video/x-m4v")


def _prepare_reference_images(images: list[str]) -> list[str]:
    """Validate and snapshot all sources before a task is accepted."""
    prepared = []
    total_bytes = 0
    for index, image in enumerate(images):
        try:
            b64, mime = engine._normalize_image(image)
            data = base64.b64decode(b64, validate=True)
        except (RefImageSafetyError, MuseGenerationError, binascii.Error, ValueError) as exc:
            raise HTTPException(400, f"images[{index}]: {exc}") from exc
        total_bytes += len(data)
        if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
            raise HTTPException(
                400, f"Total reference size exceeds the {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MiB limit")
        prepared.append(f"data:{mime};base64,{b64}")
    return prepared


def _queue_image(req: ImageRequest, prompt: str, reference_images: list[str],
                 idempotency_key: str | None = None):
    """Opt-in polling avoids reverse-proxy timeouts; no generation is repeated by polling."""
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "references": reference_images},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with IMAGE_TASK_LOCK:
        tasks = list(store.tasks.values())
        if key_hash:
            for existing in tasks:
                if existing.get("image_request_key") == key_hash:
                    if existing.get("image_request_hash") != request_hash:
                        raise HTTPException(409, "Idempotency-Key reused with different request")
                    return JSONResponse(status_code=202, content={
                        "id": existing["id"], "task_id": existing["id"],
                        "object": "image.task", "status": existing["status"],
                        "progress": existing.get("progress", 0),
                        "created_at": existing["created_at"]})
        # ponytail: one Chromium worker; cap admission instead of adding a broker.
        if sum(t.get("kind") == "image" and t.get("status") in ("queued", "processing")
               for t in tasks) >= 8:
            raise HTTPException(429, "Image queue is full; retry later")
        task = store.create_task("image", req.prompt)
        tid = task["id"]
        store.update_task(tid, api_prompt=prompt, size=req.size,
                          response_format=req.response_format, progress=0, queue_elapsed=0.0,
                          image_request_key=key_hash, image_request_hash=request_hash)

    def worker():
        timing = TaskTiming(store, tid)
        def _on_acquired():
            timing.acquired()
            store.update_task(tid, progress=5)
        try:
            res, acc_id = _run_generation(
                prompt, "image", req.timeout or CFG.image_timeout,
                reference_images=reference_images,
                on_progress=lambda p: store.update_task(tid, progress=p),
                on_acquired=_on_acquired, on_account=timing.account,
                on_queue=timing.queued, on_error=timing.error)
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              **timing.finish(),
                              url=media_url(res["filename"]),
                              result={**{k: res[k] for k in ("filename", "size", "kind")},
                                      "url": media_url(res["filename"])})
        except Exception as exc:  # noqa: BLE001
            store.update_task(tid, status="failed", error=str(exc),
                              **timing.finish())

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse(status_code=202, content={
        "id": tid, "task_id": tid, "object": "image.task", "status": "queued",
        "progress": 0, "created_at": task["created_at"]})


@app.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: str | None = Header(default=None), _=Depends(auth)):
    refs = _prepare_reference_images(_image_references(req))
    return _queue_image(req, build_image_prompt(req, _reference_kinds(refs)), refs, idempotency_key)


@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "image task not found")
    out = dict(task)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"]))
    return out


@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(auth)):
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _image_references(req))

    prompt = build_image_prompt(req, _reference_kinds(ref_images))
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_images)
    try:
        res, _acc = await asyncio.to_thread(
            _run_generation, prompt, "image", timeout, reference_images=ref_images)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _image_response(req, res)


@app.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(auth)):
    """OpenAI-compatible image-to-image / image-editing endpoint, supporting multipart/form-data and application/json."""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    ref_image_data: list[str] = []
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form(
            max_files=MAX_REFERENCE_IMAGES, max_fields=20)
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        # image may be submitted repeatedly; video / audio / file may also repeat, ordered after image by field order
        img_fields = [(name, value) for name in ("image", "image[]", "video", "audio", "file")
                      for value in form.getlist(name)]
        if len(img_fields) > MAX_REFERENCE_IMAGES:
            raise HTTPException(400, f"At most {MAX_REFERENCE_IMAGES} reference items (currently {len(img_fields)})")
        total_upload_bytes = 0
        mib = 1024 * 1024
        for index, (name, img_field) in enumerate(img_fields):
            if img_field and hasattr(img_field, "read"):
                ref_mime = (getattr(img_field, "content_type", "") or "").split(";", 1)[0].strip().lower()
                if ref_mime and ref_mime not in REFERENCE_MIME_EXT and not _generic_upload_mime(ref_mime):
                    raise HTTPException(400, f"{name}[{index}] unsupported file type ({ref_mime})")
                content = await img_field.read(MAX_REFERENCE_IMAGE_BYTES + 1)
                if len(content) > MAX_REFERENCE_IMAGE_BYTES:
                    raise HTTPException(400, f"{name}[{index}] exceeds {MAX_REFERENCE_IMAGE_BYTES // mib} MiB limit")
                total_upload_bytes += len(content)
                if total_upload_bytes > MAX_REFERENCE_TOTAL_BYTES:
                    raise HTTPException(400, f"Total reference size exceeds {MAX_REFERENCE_TOTAL_BYTES // mib} MiB limit")
                ref_mime = ref_mime or "application/octet-stream"
                ref_image_data.append(
                    f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}")
            elif isinstance(img_field, str) and img_field.strip():
                ref_image_data.append(img_field.strip())
            else:
                raise HTTPException(400, f"{name}[{index}] is not a valid reference")
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        raw_images = body.get("images")
        if raw_images is not None and not isinstance(raw_images, list):
            raise HTTPException(400, "images must be an array")
        legacy = [body.get("reference_image"), body.get("image_url"), body.get("image")]
        ref_image_data = _reference_list(raw_images, legacy, "images", {
            name: body.get(name) for name in ("videos", "audios", "files")})

    if not prompt:
        prompt = "Use this image as a reference and create an image from it"

    req_obj = ImageRequest(
        prompt=prompt,
        model=model,
        size=size,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        images=ref_image_data,
        **{"async": async_mode}
    )
    ref_image_data = await asyncio.to_thread(
        _prepare_reference_images, ref_image_data)
    full_prompt = build_image_prompt(req_obj, _reference_kinds(ref_image_data))
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data)
    try:
        res, _acc = await asyncio.to_thread(
            _run_generation, full_prompt, "image", gen_timeout,
            reference_images=ref_image_data)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- Video generation (async task) -------------------------
@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def create_video(req: VideoRequest, _=Depends(auth)):
    ref_images = await asyncio.to_thread(
        _prepare_reference_images, _video_references(req))

    prompt = build_video_prompt(req, _reference_kinds(ref_images))
    timeout = req.timeout or CFG.video_timeout
    task = store.create_task("video", req.prompt)
    store.update_task(task["id"], api_prompt=prompt, queue_elapsed=0.0)

    store.update_task(task["id"], progress=10)

    def worker():
        timing = TaskTiming(store, task["id"])
        def _on_acquired():
            timing.acquired()
            store.update_task(task["id"], progress=15)
        try:
            def prog_cb(p):
                store.update_task(task["id"], progress=p)
            res, acc_id = _run_generation(prompt, "video", timeout, on_progress=prog_cb,
                                          reference_images=ref_images, on_acquired=_on_acquired, on_account=timing.account,
                on_queue=timing.queued, on_error=timing.error)
            vurl = media_url(res["filename"])
            store.update_task(task["id"], status="completed", progress=100, account=acc_id,
                              **timing.finish(),
                              url=vurl,
                              video={"url": vurl},
                              result={"url": vurl,
                                      "filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"]})
        except Exception as exc:  # noqa: BLE001
            store.update_task(task["id"], status="failed",
                              **timing.finish(), error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"id": task["id"], "task_id": task["id"], "object": "video.task", "status": "queued",
            "progress": 10, "created_at": task["created_at"]}


@app.get("/v1/videos/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    out = dict(t)
    status = out.get("status")
    if status in ("succeeded", "success", "done"):
        out["status"] = "completed"
    if out.get("status") == "completed":
        out["progress"] = 100
    elif out.get("status") == "processing":
        # Progress reports observed milestones only; Muse exposes no percentage.
        out["progress"] = max(out.get("progress", 0) or 0, 15)
    vurl = out.get("url")
    if not vurl and isinstance(out.get("result"), dict):
        vurl = out["result"].get("url")
    if vurl:
        out["url"] = vurl
        if "video" not in out:
            out["video"] = {"url": vurl}
    return out


# ------------------------- Chat (OpenAI-compatible) -------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chat_chunk(cid: str, created: int, model: str, delta: dict,
                finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _want_usage(stream_options) -> bool:
    """Downstream clients (LangChain, some SDKs / agent frameworks) may send
    `stream_options.include_usage=true`, requiring an extra chunk before [DONE] with
    `choices: []` + `usage`. Without it, some frameworks keep waiting for usage and hang."""
    if isinstance(stream_options, dict):
        return bool(stream_options.get("include_usage"))
    return False


def _pace_text(text: str, chunk_size: int = 2, delay: float = 0.012):
    """Split large text smoothly into tiny streaming tokens that mimic a native LLM typewriter.
    If the input text itself is tiny (<= 3 chars), emit it instantly with zero delay;
    If the input is a large chunk (e.g. a DOM batch refresh), emit it smoothly every chunk_size chars with a delay.
    """
    if not text:
        return
    if len(text) <= 3 or delay <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if i + chunk_size < len(text):
            time.sleep(delay)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):
    """OpenAI-compatible chat endpoint — any agent client can connect.

    Note: the muse.ai web app is an **auto-routed** agent with no model picker in the UI; all requests
    go to the same web assistant (calls itself Koda, powered by Muse-series language models). So
    the `model` field is only for downstream compatibility and does not affect routing.

    When a request includes `tools`, the [tool-call protocol] is injected and the model's JSON output is parsed back into
    `tool_calls` (muse.ai has no native function calling; this layer is protocol adaptation).

    Chat shares one browser instance with image/video generation, serialized via `GEN_LOCK`.
    """
    prompt = build_chat_prompt(req.messages) or (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "messages is empty")

    # The tool-call protocol is not injected by default — in practice the muse.ai assistant explicitly refuses to output "pseudo tool calls",
    # and injecting it pollutes normal replies instead; see the tool_protocol comment in config.py.
    tool_note = build_tools_prompt(req.tools) if CFG.tool_protocol else ""
    if tool_note:
        # Placed at the end: the start holds the agent's own system prompt, and anything in the middle is easily ignored;
        # right before the user message, the model follows trailing instructions much more reliably.
        prompt = prompt + "\n\n" + tool_note

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "No account available; import cookies on the admin page first")

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id = acc["id"]
    cookies = acc["cookies"]
    expires = acc.get("cookies_exp")

    def tool_call_deltas(calls: list[dict]) -> list[list[dict]]:
        """Send in two chunks per OpenAI convention: id/name first, then the full arguments."""
        head = [{"index": i, "id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": ""}}
                for i, c in enumerate(calls)]
        body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                for i, c in enumerate(calls)]
        return [head, body]

    if req.stream:
        def sync_stream():
            try:
                # Send the role: assistant first packet the moment the handshake is established, so downstream clients capture the cursor within seconds
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))

                stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)
                if not tool_note:
                    for chunk in stream_gen:
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                    yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                else:
                    buf, mode = "", None
                    for chunk in stream_gen:
                        if mode == "text":
                            for piece in _pace_text(chunk):
                                yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            continue
                        buf += chunk
                        if mode is None:
                            head = buf.lstrip()
                            if len(head) >= 2:
                                if head[0] in "{[" or head.startswith("```"):
                                    mode = "maybe_tool"
                                else:
                                    mode = "text"
                                    for piece in _pace_text(buf):
                                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                                    buf = ""
                    if mode == "text":
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                    else:
                        calls, rest = parse_tool_calls(buf)
                        if calls:
                            if rest:
                                for piece in _pace_text(rest):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            for piece in tool_call_deltas(calls):
                                yield _sse(_chat_chunk(cid, created, model, {"tool_calls": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="tool_calls"))
                        else:
                            if buf:
                                for piece in _pace_text(buf):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                yield "data: [DONE]\n\n"
                store.mark(acc_id, True, "")
            except MuseAuthError as exc:
                store.mark(acc_id, False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except MuseGenerationError as exc:
                store.mark(acc_id, True, f"Assistant timed out: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            except Exception as exc:
                yield _sse({"error": {"message": f"Internal error: {exc}", "type": "server_error", "code": 500}})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": rest or None,
                                             "tool_calls": calls}}],
                    "usage": usage}
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage}


def _responses_messages(req: ResponsesRequest) -> list[ChatMessage]:
    """Normalize the Responses API instructions / input into messages."""
    msgs: list[ChatMessage] = []
    if req.instructions:
        msgs.append(ChatMessage(role="system", content=req.instructions))
    inp = req.input
    if isinstance(inp, str):
        msgs.append(ChatMessage(role="user", content=inp))
        return msgs
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if item.get("role"):                      # message shape
                msgs.append(ChatMessage(role=item["role"],
                                        content=item.get("content")))
            elif itype == "input_text":
                msgs.append(ChatMessage(role="user", content=item.get("text")))
            elif itype == "function_call_output":
                msgs.append(ChatMessage(role="user", content="[Tool execution result]\n"
                                        + str(item.get("output") or "")))
            elif itype == "function_call":
                msgs.append(ChatMessage(role="assistant", content="[Tool call requested] "
                                        + str(item.get("name") or "")))
    return msgs


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/v1/responses")
async def responses_api(req: ResponsesRequest, _=Depends(auth)):
    """OpenAI Responses API — the newer Codex defaults to this endpoint.

    Only the subset Codex actually uses is implemented: text in → text out (including streaming events).
    muse.ai never returns structured tool_calls; see the README for the tool-call boundaries.
    """
    prompt = build_chat_prompt(_responses_messages(req))
    if not prompt:
        raise HTTPException(400, "input is empty")

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "No account available; import cookies on the admin page first")

    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id, cookies, expires = acc["id"], acc["cookies"], acc.get("cookies_exp")

    def envelope(status: str, text: str = "") -> dict:
        done = status == "completed"
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model,
                "output": [{"type": "message", "id": mid, "role": "assistant",
                            "status": "completed" if done else "in_progress",
                            "content": ([{"type": "output_text", "text": text,
                                          "annotations": []}] if text else [])}],
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0}}

    if req.stream:
        def sync_stream():
            try:
                yield _sse_event("response.created", {
                    "type": "response.created",
                    "response": envelope("in_progress")})
                yield _sse_event("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []}})
                yield _sse_event("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": mid,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "",
                             "annotations": []}})
                full = ""
                for chunk in safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id):
                    full += chunk
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": mid,
                        "output_index": 0, "content_index": 0, "delta": chunk})
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": mid,
                    "output_index": 0, "content_index": 0, "text": full})
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": full,
                                          "annotations": []}]}})
                yield _sse_event("response.completed", {
                    "type": "response.completed",
                    "response": envelope("completed", full)})
            except Exception as exc:  # noqa: BLE001
                log.warning("responses streaming failed: %s", exc)
                yield _sse_event("response.failed", {
                    "type": "response.failed",
                    "response": envelope("failed")})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    return envelope("completed", text)


@app.get("/v1/media/{name}")
def get_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Invalid filename")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    return FileResponse(p)


@app.get("/admin/generation-queue")
def get_generation_queue(_=Depends(auth)):
    return QUEUE_POLICY.settings()

@app.put("/admin/generation-queue")
def save_generation_queue(req: QueueSettings, _=Depends(auth)):
    QUEUE_POLICY.save(req)
    return QUEUE_POLICY.settings()

@app.get("/v1/media/{name}/thumbnail")
def get_media_thumbnail(name: str):
    try:
        path = video_thumbnail(CFG.media_dir, os.path.join(CFG.data_dir, "thumbnails"), name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except ThumbnailUnavailable:
        raise HTTPException(503, "Video thumbnail temporarily unavailable")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


def _clear_media_thumbnail(name: str):
    try:
        clear_thumbnail(os.path.join(CFG.data_dir, "thumbnails"), name)
    except OSError:
        log.warning("Could not remove cached thumbnail for %s", name)


# ------------------------- Admin: overview -------------------------
@app.get("/admin/status")
def admin_status(_=Depends(auth)):
    base = _public_base()
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "generation_queue": QUEUE_POLICY.settings(),
        "tasks": store.list_tasks(20),
        "media_count": len(os.listdir(CFG.media_dir))
        if os.path.isdir(CFG.media_dir) else 0,
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "base_url": f"{base}/v1" if base else "",
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


# ------------------------- Admin: account pool -------------------------
@app.get("/admin/accounts")
def list_accounts(_=Depends(auth)):
    return {"accounts": store.list_accounts(), "stats": store.stats()}


@app.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(auth)):
    proxy = req.proxy.configuration() if req.proxy else None
    added: list[dict] = []
    seen: list[dict] = []          # all cookies imported this time, used to check whether the essential items are complete

    if req.batch:
        for label, cookies in parse_batch(req.batch):
            acc = store.add_account(cookies, label)
            seen.append(cookies)
            added.append({"id": acc["id"], "label": acc["label"],
                          "cookie_count": len(cookies),
                          "expires_at": acc.get("expires_at")})

    cookies = dict(req.cookies)
    if req.cookie_header:
        cookies.update(parse_cookie_text(req.cookie_header))
    if cookies:
        acc = store.add_account(cookies, req.label, cookies_exp=req.expires)
        seen.append(cookies)
        added.append({"id": acc["id"], "label": acc["label"],
                      "cookie_count": len(cookies),
                      "expires_at": acc.get("expires_at")})

    if not added:
        raise HTTPException(400, "No cookies parsed; check the format")

    if proxy:
        for account in added:
            store.update_account(account["id"], proxy=proxy)

    # Passes if any one account has all essential cookies (judged as a whole for batch imports)
    missing = [n for n in ESSENTIAL_COOKIES
               if not any(n in c for c in seen)]
    return {"added": added, "count": len(added),
            "essential_missing": missing,
            "warning": (f"Missing essential cookies: {', '.join(missing)}; this account may not be able to generate"
                        if missing else "")}


# One short-lived receipt per account; credentials are represented only by a hash.
_PROXY_TEST_RECEIPTS: dict[str, dict] = {}
_PROXY_TEST_RECEIPT_TTL = 300


def _proxy_test_identity(proxy: dict, cookies: dict | None) -> str:
    value = json.dumps({"proxy": proxy, "cookies": cookies or {}},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    with ACCOUNT_POOL.cond:
        existing = store.get_account(aid)
        if not existing:
            raise HTTPException(404, "Account not found")
        changes = {"label": req.label, "enabled": req.enabled}
        if "proxy" in req.model_fields_set:
            if aid in ACCOUNT_POOL.busy_accounts:
                raise HTTPException(409, "Account is running a task or check; try changing the proxy later")
            proxy = req.proxy.configuration(existing.get("proxy")) if req.proxy else {}
            if proxy != (existing.get("proxy") or {}):
                changes.update(proxy=proxy, synced_at=0, last_keepalive=0,
                               checked_at=0, proxy_status={}, ok=None,
                               note="Proxy changed; please test again")
                receipt = _PROXY_TEST_RECEIPTS.get(aid)
                if (proxy and receipt and 0 <= time.monotonic() - receipt["at"] <= _PROXY_TEST_RECEIPT_TTL
                        and receipt["identity"] == _proxy_test_identity(proxy, existing.get("cookies"))):
                    changes["proxy_status"] = dict(receipt["result"])
                    changes["note"] = "Proxy settings saved; account browser status still needs testing"
                engine._last_http_renew.pop(aid, None)
            _PROXY_TEST_RECEIPTS.pop(aid, None)
        acc = store.update_account(aid, **changes)
        return store.public_account(acc)


@app.post("/admin/accounts/{aid}/proxy/test")
def test_account_proxy(aid: str, req: ProxyTestRequest | None = None, _=Depends(auth)):
    import ipaddress
    import requests
    with _account_operation(aid) as acc:
        proxy = req.proxy.configuration(acc.get("proxy")) if req is not None else _account_proxy(acc)
        if not proxy:
            raise HTTPException(400, "Enter a proxy link or save this account's proxy first")
        identity = _proxy_test_identity(proxy, acc.get("cookies"))
        result = {"ok": False, "proxy_configured": True, "proxy_reachable": False,
                  "exit_ip": None, "session_ok": None, "workspace_status": None}
        try:
            # Fixed HTTPS endpoint receives no Muse cookies or proxy credentials.
            with request_proxy_context(proxy) as proxy_options, requests.Session() as session:
                session.trust_env = False
                response = session.get("https://api.ipify.org?format=json",
                                       timeout=(5, 10), allow_redirects=False,
                                       **proxy_options)
            if response.status_code != 200:
                raise MuseProxyError("Proxy exit check failed; check the proxy connection or credentials")
            result["exit_ip"] = str(ipaddress.ip_address(response.json()["ip"]))
            result["proxy_reachable"] = True
            checked = engine.renew_session_http(acc.get("cookies") or {}, acc.get("cookies_exp"),
                                                wake_vm=False, proxy=proxy)
            result.update(ok=True, session_ok=True, workspace_status=checked.get("status"),
                          message="Proxy exit and Muse session checks passed; verify generation capability with the account test and real tasks")
        except MuseAuthError:
            result.update(session_ok=False, message="Proxy is reachable, but Muse session authentication failed; please re-import cookies")
        except MuseProxyError as exc:
            result["message"] = str(exc)
        except MuseSessionUnavailable as exc:
            _persist_session_cookies(aid, exc.cookies, exc.cookies_exp)
            result.update(session_ok=False, message=str(exc))
        except MuseGenerationError as exc:
            result.update(session_ok=False, message=str(exc))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            result["message"] = "Proxy exit check failed; check the proxy address, credentials, and network"
        recorded = {**result, "checked_at": int(time.time())}
        tested_saved = False
        with ACCOUNT_POOL.cond:
            current = store.get_account(aid)
            if current and identity == _proxy_test_identity(proxy, current.get("cookies")):
                # Unsaved routes stay only in memory; saving the exact configuration may adopt this proof.
                _PROXY_TEST_RECEIPTS[aid] = {"identity": identity, "at": time.monotonic(),
                                           "result": dict(recorded)}
                tested_saved = proxy == _account_proxy(current)
                if tested_saved:
                    store.update_account(aid, proxy_status=dict(recorded))
        result["tested_saved_proxy"] = tested_saved
        return result


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "Account not found")
    with ACCOUNT_POOL.cond:
        _PROXY_TEST_RECEIPTS.pop(aid, None)
    return {"deleted": True, "id": aid}


@app.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(auth)):
    """Actually open muse.ai to verify whether this account's cookies are still logged in."""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc.get("cookies"):
        raise HTTPException(400, "This account has no cookies")

    def _probe():
        with GEN_LOCK, _account_operation(aid) as acc:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
                synced = _sync_cookies(aid)
                quota = None
                quota_error = None
                try:  # Session validity and quota readability are independent results.
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"), account_id=aid)
                    if quota.get("found"):
                        quota["checked_at"] = int(time.time())
                        store.update_account(aid, quota=quota)
                    else:
                        quota_error = "Quota panel returned no valid data; old cache not overwritten"
                        quota = None
                except Exception as exc:  # noqa: BLE001
                    quota_error = f"{type(exc).__name__}; quota cache not updated"
                store.mark(aid, True, "Session valid" + ("; quota read failed" if quota_error else ""))
                return {"ok": True, "message": "Session valid; generation works normally",
                        "synced": synced, "quota": quota,
                        "quota_error": quota_error}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"Test unconfirmed (account status kept): {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@app.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(auth)):
    return await test_account(aid, _)


@app.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(auth)):
    """Update an account's cookies (to add fresh cookies after a session expires)."""
    cookies = dict(payload.get("cookies") or {})
    if payload.get("cookie_header"):
        cookies.update(parse_cookie_text(payload["cookie_header"]))
    if not cookies:
        raise HTTPException(400, "No cookies parsed")
    exp = {k: int(v) for k, v in (payload.get("expires") or {}).items()
           if _pos(v)}
    # The added cookies belong to a "brand-new session", so the expiry-estimate anchor must be reset to now;
    # otherwise the old session's anchor would be reused and the remaining days underestimated.
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="Cookies updated",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "Account not found")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@app.post("/admin/relogin")
def relogin(_=Depends(auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "No account available")
    try:
        with GEN_LOCK, _account_operation(acc["id"]) as selected:
            engine.start()
            engine.refresh(selected["cookies"], selected.get("cookies_exp"), account_id=selected["id"])
            return {"ok": True, "account": selected["id"]}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ------------------------- Admin: quota -------------------------
@app.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(auth)):
    """Open the muse.ai Settings panel to read this account's quota (live) and cache it in the account record.

    Example return: {"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc.get("cookies"):
        raise HTTPException(400, "This account has no cookies")

    def _probe():
        with GEN_LOCK, _account_operation(aid) as acc:
            engine.start()
            q = engine.quota(acc["cookies"], acc.get("cookies_exp"), account_id=aid)
            if not q.get("found"):
                raise MuseGenerationError("Quota panel returned no valid data; old cache not overwritten")
            q["checked_at"] = int(time.time())
            store.update_account(aid, quota=q)
            return q

    try:
        return await asyncio.to_thread(_probe)
    except HTTPException:
        raise
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Quota query failed: {exc}") from exc


@app.post("/admin/quota")
async def query_any_quota(_=Depends(auth)):
    """Check quota once with the least-recently-used available account (accounts in the same pool share one muse.ai plan;
    usually enough when only one person uses it; with multiple accounts, check per account)."""
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "No account available")
    return await query_quota(acc["id"], _)


# ------------------------- Admin: access info / API Key -------------------------
def _public_base() -> str:
    return (CFG.public_base or "").rstrip("/")


@app.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    """Generate a new API Key, write it to.env, and take effect immediately (no restart needed)."""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    old = CFG.api_key
    CFG.api_key = new_key
    _persist_env("MUSE2API_KEY", new_key)
    return {"ok": True, "api_key": new_key, "previous": old,
            "message": "New key generated and effective immediately; the old key is invalid — update downstream projects"}


def _persist_env(key: str, value: str):
    """Write the config back to.env (keeping other lines, atomic replace)."""
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.replace(tmp, path)


# ------------------------- Admin: cookie retrieval -------------------------
@app.get("/admin/cookie-helper")
def cookie_helper(download: int = 0):
    """Return the helper script for fetching cookies on this machine (advanced method; requires Python)."""
    p = os.path.join(BASE_DIR, "tools", "get_muse_cookie.py")
    if not os.path.isfile(p):
        raise HTTPException(404, "Helper script missing")
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="get_muse_cookie.py"'
    return FileResponse(p, media_type="text/x-python", headers=headers)


@app.get("/admin/extension")
def extension_zip():
    """Package the browser extension as a zip and return it (recommended method, no command line).

    After downloading, unzip it → load it in chrome://extensions developer mode → click once to import cookies.
    """
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "Extension directory missing")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, names in os.walk(src):
            dirs.sort()
            for name in sorted(names):
                p = os.path.join(root, name)
                relative_path = os.path.relpath(p, src).replace(os.sep, "/")
                z.write(p, f"muse2api-extension/{relative_path}")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="muse2api-extension.zip"',
                 "Cache-Control": "no-store"})


@app.get("/admin/extension/files")
def extension_files():
    """List the extension directory contents (for frontend display)."""
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "Extension directory missing")
    files = []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        for name in sorted(names):
            p = os.path.join(root, name)
            files.append(os.path.relpath(p, src).replace(os.sep, "/"))
    return {"files": sorted(files)}


# ------------------------- Admin: tasks / media -------------------------
@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    return {"tasks": store.list_tasks(limit)}


@app.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(auth)):
    if not store.delete_task(tid):
        raise HTTPException(404, "Task not found")
    return {"deleted": True}


@app.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(auth)):
    return {"removed": store.clear_tasks(int(payload.get("keep") or 0))}


@app.get("/admin/media")
def admin_media(_=Depends(auth)):
    d = CFG.media_dir
    items = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            stat = os.stat(p)
            is_video = ext in VIDEO_FORMATS
            item = {"name": name, "url": media_url(name), "bytes": stat.st_size,
                    "mtime": int(stat.st_mtime), "kind": "video" if is_video else "image"}
            if is_video:
                from urllib.parse import quote
                item["thumbnail_url"] = f"/v1/media/{quote(name, safe='')}/thumbnail?v={source_version(stat)}"
            items.append(item)
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"media": items, "count": len(items)}


@app.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names must be an array of filenames")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
            errors.append(f"Invalid filename: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                _clear_media_thumbnail(name)
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@app.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(auth)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Invalid filename")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    try:
        os.remove(p)
        _clear_media_thumbnail(name)
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"Delete failed: {e}")


# ------------------------- Frontend pages -------------------------
def _admin_html() -> str:
    p = os.path.join(BASE_DIR, "admin.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8><body style='background:#0b0f19;"
                "color:#e6e8ee;font-family:system-ui;padding:40px'>"
                "<h2>muse2api</h2><p>Admin page file admin.html is missing.</p>"
                "<p>Available endpoints: <code>/v1/images/generations</code>, "
                "<code>/v1/videos</code></p></body>")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_admin_html(), headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(_admin_html(), headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})



# ------------------------- Automatic account keepalive and silent renewal -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "next_run": None,
}


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """Verify the upstream session through this account's saved route."""
    try:
        with _account_operation(aid) as acc:
            if not acc.get("cookies"):
                return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": "Account has no valid cookies"}
            res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=True,
                                            **({"proxy": _account_proxy(acc)} if _account_proxy(acc) else {}))
            store.update_account(
                aid,
                cookies=res["cookies"],
                cookies_exp=res["cookies_exp"],
                ok=True if res.get("ok") else False,
                synced_at=int(time.time()),
            )
            vm_state = res.get("vm_state") or "RUNNING"
            store.touch_keepalive(aid, True, f"Session valid · auto keepalive (VM: {vm_state})")
            quota = acc.get("quota")
            if check_quota and GEN_LOCK.acquire(blocking=False):
                try:
                    engine.start()
                    quota = engine.quota(res["cookies"], res["cookies_exp"],
                                         account_id=aid)
                    if not quota.get("found"):
                        raise MuseGenerationError("Quota panel returned no valid data; old cache not overwritten")
                    quota["checked_at"] = int(time.time())
                    store.update_account(aid, quota=quota)
                except Exception as qe:  # noqa: BLE001
                    log.warning("Failed to read quota for account %s: %s", aid, qe)
                finally:
                    GEN_LOCK.release()
            updated = store.get_account(aid) or {}
            return {
                "ok": True,
                "id": aid,
                "label": acc.get("label", aid),
                "vm_id": res.get("vm_id"),
                "vm_state": vm_state,
                "wake_ok": res.get("wake_ok"),
                "expires_at": updated.get("expires_at"),
                "quota": quota,
            }
    except HTTPException as exc:
        return {"ok": False, "id": aid, "busy": exc.status_code == 409, "error": str(exc.detail)}
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, f"Keepalive authentication failed: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc)}
    except MuseSessionUnavailable as exc:
        _persist_session_cookies(aid, exc.cookies, exc.cookies_exp)
        store.touch_keepalive(aid, None, f"Keepalive unconfirmed (account status kept): {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc), "cookies_rotated": exc.rotated}
    except Exception as exc:
        store.touch_keepalive(aid, None, f"Keepalive unconfirmed (account status kept): {str(exc)[:200]}")
        return {"ok": False, "id": aid, "error": str(exc)}


async def run_keepalive_all(force: bool = False) -> dict:
    """Run account keepalive: with force=True, force-refresh all enabled accounts; otherwise refresh expiring/unchecked accounts and keep the VM warm."""
    async with KEEPALIVE_LOCK:
        now = int(time.time())
        KEEPALIVE_STATE["running"] = True
        KEEPALIVE_STATE["last_run"] = now
        results = []
        skipped = []

        try:
            include_disabled = getattr(CFG, "keepalive_disabled_accounts", False)
            accounts = [a for a in store.list_accounts() if include_disabled or a.get("enabled", True)]
            for a in accounts:
                aid = a["id"]
                label = a.get("label", aid)
                exp_at = a.get("expires_at")
                last_ka = a.get("last_keepalive") or 0

                needs_run = (
                    force
                    or not last_ka
                    or ((now - last_ka) > 6 * 3600 and
                        (not exp_at or exp_at - now < 36 * 3600 or
                         a.get("ok") is not True))
                )
                if not needs_run:
                    skipped.append({"id": aid, "label": label, "reason": "Session is sufficient and was recently kept alive"})
                    continue

                log.info("[Auto keepalive] Silently renewing and waking the VM for account %s (%s)...", label, aid)
                res = await asyncio.to_thread(_probe_account_sync, aid, False)
                results.append(res)
                log.info("[Auto keepalive] Account %s result: ok=%s expires_at=%s", label, res.get("ok"), res.get("expires_at"))
                await asyncio.sleep(0.5)

            summary = {
                "checked_at": now,
                "refreshed_count": len(results),
                "refreshed": results,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
            KEEPALIVE_STATE["last_result"] = summary
            KEEPALIVE_STATE["next_run"] = now + 900
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


def _warmup_browser_sync():
    """Silently warm up the browser and the first available account's WebSocket tunnel in the background, so the first request after a restart also returns instantly."""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    if not GEN_LOCK.acquire(blocking=False):
        return
    acc = None
    try:
        acc = ACCOUNT_POOL.acquire(rotate=False, timeout=0.1, record_use=False)
        log.info("[Browser warmup] Warming up the standby tab for account %s (%s) in the background...", acc.get("label"), acc["id"])
        _bind_account_proxy(acc)
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("[Browser warmup] Standby tab and WebSocket ready for account %s (%s)", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("[Browser warmup] Warmup error: %s", exc)
    finally:
        # Warmup runs in a worker thread; do not retain its account reservation or page.
        engine.close_current_page()
        _clear_account_proxy()
        if acc:
            ACCOUNT_POOL.release(acc["id"])
        GEN_LOCK.release()


async def _keepalive_loop():
    """Long-running background daemon: poll account health every 15 minutes and keep the VM warm, while silently syncing repo updates."""
    log.info("[Auto-keepalive daemon] Started; check interval: 15 minutes")
    await asyncio.sleep(1)
    try:
        await asyncio.to_thread(_check_update_sync, True)
    except Exception:
        pass
    try:
        await asyncio.to_thread(_warmup_browser_sync)
    except Exception as e:  # noqa: BLE001
        log.warning("[Browser warmup] Error: %s", e)
    while True:
        try:
            await asyncio.to_thread(_check_update_sync, True)
        except Exception:
            pass
        try:
            await run_keepalive_all(force=False)
        except Exception as e:  # noqa: BLE001
            log.error("[Auto-keepalive daemon] Polling error: %s", e)
        await asyncio.sleep(900)


@app.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(auth)):
    """Admin manually triggers a keepalive renewal for all accounts."""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "Keepalive task is already running; please wait"}
    return await run_keepalive_all(force=force)


@app.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(auth)):
    """Manually run keepalive renewal for a single account."""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@app.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(auth)):
    """Get the keepalive daemon coroutine status."""
    return KEEPALIVE_STATE


# ------------------------- Real-time repo update detection, notifications, and one-click online upgrade -------------------------
REPO_SLUG = os.environ.get("MUSE2API_REPOSITORY", "czg86389-hub/muse2api")
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", REPO_SLUG):
    raise ValueError("Invalid MUSE2API_REPOSITORY")
REPO_REF = os.environ.get("MUSE2API_REPO_REF", "main")
REPO_URL = f"https://github.com/{REPO_SLUG}"
MANAGED_DEPLOYMENT = os.environ.get("MUSE2API_MANAGED_DEPLOYMENT", "0") == "1"
TRACKED_REPO_PATHS = [
    "app.py", "engine.py", "store.py", "cdp.py", "config.py", "proxy.py",
    "generation_timing.py", "thumbnails.py", "GENERATION_QUEUE.md", "VERSION_REPORTING.md",
    "admin.html", "README.md", "version.json", "requirements.txt",
    "Dockerfile", "docker-compose.yml", ".env.example", ".gitignore",
    "LICENSE", "extension", "deploy", "tools",
]
_UPDATE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _read_env_key(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                s = ln.strip()
                if s.startswith(key + "="):
                    return s.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _read_local_version() -> dict:
    p = os.path.join(BASE_DIR, "version.json")
    try:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {"version": "1.5.0", "highlights": []}


def _installed_sha_file() -> str:
    return os.path.join(CFG.data_dir, ".installed_sha")


def _git(args: list[str], timeout: int = 30):
    import subprocess
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-c", f"safe.directory={BASE_DIR}", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ensure_git_repo(token: str = ""):
    """Ensure BASE_DIR is initialized as a Git repo bound to czg86389-hub/muse2api."""
    git_dir = os.path.join(BASE_DIR, ".git")
    remote_url = f"https://x-access-token:{token}@github.com/{REPO_SLUG}.git" if token else f"{REPO_URL}.git"
    if not os.path.isdir(git_dir):
        _git(["init", "-b", "main"])
        _git(["remote", "add", "origin", remote_url])
    else:
        _git(["remote", "set-url", "origin", remote_url])
    _git(["config", "user.name", "czg86389-hub"])
    _git(["config", "user.email", "czg86389-hub@users.noreply.github.com"])


def _check_update_sync(force: bool = False, _auto_upgrade: bool = False) -> dict:
    """A check is read-only. Report installed files and remote state separately."""
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and now - _UPDATE_CACHE["ts"] < 90:
        return _UPDATE_CACHE["data"]
    import requests
    from urllib.parse import quote
    local = _read_local_version()
    local_version = str(local.get("version") or "unknown")
    local_sha = os.environ.get("MUSE2API_BUILD_SHA", "").strip()
    has_git = os.path.exists(os.path.join(BASE_DIR, ".git"))
    if not local_sha and has_git:
        result = _git(["rev-parse", "HEAD"])
        if result.returncode == 0:
            local_sha = result.stdout.strip()
    if not local_sha and os.path.isfile(_installed_sha_file()):
        with open(_installed_sha_file()) as f:
            local_sha = f.read().strip()
    local_sha = local_sha[:7]
    local_dirty = False
    if has_git:
        changed = _git(["status", "--porcelain", "--", *TRACKED_REPO_PATHS])
        local_dirty = changed.returncode == 0 and bool(changed.stdout.strip())
    remote_version, remote_sha, remote_commit, remote_msg, remote_time = "", "", "", "", ""
    commits, highlights, highlights_en, errors = [], [], [], []
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "muse2api-updater"}
    token = _read_env_key("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(
            f"https://api.github.com/repos/{REPO_SLUG}/commits",
            params={"sha": REPO_REF, "per_page": 5}, headers=headers, timeout=6)
        response.raise_for_status()
        raw_commits = response.json()
        remote_commit = raw_commits[0]["sha"] if raw_commits else ""
        for commit in raw_commits:
            info = commit.get("commit") or {}
            commits.append({"sha": commit["sha"][:7], "message": info.get("message", "").split("\n")[0],
                            "date": (info.get("committer") or {}).get("date", ""),
                            "url": commit.get("html_url") or REPO_URL})
        if commits:
            remote_sha, remote_msg, remote_time = (commits[0][k] for k in ("sha", "message", "date"))
    except Exception:
        errors.append("Failed to read remote commits")
    # Pin release notes and version to the same commit as the reported remote SHA.
    try:
        response = requests.get(
            f"https://raw.githubusercontent.com/{REPO_SLUG}/{quote(remote_commit or REPO_REF, safe='')}/version.json",
            timeout=6)
        response.raise_for_status()
        remote = response.json()
        remote_version = str(remote.get("version") or "")
        highlights = list(remote.get("highlights") or [])
        highlights_en = list(remote.get("highlights_en") or [])
    except Exception:
        errors.append("Failed to read remote version file")
    verified = bool(local_sha and remote_sha and remote_version)
    has_update = bool((local_sha and remote_sha and local_sha != remote_sha) or
                      (remote_version and local_version != remote_version))
    data = {"repo_url": REPO_URL, "repo_ref": REPO_REF, "has_git": has_git,
            "local_version": local_version, "local_sha": local_sha,
            "remote_version": remote_version, "remote_sha": remote_sha, "remote_commit": remote_commit,
            "remote_msg": remote_msg, "remote_time": remote_time,
            "has_update": has_update, "up_to_date": (not has_update and not local_dirty) if verified else None,
            "local_dirty": local_dirty,
            "check_error": "; ".join(errors), "managed_deployment": MANAGED_DEPLOYMENT,
            "highlights": highlights, "highlights_en": highlights_en,
            "recent_commits": commits, "checked_at": int(now)}
    _UPDATE_CACHE.update(ts=now, data=data)
    return data


def _upgrade_from_github_sync() -> dict:
    """Explicit upgrades pin one verified remote commit and preserve deployment data."""
    if MANAGED_DEPLOYMENT:
        raise HTTPException(409, "This instance is deployed via an image; build and update the container from the configured repo branch")
    if GEN_LOCK.locked() or any(t.get("status") in ("queued", "processing") for t in store.tasks.values()):
        raise HTTPException(409, "Tasks are still queued or running; wait for them to finish before updating")
    import requests
    import tarfile
    target = _check_update_sync(force=True, _auto_upgrade=False).get("remote_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise HTTPException(502, "Could not confirm the remote target commit; update not performed")
    upgraded_via = ""
    try:
        _ensure_git_repo(_read_env_key("GITHUB_TOKEN"))
        for command in (["fetch", "origin", REPO_REF],
                        ["checkout", "-f", target, "--", "."],
                        ["reset", "--mixed", target]):
            result = _git(command, timeout=45)
            result.check_returncode()
        upgraded_via = "git"
    except Exception as exc:
        log.warning("Git update incomplete; trying the pinned-commit archive (%s)", type(exc).__name__)
    if not upgraded_via:
        response = requests.get(f"https://codeload.github.com/{REPO_SLUG}/tar.gz/{target}", timeout=60)
        if response.status_code != 200:
            raise HTTPException(502, f"Failed to download update package (HTTP {response.status_code})")
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            for member in archive.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1].replace("\\", "/")
                if rel.startswith("/") or ".." in rel.split("/") or rel == ".env" or rel.startswith("data/") or rel.startswith(".git/"):
                    continue
                path = os.path.join(BASE_DIR, rel)
                if member.isdir():
                    os.makedirs(path, exist_ok=True)
                elif member.isfile():
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with archive.extractfile(member) as source, open(path, "wb") as out:
                        out.write(source.read())
        upgraded_via = "tarball"
    # Record what was installed, never a newer remote HEAD observed after the download.
    with open(_installed_sha_file(), "w") as f:
        f.write(target)
    _UPDATE_CACHE["ts"] = 0.0
    status = _check_update_sync(force=True, _auto_upgrade=False)
    status["local_sha"] = target[:7]
    status["has_update"] = bool(status.get("remote_sha") and status["remote_sha"] != target[:7])
    status["up_to_date"] = not status["has_update"] if status.get("remote_sha") else None
    return {"ok": True, "via": upgraded_via, "installed_sha": target,
            "message": f"Installed version {_read_local_version().get('version')} ({target[:7]})", "status": status}


@app.get("/admin/update/check")
@app.get("/admin/repo/status")
async def admin_check_update(force: bool = False):
    """Lets every deployed node check in real time whether the official GitHub repo has a new version."""
    return await asyncio.to_thread(_check_update_sync, force)


@app.post("/admin/update/upgrade")
@app.post("/admin/repo/pull")
async def admin_upgrade_now(payload: dict = Body(default={}), _=Depends(auth)):
    """Pull the latest update from the official GitHub repo in one click and restart the service smoothly."""
    res = await asyncio.to_thread(_upgrade_from_github_sync)
    restart = payload.get("restart", True) if isinstance(payload, dict) else True
    if restart:
        def _delayed_restart():
            time.sleep(0.5)
            try:
                engine.stop()
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_delayed_restart, daemon=True).start()
    return res


@app.post("/admin/repo/push")
async def admin_repo_push(payload: dict = Body(default={}), _=Depends(auth)):
    """Maintainer only: push this node's core code to the GitHub repo (automatically filters out.env and the data directory)."""
    msg = (payload.get("message") or "").strip() or f"chore: sync update ({time.strftime('%Y-%m-%d %H:%M:%S')})"
    new_token = (payload.get("github_token") or "").strip()
    if new_token:
        _persist_env("GITHUB_TOKEN", new_token)
        os.environ["GITHUB_TOKEN"] = new_token
    token = new_token or _read_env_key("GITHUB_TOKEN")

    def _do_push():
        _ensure_git_repo(token)
        existing_paths = [p for p in TRACKED_REPO_PATHS if os.path.exists(os.path.join(BASE_DIR, p))]
        _git(["add", "--", *existing_paths])
        st = _git(["status", "--porcelain", "--", *existing_paths])
        committed = False
        if st.stdout.strip():
            c_res = _git(["commit", "-m", msg])
            if c_res.returncode != 0:
                raise HTTPException(500, f"Git commit failed: {c_res.stderr or c_res.stdout}")
            committed = True
        p_res = _git(["push", "origin", "HEAD:main"], timeout=60)
        if p_res.returncode != 0:
            err = (p_res.stderr or p_res.stdout or "").strip()
            raise HTTPException(500, f"Git push failed: {err[:300]}")
        _UPDATE_CACHE["ts"] = 0.0
        status = _check_update_sync(force=True)
        return {
            "ok": True,
            "committed": committed,
            "message": "Successfully committed and pushed to the GitHub repo",
            "status": status,
        }

    return await asyncio.to_thread(_do_push)


@app.on_event("startup")
async def _startup():
    for task in list(store.tasks.values()):
        if task.get("kind") in ("image", "video") and task.get("status") in ("queued", "processing"):
            store.update_task(task["id"], status="failed", error="Service restart interrupted the task; please resubmit")
    if _seed_overload_from_tasks(store.list_tasks(200)):
        log.warning("[Parallel generation] Recent tasks hit muse.ai overload, so videos will use parallel mode first")
    if not CFG.api_key:
        import secrets
        new_key = "m2a_" + secrets.token_hex(24)
        CFG.api_key = new_key
        _persist_env("MUSE2API_KEY", new_key)
        log.info("🔑 MUSE2API_KEY not detected; an initial key was auto-generated: %s", new_key)
    asyncio.create_task(_keepalive_loop())


@app.on_event("shutdown")
def _shutdown():
    engine.stop()
