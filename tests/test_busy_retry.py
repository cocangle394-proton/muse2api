"""A temporary Muse media outage (overload / 503) is retried; a refusal still is not."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("busy_retry_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    busy = app.MuseEngine._media_service_busy

    # Replies observed from muse.ai on 2026-11-24 while its video backend was overloaded.
    for text in (
        "It couldn't be generated — the video service is overloaded right now, returning 503; it's not a spec problem.",
        "This generation failed — the video generation service is temporarily overloaded (503) and produced no video file.",
        "The video generation service is temporarily unavailable, so nothing was generated this time.",
        "This generation failed: the video generation service returned errors and was still unavailable after 3 retries, so no video was produced.",
        "Sorry, nothing was generated this time. When I tried, the video service was temporarily unavailable and kept returning errors.",
        "Sorry, the video service is overloaded right now (503). Please try again later.",
    ):
        assert busy(text), text
    for text in (
        "Generation failed: video generation is not supported in your country or region yet.",
        "This content violates the usage policy, so I can't generate it.",
        "Your account quota is used up, so video generation is temporarily unavailable.",
        "My suggested reconciled version: format: 9:16 portrait, 720x1280",
        "",
    ):
        assert not busy(text), text
    print("PASS: overload/503 replies are classified as a temporary outage")


    first = app.store.add_account({"hatch_sess": "one", "hatch_vml": "one",
                                   "hatch_native_auth_device": "one"}, "one")
    app.store.add_account({"hatch_sess": "two", "hatch_vml": "two",
                           "hatch_native_auth_device": "two"}, "two")
    common = [
        patch.object(app, "_renew_and_persist", side_effect=lambda ident, **_: app.store.get_account(ident)),
        patch.object(app.engine, "start"), patch.object(app.engine, "reset_thread"),
        patch.object(app.engine, "close_current_page"),
        patch.object(app, "BUSY_RETRY_DELAY", 0.0), patch.object(app, "BUSY_RETRY_MIN_REMAINING", 0.0),
    ]

    def run(side_effect, account_id=None):
        calls = []

        def generate(cookies, prompt, **kwargs):
            calls.append(cookies["hatch_sess"])
            return side_effect(len(calls))

        for p in common:
            p.start()
        try:
            with patch.object(app.engine, "generate", side_effect=generate):
                try:
                    result = app._run_generation_locked("video", "video", 60, account_id=account_id,
                                                        deadline=app.time.monotonic() + 60)
                except Exception as exc:  # noqa: BLE001
                    return calls, exc
                return calls, result
        finally:
            for p in reversed(common):
                p.stop()

    outage = app.MuseServiceBusyError("Muse media service temporarily unavailable (upstream overload/503)")

    def recover(n):
        if n == 1:
            raise outage
        return {"filename": "ok.mp4", "size": 1, "kind": "video"}

    calls, result = run(recover, account_id=first["id"])
    assert isinstance(result, tuple) and result[0]["filename"] == "ok.mp4", result
    assert len(calls) == 2, calls
    print("PASS: a request succeeds when the outage clears on the retry")

    def always_busy(n):
        raise outage

    calls, exc = run(always_busy, account_id=first["id"])
    assert isinstance(exc, app.MuseServiceBusyError), exc
    assert len(calls) == 2 + app.BUSY_RETRIES, calls
    print("PASS: a persistent outage stops after the bounded number of retries")

    def busy_then_timeout(n):
        if n == 1:
            raise outage
        raise app.MuseGenerationError("Timed out waiting for generation; no new generation result appeared")

    calls, exc = run(busy_then_timeout, account_id=first["id"])
    assert exc is outage, exc
    print("PASS: a retry that times out still reports the outage")

    def refuse(n):
        raise app.MuseModelRefusalError("The model produced no media, only text: region unavailable")

    calls, exc = run(refuse, account_id=first["id"])
    assert isinstance(exc, app.MuseModelRefusalError) and calls == ["one"], (exc, calls)
    print("PASS: a model refusal is still terminal")

    app.store.delete_account([a for a in app.store.list_accounts() if a["label"] == "two"][0]["id"])
    calls, result = run(recover)
    assert isinstance(result, tuple) and calls == ["one", "one"], (result, calls)
    print("PASS: with one account the outage is retried on the same account")
