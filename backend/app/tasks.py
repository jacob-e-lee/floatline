"""APScheduler background task: the Floatline PID control loop.

Every ~60 seconds this job:
  1. Reads the last-known checking / savings balances from the DB (source of truth).
  2. Runs calculate_pid() to compute the required transfer.
  3. Executes the transfer on Nessie (checking <-> savings).
  4. If savings > cap, sweeps overflow to Alpaca (market buy).
  5. Logs a BalanceSnapshot + TransferLog to TimescaleDB.
  6. Broadcasts an SSE event to connected frontend clients.

Local balance tracking:
  The Nessie sandbox does not update account ``balance`` after transactions, so
  Floatline maintains the canonical running balances in its own database.
  Each sweep adjusts the locally-tracked balances, which is what the PID loop
  reads, making the engine's view of balances fully self-consistent.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alpaca import AlpacaClient
from app.config import settings
from app.db import AsyncSessionLocal
from app.models import AppSetting, BalanceSnapshot, TransferLog
from app.nessie import NessieClient
from app.pid import PIDState, calculate_pid
from app.stream import broadcast_event

logger = logging.getLogger("floatline.tasks")

# Module-level PID state, carried across polling cycles.
_pid_state = PIDState()


async def _get_latest_snapshot(session: AsyncSession) -> BalanceSnapshot | None:
    result = await session.execute(
        select(BalanceSnapshot).order_by(BalanceSnapshot.timestamp.desc()).limit(1)
    )
    return result.scalars().first()


async def effective_pid_params(session: AsyncSession) -> tuple[float, float]:
    """Resolve the (setpoint, savings_cap) the controller should act on.

    Values persisted in the ``app_settings`` table (editable from the dashboard)
    override the .env defaults. Returns the (setpoint, savings_cap) tuple.
    """
    result = await session.execute(
        select(AppSetting).where(AppSetting.key.in_(("pid_setpoint", "savings_cap")))
    )
    overrides = {row.key: row.value for row in result.scalars().all()}

    try:
        setpoint = float(overrides.get("pid_setpoint", settings.pid_setpoint))
    except (TypeError, ValueError):
        setpoint = settings.pid_setpoint
    try:
        savings_cap = float(overrides.get("savings_cap", settings.savings_cap))
    except (TypeError, ValueError):
        savings_cap = settings.savings_cap

    return setpoint, savings_cap


async def save_pid_params(session: AsyncSession, setpoint: float, savings_cap: float) -> None:
    """Persist dashboard-provided setpoint / savings cap overrides."""
    for key, value in (("pid_setpoint", setpoint), ("savings_cap", savings_cap)):
        existing = await session.get(AppSetting, key)
        if existing is not None:
            existing.value = repr(float(value))
        else:
            session.add(AppSetting(key=key, value=repr(float(value))))
    await session.commit()


async def _log_transfer(
    session: AsyncSession,
    source: str,
    destination: str,
    amount: float,
    reason: str,
    status: str,
    api_response: str | None = None,
    request_id: str | None = None,
) -> TransferLog:
    log = TransferLog(
        source_account_id=source,
        destination=destination,
        amount=amount,
        reason=reason,
        status=status,
        api_response=api_response,
        request_id=request_id,
    )
    session.add(log)
    await session.flush()
    return log


async def _await_invested_value(
    alpaca: AlpacaClient,
    previous: float,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 1.0,
) -> tuple[float, bool]:
    """Poll open positions until the invested value moves past ``previous``.

    A market buy returns while the order is still filling, and ``GET
    /positions`` only lists *filled* shares - so an immediate re-read after
    ``submit_notional_market_buy`` returns the stale pre-buy value and that
    stale number is what gets persisted. Poll here (bounded, non-blocking to
    the scheduler thanks to ``max_instances=1`` + ``coalesce=True``) until the
    value exceeds ``previous`` or the timeout expires.

    Outside market hours Alpaca parks orders as ``accepted``/``new`` instead of
    filling them, so positions stay empty for hours and the poll always times
    out. In that case fall back to the order's own ``filled_avg_price ×
    filled_qty`` (real executed value once partially filled, else 0)... but
    crucially the caller is told it is *unsettled* so it keeps the previous
    tick's number instead of cementing a pre-fill zero.

    Returns ``(value, settled)``: ``settled`` is True when the value moved past
    ``previous`` (or was already above it), False on timeout - in which case
    ``value`` is the freshest read and the *caller* must keep the previous
    tick's number rather than cementing the pre-fill value.
    """
    try:
        current = await alpaca.invested_market_value()
    except Exception as exc:
        logger.warning("alpaca positions read failed: %s", exc)
        return previous, False
    if current > previous + 1e-9:
        return current, True
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(poll_seconds)
        try:
            current = await alpaca.invested_market_value()
        except Exception as exc:
            logger.warning("alpaca positions poll failed: %s", exc)
            continue
        if current > previous + 1e-9:
            return current, True
    logger.warning(
        "alpaca fill not visible after %.0fs (prev=%.2f last=%.2f) - "
        "keeping previous brokerage value",
        timeout_seconds, previous, current,
    )
    return current, False


async def _read_live_balances(
    nessie: NessieClient, alpaca: AlpacaClient
) -> tuple[float, float, float]:
    """Read checking/savings straight from Nessie and invested ETF value from Alpaca."""
    checking = await nessie.checking_balance()
    savings = await nessie.savings_balance()
    try:
        brokerage = await alpaca.invested_market_value()
    except Exception as exc:  # Alpaca unreachable -> ignore brokerage movement
        logger.warning("alpaca positions read failed: %s", exc)
        brokerage = 0.0
    return float(checking), float(savings), float(brokerage)


async def _current_balances(
    session: AsyncSession, nessie: NessieClient, alpaca: AlpacaClient
) -> tuple[float, float, float]:
    """Resolve the balances the controller should act on.

    ``BALANCE_SOURCE=live``   -> read Nessie/Alpaca on every cycle.
    ``BALANCE_SOURCE=ledger`` -> use Floatline's own shadow ledger (the newest
                                 ``BalanceSnapshot``), falling back to the live
                                 APIs when the ledger is still empty.

    Why the shadow ledger is the default: the Capital One Nessie sandbox accepts
    every deposit / withdrawal / transfer (HTTP 201, real transaction records)
    but never mutates an account's ``balance`` field - it stays static. A
    controller reading live balances there would observe a frozen number forever
    and could never converge on the setpoint. Floatline therefore records the
    effect of every transaction it issues in TimescaleDB and treats that ledger
    as the observed balance. Set ``BALANCE_SOURCE=live`` to drive a real bank
    account that does persist balance changes.

    Brokerage is always read live from Alpaca open positions (see
    ``AlpacaClient.invested_market_value``): the paper account's $100,000 of
    fake cash is invisible to the positions endpoint, so the value starts at
    $0.00 and only reflects shares Floatline actually bought. On a failed
    positions read we fall back to the ledger value.
    """
    latest = await _get_latest_snapshot(session)

    if settings.balance_source.strip().lower() != "ledger" or latest is None:
        return await _read_live_balances(nessie, alpaca)

    try:
        brokerage_live = await alpaca.invested_market_value()
    except Exception as exc:  # Alpaca unreachable -> keep the ledger value
        logger.warning("alpaca positions read failed: %s", exc)
        brokerage_live = float(latest.brokerage_balance)

    return (
        float(latest.checking_balance),
        float(latest.savings_balance),
        float(brokerage_live),
    )


async def _persist_snapshot(
    session: AsyncSession,
    checking: float,
    savings: float,
    brokerage: float,
    pid_output: float,
    setpoint: float,
) -> BalanceSnapshot:
    """Append one telemetry row to the TimescaleDB hypertable."""
    snapshot = BalanceSnapshot(
        timestamp=datetime.now(timezone.utc),
        checking_balance=round(checking, 2),
        savings_balance=round(savings, 2),
        brokerage_balance=round(brokerage, 2),
        setpoint=round(setpoint, 2),
        pid_output=round(pid_output, 2),
    )
    session.add(snapshot)
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


def _snapshot_event(snapshot: BalanceSnapshot, transfers: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the SSE payload broadcast to every connected dashboard."""
    return {
        "type": "tick",
        "timestamp": snapshot.timestamp.isoformat(),
        "checking_balance": snapshot.checking_balance,
        "savings_balance": snapshot.savings_balance,
        "brokerage_balance": snapshot.brokerage_balance,
        "setpoint": snapshot.setpoint,
        "pid_output": snapshot.pid_output,
        "transfers": transfers,
    }

async def run_control_cycle() -> dict[str, Any]:
    """Execute one PID control cycle, log it, and broadcast the new state.

    Steps: read tracked balances -> calculate_pid() -> sweep checking<->savings
    -> if savings exceed the cap, cascade the overflow to Alpaca -> persist a
    BalanceSnapshot -> broadcast an SSE event.
    """
    nessie = NessieClient()
    alpaca = AlpacaClient()
    transfers: list[dict[str, Any]] = []
    try:
        async with AsyncSessionLocal() as session:
            setpoint, savings_cap = await effective_pid_params(session)
            checking, savings, brokerage = await _current_balances(session, nessie, alpaca)

            # dt is expressed in MINUTES so the tuned gains stay well damped.
            output = calculate_pid(
                current_balance=checking,
                setpoint=setpoint,
                kp=settings.pid_kp,
                ki=settings.pid_ki,
                kd=settings.pid_kd,
                state=_pid_state,
                dt=settings.poll_interval / 60.0,
                deadband=settings.pid_deadband,
            )

            # --- 1) PID sweep between checking and savings -------------------
            if abs(output) >= 0.01:
                if output > 0:  # over-funded -> sweep the excess to savings
                    amount = round(min(output, max(checking, 0.0)), 2)
                    source = settings.nessie_checking_account_id
                    destination = settings.nessie_savings_account_id
                    reason = "pid_sweep_to_savings"
                else:  # under-funded -> pull the shortfall from savings
                    amount = round(min(-output, max(savings, 0.0)), 2)
                    source = settings.nessie_savings_account_id
                    destination = settings.nessie_checking_account_id
                    reason = "pid_pull_from_savings"

                if amount >= 0.01:
                    try:
                        response = await nessie.create_transfer(
                            source, destination, amount, "Floatline PID sweep"
                        )
                        status = "executed"
                        if output > 0:
                            checking -= amount
                            savings += amount
                        else:
                            checking += amount
                            savings -= amount
                    except Exception as exc:
                        response, status = {"error": str(exc)}, "failed"
                        logger.exception("pid sweep failed")

                    await _log_transfer(
                        session,
                        source=source,
                        destination=destination,
                        amount=amount,
                        reason=reason,
                        status=status,
                        api_response=json.dumps(response, default=str),
                    )
                    transfers.append(
                        {
                            "type": "pid_sweep",
                            "direction": "to_savings" if output > 0 else "to_checking",
                            "amount": amount,
                            "status": status,
                            "pid_output": round(output, 2),
                        }
                    )

            # --- 2) Savings cap -> cascade overflow into Alpaca --------------
            if savings > savings_cap:
                excess = round(savings - savings_cap, 2)
                if excess >= 1.0:
                    status = "executed"
                    details: dict[str, Any] = {}
                    try:
                        withdrawal = await nessie.create_withdrawal(
                            settings.nessie_savings_account_id,
                            excess,
                            "Floatline overflow to brokerage",
                        )
                        details["withdrawal"] = withdrawal.payload
                    except Exception as exc:
                        details["withdrawal_error"] = str(exc)
                        status = "failed"
                        logger.exception("overflow withdrawal failed")
                    order_ok = False
                    try:
                        details["order"] = await alpaca.submit_notional_market_buy(excess)
                        order_ok = True
                    except Exception as exc:
                        details["order_error"] = str(exc)
                        status = "failed"
                        logger.exception("alpaca notional order failed")

                    savings -= excess
                    # The buy returns while the order is still filling and
                    # positions only list *filled* shares - an immediate re-read
                    # returns the stale pre-buy value. Poll (bounded) until the
                    # fill lands; on timeout keep the previous tick's value so
                    # we never cement the pre-fill number (the next cycle's
                    # live read picks the fill up once it settles).
                    if order_ok:
                        settled_value, settled = await _await_invested_value(
                            alpaca, previous=brokerage
                        )
                        if settled:
                            brokerage = settled_value
                            details["brokerage_settled"] = True
                        else:
                            details["brokerage_settled"] = False
                            details["brokerage_pending_read"] = settled_value
                    else:
                        # No buy was submitted - keep the live value from the
                        # start of the cycle instead of ledger-adding excess
                        # (which would double-count once the fill later lands).
                        pass

                    await _log_transfer(
                        session,
                        source=settings.nessie_savings_account_id,
                        destination=f"alpaca:{settings.alpaca_order_symbol}",
                        amount=excess,
                        reason="savings_cap_overflow_to_brokerage",
                        status=status,
                        api_response=json.dumps(details, default=str),
                    )
                    transfers.append(
                        {
                            "type": "brokerage_sweep",
                            "amount": excess,
                            "symbol": settings.alpaca_order_symbol,
                            "status": status,
                        }
                    )

            snapshot = await _persist_snapshot(session, checking, savings, brokerage, output, setpoint)
    finally:
        await nessie.aclose()
        await alpaca.aclose()

    event = _snapshot_event(snapshot, transfers)
    await broadcast_event(event)
    logger.info(
        "tick chk=%.2f sav=%.2f brk=%.2f pid=%.2f transfers=%d",
        snapshot.checking_balance,
        snapshot.savings_balance,
        snapshot.brokerage_balance,
        snapshot.pid_output,
        len(transfers),
    )
    return event


async def apply_simulated_transaction(kind: str, amount: float) -> dict[str, Any]:
    """Simulate income/expense on Nessie and record the resulting balance.

    ``kind`` is ``"income"`` (deposit into checking) or ``"expense"``
    (withdrawal from checking).  Backs the stage-demo endpoints.
    """
    if kind not in ("income", "expense"):
        raise ValueError(f"unknown simulation kind: {kind!r}")
    amount = float(round(abs(amount), 2))

    nessie = NessieClient()
    alpaca = AlpacaClient()
    transfers: list[dict[str, Any]] = []
    try:
        async with AsyncSessionLocal() as session:
            setpoint, _savings_cap = await effective_pid_params(session)
            checking, savings, brokerage = await _current_balances(session, nessie, alpaca)

            if kind == "income":
                response = await nessie.simulate_income(amount)
                checking += amount
                source, destination = "external", settings.nessie_checking_account_id
            else:
                response = await nessie.simulate_expense(amount)
                checking -= amount
                source, destination = settings.nessie_checking_account_id, "external"

            await _log_transfer(
                session,
                source=source,
                destination=destination,
                amount=amount,
                reason=f"simulated_{kind}",
                status="executed",
                api_response=json.dumps(response, default=str),
            )
            transfers.append({"type": f"simulated_{kind}", "amount": amount, "status": "executed"})

            # Track market drift even in loop-off sessions: simulations don't
            # submit orders, but the shares we already own still move with the
            # market. Fall back to the ledger copy when Alpaca is unreachable.
            try:
                brokerage = await alpaca.invested_market_value()
            except Exception as exc:
                logger.warning("alpaca positions read failed: %s", exc)

            snapshot = await _persist_snapshot(
                session,
                checking,
                savings,
                brokerage,
                settings.pid_kp * (checking - setpoint),
                setpoint,
            )
    finally:
        await nessie.aclose()
        await alpaca.aclose()

    event = _snapshot_event(snapshot, transfers)
    await broadcast_event(event)
    return event


