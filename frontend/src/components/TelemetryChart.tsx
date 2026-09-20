/**
 * TelemetryChart.tsx
 *
 * Owns the live data subscription for the Floatline dashboard and renders the
 * Recharts time-series graph.
 *
 *  - `useTelemetry()` mirrors the backend: GET /api/telemetry seeds the history
 *    for the selected window, then GET /api/stream (Server-Sent Events) appends
 *    live PID ticks. A slow reconciliation poll backs the stream up, so the
 *    graph can never silently freeze. TimescaleDB stays the source of truth.
 *  - `TelemetryChart` plots Checking, Savings and Brokerage against the $1500
 *    setpoint reference line, on a REAL time axis.
 *
 * WHY THE TIME AXIS AND THE TIME WINDOW BOTH MATTER:
 * The control loop ticks once a minute. Recharts' default X axis is a *category*
 * axis, which spaces points evenly by index and ignores the real gaps between
 * them - so 30 days of hourly history and a one-minute tick each advanced by the
 * same single step, and a tick moved the line by under two pixels on a 600-point
 * series. The graph looked frozen even though SSE was delivering every frame.
 * A `type="number" scale="time"` axis plus window-based trimming (instead of a
 * fixed point cap) makes live movement plainly visible.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

const API_BASE = '/api';

/**
 * Selectable history windows. `Live 1h` is the default because a 60-second PID
 * tick occupies roughly 1/60th of the chart width there - clearly visible -
 * whereas on a 30-day axis the same tick is sub-pixel.
 */
export const WINDOWS = [
  { id: '1h', label: 'Live 1h', hours: 1 },
  { id: '6h', label: '6h', hours: 6 },
  { id: '24h', label: '24h', hours: 24 },
  { id: '7d', label: '7d', hours: 24 * 7 },
  { id: '30d', label: '30d', hours: 24 * 30 },
] as const;

export type WindowId = (typeof WINDOWS)[number]['id'];

export const DEFAULT_WINDOW: WindowId = '1h';

/** Hard ceiling on rendered points so a dense window cannot lock up the tab. */
const MAX_RENDERED_POINTS = 2000;

/** Reconciliation poll period, in ms - the safety net behind the SSE stream. */
const POLL_INTERVAL_MS = 15_000;

function windowHours(id: WindowId): number {
  return (WINDOWS.find((option) => option.id === id) ?? WINDOWS[0]).hours;
}

export const COLORS = {
  checking: '#2563eb',
  savings: '#059669',
  brokerage: '#7c3aed',
  total: '#0f172a',
  setpoint: '#b45309',
  cap: '#94a3b8',
} as const;

export type TelemetryPoint = {
  /** Epoch milliseconds - drives the time-scaled X axis. */
  t: number;
  timestamp: string;
  checking_balance: number;
  savings_balance: number;
  brokerage_balance: number;
  setpoint: number;
  pid_output: number;
};

/** API rows carry an ISO timestamp; the chart needs a numeric one. */
function toPoint(row: Omit<TelemetryPoint, 't'>): TelemetryPoint {
  const parsed = new Date(row.timestamp).getTime();
  return { ...row, t: Number.isFinite(parsed) ? parsed : Date.now() };
}

/**
 * Chart row = a real telemetry point (or a PID-projected one) plus the derived
 * stack total. `projected` marks rows that were extrapolated rather than
 * observed.
 */
type ChartPoint = TelemetryPoint & {
  /** Sum of the three balances. */
  total: number;
  projected: boolean;
};

/**
 * PID projection horizon. The control loop runs once per POLL_INTERVAL (60s),
 * so the controller's pid_output is a *rate* (dollars/minute) valid for one
 * cycle. Extrapolating further than that would invent behaviour the controller
 * never committed to.
 */
const PROJECTION_CAP_MS = 60_000;

/** Sample the projection once per second for a smooth leading edge. */
const PROJECTION_STEP_MS = 1_000;

/**
 * Extend the last real telemetry point forward to `now` using the controller's
 * own intent: a positive pid_output sweeps checking -> savings, a negative one
 * pulls savings -> checking. Brokerage only changes on savings-cap overflow,
 * which never happens mid-cycle, so it stays flat, and the total is invariant
 * under a sweep (money moves, it does not appear or disappear). When a new
 * real tick arrives it simply becomes the new last point and the projection
 * recomputes from there - merge, then restart.
 */
