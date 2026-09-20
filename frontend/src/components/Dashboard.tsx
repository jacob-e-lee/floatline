/**
 * Dashboard.tsx
 *
 * The single interactive island on the page. It renders the Recharts telemetry
 * graph, live balance cards, the PID activity feed and the stage-demo controls.
 *
 * Data flow: the FastAPI backend (backed by TimescaleDB) is the source of truth.
 * This component only MIRRORS it - history via GET /api/telemetry, live updates
 * via the SSE stream, mutations via POST /api/simulate-* - so there is no
 * client-side state library anywhere in the app.
 */
import { useEffect, useState } from 'react';
import TelemetryChart, { COLORS, WINDOWS, money, useTelemetry } from './TelemetryChart';

const DEMO_AMOUNTS = [250, 1000] as const;

/**
 * Shows how stale the feed is. Ticks once a second so a stalled stream becomes
 * obvious instead of being inferred from a motionless line.
 */
function TickAge({ lastTickAt }: { lastTickAt: number | null }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  if (lastTickAt === null) {
    return <span className="tabular text-xs text-slate-400">awaiting first update</span>;
  }
  const seconds = Math.max(0, Math.round((Date.now() - lastTickAt) / 1000));
  return (
    <span className="tabular text-xs text-slate-400">
      Last updated {seconds}s ago
    </span>
  );
}

type AccountSegment = {
  label: string;
  value: number;
  color: string;
};

/**
 * The hero readout: one big total balance plus a proportional bar showing how
 * the total is split between checking, savings and brokerage. Segments keep a
 * proportional width; the legend below always lists every account's value.
 */
