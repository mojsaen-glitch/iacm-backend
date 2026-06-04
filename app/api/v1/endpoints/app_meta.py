"""App metadata — client version / update info.

`GET /app/version` is PUBLIC (the desktop/mobile client calls it on startup,
before login) and returns the latest + minimum-supported versions so the client
can decide whether to suggest — or later force — an update.

Values come from environment variables so a new release can be announced WITHOUT
a code change; sensible defaults keep it working during development. The response
SHAPE is the contract — wiring Velopack / Cloudflare R2 later only means pointing
APP_DOWNLOAD_URL at the real release feed, no client rebuild.
"""
import os
from fastapi import APIRouter, Query

router = APIRouter(prefix="/app", tags=["App"])


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@router.get("/version")
async def app_version(
    platform: str = Query("windows"),
    current: str | None = Query(None),
):
    """Latest-version metadata for a client platform. Public, no auth.

    NOTE: `forceUpdate` / `minimumSupportedVersion` are returned but the desktop
    client does NOT enforce blocking yet (pre-launch phase). They're part of the
    contract so enforcement can be switched on later without an API change.
    """
    return {
        "latestVersion":           os.getenv("APP_LATEST_VERSION", "1.0.0"),
        "minimumSupportedVersion": os.getenv("APP_MIN_VERSION", "1.0.0"),
        "forceUpdate":             _as_bool(os.getenv("APP_FORCE_UPDATE", "false")),
        "downloadUrl":             os.getenv("APP_DOWNLOAD_URL", ""),
        "releaseNotes":            os.getenv("APP_RELEASE_NOTES", ""),
        "publishedAt":             os.getenv("APP_PUBLISHED_AT", ""),
        "platform":                platform,
    }