function buildChartData(points: TelemetryPoint[], now: number): ChartPoint[] {
  const data: ChartPoint[] = points.map((point) => ({
    ...point,
    total: point.checking_balance + point.savings_balance + point.brokerage_balance,
    projected: false,
  }));

  const last = points[points.length - 1];
  if (!last) return data;

  const elapsed = now - last.t;
  if (elapsed <= 0) return data;

  const lastTotal = last.checking_balance + last.savings_balance + last.brokerage_balance;
  const horizon = Math.min(elapsed, PROJECTION_CAP_MS);
  const dollarsPerMs = last.pid_output / 60_000;
  const start = Math.max(last.t + PROJECTION_STEP_MS, now - horizon);

  for (let t = start; t <= last.t + horizon; t += PROJECTION_STEP_MS) {
    const swept = dollarsPerMs * (t - last.t);
    data.push({
      t,
      timestamp: new Date(t).toISOString(),
      checking_balance: last.checking_balance - swept,
      savings_balance: last.savings_balance + swept,
      brokerage_balance: last.brokerage_balance,
      setpoint: last.setpoint,
      pid_output: last.pid_output,
      total: lastTotal,
      projected: true,
    });
  }

  return data;
}

export type TransferSummary = {
  type: string;
  amount: number;
  status: string;
  direction?: string;
  pid_output?: number;
  symbol?: string;
};

export type StreamStatus = 'connecting' | 'live' | 'reconnecting';

export type TelemetryState = {
  points: TelemetryPoint[];
  transfers: TransferSummary[];
  latest: TelemetryPoint | null;
  setpoint: number;
  savingsCap: number;
  status: StreamStatus;
  error: string | null;
  busy: string | null;
  /** Currently selected history window. */
  windowId: WindowId;
  selectWindow: (id: WindowId) => void;
  /** Epoch ms of the last SSE tick, or null if none has arrived yet. */
  lastTickAt: number | null;
  /** Persist new setpoint / savings-cap values (POST /api/config). */
  updateConfig: (setpoint: number, savingsCap: number) => Promise<boolean>;
  /** Force a refetch from the source of truth. */
  refresh: () => Promise<void>;
  simulate: (kind: 'income' | 'expense', amount: number) => Promise<void>;
};