function TotalHero({ total, segments }: { total: number; segments: AccountSegment[] }) {
  const positive = segments.map((s) => ({ ...s, value: Math.max(0, s.value) }));
  const sum = positive.reduce((acc, s) => acc + s.value, 0);
  return (
    <div className="panel p-5">
      <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Total balance</p>
      <p className="tabular mt-1 text-4xl font-semibold text-slate-900">{money(total)}</p>
      <div className="mt-4 flex h-2.5 w-full overflow-hidden rounded-sm bg-slate-100">
        {positive.map((segment) => (
          <div
            key={segment.label}
            style={{
              width: sum > 0 ? `${(segment.value / sum) * 100}%` : '33.33%',
              backgroundColor: segment.color,
            }}
          />
        ))}
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
        {positive.map((segment) => (
          <div key={segment.label} className="flex items-center gap-2 text-sm">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ backgroundColor: segment.color }}
            />
            <span className="text-slate-500">{segment.label}</span>
            <span className="tabular ml-auto font-medium text-slate-900">
              {money(segment.value)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function Dashboard() {
  const {
    points,
    transfers,
    latest,
    setpoint,
    savingsCap,
    error,
    busy,
    windowId,
    selectWindow,
    lastTickAt,
    updateConfig,
    refresh,
    simulate,
  } = useTelemetry();

  const checking = latest?.checking_balance ?? 0;
  const savings = latest?.savings_balance ?? 0;
  const brokerage = latest?.brokerage_balance ?? 0;
  const total = checking + savings + brokerage;

  // Editable copies of the runtime targets; re-synced whenever the backend
  // confirms new values (SSE config event, refresh, or a successful save).
  const [setpointDraft, setSetpointDraft] = useState(String(setpoint));
  const [capDraft, setCapDraft] = useState(String(savingsCap));
  const [savingTargets, setSavingTargets] = useState(false);
  useEffect(() => {
    setSetpointDraft(String(setpoint));
    setCapDraft(String(savingsCap));
  }, [setpoint, savingsCap]);

  const saveTargets = async () => {
    const nextSetpoint = Number(setpointDraft);
    const nextCap = Number(capDraft);
    if (!Number.isFinite(nextSetpoint) || nextSetpoint <= 0) return;
    if (!Number.isFinite(nextCap) || nextCap <= 0) return;
    setSavingTargets(true);
    await updateConfig(nextSetpoint, nextCap);
    setSavingTargets(false);
  };

  return (
    <main className="mx-auto w-full max-w-6xl px-5 py-10">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Floatline
            <span className="ml-3 align-middle text-xs font-normal uppercase tracking-[0.2em] text-slate-400">
              autonomous liquidity engine
            </span>
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-slate-500">
            A PID controller holds checking at{' '}
            <span className="tabular text-slate-900">{money(setpoint, 0)}</span>, sweeps excess to
            savings, and cascades anything past{' '}
            <span className="tabular text-slate-900">{money(savingsCap, 0)}</span> into the Alpaca
            brokerage account.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <TickAge lastTickAt={lastTickAt} />
          <button
            type="button"
            onClick={() => void refresh()}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-500 transition hover:text-slate-900"
          >
            Refresh
          </button>
        </div>
      </header>

      <TotalHero
        total={total}
        segments={[
          { label: 'Checking', value: checking, color: COLORS.checking },
          { label: 'Savings', value: savings, color: COLORS.savings },
          { label: 'Brokerage', value: brokerage, color: COLORS.brokerage },
        ]}
      />

      <div className="mb-6 mt-4 flex flex-wrap items-end gap-3 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-[11px] uppercase tracking-[0.14em] text-slate-500">
            Checking setpoint
          </span>
          <input
            type="number"
            min="1"
            step="50"
            value={setpointDraft}
            onChange={(event) => setSetpointDraft(event.target.value)}
            className="tabular w-28 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-slate-500 focus:outline-none"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[11px] uppercase tracking-[0.14em] text-slate-500">
            Savings limit
          </span>
          <input
            type="number"
            min="1"
            step="100"
            value={capDraft}
            onChange={(event) => setCapDraft(event.target.value)}
            className="tabular w-28 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-slate-500 focus:outline-none"
          />
        </label>
        <button
          type="button"
          disabled={savingTargets}
          onClick={() => void saveTargets()}
          className="rounded-md bg-slate-900 px-4 py-1.5 text-sm text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {savingTargets ? 'Saving...' : 'Save targets'}
        </button>
        <span className="pb-2 text-xs text-slate-400">
          Applies to the next PID cycle.
        </span>
      </div>

      {error ? (
        <div className="mb-6 rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {error}
        </div>
      ) : null}

      <section className="panel mb-6 p-4">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div
            role="group"
            aria-label="History range"
            className="inline-flex flex-wrap gap-1 rounded-md border border-slate-200 bg-slate-50 p-1"
          >
            {WINDOWS.map((option) => {
              const active = option.id === windowId;
              return (
                <button
                  key={option.id}
                  type="button"
                  aria-pressed={active}
                  onClick={() => selectWindow(option.id)}
                  className={
                    'rounded-sm px-3 py-1 text-xs transition ' +
                    (active
                      ? 'bg-white text-slate-900 shadow-none ring-1 ring-slate-300'
                      : 'text-slate-500 hover:text-slate-900')
                  }
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </div>
        <TelemetryChart
          points={points}
          setpoint={setpoint}
          savingsCap={savingsCap}
          windowId={windowId}
        />
      </section>

      <section className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="panel p-4 lg:col-span-2">
          <h2 className="mb-3 text-sm font-medium text-slate-900">Simulate activity</h2>
          <p className="mb-4 text-xs text-slate-500">
            Writes a real deposit / withdrawal to the Capital One Nessie account. The next PID tick
            reacts and the graph updates over SSE.
          </p>
          <div className="flex flex-wrap gap-3">
            {DEMO_AMOUNTS.map((amount) => (
              <button
                key={`income-${amount}`}
                type="button"
                disabled={busy !== null}
                onClick={() => void simulate('income', amount)}
                className="rounded-md bg-emerald-600 px-4 py-2 text-sm text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy === 'income' ? 'Sending...' : `Simulate income +${money(amount, 0)}`}
              </button>
            ))}
            {DEMO_AMOUNTS.map((amount) => (
              <button
                key={`expense-${amount}`}
                type="button"
                disabled={busy !== null}
                onClick={() => void simulate('expense', amount)}
                className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm text-slate-700 transition hover:border-rose-400 hover:text-rose-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy === 'expense' ? 'Sending...' : `Simulate expense -${money(amount, 0)}`}
              </button>
            ))}
          </div>
        </div>

        <div className="panel p-4">
          <h2 className="mb-3 text-sm font-medium text-slate-900">Activity</h2>
          {transfers.length === 0 ? (
            <p className="text-xs text-slate-500">No transfers yet.</p>
          ) : (
            <ul className="space-y-2">
              {transfers.slice(0, 8).map((transfer, index) => {
                // Sign + color from the transfer type: money arriving in an
                // account (income, sweep into savings, brokerage sweep) is
                // green "+", money leaving (expense, pull from savings) is
                // red "-". Failed entries keep an explicit badge.
                const positive = /(_income|to_savings|brokerage_sweep)$/.test(transfer.type);
                const failed = transfer.status === 'failed';
                const sign = positive ? '+' : '-';
                const amountClass = failed
                  ? 'text-slate-400'
                  : positive
                    ? 'text-emerald-600'
                    : 'text-rose-600';
                return (
                  <li
                    key={`${transfer.type}-${index}`}
                    className="flex items-center justify-between gap-3 text-xs"
                  >
                    <span className="truncate text-slate-500">
                      {transfer.type.replace(/_/g, ' ')}
                    </span>
                    <span className={`tabular whitespace-nowrap font-medium ${amountClass}`}>
                      {sign}
                      {money(transfer.amount, 2)}
                    </span>
                    {failed ? <span className="text-rose-600">failed</span> : null}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </section>

      <footer className="mt-10 text-center text-[11px] text-slate-400">
        FastAPI + APScheduler (AsyncIOScheduler) &middot; TimescaleDB telemetry &middot; SSE via
        asyncio.Queue &middot; Nessie + Alpaca paper
      </footer>

    </main>
  );
}