async def emit_heartbeat() -> None:
    """Push a lightweight SSE keepalive frame to every connected client.

    When CONTROL_LOOP_ENABLED is false (local dev / --smoke / --no-loop) the PID
    loop never runs, so broadcast_event() is never called and the SSE stream sits
    silent between user-driven simulations.  The frontend's EventSource
    interprets prolonged silence as a dead stream, so we emit a data-less
    ``status`` frame every few seconds to keep the connection visibly alive.
    No financial data is included - this frame carries no balances.

    Must be a coroutine: AsyncIOScheduler awaits coroutine jobs on the event
    loop, which is what makes the awaited broadcast_event() actually run.
    """
    await broadcast_event(
        {
            "type": "status",
            "ts": datetime.now(timezone.utc).isoformat(),
            "message": "control loop disabled - heartbeat",
        }
    )


def create_heartbeat_scheduler(interval_seconds: int = 5) -> AsyncIOScheduler:
    """Scheduler used when the PID control loop is disabled.

    Same AsyncIOScheduler plumbing as the control loop, so main.py's shutdown
    path (app.state.scheduler.shutdown()) works unchanged for either mode.
    """
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(
        emit_heartbeat,
        trigger="interval",
        seconds=interval_seconds,
        id="sse_heartbeat",
        name="Floatline SSE heartbeat",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    return scheduler


def create_scheduler() -> AsyncIOScheduler:
    """Build the AsyncIOScheduler that drives the PID control loop.

    Per the project constraints this replaces Celery/Redis entirely: APScheduler
    runs inside the FastAPI lifespan context manager.
    """
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(
        run_control_cycle,
        trigger="interval",
        seconds=settings.poll_interval,
        id="pid_control_loop",
        name="Floatline PID control loop",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=3),
    )
    return scheduler
