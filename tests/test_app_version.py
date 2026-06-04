"""GET /app/version — public client version metadata.

Verifies the response shape (the client contract) and that env vars override the
defaults so a release can be announced without a code change.

Run:  venv/Scripts/python -m pytest tests/test_app_version.py -q
"""
import asyncio
from app.api.v1.endpoints import app_meta


def _call(**q):
    return asyncio.run(app_meta.app_version(**q))


def test_defaults_shape():
    r = _call(platform="windows", current="1.0.0")
    assert set(r) >= {
        "latestVersion", "minimumSupportedVersion", "forceUpdate",
        "downloadUrl", "releaseNotes", "publishedAt", "platform",
    }
    assert r["platform"] == "windows"
    assert isinstance(r["forceUpdate"], bool)
    assert r["forceUpdate"] is False          # default off during development
    assert r["latestVersion"] == "1.0.0"      # default


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("APP_LATEST_VERSION", "1.2.3")
    monkeypatch.setenv("APP_MIN_VERSION", "1.1.0")
    monkeypatch.setenv("APP_FORCE_UPDATE", "true")
    monkeypatch.setenv("APP_DOWNLOAD_URL", "https://example.com/IACM-1.2.3.exe")
    r = _call(platform="windows")
    assert r["latestVersion"] == "1.2.3"
    assert r["minimumSupportedVersion"] == "1.1.0"
    assert r["forceUpdate"] is True
    assert r["downloadUrl"].endswith("IACM-1.2.3.exe")
