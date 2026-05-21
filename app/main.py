import logging, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.core.exceptions import IACMException
from app.core.rate_limit import limiter
from app.api.v1.router import api_router
from app.websockets.manager import ws_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create upload directories (skip silently if filesystem is read-only, e.g. Vercel)
    try:
        for subdir in ["crew_photos", "documents", "training"]:
            os.makedirs(os.path.join(settings.UPLOAD_DIR, subdir), exist_ok=True)
    except OSError:
        pass

    # Test Supabase connection
    try:
        from app.db.supabase_client import get_supabase
        sb = get_supabase()
        sb.table("companies").select("id").limit(1).execute()
        logger.info("Supabase connected successfully")
    except Exception as e:
        logger.warning("Supabase connection failed: %s", e)

    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Professional Crew Management System for Iraqi Airways",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# Rate limiting — must be wired to the app and exception handler before routes
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_HOSTS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Company-ID"],
)


# Security response headers — defense-in-depth on every response.
#   • nosniff           : stops browsers MIME-sniffing a JSON body into HTML/JS.
#   • DENY framing      : clickjacking protection (no surface needs to be framed).
#   • Referrer-Policy   : don't leak full URLs (which may carry ?token=) cross-site.
#   • X-XSS-Protection 0: modern guidance — disable the buggy legacy IE/Chrome filter.
# HSTS is already set at the Vercel edge, so we don't duplicate it. CSP is
# intentionally omitted: a strict policy would break the Swagger UI's CDN assets
# at /api/docs, and every other response is JSON consumed by the Flutter client.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "0"
    return response

# Authenticated download endpoint replaces the previous unauthenticated
# StaticFiles mount on /uploads. We pass JWT as Authorization header OR as a
# `?token=` query param so <img> tags can still load avatars/photos.
@app.get("/uploads/{path:path}")
async def serve_upload(path: str, request: Request):
    from app.core.security import decode_token

    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    if not token:
        token = request.query_params.get("token", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Resolve safely inside UPLOAD_DIR — block path traversal
    base = Path(settings.UPLOAD_DIR).resolve()
    target = (base / path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)


# API routes
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


# ── WebSocket connect throttle (in-memory per-IP) ───────────────────────────
_WS_CONNECT_LOG: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
_WS_LIMIT_PER_MIN = 10


def _ws_throttle(ip: str) -> bool:
    """Return True if this IP is allowed to open another WS connection."""
    now = time.monotonic()
    log_q = _WS_CONNECT_LOG[ip]
    cutoff = now - 60.0
    while log_q and log_q[0] < cutoff:
        log_q.popleft()
    if len(log_q) >= _WS_LIMIT_PER_MIN:
        return False
    log_q.append(now)
    return True


@app.exception_handler(IACMException)
async def iacm_exception_handler(request: Request, exc: IACMException):
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    if settings.DEBUG:
        raise exc
    return JSONResponse(
        status_code=500,
        content={"message": "Internal server error", "error_code": "INTERNAL_ERROR"},
    )


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    from app.core.security import decode_token
    from app.db.supabase_client import get_supabase

    # Per-IP connect throttle to slow brute-force JWT validation
    client_ip = (websocket.client.host if websocket.client else "unknown") or "unknown"
    if not _ws_throttle(client_ip):
        await websocket.close(code=4290)  # custom: too many requests
        return

    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001)
        return
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        await websocket.close(code=4001)
        return

    # Derive identity from the JWT — never trust path/query params
    token_user_id = payload.get("sub")
    if not token_user_id or token_user_id != user_id:
        await websocket.close(code=4003)
        return

    # Look up the active user and derive company_id from DB
    sb = get_supabase()
    result = sb.table("users").select("id,company_id,is_active") \
        .eq("id", token_user_id).eq("is_active", True).execute()
    if not result.data:
        await websocket.close(code=4003)
        return
    company_id = result.data[0]["company_id"]

    await ws_manager.connect(websocket, token_user_id, company_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(token_user_id, company_id, websocket)


@app.get("/")
async def root():
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "online",
        "docs": "/api/docs",
        "health": "/health",
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": settings.APP_VERSION, "db": "supabase"}
