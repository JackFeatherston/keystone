import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.routes import router
from .config import settings
from .jobs.worker import worker_loop
from .mcp.server import mcp

mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(worker_loop())
    async with mcp.session_manager.run():
        yield
    task.cancel()


app = FastAPI(title="Keystone", lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def protect_mcp(request: Request, call_next):
    if request.url.path.startswith("/mcp") and request.headers.get("authorization") != f"Bearer {settings.KEYSTONE_API_KEY}":
        return JSONResponse({"detail": "Invalid API key"}, status_code=401)
    return await call_next(request)


app.mount("/mcp", mcp_app)
