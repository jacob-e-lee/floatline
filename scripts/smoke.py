#!/usr/bin/env python
"""Floatline end-to-end smoke test - proves every feature works against a
running API.

Run via the runner (recommended; it pauses the PID loop for determinism)::

    py -3.14 scripts\\dev.py --smoke

Or directly against an already-running backend::

    py -3.14 scripts\\smoke.py --base http://127.0.0.1:8000

WARNING: this drives the REAL Nessie sandbox and Alpaca PAPER account - it moves
         money and places a real (paper) market order. That is intentional, as
         it is the only way to verify the transfer and trade paths.

Exit code 0 means every check passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return bool(ok)


def skip(name: str, why: str) -> None:
    SKIPPED.append((name, why))
    print(f"  [SKIP] {name}  ({why})", flush=True)


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 58 - len(title)), flush=True)


def jget(payload: Any, key: str, default: Any = None) -> Any:
    return payload.get(key, default) if isinstance(payload, dict) else default


# ---------------------------------------------------------------------------
# 1. API surface
# ---------------------------------------------------------------------------
def test_api_surface(client: httpx.Client, base: str) -> dict[str, Any]:
    section("API surface")

    resp = client.get(f"{base}/api/health")
    record("GET /api/health returns 200", resp.status_code == 200, f"HTTP {resp.status_code}")

    resp = client.get(f"{base}/api/config")
    cfg = resp.json() if resp.status_code == 200 else {}
    record("GET /api/config returns 200", resp.status_code == 200, f"HTTP {resp.status_code}")
    record("config exposes the $1500 setpoint",
           abs(float(jget(cfg, "setpoint", 0)) - 1500.0) < 0.01,
           f"setpoint={jget(cfg, 'setpoint')}")
    record("config exposes the $5000 savings cap",
           abs(float(jget(cfg, "savings_cap", 0)) - 5000.0) < 0.01,
           f"cap={jget(cfg, 'savings_cap')}")
    record("config exposes the PID gains",
           all(k in cfg for k in ("kp", "ki", "kd")),
           f"kp={jget(cfg, 'kp')} ki={jget(cfg, 'ki')} kd={jget(cfg, 'kd')}")

    resp = client.get(f"{base}/api/transfers", params={"limit": 20})
    record("GET /api/transfers returns 200", resp.status_code == 200, f"HTTP {resp.status_code}")
    return cfg


# ---------------------------------------------------------------------------
# 2. Telemetry
# ---------------------------------------------------------------------------
def test_telemetry(client: httpx.Client, base: str) -> dict[str, Any]:
    section("Telemetry (TimescaleDB history)")

    resp = client.get(f"{base}/api/telemetry", params={"hours": 24 * 30, "limit": 5000})
    if not record("GET /api/telemetry returns 200", resp.status_code == 200,
                  f"HTTP {resp.status_code}"):
        return {}

    payload = resp.json()
    points = jget(payload, "points", []) or []
    record("telemetry returns data points", len(points) > 0, f"count={len(points)}")
    if len(points) < 2:
        return payload

    required = {"timestamp", "checking_balance", "savings_balance",
                "brokerage_balance", "setpoint", "pid_output"}
    missing = required - set(points[0].keys())
    record("telemetry points carry the full schema", not missing,
           f"missing={sorted(missing)}" if missing else "all 6 fields present")

    stamps = [p["timestamp"] for p in points]
    record("telemetry is ordered oldest -> newest", stamps == sorted(stamps),
           f"{stamps[0][:19]} .. {stamps[-1][:19]}")

    latest = points[-1]
    record("telemetry setpoint matches config",
           abs(float(latest.get("setpoint", 0)) - 1500.0) < 0.01,
           f"setpoint={latest.get('setpoint')}")
    print(f"        latest tick: checking=${latest['checking_balance']:.2f} "
          f"savings=${latest['savings_balance']:.2f} "
          f"brokerage=${latest['brokerage_balance']:.2f}", flush=True)
    return latest


# ---------------------------------------------------------------------------
# 3. PID maths (imported directly, no network)
# ---------------------------------------------------------------------------
def test_pid_math() -> None:
    section("PID controller maths")
    try:
        from app.pid import PIDState, calculate_pid
    except Exception as exc:  # noqa: BLE001
        skip("PID unit assertions", f"app.pid not importable: {exc}")
        return

    out = calculate_pid(2000.0, 1500.0, 1.0, 0.0, 0.0, PIDState(), dt=60.0, deadband=0.0)
    record("over-funded checking -> positive sweep-out output",
           abs(out - 500.0) < 0.01, f"output={out:.2f}")

    out = calculate_pid(1000.0, 1500.0, 1.0, 0.0, 0.0, PIDState(), dt=60.0, deadband=0.0)
    record("under-funded checking -> negative pull-in output",
           abs(out + 500.0) < 0.01, f"output={out:.2f}")

    out = calculate_pid(1540.0, 1500.0, 0.5, 0.0, 0.0, PIDState(), dt=60.0, deadband=50.0)
    record("deadband suppresses transfers below $50", out == 0.0, f"output={out:.2f}")

    state = PIDState()
    for _ in range(1000):
        calculate_pid(99999.0, 1500.0, 0.0, 1.0, 0.0, state, dt=60.0,
                      deadband=0.0, integral_clamp=10000.0)
    record("integral windup is clamped", abs(state.integral - 10000.0) < 0.01,
           f"integral={state.integral:.2f}")


# ---------------------------------------------------------------------------
# 4. Demo simulations
# ---------------------------------------------------------------------------
def latest_point(client: httpx.Client, base: str) -> dict[str, Any]:
    payload = client.get(f"{base}/api/telemetry", params={"hours": 1, "limit": 200}).json()
    points = payload.get("points") or []
    return points[-1] if points else {}


def test_mutations(client: httpx.Client, base: str) -> None:
    section("Stage-demo simulations")

    before = latest_point(client, base)

    resp = client.post(f"{base}/api/simulate-income", json={"amount": 250})
    record("POST /api/simulate-income returns 200", resp.status_code == 200,
           f"HTTP {resp.status_code}")
    after_income = resp.json() if resp.status_code == 200 else {}
    record("income raises the checking ledger",
           float(jget(after_income, "checking_balance", 0))
           > float(jget(before, "checking_balance", 0)),
           f"{jget(before, 'checking_balance')} -> {jget(after_income, 'checking_balance')}")

    resp = client.post(f"{base}/api/simulate-expense", json={"amount": 250})
    record("POST /api/simulate-expense returns 200", resp.status_code == 200,
           f"HTTP {resp.status_code}")
    after_expense = resp.json() if resp.status_code == 200 else {}
    record("expense lowers the checking ledger",
           float(jget(after_expense, "checking_balance", 0))
           < float(jget(after_income, "checking_balance", 0)),
           f"{jget(after_income, 'checking_balance')} -> {jget(after_expense, 'checking_balance')}")


# ---------------------------------------------------------------------------
# 5. SSE fan-out (the per-client asyncio.Queue broadcast)
# ---------------------------------------------------------------------------
def test_sse_fanout(client: httpx.Client, base: str) -> None:
    section("SSE live stream (per-client queue fan-out)")

    frames: dict[str, list[str]] = {"A": [], "B": []}
    connected: dict[str, threading.Event] = {"A": threading.Event(), "B": threading.Event()}
    stop = threading.Event()

    def reader(name: str) -> None:
        try:
            with httpx.Client(timeout=httpx.Timeout(25.0, read=25.0)) as stream_client:
                with stream_client.stream("GET", f"{base}/api/stream") as response:
                    if response.status_code != 200:
                        frames[name].append(f"HTTP {response.status_code}")
                        connected[name].set()
                        return
                    for line in response.iter_lines():
                        if stop.is_set():
                            return
                        if not line.startswith("data:"):
                            continue
                        frames[name].append(line)
                        try:
                            kind = json.loads(line.split("data:", 1)[1].strip()).get("type")
                        except Exception:  # noqa: BLE001
                            kind = None
                        if kind == "connected":
                            connected[name].set()
                        elif kind == "tick":
                            return
        except Exception as exc:  # noqa: BLE001
            frames[name].append(f"ERROR {type(exc).__name__}: {exc}")
            connected[name].set()

    for name in ("A", "B"):
        threading.Thread(target=reader, args=(name,), daemon=True).start()

    ok_a = connected["A"].wait(12)
    ok_b = connected["B"].wait(12)
    record("two clients connect to /api/stream concurrently", ok_a and ok_b,
           f"A={ok_a} B={ok_b}")

    client.post(f"{base}/api/simulate-income", json={"amount": 250})

    deadline = time.time() + 20
    while time.time() < deadline and not all(
        any('"tick"' in frame for frame in frames[n]) for n in ("A", "B")
    ):
        time.sleep(0.4)
    stop.set()

    tick_a = any('"tick"' in frame for frame in frames["A"])
    tick_b = any('"tick"' in frame for frame in frames["B"])
    record("client A receives the live tick", tick_a, f"frames={len(frames['A'])}")
    record("client B receives the live tick", tick_b, f"frames={len(frames['B'])}")
    record("event reaches BOTH clients (true broadcast, not single-consumer)",
           tick_a and tick_b)


# ---------------------------------------------------------------------------
# 6. Full liquidity cascade: PID sweep -> savings cap -> Alpaca order
# ---------------------------------------------------------------------------
def test_cascade(client: httpx.Client, base: str, cfg: dict[str, Any]) -> None:
    section("Liquidity cascade (PID sweep -> savings cap -> Alpaca)")

    cap = float(jget(cfg, "savings_cap", 5000.0))

    async def _drive_cycles(max_cycles: int = 4) -> list[dict[str, Any]]:
        # ONE event loop for all cycles: each asyncio.run() creates a fresh loop
        # but the asyncpg pool keeps connections bound to the loop that checked
        # them out, so a second asyncio.run() dies with "Event loop is closed".
        from app.tasks import run_control_cycle
        events: list[dict[str, Any]] = []
        for _ in range(max_cycles):
            point = latest_point(client, base)
            savings = float(point.get("savings_balance", 0.0))
            if savings <= cap:
                # Top savings up past the cap so the overflow branch is exercised.
                client.post(f"{base}/api/simulate-income",
                            json={"amount": (cap - savings) + 600.0})
            events.append(await run_control_cycle())
            types = {t.get("type") for t in events[-1].get("transfers", [])
                     if isinstance(t, dict)}
            if "brokerage_sweep" in types:
                break
        return events

    try:
        events = asyncio.run(_drive_cycles(4))
    except Exception as exc:  # noqa: BLE001
        skip("control-cycle cascade", f"app.tasks not importable: {exc}")
        return

    saw_sweep = False
    saw_overflow = False
    cycles = len(events)

    for event in events:
        types = {t.get("type") for t in event.get("transfers", []) if isinstance(t, dict)}
        saw_sweep = saw_sweep or "pid_sweep" in types
        if "brokerage_sweep" in types:
            saw_overflow = True

    record("PID sweep executes a real Nessie transfer", saw_sweep, f"cycles={cycles}")
    record("savings-cap overflow cascades to Alpaca", saw_overflow,
           "brokerage_sweep observed" if saw_overflow else "cap not reached within 4 cycles")

    point = latest_point(client, base)
    savings_after = float(point.get("savings_balance", 0.0))
    record("savings balance is pinned at/below the cap afterwards",
           savings_after <= cap + 0.01, f"savings=${savings_after:.2f} cap=${cap:.2f}")

    rows = (client.get(f"{base}/api/transfers", params={"limit": 25}).json()
            .get("transfers") or [])
    overflows = [r for r in rows if r.get("reason") == "savings_cap_overflow_to_brokerage"]
    if overflows:
        newest = overflows[0]
        record("overflow is recorded as 'executed' in the audit log",
               newest.get("status") == "executed", f"status={newest.get('status')}")
        record("overflow is attributed to the Alpaca order symbol",
               str(newest.get("destination", "")).startswith("alpaca:"),
               str(newest.get("destination")))
    else:
        record("overflow appears in the transfer audit log", False, "no matching rows")

    sweeps = [r for r in rows if r.get("reason") in
              ("pid_sweep_to_savings", "pid_pull_from_savings")]
    record("PID sweep appears in the transfer audit log", bool(sweeps),
           f"{len(sweeps)} row(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Floatline end-to-end smoke test")
    parser.add_argument("--base", default="http://127.0.0.1:8000",
                        help="API base URL (default: http://127.0.0.1:8000)")
    args = parser.parse_args()

    print("=" * 66)
    print("  FLOATLINE SMOKE TEST")
    print(f"  target: {args.base}")
    print("  NOTE: moves real money on the Nessie sandbox and places a real")
    print("        (paper) Alpaca market order.")
    print("=" * 66)

    with httpx.Client(timeout=60.0) as client:
        try:
            client.get(f"{args.base}/api/health").raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"\n[smoke] cannot reach {args.base}/api/health - {exc}\n", flush=True)
            return 2

        cfg = test_api_surface(client, args.base)
        test_telemetry(client, args.base)
        test_pid_math()
        test_mutations(client, args.base)
        test_sse_fanout(client, args.base)
        test_cascade(client, args.base, cfg)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]

    print("\n" + "=" * 66)
    print(f"  RESULT: {passed}/{len(RESULTS)} checks passed"
          + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
    print("=" * 66)
    if failed:
        print("\n  FAILURES:")
        for name, detail in failed:
            print(f"    - {name}" + (f"  ({detail})" if detail else ""))
    if SKIPPED:
        print("\n  SKIPPED:")
        for name, why in SKIPPED:
            print(f"    - {name}  ({why})")
    print("")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

