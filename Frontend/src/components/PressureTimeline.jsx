/**
 * PressureTimeline — 02_FRONTEND_CONTRACT.md §5.4.
 *
 * The hero-number rule: the top row's `time_to_critical_sec` renders at
 * `text-6xl font-bold`. It is the largest text on the screen at all times and
 * nothing else competes with it.
 *
 * Rows arrive already sorted by urgency. Render in order — re-sorting breaks the
 * animation contract shared with the cascade overlay.
 */
import { riskColor } from '../lib/colors.js';
import { minutesValue, percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

const OFFSETS = [0, 300, 600, 900, 1200, 1800]; // fixed x positions, never recomputed
const SPARK_W = 132;
const SPARK_H = 34;
const CRITICAL = 0.9;

/**
 * Fixed-width sparkline. The segment above the critical threshold turns red, so
 * the shape of the problem is readable before the number is.
 */
function Sparkline({ trajectory }) {
  const max = Math.max(1.05, ...trajectory.map((p) => p.utilisation));
  const x = (h) => (OFFSETS.indexOf(h) / (OFFSETS.length - 1)) * SPARK_W;
  const y = (u) => SPARK_H - (u / max) * SPARK_H;

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
        strokeDasharray="3 3"
        opacity="0.45"
      />
      <path d={line} fill="none" stroke="#60A5FA" strokeWidth="1.75" strokeLinecap="round" />
      {hot.map((d, i) => (
        <path key={i} d={d} fill="none" stroke="#EF4444" strokeWidth="2.25" strokeLinecap="round" />
      ))}
      <circle cx={points[0].x} cy={points[0].y} r="2.5" fill="#60A5FA" />
    </svg>
  );
}

function Row({ item, isHero, onSelect }) {
  const band = riskColor(item.current_band);
  return (
    <button
      type="button"
      onClick={() => onSelect(item.entity_id)}
      className={`w-full text-left transition-colors ${
        isHero
          ? 'panel-hero-critical mb-1 px-3 py-2.5 hover:border-red-500/30'
          : 'rounded-md border border-transparent px-3 py-2 hover:border-surface-600 hover:bg-surface-700/60'
      }`}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className={`truncate font-medium ${isHero ? 'text-sm text-slate-100' : 'text-sm text-slate-300'}`}>
            {item.display_name}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[11px] text-slate-400">
            <span className="tabular-nums">{percent(item.current_utilisation)} now</span>
            <span className="h-1 w-1 rounded-full" style={{ backgroundColor: band.hex }} />
            <span style={{ color: band.hex }}>{item.current_band}</span>
          </div>
        </div>
        <Sparkline trajectory={item.trajectory} />
        <div className="shrink-0 text-right">
          {isHero ? (
            <>
              <div className="text-6xl font-bold leading-none tabular-nums text-red-400">
                {minutesValue(item.time_to_critical_sec)}
              </div>
              <div className="mt-1 text-[10px] uppercase tracking-[0.15em] text-slate-500">
                min to critical
              </div>
            </>
          ) : (
            <div className="text-lg font-semibold tabular-nums text-orange-300">
              {minutesValue(item.time_to_critical_sec)}
              <span className="ml-1 text-[11px] font-normal text-slate-500">min</span>
            </div>
          )}
        </div>
      </div>
    </button>
  );
}

export default function PressureTimeline() {
  // NOTE: setActiveCascadeRoot intentionally NOT destructured here.
  // Cascade lines must only appear via the "Show cascade" button in
  // EntityDetailPanel (02 §5.8). Selecting a timeline row opens the detail
  // panel; the operator then clicks "Show cascade" if they want the arcs.
  const { pressureTimeline, activeForecastSource, selectEntity } = useStore();

  function handleSelect(entityId) {
    selectEntity(entityId);
    // Do NOT call setActiveCascadeRoot here — that would auto-arm cascade lines.
  }

  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-header">
        <h2 className="panel-title">Pressure timeline</h2>
        <span
          className="chip bg-surface-700 text-slate-400"
          title="Which model produced this forecast (00 §4 degradation contract)"
        >
          {activeForecastSource}
        </span>
      </div>

      {pressureTimeline.length === 0 ? (
        // The calm opening of the demo — it should look genuinely calm (02 §5.4).
        <div className="m-3 flex flex-1 items-center justify-center rounded-md border border-green-500/25 bg-green-500/10 px-4 py-8 text-center">
          <p className="text-sm text-green-300">
            No entity predicted to reach critical within 60 minutes.
          </p>
        </div>
      ) : (
        <div className="mt-1 flex-1 overflow-y-auto px-1 pb-2">
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
