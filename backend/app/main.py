import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.database import init_db, close_db
from app.api import trend, sector, symbol, search, admin, admin_panel, auth, portfolio, superstrength, watchlist

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("TrendPulse API starting up...")
    await init_db()
    yield
    logger.info("TrendPulse API shutting down...")
    await close_db()


app = FastAPI(
    title="TrendPulse API",
    description="NSE Momentum Analytics — 2143 stocks, 7 metrics, daily signals",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — tighten origins in production via ALLOWED_ORIGINS env var
origins = (
    ["*"]
    if settings.ALLOWED_ORIGINS == "*"
    else [o.strip() for o in settings.ALLOWED_ORIGINS.split(",")]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────────
app.include_router(trend.router,   prefix="/api/trend",   tags=["Screener"])
app.include_router(sector.router,  prefix="/api/sector",  tags=["Sectors"])
app.include_router(symbol.router,  prefix="/api/symbol",  tags=["Symbol"])
app.include_router(search.router,  prefix="/api/search",  tags=["Search"])
app.include_router(admin.router,        prefix="/api/admin",        tags=["Admin"])
app.include_router(admin_panel.router,  prefix="/api/admin/panel",  tags=["Admin Panel"])
app.include_router(auth.router,         prefix="/api/auth",         tags=["Auth"])
app.include_router(portfolio.router,    prefix="/api/portfolio",    tags=["Portfolio"])
app.include_router(superstrength.router, prefix="/api/superstrength", tags=["Super Strength"])
app.include_router(watchlist.router,     prefix="/api/watchlist",     tags=["Watchlist"])


# ── Root & health ─────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return {"service": "TrendPulse API", "version": "2.0.0", "docs": "/docs"}


@app.get("/health", tags=["Status"])
async def health():
    return {"status": "ok"}


# ── Global error handler ──────────────────────────────────────────────
@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )