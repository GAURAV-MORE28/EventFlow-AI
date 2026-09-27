/**
 * PressureTimeline — 02_FRONTEND_CONTRACT.md §5.4.
 *
 * Cards render with balanced hierarchy:
 *   ENTITY: Display name & type
 *   CURRENT LOAD: percentage with risk color
 *   STATUS: risk band pill
 *   FORECAST: Min to critical (prominent but not oversized)
 *
 * Rows arrive already sorted by urgency. Render in order — re-sorting breaks the
 * animation contract shared with the cascade overlay.
 */
import { AlertTriangle, Clock, TrendingUp, CheckCircle2 } from 'lucide-react';
import { riskColor } from '../lib/colors.js';
import { minutes, minutesValue, percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const OFFSETS = [0, 300, 600, 900, 1200, 1800];
const SPARK_W = 140;
const SPARK_H = 38;
const CRITICAL = 0.9;

/**
 * Fixed-width sparkline. The segment above the critical threshold turns red, so
 * the shape of the problem is readable before the number is.
 */
function Sparkline({ trajectory }) {
  const max = Math.max(1.05, ...trajectory.map((p) => p.utilisation));
  const x = (h) => (OFFSETS.indexOf(h) / (OFFSETS.length - 1)) * SPARK_W;
  const y = (u) => Math.max(2, Math.min(SPARK_H - 2, SPARK_H - (u / max) * SPARK_H));

  const points = trajectory.map((p) => ({ x: x(p.horizon_sec), y: y(p.utilisation), u: p.utilisation }));
  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');

  // Split the path at the threshold crossing so the hot part is genuinely red.
  const hot = [];
  for (let i = 0; i < points.length - 1; i += 1) {
    const a = points[i];
    const b = points[i + 1];
    if (a.u >= CRITICAL || b.u >= CRITICAL) {
      hot.push(`M${a.x.toFixed(1)},${a.y.toFixed(1)} L${b.x.toFixed(1)},${b.y.toFixed(1)}`);
    }
  }

  return (
    <svg width={SPARK_W} height={SPARK_H} className="shrink-0" aria-hidden="true">
      <line
        x1="0"
        x2={SPARK_W}
        y1={y(CRITICAL)}
        y2={y(CRITICAL)}
        stroke="#EF4444"
        strokeWidth="1"
        strokeDasharray="2 2"
        opacity="0.5"
      />
      <path d={line} fill="none" stroke="#2DD4BF" strokeWidth="1.75" strokeLinecap="round" />
      {hot.map((d, i) => (
        <path key={i} d={d} fill="none" stroke="#EF4444" strokeWidth="2.25" strokeLinecap="round" />
      ))}
      <circle cx={points[0].x} cy={points[0].y} r="2.5" fill="#2DD4BF" />
    </svg>
  );
}

function Row({ item, isHero, onSelect }) {
  const band = riskColor(item.current_band);
  const minToCritical = minutes(item.time_to_critical_sec);
  const isCritical = item.current_band === 'critical' || item.time_to_critical_sec < 180;

  return (
    <button
      type="button"
      onClick={() => onSelect(item.entity_id)}
      className={`group w-full text-left transition-all rounded-md p-2.5 mb-1.5 border select-none ${
        isHero
          ? 'panel-hero-critical hover:border-red-500/50'
          : 'border-surface-700/60 bg-surface-850/60 hover:border-slate-500/50 hover:bg-surface-800'
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        {/* Entity info & current status */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span
              className="h-1.5 w-1.5 rounded-full shrink-0"
              style={{ backgroundColor: band.hex }}
            />
            <span className="truncate text-xs font-semibold text-slate-100 group-hover:text-slate-50">
              {item.display_name}
            </span>
          </div>

          <div className="mt-1 flex items-center gap-2.5 text-[10px]">
            <div className="flex items-center gap-1 font-mono">
              <span className="text-slate-400">LOAD:</span>
              <span className="font-bold tabular-nums" style={{ color: band.hex }}>
                {percent(item.current_utilisation)}
              </span>
            </div>

            <span className="text-slate-600">·</span>

            <div className="flex items-center gap-1">
              <span className="text-slate-400 text-[9px] uppercase">STATUS:</span>
              <span
                className="font-bold uppercase tracking-wider text-[9px]"
                style={{ color: band.hex }}
              >
                {item.current_band}
              </span>
            </div>
          </div>
        </div>

        {/* Trajectory Sparkline */}
        <div className="hidden sm:block">
          <Sparkline trajectory={item.trajectory} />
        </div>

        {/* Min to Critical Countdown Pill */}
        <div className="shrink-0 flex flex-col items-end justify-center pl-2">
          <span className="text-[8px] font-bold uppercase tracking-wider text-slate-400">
            Min To Critical
          </span>
          <div
            className={`mt-0.5 flex items-center gap-1 rounded px-2 py-0.5 text-xs font-bold font-mono tabular-nums border ${
              isCritical
                ? 'border-red-500/50 bg-red-500/15 text-red-300 shadow-sm'
                : 'border-orange-500/40 bg-orange-500/10 text-orange-300'
            }`}
          >
            {isCritical && <AlertTriangle className="h-3 w-3 text-red-400 shrink-0" />}
            <span>{minToCritical}</span>
          </div>
        </div>
      </div>
    </button>
  );
}

export default function PressureTimeline() {
  const { pressureTimeline, activeForecastSource, selectEntity } = useStore();

  function handleSelect(entityId) {
    selectEntity(entityId);
  }

  return (
    <section className="panel flex min-h-0 h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="panel-header">
        <div className="flex items-center gap-2">
          <div className="flex h-5 w-5 items-center justify-center rounded bg-red-500/15 text-red-400">
            <TrendingUp className="h-3 w-3" />
          </div>
          <div>
            <h2 className="panel-title">Pressure Timeline</h2>
            <div className="text-[9px] font-mono text-slate-400 leading-none">
              URGENCY RANKED SURGE FORECAST
            </div>
          </div>
        </div>

        <span
          className="rounded bg-surface-750 px-2 py-0.5 font-mono text-[9px] text-slate-400 border border-surface-650"
          title="Active forecast generator (00 §4 degradation contract)"
        >
          {activeForecastSource || 'persistence'}
        </span>
      </div>

      {pressureTimeline.length === 0 ? (
        <div className="m-3 flex flex-1 flex-col items-center justify-center rounded-md border border-emerald-500/25 bg-emerald-500/[0.04] p-4 text-center">
          <CheckCircle2 className="h-6 w-6 text-emerald-400 mb-1.5" />
          <p className="text-xs font-semibold text-emerald-300">
            All Venues Operating Nominally
          </p>
          <p className="mt-0.5 text-[10px] text-slate-400">
            No venue entity is forecasted to breach critical threshold within 60 minutes.
          </p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-2">
          {pressureTimeline.map((item, index) => (
            <Row
              key={item.entity_id}
              item={item}
              isHero={index === 0}
              onSelect={handleSelect}
            />
          ))}
        </div>
      )}
    </section>
  );
}
