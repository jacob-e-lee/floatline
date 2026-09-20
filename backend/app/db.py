from __future__ import annotations

from urllib.parse import parse_qsl, urlunparse, urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings  # noqa: F401  (loads .env -> settings.database_url)
from app.models import Base  # noqa: F401  (registers tables on Base.metadata)

# Normalize the DB URL for the asyncpg driver:
#  - switch postgresql:// -> postgresql+asyncpg://
#  - asyncpg does NOT understand libpq's `sslmode` param; detect it instead and
#    pass ssl=True through connect_args (Timescale Cloud/Tiger requires TLS).
_parsed = urlparse(settings.database_url)
_qsl = dict(parse_qsl(_parsed.query))
ssl_required = _qsl.pop("sslmode", "") in ("require", "prefer", "true", "1")
_norm = urlunparse((_parsed.scheme, _parsed.netloc, _parsed.path or "/", "", "", ""))
if _norm.startswith("postgresql://"):
    _norm = "postgresql+asyncpg://" + _norm[len("postgresql://"):]

engine: AsyncEngine = create_async_engine(
    _norm,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={"ssl": "require"} if ssl_required else {},
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def create_db_and_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_hypertables()


async def _ensure_hypertables() -> None:
    """Convert the log tables into TimescaleDB hypertables (idempotent).

    Each statement runs in its own transaction: a failure on one table must not
    abort the other, since a shared transaction is poisoned by the first error.
    """
    stmts = [
        "SELECT create_hypertable('balance_snapshots', 'timestamp', if_not_exists => TRUE)",
        "SELECT create_hypertable('transfer_logs', 'timestamp', if_not_exists => TRUE)",
    ]
    for stmt in stmts:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception as exc:  # already hypertabled / no extension / etc.
            print(f"[db] hypertable note: {exc}")


async def close_db() -> None:
    await engine.dispose()