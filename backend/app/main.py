"""Floatline FastAPI application entrypoint.

Starts the PID control loop with APScheduler inside the lifespan context
manager - no Celery, no Redis (project constraint).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import close_db, create_db_and_tables
from app.routes import router
from app.tasks import create_heartbeat_scheduler, create_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
)
logger = logging.getLogger("floatline.main")

# The Nessie API authenticates with a `key` QUERY PARAMETER, and httpx logs
# every request URL at INFO level - which would print the API key into the
# container logs in cleartext. Keep the transport layers quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bootstrap the database, start APScheduler, then tear both down cleanly."""
    await create_db_and_tables()
    logger.info("database ready (tables + TimescaleDB hypertables)")

    if settings.control_loop_enabled:
        scheduler = create_scheduler()
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info(
            "APScheduler started - PID control loop every %ss (setpoint $%.2f)",
            settings.poll_interval,
            settings.pid_setpoint,
        )
    else:
        app.state.scheduler = create_heartbeat_scheduler()
        app.state.scheduler.start()
        logger.warning(
            "control loop disabled (CONTROL_LOOP_ENABLED=false) - "
            "SSE heartbeat every %ss keeps the dashboard stream alive",
            5,
        )

    try:
        yield
    finally:
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            logger.info("APScheduler stopped")
        await close_db()


app = FastAPI(
    title="Floatline",
    description="Autonomous liquidity management engine (PID-driven cash sweeping)",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "floatline", "docs": "/docs", "health": "/api/health"}