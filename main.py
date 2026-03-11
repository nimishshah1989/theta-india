"""JIP Horizon India — FastAPI Application Entry Point."""
from __future__ import annotations

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from india_alpha.api.models import HealthResponse
from india_alpha.api.routes import router
from india_alpha.config import get_settings

logger = structlog.get_logger()
settings = get_settings()

# ─────────────────────────────────────────────────────────────
# App Initialization
# ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="JIP Horizon India",
    description="Quantitative hidden gem stock screener for Indian mid/small-cap markets",
    version=settings.app_version,
)

# CORS — parse comma-separated origins, include sensible defaults for local dev
_default_origins = ["http://localhost:3000", "http://localhost:5173"]
_custom_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
allowed_origins = list(set(_default_origins + _custom_origins))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routes under /api/v1
app.include_router(router, prefix="/api/v1", tags=["api"])


# ─────────────────────────────────────────────────────────────
# Health Check (at root, not behind /api/v1)
# ─────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check():
    """Check API and database connectivity."""
    db_ok = False
    try:
        from india_alpha.db import get_async_db
        db = await get_async_db()
        # Simple query to verify connectivity
        result = await db.table("india_companies").select("id").limit(1).execute()
        db_ok = result.data is not None
    except Exception as exc:
        logger.warning("health_check_db_failed", error=str(exc))

    return HealthResponse(
        status="healthy" if db_ok else "degraded",
        environment=settings.environment,
        version=settings.app_version,
        db_connected=db_ok,
    )


# ─────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=(settings.environment == "development"),
    )
