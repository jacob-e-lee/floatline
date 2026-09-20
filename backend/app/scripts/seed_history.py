"""Seed TimescaleDB with synthetic Floatline balance history.

Usage (run from the ``backend`` directory)::

    python -m app.scripts.seed_history
    python -m app.scripts.seed_history --days 30 --interval-minutes 60 --force

Populates ``balance_snapshots`` with 30 days of hourly points that oscillate
around the $1500 setpoint, cascade into savings, and finally overflow into the
brokerage account - so the Recharts dashboard has a populated graph on first
load instead of an empty axis.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from app.config import settings
from app.db import AsyncSessionLocal, close_db, create_db_and_tables
from app.models import BalanceSnapshot

RANDOM_SEED = 42
BATCH_SIZE = 500
CHECKING_AMPLITUDE = 250.0   # checking oscillates +/- $250 around the setpoint
CHECKING_NOISE = 80.0
SAVINGS_START = 3000.0
SAVINGS_GROWTH = 2000.0      # savings climb to (and then ride) the cap
CASCADE_FRACTION = 0.6       # savings saturate at 60% of the window
BROKERAGE_START = 500.0
BROKERAGE_GROWTH = 1500.0    # overflow ramps the brokerage balance up

# The dashboard's default range is the last hour, so the final stretch of the
# seed is written at MINUTE resolution. Without it the "Live 1h" view would hold
# a single hourly point and look empty until the control loop had been ticking
# for most of an hour.
DENSE_TAIL_MINUTES = 60
DENSE_TAIL_STEP_MINUTES = 1


def _timestamps(
    days: int,
    interval_minutes: int,
    dense_minutes: int,
    dense_step_minutes: int,
) -> list[datetime]:
    """Build the point schedule: coarse history first, then a dense tail.

    The tail begins immediately after the last coarse point, so the two series
    join without a gap and without a duplicated timestamp.
    """
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    step = timedelta(minutes=interval_minutes)
    count = max(2, int(days * 24 * 60 / interval_minutes))

    # The coarse series stops one step short of `end` - the tail owns the rest.
    coarse_end = end - step
    stamps = [coarse_end - step * (count - 1 - i) for i in range(count)]

    if dense_minutes > 0 and dense_step_minutes > 0:
        dense_step = timedelta(minutes=dense_step_minutes)
        dense_count = max(1, dense_minutes // dense_step_minutes)
        stamps.extend(end - dense_step * (dense_count - 1 - k) for k in range(dense_count))

    return stamps


def build_points(
    days: int,
    interval_minutes: int,
    dense_minutes: int = DENSE_TAIL_MINUTES,
    dense_step_minutes: int = DENSE_TAIL_STEP_MINUTES,
) -> list[dict[str, Any]]:
    """Generate deterministic synthetic balance points (oldest -> newest)."""
    rng = random.Random(RANDOM_SEED)
    stamps = _timestamps(days, interval_minutes, dense_minutes, dense_step_minutes)
    total = len(stamps)

    setpoint = settings.pid_setpoint
    cap = settings.savings_cap
    points: list[dict[str, Any]] = []

    for i, timestamp in enumerate(stamps):
        progress = i / (total - 1)

        # Checking oscillates around the setpoint plus noise. The period is
        # measured in STEPS, so in the dense tail it reads as the controller
        # actively working minute by minute.
        checking = (
            setpoint
            + CHECKING_AMPLITUDE * math.sin(2 * math.pi * i / 48.0)
            + rng.uniform(-CHECKING_NOISE, CHECKING_NOISE)
        )

        # Savings cascade upward, saturating at the cap (60% into the window).
        ramp = min(1.0, progress / CASCADE_FRACTION)
        savings = SAVINGS_START + SAVINGS_GROWTH * ramp + 40.0 * math.sin(2 * math.pi * i / 72.0)
        savings = max(0.0, min(cap, savings))

        # Once savings saturate, the overflow cascades into the brokerage account.
        spill = max(0.0, progress - CASCADE_FRACTION) / max(1e-9, 1.0 - CASCADE_FRACTION)
        brokerage = BROKERAGE_START + BROKERAGE_GROWTH * spill + rng.uniform(-20.0, 20.0)

        points.append(
            {
                "timestamp": timestamp,
                "checking_balance": round(checking, 2),
                "savings_balance": round(savings, 2),
                "brokerage_balance": round(brokerage, 2),
                "setpoint": setpoint,
                "pid_output": round(settings.pid_kp * (checking - setpoint), 2),
            }
        )
    return points


async def seed(
    days: int,
    interval_minutes: int,
    force: bool,
    dense_minutes: int = DENSE_TAIL_MINUTES,
    dense_step_minutes: int = DENSE_TAIL_STEP_MINUTES,
) -> int:
    """Insert the synthetic history. Skips a populated table unless ``force``."""
    await create_db_and_tables()

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(func.count()).select_from(BalanceSnapshot))
        ).scalar_one()
        if existing and not force:
            print(
                f"[seed] balance_snapshots already holds {existing} rows "
                "- nothing to do (use --force to reseed)"
            )
            return 0
        if existing and force:
            await session.execute(delete(BalanceSnapshot))
            await session.commit()
            print(f"[seed] cleared {existing} existing rows")

        points = build_points(days, interval_minutes, dense_minutes, dense_step_minutes)
        for offset in range(0, len(points), BATCH_SIZE):
            batch = points[offset : offset + BATCH_SIZE]
            session.add_all([BalanceSnapshot(**point) for point in batch])
            await session.commit()

    first = points[0]["timestamp"].isoformat()
    last = points[-1]["timestamp"].isoformat()
    values = [p["checking_balance"] for p in points]
    print(
        f"[seed] inserted {len(points)} snapshots covering {days} day(s) "
        f"at {interval_minutes}m resolution"
    )
    if dense_minutes > 0:
        print(
            f"[seed] plus a dense {dense_minutes}m tail at {dense_step_minutes}m "
            "resolution, so the dashboard's 'Live' range is populated"
        )
    print(f"[seed] window: {first} -> {last}")
    print(
        f"[seed] checking range: {min(values):.2f} .. {max(values):.2f} "
        f"(setpoint {settings.pid_setpoint:.2f})"
    )
    print(f"[seed] savings cap: {settings.savings_cap:.2f}")
    return len(points)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic Floatline balance history")
    parser.add_argument("--days", type=int, default=30, help="days of history (default: 30)")
    parser.add_argument(
        "--interval-minutes", type=int, default=60, help="minutes between points (default: 60)"
    )
    parser.add_argument("--force", action="store_true", help="delete existing snapshots first")
    parser.add_argument(
        "--dense-minutes",
        type=int,
        default=DENSE_TAIL_MINUTES,
        help=f"length of the minute-resolution tail (default: {DENSE_TAIL_MINUTES})",
    )
    parser.add_argument(
        "--dense-step-minutes",
        type=int,
        default=DENSE_TAIL_STEP_MINUTES,
        help=f"minutes between tail points (default: {DENSE_TAIL_STEP_MINUTES})",
    )
    args = parser.parse_args()

    async def _run() -> None:
        try:
            await seed(args.days, args.interval_minutes, args.force)
        finally:
            await close_db()

    asyncio.run(_run())


if __name__ == "__main__":
    main()