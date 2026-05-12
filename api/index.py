import sys
import os
import traceback

# Add the backend root to path so "app" package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from app.main import app  # noqa: F401 — Vercel picks up this `app`
except Exception as _import_err:
    # Fallback minimal app to surface the real error
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    _err_detail = traceback.format_exc()

    @app.get("/{path:path}")
    async def catch_all(path: str):
        return JSONResponse(
            status_code=500,
            content={"error": "Import failed", "detail": _err_detail},
        )
