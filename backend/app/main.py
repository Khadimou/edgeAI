import logging
import sentry_sdk
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.redis import get_redis, close_redis
from app.db.session import engine
from app.api.routes import auth, matches, recommendations, bets, bankroll, stats, users, webhooks, plan, today, model_stats, backtest, tracking, admin

logger = logging.getLogger("edgeai")

if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()
    yield
    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="edgeAI API",
    description="Plateforme de conseil en paris sportifs basée sur l'IA",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://edgeai.fr",
        "https://www.edgeai.fr",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Catch-all : retransforme les exceptions non-handled en JSONResponse
# AVEC les headers CORS (sinon le browser affiche un faux "CORS error").
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled_exception", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Erreur serveur: {type(exc).__name__}", "message": str(exc)[:200]},
        headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
    )

app.include_router(auth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(matches.router, prefix="/api/v1")
app.include_router(recommendations.router, prefix="/api/v1")
app.include_router(bets.router, prefix="/api/v1")
app.include_router(bankroll.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")
app.include_router(webhooks.router, prefix="/api/v1")
app.include_router(plan.router, prefix="/api/v1")
app.include_router(today.router, prefix="/api/v1")
app.include_router(model_stats.router, prefix="/api/v1")
app.include_router(backtest.router, prefix="/api/v1")
app.include_router(tracking.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/")
async def root():
    return {"message": "edgeAI API — Value betting & Kelly Criterion"}
