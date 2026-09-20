# Floatline

**An autonomous liquidity management engine.** Floatline pins a Capital One Nessie
checking account to an exact cash setpoint (default **$1500**) using a
Proportional-Integral-Derivative (PID) controller, sweeps the excess into savings,
and cascades anything above the savings cap (**$5000**) into an Alpaca brokerage
account via notional market buys.

```
                 PID controller (Kp/Ki/Kd, $50 deadband)
                              |
   Nessie checking --sweep--> Nessie savings --overflow--> Alpaca (SPY)
     target $1500              capped $5000                invested
```

---

## Architecture

| Layer | Technology | Notes |
|---|---|---|
| API | FastAPI (async) | Python 3.12+, `uvicorn` |
| Scheduler | **APScheduler** `AsyncIOScheduler` | PID loop every 60s, started in the FastAPI `lifespan` |
| Live updates | **`asyncio.Queue`** per client | Server-Sent Events, in-memory broadcast |
| Database | PostgreSQL + **TimescaleDB** | `balance_snapshots` is a hypertable |
| ORM | SQLAlchemy 2.x async + `asyncpg` | async engine, no blocking I/O |
| Frontend | Astro 7 + Tailwind 4 + React 19 | React used **only** for Recharts |
| Proxy | Caddy 2 | auto-HTTPS (Let's Encrypt), serves static build, proxies `/api/*` |

### Three deliberate constraints (and how they are honoured)

1. **No Celery, no Redis.** All background work runs through `AsyncIOScheduler`
   inside `app/main.py`'s `lifespan` context manager. Redis appears nowhere in
   the stack.
2. **SSE over native `asyncio.Queue`.** `app/stream.py` keeps a
   `connected_clients: set[asyncio.Queue]`; the control loop fans every event out
   to all connected dashboards, so a browser refresh or a second tab never drops
   the stream.
3. **No client-side state library.** TimescaleDB is the single source of truth:
   `/api/telemetry` returns persisted history and `/api/stream` streams new ticks.
   React holds a small in-memory buffer for rendering only.

---

## Project layout

```
floatline/
├── docker-compose.yml         # timescaledb + fastapi_web + frontend + caddy
├── docker-compose.local.yml   # local override: Caddy on plain HTTP :8080, no ACME
├── Caddyfile                  # production: auto-SSL, static files, /api/* proxy
├── Caddyfile.local            # local: same proxy rules, localhost + HTTP
├── Makefile                   # optional wrappers around scripts/dev.py
├── .env.example               # credential template (copy to .env)
├── initdb/01-init.sql         # TimescaleDB extension + hypertable DDL
├── scripts/
│   ├── dev.py                 # one-command local stack runner (dev / prod / smoke)
│   ├── dev.cmd                # policy-proof Windows wrapper for dev.py
│   └── smoke.py               # 29-check end-to-end test against a running API
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py            # FastAPI app + lifespan + APScheduler boot
│   │   ├── config.py          # pydantic-settings (reads ../.env)
│   │   ├── db.py              # async engine, DDL, hypertable creation
│   │   ├── models.py          # BalanceSnapshot (hypertable), TransferLog
│   │   ├── nessie.py          # NessieClient  (balances, deposits, transfers)
│   │   ├── alpaca.py          # AlpacaClient  (account, notional market buys)
│   │   ├── pid.py             # calculate_pid() + anti-windup state
│   │   ├── stream.py          # connected_clients set + broadcast_event()
│   │   ├── tasks.py           # control cycle, simulations, scheduler factory
│   │   ├── routes.py          # /api/* REST + SSE endpoints
│   │   └── scripts/
│   │       └── seed_history.py  # 30 days of synthetic telemetry
│   └── tests/test_pid.py      # PID unit tests (pure math, no network)
└── frontend/
    ├── Dockerfile             # two-stage Astro build -> static assets
    ├── astro.config.mjs       # React + Tailwind 4 + /api dev proxy
    └── src/
        ├── pages/index.astro
        ├── styles/global.css
        └── components/
            ├── Dashboard.tsx        # layout, stat tiles, control buttons
            └── TelemetryChart.tsx   # Recharts + SSE subscription
```

---

## Local development & testing

Everything runs through **`scripts/dev.py`** — a *Python* launcher rather than a
shell script, because on Windows the PowerShell execution policy is `Undefined`,
which blocks both the bare `npm` shim and any `.\*.ps1` file. `py` always runs.

```powershell
py -3.14 scripts\dev.py             # backend + Astro dev server, PID loop ON
py -3.14 scripts\dev.py --no-loop   # pause real trading (pure UI work)
py -3.14 scripts\dev.py --prod      # build + serve the real dist on :4173
py -3.14 scripts\dev.py --smoke     # start the stack and run the whole test suite
py -3.14 scripts\dev.py --backend-only
```

`scripts\dev.cmd` is a double-clickable wrapper around the same runner.

| URL | What |
|---|---|
| <http://localhost:4321> | dashboard (dev, hot reload) |
| <http://localhost:4173> | dashboard (`--prod`, the real compiled `dist/`) |
| <http://127.0.0.1:8000/docs> | FastAPI docs |
| <http://127.0.0.1:8000/api/health> | health probe |

**Interpreter:** use `py -3.14`. Bare `python` on some Windows dev boxes resolves
to MSYS2's interpreter, which has none of the dependencies — `dev.py` detects
that and prints the exact command to fix it.

**The PID loop is ON by default** and will place a real Nessie transfer plus a
real (paper) Alpaca order every 60 seconds. Pass `--no-loop` when you only care
about the UI. `--smoke` disables the loop automatically so cycles are
deterministic.

### 1. Configure credentials

```bash
cp .env.example .env
# then fill in the Nessie + Alpaca keys
```

`docker-compose.yml` overrides `DATABASE_URL` to the bundled `timescaledb`
service, so the same `.env` works for Docker and bare-metal runs alike (where
the `DATABASE_URL` in `.env` — e.g. a Timescale/Tiger Cloud URL — is used).

### 2. Seed 30 days of history (so the chart isn't blank)

```powershell
cd backend
py -3.14 -m app.scripts.seed_history          # skips if data already exists
py -3.14 -m app.scripts.seed_history --force  # truncate and reseed
```

Generates 720 hourly points that oscillate around the $1500 setpoint, ramp
savings into the $5000 cap, then spill the overflow into the brokerage line.

### 3. Verify everything before deploying

```powershell
py -3.14 scripts\dev.py --smoke
```

Runs the 9 PID unit tests, then a **29-check end-to-end smoke test** covering the
API surface, telemetry schema + ordering, the PID maths, both simulate
endpoints, **SSE fan-out to two concurrent clients**, and the **full liquidity
cascade** (PID sweep → savings cap → Alpaca order → audit log). Exit code `0`
means every check passed.

To test the API only, against an already-running backend:

```powershell
py -3.14 scripts\smoke.py --base http://127.0.0.1:8000
```

> The smoke test deliberately moves **real money** on the Nessie sandbox and
> places a **real Alpaca paper order** — that is the only way to verify the
> transfer and trade paths.


---

## Docker (the production shape)

```bash
# Full stack with real TLS on :80/:443 (for the VPS)
docker compose up -d --build

# Same stack, but locally testable: plain HTTP on :8080, no ACME
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

**Why the local override exists.** The production `Caddyfile` uses the site
address `floatline.biz` with `auto_https on`. Run on a laptop, Caddy would
attempt a *real* ACME challenge for a domain you do not control. That fails, and
repeated failures can consume Let's Encrypt's failed-validation rate limit — which
would then also block your actual deployment. `Caddyfile.local` therefore uses
`localhost` + plain HTTP (`auto_https off`) while mirroring the **exact same**
proxy rules, so the routing behaviour is still faithfully tested.

The container DB starts empty, so seed it once the stack is healthy:

```bash
docker compose exec fastapi_web python -m app.scripts.seed_history
```

Notes:

* `initdb/01-init.sql` runs **only on first initialisation** of the
  `timescaledb_data` volume. To re-run it after editing: `docker compose down -v`.
* The `frontend` service is a one-shot publisher: it builds the Astro bundle and
  copies it into the `frontend_dist` volume that Caddy serves, then exits `0`.
  Seeing it as `Exited (0)` in `docker compose ps` is expected, not a failure.
* Both tables use a composite `PRIMARY KEY (id, timestamp)` because TimescaleDB
  requires the partitioning column in every unique index — including the PK.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | liveness probe |
| `GET` | `/api/config` | active setpoint, cap, PID gains, poll interval |
| `GET` | `/api/telemetry?hours=720&limit=5000` | persisted balance history (oldest -> newest) |
| `GET` | `/api/transfers?limit=50` | transfer audit log |
| `GET` | `/api/stream` | **SSE** feed of live PID ticks |
| `POST` | `/api/simulate-expense` | `{"amount": 250}` -> withdraw from checking |
| `POST` | `/api/simulate-income` | `{"amount": 500}` -> deposit into checking |

### SSE payload

```json
{
  "type": "tick",
  "timestamp": "2026-09-19T22:35:26+00:00",
  "checking_balance": 2274.73,
  "savings_balance": 4996.51,
  "brokerage_balance": 2012.54,
  "setpoint": 1500.0,
  "pid_output": 464.84,
  "transfers": [
    {"type": "pid_sweep", "direction": "to_savings", "amount": 464.84, "status": "executed", "pid_output": 464.84},
    {"type": "brokerage_sweep", "amount": 461.35, "symbol": "SPY", "status": "executed"}
  ]
}
```

---

## PID controller

`app/pid.py` implements a textbook PID with two safety rails:

```python
error      = current_checking_balance - setpoint   # >0 => over-funded
integral  += error * dt                            # clamped to +/-10000 (anti-windup)
derivative = (error - prev_error) / dt
output     = Kp*error + Ki*integral + Kd*derivative
if abs(output) < deadband: return 0                # default $50: never spam the API
```

* **`output > 0`** -> sweep from checking **to** savings.
* **`output < 0`** -> pull the shortfall **from** savings into checking.
* `dt` is supplied in **minutes** so the configured gains stay well damped.
* Transfers are clamped to the available source balance before hitting Nessie.

```bash
cd backend && python -m pytest tests -q     # 9 tests
```

---

## Two behaviours worth knowing

**1. Shadow ledger (`BALANCE_SOURCE`, default `ledger`)**

The Capital One Nessie sandbox accepts every deposit, withdrawal and transfer
(HTTP 201, with real transaction records) but **never mutates an account's
`balance` field** -- it stays frozen. A controller reading live balances would
therefore observe a constant number and never converge. Floatline records the
effect of each transaction it issues in TimescaleDB and treats that ledger as the
observed balance. Set `BALANCE_SOURCE=live` to drive an account whose balances do
persist.

**2. Brokerage sweep model**

Real ACH linkage between Nessie and Alpaca cannot be wired up in a hackathon, so
the savings-cap overflow is modelled as two coordinated actions: a Nessie
withdrawal from savings for the excess, plus an Alpaca notional market buy of the
same amount in `ALPACA_ORDER_SYMBOL` (default `SPY`).

**Nessie base URL.** The default is `https://api.nessieisreal.com`. Some networks
refuse plain HTTP on port 80, which is where Nessie's own docs point; the HTTPS
host serves the identical API. Override with `NESSIE_BASE_URL` if you prefer HTTP.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NESSIE_BASE_URL` | `https://api.nessieisreal.com` | Nessie API root |
| `NESSIE_API_KEY` | -- | Nessie API key (`?key=` query param) |
| `NESSIE_CUSTOMER_ID` | -- | Nessie customer UUID |
| `NESSIE_CHECKING_ACCOUNT_ID` | -- | account pinned to the setpoint |
| `NESSIE_SAVINGS_ACCOUNT_ID` | -- | sweep destination |
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | -- | Alpaca paper credentials |
| `APCA_API_BASE_URL` | `https://paper-api.alpaca.markets/v2` | Alpaca API root |
| `ALPACA_ORDER_SYMBOL` | `SPY` | symbol bought with saved overflow |
| `DATABASE_URL` | -- | PostgreSQL/TimescaleDB connection string |
| `PID_SETPOINT` | `1500` | target checking balance |
| `PID_KP` / `PID_KI` / `PID_KD` | `0.5` / `0.1` / `0.05` | controller gains |
| `PID_DEADBAND` | `50` | minimum transfer size |
| `SAVINGS_CAP` | `5000` | overflow threshold -> brokerage |
| `POLL_INTERVAL` | `60` | control-loop period (seconds) |
| `BALANCE_SOURCE` | `ledger` | `ledger` or `live` |
| `CONTROL_LOOP_ENABLED` | `true` | set to `false` to disable the scheduler |

---

## Troubleshooting

* **TimescaleDB: `cannot create a unique index without the column "timestamp"`.**
  Hypertables require the partitioning column inside the primary key, so both
  tables use a composite `PRIMARY KEY (id, timestamp)`. `app/db.py` creates
  hypertables idempotently and tolerates a missing extension (plain PostgreSQL
  still works, you just lose time-series partitioning).
* **`TypeError: connect() got an unexpected keyword argument 'sslmode'`.**
  `asyncpg` does not understand libpq's `sslmode`. `app/db.py` strips it from the
  URL and passes `ssl=True` through `connect_args` when TLS is required.
* **Container is permanently `unhealthy`.** The REST router is mounted under
  `/api`, so the health path is `/api/health` — not `/health`. Both the
  `Dockerfile` `HEALTHCHECK` and the Caddyfile must use the `/api` prefix.
* **`/api/*` returns the dashboard's HTML instead of JSON.** Caddy orders
  `try_files` *before* `reverse_proxy`, so a bare `try_files {path} /index.html`
  rewrites API requests to the SPA. The fix is mutually-exclusive `handle`
  blocks (see `Caddyfile`), which also keeps the SPA fallback scoped to static
  assets only.
* **Telemetry is still being sent.** The `timescale/timescaledb` image reads
  `TIMESCALEDB_TELEMETRY=off`; other spellings are silently ignored. It only
  applies while the data volume is being created.
* **Chart is empty on first load.** Run the seed script (Local development,
  step 2).
* **No live updates.** Confirm `/api/stream` stays open; the endpoint emits a
  `: keepalive` comment every 15s to survive proxy timeouts.
* **Nessie API key appearing in logs.** The key travels as the `key` *query
  parameter*, and `httpx` logs full URLs at INFO. `app/main.py` pins the `httpx`
  and `httpcore` loggers to `WARNING` to keep it out of container logs.

