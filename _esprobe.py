#!/usr/bin/env python3
"""EventSource SSE probe — tests whether the backend push events to clients.

Run:  py -3.14 _esprobe.py
       (backend must be running: py -3.14 scripts/dev.py --backend-only)
"""
import asyncio
import json
import time
import urllib.request
import urllib.error

BACKEND = "http://127.0.0.1:8000/api/stream"


async def probe() -> None:
    print(f"Connecting to {BACKEND} ...")
    try:
        req = urllib.request.Request(BACKEND)
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  HTTP {resp.status}")
            print(f"  Content-Type: {resp.headers.get('Content-Type')}")
            print(f"  Cache-Control: {resp.headers.get('Cache-Control')}")
            print()
            print("Reading SSE stream for 8 s ...")
            deadline = time.time() + 8
            n = 0
            for raw in resp:
                if time.time() > deadline:
                    print(f"\n  TIMEOUT — {n} events in 8 s")
                    return
                line = raw.decode("utf-8").strip()
                if line.startswith("data:"):
                    n += 1
                    payload = json.loads(line[5:].strip())
                    chk = payload.get("checking_balance", "??")
                    sav = payload.get("savings_balance", "??")
                    print(f"  event {n:>2}  type={payload.get('type')}  chk={chk}  sav={sav}")
                elif line == ": keepalive":
                    print("  keepalive")
                elif line:
                    print(f"  [raw] {line[:90]}")
            print(f"\n  stream closed after {n} events")
            if n == 0:
                print("  !!! NO EVENTS — SSE broken or backend idle !!!")
    except urllib.error.URLError as exc:
        print(f"  connection failed: {exc}")
        print("  start the backend:  py -3.14 scripts/dev.py --backend-only")
    except Exception as exc:
        print(f"  error: {type(exc).__name__}: {exc}")


asyncio.run(probe())
