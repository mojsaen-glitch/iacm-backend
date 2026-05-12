from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import os

from app.core.config import settings
from app.core.exceptions import IACMException
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
        print("[OK] Supabase connected successfully")
    except Exception as e:
        print(f"[WARN] Supabase connection: {e}")

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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_HOSTS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files for uploads
if os.path.exists(settings.UPLOAD_DIR):
    app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

# API routes
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


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
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001)
        return
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=4001)
        return
    company_id = websocket.query_params.get("company_id", "")
    await ws_manager.connect(websocket, user_id, company_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, company_id, websocket)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": settings.APP_VERSION, "db": "supabase"}