export function money(value: number, digits = 2): string {
  return `$${value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** Deduplicate points by timestamp, keeping the latest value for each moment. */
function mergePoints(
  backend: TelemetryPoint[],
  previous: TelemetryPoint[],
  live: TelemetryPoint | null,
  windowHours_: number,
): TelemetryPoint[] {
  const cutoff = Date.now() - windowHours_ * 3_600_000;

  // Combine all sources and dedupe by timestamp (latest value wins).
  const map = new Map<number, TelemetryPoint>();
  for (const p of [...backend, ...previous, ...(live ? [live] : [])]) {
    if (p.t >= cutoff) map.set(p.t, p);
  }
  const merged = Array.from(map.values()).sort((a, b) => a.t - b.t);

  // Trim to the hard ceiling.
  return merged.length > MAX_RENDERED_POINTS
    ? merged.slice(merged.length - MAX_RENDERED_POINTS)
    : merged;
}

function clockSecondsLabel(value: number): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString('en-US', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function clockLabel(value: number): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString('en-US', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  });
}

function dayLabel(value: number): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/**
 * Subscribes to the backend: history for the selected window, live SSE ticks,
 * and a slow reconciliation poll so the graph can never silently freeze.
 */
export function useTelemetry(): TelemetryState {
  const [points, setPoints] = useState<TelemetryPoint[]>([]);
  const [transfers, setTransfers] = useState<TransferSummary[]>([]);
  const [latest, setLatest] = useState<TelemetryPoint | null>(null);
  const [setpoint, setSetpoint] = useState<number>(1500);
  const [savingsCap, setSavingsCap] = useState<number>(5000);
  const [status, setStatus] = useState<StreamStatus>('connecting');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [windowId, setWindowId] = useState<WindowId>(DEFAULT_WINDOW);
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);

  const mounted = useRef(true);
  /** Mirrors windowId so the SSE handler never closes over a stale value. */
  const windowIdRef = useRef<WindowId>(DEFAULT_WINDOW);
  /**
   * Newest live point seen over SSE. Guards the race where a slow history
   * refetch resolves *after* a tick landed and would otherwise discard it.
   */
  const liveRef = useRef<TelemetryPoint | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  /** Pull the selected window (and the activity feed) from TimescaleDB. */
  const refresh = useCallback(async (id: WindowId = windowIdRef.current): Promise<void> => {
    try {
      const hours = windowHours(id);
      const response = await fetch(
        `${API_BASE}/telemetry?hours=${hours}&limit=${MAX_RENDERED_POINTS}`,
      );
      if (!response.ok) throw new Error(`telemetry ${response.status}`);
      const body = (await response.json()) as {
        points: Array<Omit<TelemetryPoint, 't'>>;
        setpoint: number;
        savings_cap: number;
      };
      if (!mounted.current) return;

      let incoming = (body.points ?? []).map(toPoint);
      const cutoff = Date.now() - hours * 3_600_000;

      // Merge: preserve every live point SSE added since the last refresh.
      // The backend snapshot is frozen at the last PID cycle; the points SSE
      // appended to state are newer and would be discarded by a naive
      // `setPoints(incoming)` on every 15-second reconciliation poll.
      setPoints((previous) => {
        const backendTimes = new Set(incoming.map((p) => p.t));
        const liveOnly = previous.filter(
          (p) => !backendTimes.has(p.t) && p.t >= cutoff,
        );
        const merged = [...incoming, ...liveOnly].sort((a, b) => a.t - b.t);
        const trimmed =
          merged.length > MAX_RENDERED_POINTS
            ? merged.slice(merged.length - MAX_RENDERED_POINTS)
            : merged;
        if (trimmed.length) setLatest(trimmed[trimmed.length - 1]);
        return trimmed;
      });
      if (body.setpoint) setSetpoint(body.setpoint);
      if (body.savings_cap) setSavingsCap(body.savings_cap);
      setError(null);
    } catch (exc) {
      if (mounted.current) setError(`Failed to load telemetry: ${String(exc)}`);
    }

    try {
      const response = await fetch(`${API_BASE}/transfers?limit=12`);
      if (!response.ok || !mounted.current) return;
      const body = (await response.json()) as {
        transfers: Array<{ amount: number; reason: string; status: string }>;
      };
      if (!mounted.current) return;
      setTransfers(
        (body.transfers ?? []).map((row) => ({
          type: row.reason,
          amount: row.amount,
          status: row.status,
        })),
      );
    } catch {
      /* the activity feed is cosmetic - never block the chart on it */
    }
  }, []);

  // ---- 1. Window history + the reconciliation poll ------------------------
  // SSE stays the primary live path; this poll is the safety net for the
  // deployed stack, where a proxy can buffer `text/event-stream` and leave
  // EventSource looking open while delivering nothing.
  useEffect(() => {
    windowIdRef.current = windowId;
    void refresh(windowId);
    const timer = window.setInterval(() => void refresh(windowId), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh, windowId]);

  const selectWindow = useCallback((id: WindowId) => setWindowId(id), []);

  // ---- 2. Live SSE stream (native EventSource -> asyncio.Queue per client) -
  useEffect(() => {
    const source = new EventSource(`${API_BASE}/stream`);

    source.onopen = () => setStatus('live');

    source.onmessage = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data) as Omit<TelemetryPoint, 't'> & {
          type: string;
          transfers?: TransferSummary[];
        };
        if (payload.type === 'config') {
          // POST /config broadcast - every open dashboard retunes live.
          if (typeof payload.setpoint === 'number') setSetpoint(payload.setpoint);
          if (typeof payload.savings_cap === 'number') setSavingsCap(payload.savings_cap);
          return;
        }
        if (payload.type !== 'tick') {
          // Heartbeat / status frames carry no balances, but they prove the
          // stream is alive - keep the "live" badge and the tick clock fresh.
          setStatus('live');
          setLastTickAt(Date.now());
          return;
        }
        const point = toPoint(payload);
        liveRef.current = point;

        // Trim by TIME WINDOW, not by a fixed point count. The old hard cap
        // pinned the series at exactly N points, so each incoming tick pushed
        // one off the far end and the graph never appeared to grow - while the
        // header counter sat frozen on the cap value.
        const cutoff = Date.now() - windowHours(windowIdRef.current) * 3_600_000;
        setPoints((previous) => {
          const next = [...previous.filter((existing) => existing.t >= cutoff), point];
          return next.length > MAX_RENDERED_POINTS
            ? next.slice(next.length - MAX_RENDERED_POINTS)
            : next;
        });
        setLatest(point);
        setSetpoint(payload.setpoint);
        setLastTickAt(Date.now());
        setStatus('live');
        if (payload.transfers?.length) {
          setTransfers((previous) => [...payload.transfers!, ...previous].slice(0, 12));
        }
      } catch {
        /* malformed frame - skip it */
      }
    };

    // EventSource reconnects automatically; surface that to the operator.
    source.onerror = () => {
      if (mounted.current) setStatus('reconnecting');
    };

    return () => {
      source.close();
    };
  }, []);

  // ---- 3. Demo controls ---------------------------------------------------
  const simulate = useCallback(
    async (kind: 'income' | 'expense', amount: number) => {
      setBusy(kind);
      setError(null);
      try {
        const response = await fetch(`${API_BASE}/simulate-${kind}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ amount }),
        });
        if (!response.ok) {
          const detail = await response.text();
          throw new Error(`${response.status}: ${detail.slice(0, 180)}`);
        }
        // The API also broadcasts over SSE, but refresh straight away so the
        // graph updates even if an intermediate proxy buffers the stream.
        await refresh();
      } catch (exc) {
        setError(`simulate-${kind} failed - ${String(exc)}`);
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  /** Persist setpoint / savings-cap changes made on the dashboard. */
  const updateConfig = useCallback(
    async (nextSetpoint: number, nextCap: number): Promise<boolean> => {
      setError(null);
      try {
        const response = await fetch(`${API_BASE}/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ setpoint: nextSetpoint, savings_cap: nextCap }),
        });
        if (!response.ok) {
          const detail = await response.text();
          throw new Error(`${response.status}: ${detail.slice(0, 180)}`);
        }
        const body = (await response.json()) as { setpoint: number; savings_cap: number };
        setSetpoint(body.setpoint);
        setSavingsCap(body.savings_cap);
        return true;
      } catch (exc) {
        setError(`saving targets failed - ${String(exc)}`);
        return false;
      }
    },
    [],
  );

  return {
    points,
    transfers,
    latest,
    setpoint,
    savingsCap,
    status,
    error,
    busy,
    windowId,
    selectWindow,
    lastTickAt,
    updateConfig,
    refresh,
    simulate,
  };
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs">
      <p className="mb-1 font-medium text-slate-500">
        {dayLabel(label)} {clockLabel(label)}
      </p>
      {payload.map((entry: any) => (
        <p key={entry.dataKey} className="tabular flex items-center gap-2">
          <span style={{ color: entry.color }}>{entry.name}</span>
          <span className="text-slate-900">{money(Number(entry.value))}</span>
        </p>
      ))}
    </div>
  );
}

type TelemetryChartProps = {
  points: TelemetryPoint[];
  setpoint: number;
  savingsCap: number;
  /** Selected history window - pins the X axis to the wall clock. */
  windowId: WindowId;
  height?: number;
};

/** Stacked-areas time series with a total line and a PID-projected lead. */
export default function TelemetryChart({
  points,
  setpoint,
  savingsCap,
  windowId,
  height = 380,
}: TelemetryChartProps) {
  // The projection must reach "now" even between ticks, so re-render on a
  // 1-second clock. Cheap: buildChartData only re-samples the projected tail.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const data = useMemo(() => buildChartData(points, now), [points, now]);

  // End-of-line value labels: the rightmost section of the graph always shows
  // the current amount of each account next to its line (and the total next
  // to the total line). Anchored "end" so the text stays inside the plot and
  // cannot be clipped at the SVG boundary.
  const lastReal = points[points.length - 1];
  const lastIndex = data.length - 1;
  const endDot = (color: string, text: string, dy: number) => (props: any) => {
    const { cx, cy, index } = props;
    if (typeof cx !== 'number' || typeof cy !== 'number' || index !== lastIndex) return null;
    return (
      <g>
        <circle cx={cx} cy={cy} r={3} fill={color} />
        <text
          x={cx - 7}
          y={cy + dy}
          fill={color}
          fontSize={11}
          fontWeight={600}
          textAnchor="end"
          dominantBaseline="middle"
        >
          {text}
        </text>
      </g>
    );
  };
  const edgeLabels =
    lastReal !== undefined
      ? {
          checking: money(lastReal.checking_balance, 0),
          savings: money(lastReal.savings_balance, 0),
          brokerage: money(lastReal.brokerage_balance, 0),
          total: money(
            lastReal.checking_balance + lastReal.savings_balance + lastReal.brokerage_balance,
            0,
          ),
        }
      : null;

  if (!points.length) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-4 text-center text-sm text-slate-500"
        style={{ height }}
      >
        <p>No telemetry in this window yet.</p>
        <p className="text-xs">
          Widen the range above, or seed history with
          <code className="mx-1 text-slate-300">py -3.14 -m app.scripts.seed_history --force</code>
        </p>
      </div>
    );
  }

  // The viewport is anchored to the WALL CLOCK, not to the data: the right
  // edge is always "now", the left edge always "now - window". The whole frame
  // therefore slides left at one second per second, whether or not any data
  // arrives. Real ticks merge in where they land, the projection restarts
  // from each new last point, and a capped-out projection just leaves the
  // right-hand region empty until the next event instead of freezing time.
  const spanMs = windowHours(windowId) * 3_600_000;
  const axisDomain: [number, number] = [now - spanMs, now];

  // Label granularity follows the fixed span: seconds for a tight live
  // window, dates once the view stretches across days.
  const spanHours = spanMs / 3_600_000;
  const tickFormatter =
    spanHours <= 0.25 ? clockSecondsLabel : spanHours <= 48 ? clockLabel : dayLabel;

  return (
    <div style={{ width: '100%', height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 18, bottom: 4, left: 4 }}>
          <defs>
            <linearGradient id="checkingFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={COLORS.checking} stopOpacity={0.35} />
              <stop offset="100%" stopColor={COLORS.checking} stopOpacity={0.04} />
            </linearGradient>
            <linearGradient id="savingsFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={COLORS.savings} stopOpacity={0.35} />
              <stop offset="100%" stopColor={COLORS.savings} stopOpacity={0.04} />
            </linearGradient>
            <linearGradient id="brokerageFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={COLORS.brokerage} stopOpacity={0.35} />
              <stop offset="100%" stopColor={COLORS.brokerage} stopOpacity={0.04} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#eef2f7" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={axisDomain}
            tickFormatter={tickFormatter}
            stroke="#94a3b8"
            tick={{ fontSize: 11 }}
            minTickGap={48}
            tickCount={7}
          />
          <YAxis
            stroke="#94a3b8"
            tick={{ fontSize: 11 }}
            width={66}
            domain={['auto', 'auto']}
            tickFormatter={(value: number) => `$${Math.round(value).toLocaleString('en-US')}`}
          />
          <Tooltip content={<ChartTooltip />} />
          <Legend
            verticalAlign="top"
            height={28}
            iconType="plainline"
            wrapperStyle={{ fontSize: 12, color: '#64748b' }}
          />
          <ReferenceLine
            y={setpoint}
            stroke={COLORS.setpoint}
            strokeDasharray="6 4"
            label={{
              value: `setpoint ${money(setpoint, 0)}`,
              position: 'insideTopRight',
              fill: COLORS.setpoint,
              fontSize: 11,
            }}
          />
          <ReferenceLine y={savingsCap} stroke={COLORS.cap} strokeDasharray="2 6" />
          {/*
           * Stacked composition: checking at the bottom, savings stacked on
           * it, brokerage on top - the stack height IS the total balance.
           * Projected rows reuse the same data keys, so the areas flow
           * seamlessly through the boundary into the projected lead.
           * Area animation is off: the series re-renders every second as the
           * projection advances, and re-animating that would strobe.
           */}
          <Area
            type="monotone"
            dataKey="checking_balance"
            name="Checking"
            stackId="balances"
            stroke={COLORS.checking}
            strokeWidth={1.5}
            fill="url(#checkingFill)"
            dot={edgeLabels ? endDot(COLORS.checking, edgeLabels.checking, -9) : false}
            isAnimationActive={false}
          />
          <Area
            type="monotone"
            dataKey="savings_balance"
            name="Savings"
            stackId="balances"
            stroke={COLORS.savings}
            strokeWidth={1.5}
            fill="url(#savingsFill)"
            dot={edgeLabels ? endDot(COLORS.savings, edgeLabels.savings, -9) : false}
            isAnimationActive={false}
          />
          <Area
            type="monotone"
            dataKey="brokerage_balance"
            name="Brokerage"
            stackId="balances"
            stroke={COLORS.brokerage}
            strokeWidth={1.5}
            fill="url(#brokerageFill)"
            dot={edgeLabels ? endDot(COLORS.brokerage, edgeLabels.brokerage, 14) : false}
            isAnimationActive={false}
          />
          {/*
           * Observed total: solid. Animation must stay OFF here - the chart
           * re-renders every second as the projection advances (fresh data
           * array identity each time), so Recharts would replay the 600ms
           * entrance morph every second and the line would visibly wriggle.
           */}
          <Line
            type="monotone"
            dataKey="total"
            name="Total"
            stroke={COLORS.total}
            strokeWidth={2.5}
            dot={edgeLabels ? endDot(COLORS.total, edgeLabels.total, -9) : false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}


