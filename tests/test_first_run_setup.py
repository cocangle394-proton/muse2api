"""First-run setup wizard: fresh-state gating and validation.
Run: python tests/test_first_run_setup.py; uses no live account, no
network, no browser. The restore itself is covered by the isolated
/tmp integration test (real B2), not here.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent.parent / "app.py"


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body)


class Client:
    """Minimal ASGI test client using the standard library, no httpx
    dependency (same pattern as tests/test_admin_base_url.py)."""

    def __init__(self, app, headers=None):
        self.app, self.headers = app, headers or {}

    async def request(self, method, path, body=b"", headers=None):
        hdr = {**self.headers, **(headers or {})}
        hdr["content-length"] = str(len(body))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path,
                 "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in hdr.items()],
                 "server": ("test", 80), "client": ("test", 1)}
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post_json(self, path, payload):
        return await self.request("POST", path, body=json.dumps(payload).encode(),
                                  headers={"content-type": "application/json"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def load_app(home, env_key="test-only"):
    env = {"MUSE2API_HOME": home, "MUSE2API_PROFILE_ROOT": home}
    if env_key is None:
        os.environ.pop("MUSE2API_KEY", None)
    else:
        env["MUSE2API_KEY"] = env_key
    os.environ.update(env)
    sys.path.insert(0, str(SOURCE.parent))
    for name in ("config", "store", "engine", "cdp"):
        sys.modules.pop(name, None)  # config reads MUSE2API_* at import
    spec = importlib.util.spec_from_file_location("setup_test_target", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # The wizard's storage check uses BASE_DIR (the repo dir); point it at
    # the temp home so the test never sees the live repo's .env.storage.
    module.BASE_DIR = home
    return module


def check_reload(home):
    """Regression test for the post-restore hot reload: on a fresh boot
    the startup handler auto-generates a key into .env, and _load_dotenv
    copies it into os.environ at import. A restore then overwrites .env
    with the bundle's key. The reload must read the .env FILE (the
    restored key), not os.environ (which still holds the pre-restore
    auto-generated key) — otherwise the restored key can never log in
    and the server is stuck on a key nobody knows."""
    key_a = "m2a_" + "a" * 48   # the auto-generated key from first boot
    key_b = "m2a_" + "b" * 48   # the key restored from the backup bundle
    with open(os.path.join(home, ".env"), "w") as f:
        f.write(f"MUSE2API_KEY={key_a}\n")
    mod = load_app(home, env_key=None)
    checks = 0

    def expect(condition, message):
        nonlocal checks
        checks += 1
        assert condition, message

    expect(mod.CFG.api_key == key_a, "CFG must start with the .env key")
    expect(os.environ.get("MUSE2API_KEY") == key_a,
           "_load_dotenv must have copied the .env key into os.environ")
    # Simulate the restore overwriting .env with the bundle's key.
    with open(os.path.join(home, ".env"), "w") as f:
        f.write(f"MUSE2API_KEY={key_b}\n")
    mod._reload_after_restore()
    expect(mod.CFG.api_key == key_b,
           "after reload CFG.api_key must be the RESTORED key, "
           f"got {mod.CFG.api_key[:6]}... instead of {key_b[:6]}...")
    expect(os.environ.get("MUSE2API_KEY") == key_b,
           "os.environ must also be updated to the restored key")
    print(f"hot-reload regression checks PASS ({checks} checks)")
    return checks


async def check(home):
    mod = load_app(home)
    checks = 0

    def expect(condition, message):
        nonlocal checks
        checks += 1
        assert condition, message

    good = {"B2_ENDPOINT": "https://s3.us-west-004.backblazeb2.com",
            "B2_KEY_ID": "x" * 25, "B2_APPLICATION_KEY": "K" + "x" * 31,
            "BACKUP_PASSPHRASE": "test-passphrase"}

    async with Client(mod.app) as client:
        # (1) Fresh: no accounts, no storage file -> setup needed.
        st = await client.get("/admin/setup-status")
        expect(st.status_code == 200, f"setup-status failed: {st.status_code}")
        expect(st.json() == {"setup_needed": True},
               f"fresh server must need setup, got {st.json()!r}")

        # (2) Missing field -> 400, and the server stays fresh.
        bad = dict(good)
        del bad["BACKUP_PASSPHRASE"]
        r = await client.post_json("/admin/setup", bad)
        expect(r.status_code == 400, f"missing field must be 400, got {r.status_code}")
        st = await client.get("/admin/setup-status")
        expect(st.json() == {"setup_needed": True}, "failed setup must stay fresh")

        # (3) Non-https endpoint -> 400.
        bad = dict(good, B2_ENDPOINT="s3.us-west-004.backblazeb2.com")
        r = await client.post_json("/admin/setup", bad)
        expect(r.status_code == 400, f"non-https endpoint must be 400, got {r.status_code}")

        # (4) Accounts on disk -> not fresh, setup locked with 403.
        os.makedirs(os.path.join(home, "data"), exist_ok=True)
        with open(os.path.join(home, "data", "accounts.json"), "w") as f:
            json.dump([{"id": "a1", "label": "t", "cookies": {}}], f)
        mod.store.reload()
        st = await client.get("/admin/setup-status")
        expect(st.json() == {"setup_needed": False},
               f"server with accounts must not need setup, got {st.json()!r}")
        r = await client.post_json("/admin/setup", good)
        expect(r.status_code == 403, f"setup on a live server must be 403, got {r.status_code}")

        # (5) Storage file present (no accounts) -> not fresh, setup locked.
        os.unlink(os.path.join(home, "data", "accounts.json"))
        mod.store.reload()
        os.makedirs(os.path.join(home, "scripts"), exist_ok=True)
        with open(os.path.join(home, "scripts", ".env.storage"), "w") as f:
            f.write("B2_ENDPOINT=https://example.invalid\n")
        st = await client.get("/admin/setup-status")
        expect(st.json() == {"setup_needed": False},
               "server with storage config must not need setup")
        r = await client.post_json("/admin/setup", good)
        expect(r.status_code == 403, f"setup with storage present must be 403, got {r.status_code}")

        # (6) store.reload() picks up accounts written to disk.
        os.unlink(os.path.join(home, "scripts", ".env.storage"))
        with open(os.path.join(home, "data", "accounts.json"), "w") as f:
            json.dump([{"id": "a1", "label": "t", "cookies": {}},
                       {"id": "a2", "label": "t2", "cookies": {}}], f)
        mod.store.reload()
        expect(len(mod.store.accounts) == 2, "store.reload must re-read accounts")
        expect(mod._accounts_count() == 2, "_accounts_count must be 2")

    print(f"setup wizard checks PASS ({checks} checks)")
    return checks


if __name__ == "__main__":
    total = 0
    with tempfile.TemporaryDirectory(prefix="muse-setup-reload-") as home:
        total += check_reload(home)
    with tempfile.TemporaryDirectory(prefix="muse-setup-test-") as home:
        total += asyncio.run(check(home))
    print(f"PASS {total} checks; no live accounts/network/browser")
