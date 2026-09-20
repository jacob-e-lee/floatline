"""HTTP API: historical telemetry, SSE live stream, and demo simulations."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import BalanceSnapshot, TransferLog
from app.stream import broadcast_event, format_sse, new_client_queue, remove_client_queue
from app.tasks import apply_simulated_transaction, effective_pid_params, save_pid_params

router = APIRouter()

#: Idle seconds before an SSE keepalive comment is emitted.
HEARTBEAT_SECONDS = 15


class SimulationRequest(BaseModel):
    """Body accepted by the demo simulation endpoints."""

    amount: float = Field(default=500.0, gt=0, le=100_000)


class ConfigUpdate(BaseModel):
    """Body for POST /config - only the provided fields are updated."""

    setpoint: float | None = Field(default=None, gt=0, le=1_000_000)
    savings_cap: float | None = Field(default=None, gt=0, le=1_000_000)


async def _effective_config() -> dict[str, Any]:
    """Controller tuning, including any dashboard-persisted overrides."""
    async with AsyncSessionLocal() as session:
        setpoint, savings_cap = await effective_pid_params(session)
    return {
        "setpoint": setpoint,
        "savings_cap": savings_cap,
        "kp": settings.pid_kp,
        "ki": settings.pid_ki,
        "kd": settings.pid_kd,
        "deadband": settings.pid_deadband,
        "poll_interval": settings.poll_interval,
        "order_symbol": settings.alpaca_order_symbol,
    }


def _serialize_snapshot(row: BalanceSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "checking_balance": row.checking_balance,
        "savings_balance": row.savings_balance,
        "brokerage_balance": row.brokerage_balance,
        "setpoint": row.setpoint,
        "pid_output": row.pid_output,
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe used by Caddy / docker healthchecks."""
    return {"status": "ok", "service": "floatline", "time": datetime.now(timezone.utc).isoformat()}


@router.get("/config")
async def get_config() -> dict[str, Any]:
    """Expose the active controller tuning so the UI can label the chart."""
    return await _effective_config()


@router.post("/config")
async def update_config(body: ConfigUpdate) -> dict[str, Any]:
    """Persist dashboard-provided setpoint / savings cap overrides.

    Values are stored in the ``app_settings`` table and take effect on the
    next control cycle. Every connected dashboard is notified over SSE with a
    ``config`` event so open tabs update without a refresh.
    """
    async with AsyncSessionLocal() as session:
        current_setpoint, current_cap = await effective_pid_params(session)
        new_setpoint = body.setpoint if body.setpoint is not None else current_setpoint
        new_cap = body.savings_cap if body.savings_cap is not None else current_cap
        await save_pid_params(session, new_setpoint, new_cap)

    await broadcast_event(
        {
            "type": "config",
            "setpoint": new_setpoint,
            "savings_cap": new_cap,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )
    return {"setpoint": new_setpoint, "savings_cap": new_cap, "status": "ok"}


@router.get("/telemetry")
async def telemetry(
    hours: int = Query(default=24 * 30, ge=1, le=24 * 365),
    limit: int = Query(default=5000, ge=1, le=50_000),
) -> dict[str, Any]:
    """Historical balance snapshots for the Recharts dashboard."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with AsyncSessionLocal() as session:
        stmt = (
            select(BalanceSnapshot)
            .where(BalanceSnapshot.timestamp >= since)
            .order_by(BalanceSnapshot.timestamp.desc())
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
    rows.reverse()  # return oldest -> newest for the chart
    async with AsyncSessionLocal() as session:
        setpoint, savings_cap = await effective_pid_params(session)
    return {
        "setpoint": setpoint,
        "savings_cap": savings_cap,
        "count": len(rows),
        "points": [_serialize_snapshot(r) for r in rows],
    }


@router.get("/transfers")
async def transfers(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """Most recent transfer audit-log entries (dashboard activity feed)."""
    async with AsyncSessionLocal() as session:
        stmt = select(TransferLog).order_by(TransferLog.timestamp.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
    return {
        "count": len(rows),
        "transfers": [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "source_account_id": r.source_account_id,
                "destination": r.destination,
                "amount": r.amount,
                "reason": r.reason,
                "status": r.status,
            }
            for r in rows
        ],
    }

@router.get("/stream")
async def stream() -> StreamingResponse:
    """Server-Sent Events feed backed by a per-client in-memory asyncio.Queue."""
    queue = new_client_queue()

    async def event_generator() -> AsyncIterator[str]:
        try:
            yield format_sse(
                json.dumps(
                    {
                        "type": "connected",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield format_sse(payload)
        except asyncio.CancelledError:
            raise
        finally:
            remove_client_queue(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _simulate(kind: str, body: SimulationRequest | None) -> dict[str, Any]:
    amount = body.amount if body is not None else SimulationRequest().amount
    try:
        return await apply_simulated_transaction(kind, amount)
    except Exception as exc:  # surface upstream API failures as 502
        raise HTTPException(status_code=502, detail=f"{kind} simulation failed: {exc}") from exc


@router.post("/simulate-expense")
async def simulate_expense(body: SimulationRequest | None = None) -> dict[str, Any]:
    """Withdraw cash from checking to demo the PID pulling funds from savings."""
    return await _simulate("expense", body)


@router.post("/simulate-income")
async def simulate_income(body: SimulationRequest | None = None) -> dict[str, Any]:
    """Deposit cash into checking to demo the PID sweeping excess to savings."""
    return await _simulate("income", body)
