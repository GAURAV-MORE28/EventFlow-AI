/**
 * Digital Twin — the weather layer of the EventFlow twin.
 *
 * This page renders the SAME world and the SAME store the Command Centre reads.
 * It holds no simulation of its own and computes no impact numbers: every
 * multiplier, band, confidence value and causal link on screen is a field the
 * backend already decided (`GET /weather`, `GET /social/signals`,
 * `POST /simulate`). If this file ever starts deriving one, the contract's
 * "nothing is computed twice" rule is broken.
 *
 * The three claims the page must never overstate, each driven by a real field:
 *   `availability`   live | cached | synthetic | unavailable — is the reading real?
 *   `driving_live`   is any of it actually reaching the simulation?
 *   `is_real_post`   is this signal something a person published?
 */
import { useEffect, useRef, useState } from 'react';
import {
  Area, AreaChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import {
  AlertTriangle, ArrowRight, CloudRain, Droplets, ExternalLink, Play, RefreshCw, Thermometer, Wind, Zap,
} from 'lucide-react';

import PageShell, { BandChip, ErrorNote, Stat } from '../components/PageShell.jsx';
import { api } from '../lib/api.js';
import { clock, decimals, integer, minutes, percent } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

/** Provenance chips. The label is the honest name for what the payload says. */
const AVAILABILITY = {
  live: { label: 'Live observation', tone: 'text-emerald-700 border-emerald-500/40 bg-emerald-500/10' },
  cached: { label: 'Cached — provider unreachable', tone: 'text-amber-800 border-amber-500/40 bg-amber-500/10' },
  synthetic: { label: 'Synthetic model — not an observation', tone: 'text-sky-800 border-sky-500/40 bg-sky-500/10' },
  scenario: { label: 'Operator scenario', tone: 'text-teal-800 border-teal-500/40 bg-teal-500/10' },
  unavailable: { label: 'Weather unavailable', tone: 'text-slate-500 border-slate-500/40 bg-slate-500/10' },
  fixture: { label: 'Offline fixture — not real posts', tone: 'text-sky-800 border-sky-500/40 bg-sky-500/10' },
};

const SEVERITY_BAND = { calm: 'low', moderate: 'moderate', severe: 'high', extreme: 'critical' };

/** Presets for the what-if. Every value is a real quantity, not a vibe. */
const PRESETS = [
  { key: 'calm', label: 'Calm', params: { rain_mm_per_hr: 0, wind_kph: 8 } },
  { key: 'moderate', label: 'Moderate rain', params: { rain_mm_per_hr: 6, wind_kph: 20 } },
  { key: 'heavy', label: 'Heavy rain', params: { rain_mm_per_hr: 25, wind_kph: 35, storm_duration_min: 90 } },
  { key: 'storm', label: 'Storm', params: { rain_mm_per_hr: 40, wind_kph: 75, storm_duration_min: 120 } },
  { key: 'heat', label: 'Extreme heat', params: { rain_mm_per_hr: 0, temp_c: 44 } },
  { key: 'flood', label: 'Flooding', params: { rain_mm_per_hr: 35, flood_severity: 0.9, storm_duration_min: 150 } },
];

/**
 * The comparison metrics, chosen because weather genuinely moves them. Peak
 * utilisation is deliberately NOT the headline: the demo world's peak sits on a
 * venue that filled before the simulation started, so it is insensitive to
 * weather by construction and would understate a real effect.
 */
const COMPARE = [
  ['avg_travel_time_sec', 'Mean trip time', minutes, 'up-bad'],
  ['queued_people', 'People queued', integer, 'up-bad'],
  ['road_pressure', 'Road pressure', percent, 'up-bad'],
  ['venue_pressure', 'Venue & gate pressure', percent, 'up-bad'],
  ['transport_pressure', 'Transport pressure', percent, 'up-bad'],
  ['critical_count', 'Locations reaching critical', integer, 'up-bad'],
  ['late_entries', 'Late entries', integer, 'up-bad'],
  ['peak_utilisation', 'Peak utilisation', percent, 'up-bad'],
];

const FIELD_LABELS = {
  travel_time_mult: 'Travel time',
  dwell_mult: 'Dwell / time in place',
  service_rate_mult: 'Gate & station service rate',
  attendance_mult: 'Attendance',
  arrival_shift_min: 'Arrival timing',
  arrival_spread_mult: 'Arrival bunching',
  emergency_gain_mult: 'Incident load gain',
  capacity_mult: 'Road & parking capacity',
  flood_capacity_mult: 'Flood capacity loss',
};

function Chip({ kind, children }) {
  const style = AVAILABILITY[kind] || AVAILABILITY.unavailable;
  return (
    <span className={`chip border text-[10px] font-semibold uppercase ${style.tone}`}>
      {children || style.label}
    </span>
  );
}

/** A multiplier with its band. `×1.00` is rendered as "no change", not as 1.00. */
function Multiplier({ value, band, suffix = '×' }) {
  const neutral = suffix === '×' ? 1 : 0;
  if (value === null || value === undefined) return <span className="text-slate-400">—</span>;
  const changed = Math.abs(value - neutral) > 0.0005;
  return (
    <span className="tabular-nums">
      <b className={changed ? 'text-slate-100' : 'text-slate-400'}>
        {changed ? `${suffix === '×' ? '×' : ''}${value.toFixed(suffix === '×' ? 3 : 1)}${suffix === '×' ? '' : ' min'}` : 'no change'}
      </b>
      {band && band.lower !== band.upper && (
        <span className="ml-1 text-[10px] text-slate-400">
          ({band.lower.toFixed(suffix === '×' ? 3 : 1)}–{band.upper.toFixed(suffix === '×' ? 3 : 1)})
        </span>
      )}
    </span>
  );
}

export default function DigitalTwin() {
  const {
    weather, socialSignals, world, event, summary, nodesById, simTime, cycleNumber,
    mockMode, toast, setWeather, setSocialSignals, weatherScenario, setWeatherScenario,
    setWhatIfOverlay,
  } = useStore();
  const name = (id) => nodesById[id]?.display_name || id;

  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [params, setParams] = useState(PRESETS[2].params);
  const timer = useRef(null);
  useEffect(() => () => clearInterval(timer.current), []);

  // Replay mode has one recorded weather what-if; show that instead of
  // pretending to run a new one.
  useEffect(() => {
    if (!mockMode || weatherScenario.result) return;
    api
      .simulation('sim_mock_weather')
      .then((result) => setWeatherScenario({ status: 'complete', result, params: null }))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mockMode]);

  async function refresh() {
    setBusy('refresh');
    setError(null);
    try {
      const [next, signals] = await Promise.all([
        api.refreshWeather(),
        api.socialSignals().catch(() => null),
      ]);
      setWeather(next);
      if (signals) setSocialSignals(signals);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  }

  async function toggleDriveLive() {
    setBusy('apply');
    setError(null);
    try {
      const next = await api.applyWeather(!weather?.driving_live);
      setWeather(next);
      toast(
        next.driving_live
          ? 'Live weather is now driving the simulated city'
          : 'Live weather no longer affects the simulated city',
        'success',
      );
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  }

  async function runScenario() {
    clearInterval(timer.current);
    setWeatherScenario({ status: 'running', result: null, params });
    setError(null);
    try {
      const accepted = await api.simulate(
        [{ scenario_type: 'weather_scenario', params }],
        'Weather scenario',
        3600,
      );
      const started = Date.now();
      timer.current = setInterval(async () => {
        try {
          const result = await api.simulation(accepted.simulation_id);
          if (result.status === 'complete') {
            clearInterval(timer.current);
            setWeatherScenario({ status: 'complete', result, params });
            // Show the affected entities on the shared map, in the existing
            // "SIMULATED / not live" style.
            setWhatIfOverlay({ label: 'Weather scenario', result });
          } else if (result.status === 'failed' || Date.now() - started > 40000) {
            clearInterval(timer.current);
            setWeatherScenario({ status: 'failed' });
          }
        } catch (err) {
          clearInterval(timer.current);
          setWeatherScenario({ status: 'failed' });
          setError(err);
        }
      }, 800);
    } catch (err) {
      setWeatherScenario({ status: 'failed' });
      setError(err);
    }
  }

  const current = weather?.current;
  const impact = weather?.impact;
  const result = weatherScenario.result;
  const baseline = result?.baseline;
  const scenario = result?.scenario;
  const uncertainty = result?.weather?.uncertainty;

  const forecastChart = (weather?.forecast_impacts || []).map((p) => ({
    t: clock(p.valid_at),
    rain: p.conditions?.precipitation_mm_per_hr ?? 0,
    temp: p.conditions?.apparent_temp_c ?? null,
    travel: Math.round((p.travel_time_mult - 1) * 100),
    lower: Math.round(((p.bands?.travel_time_mult?.lower ?? p.travel_time_mult) - 1) * 100),
    upper: Math.round(((p.bands?.travel_time_mult?.upper ?? p.travel_time_mult) - 1) * 100),
  }));

  const timeline = (result?.timeline || []).map((p) => ({
    t: `+${Math.round(p.offset_sec / 60)}m`,
    baseline: Math.round(p.baseline_peak * 100),
    scenario: Math.round(p.scenario_peak * 100),
  }));

  return (
    <PageShell
      title="Digital Twin — weather"
      subtitle={
        'The weather layer of the running twin: live observations and forecast for this world’s venue, ' +
        'the rule-based impact they imply, and a what-if that runs on a copy of the live city. ' +
        'Every number here was computed by the backend; this page renders it.'
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-secondary" disabled={busy === 'refresh'} onClick={refresh}>
            <RefreshCw className={`h-3.5 w-3.5 ${busy === 'refresh' ? 'animate-spin' : ''}`} /> Refresh
          </button>
          <button
            type="button"
            className={weather?.driving_live ? 'btn-secondary border-red-500/40 text-red-700' : 'btn-primary'}
            disabled={mockMode || busy === 'apply' || !impact?.applied}
            onClick={toggleDriveLive}
            title={
              mockMode
                ? 'Changing the live city needs the live backend'
                : !impact?.applied
                  ? 'The current reading is calm enough that there is nothing to apply'
                  : undefined
            }
          >
            <Zap className="h-3.5 w-3.5" />
            {weather?.driving_live ? 'Stop driving the live city' : 'Drive the live city'}
          </button>
        </div>
      }
    >
      {/* --- 1. world + provenance -------------------------------------------- */}
      <section className="panel p-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="panel-title">
              {world?.venue_name || event?.name || world?.world_id || 'Active world'}
            </h2>
            <p className="mt-0.5 text-[11px] text-slate-400">
              {world?.source === 'generated_blueprint'
                ? `Generated from OpenStreetMap · ${world.node_count} entities`
                : 'Synthetic demo world'}
              {weather?.location?.label ? ` · weather for ${weather.location.label}` : ''}
              {weather?.location
                ? ` (${weather.location.lat.toFixed(4)}, ${weather.location.lon.toFixed(4)})`
                : ''}
              {' · sim '}
              {clock(simTime)} · cycle {cycleNumber}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <Chip kind={weather?.availability || 'unavailable'} />
            {weather?.stale && <Chip kind="cached">Stale</Chip>}
            <span
              className={`chip border text-[10px] font-semibold uppercase ${
                weather?.applied_to_live
                  ? 'border-red-500/40 bg-red-500/10 text-red-700'
                  : 'border-slate-500/40 bg-slate-500/10 text-slate-500'
              }`}
            >
              {weather?.applied_to_live ? 'Driving the live city' : 'Advisory only'}
            </span>
          </div>
        </div>
        {weather?.detail && <p className="mt-2 text-[11px] text-amber-800">{weather.detail}</p>}
        {!weather && (
          <p className="mt-2 text-xs text-slate-400">Loading the weather reading for this world…</p>
        )}
        {weather?.availability === 'unavailable' && (
          <p className="mt-2 flex items-start gap-1.5 text-[11px] text-slate-400">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-700" />
            No reading is available, so <b>no weather effect is applied</b> to the twin. The what-if below
            still works — it simulates the conditions you enter.
          </p>
        )}
        <ErrorNote error={error} />
      </section>

      {/* --- 2. current conditions + 3. twin state ---------------------------- */}
      <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Rainfall"
          value={current?.precipitation_mm_per_hr === null || current === undefined
            ? '—'
            : `${decimals(current?.precipitation_mm_per_hr ?? null, 1)} mm/hr`}
          sub={current?.condition || undefined}
        />
        <Stat
          label="Feels like"
          value={current?.apparent_temp_c === undefined ? '—' : `${decimals(current?.apparent_temp_c, 1)} °C`}
          sub={current?.temp_c !== undefined && current?.temp_c !== null ? `air ${decimals(current.temp_c, 1)} °C` : undefined}
        />
        <Stat
          label="Wind"
          value={current?.wind_kph === undefined ? '—' : `${decimals(current?.wind_kph, 0)} km/h`}
          sub={current?.humidity !== null && current?.humidity !== undefined ? `humidity ${percent(current.humidity)}` : undefined}
        />
        <div className="panel px-3 py-2">
          <div className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">
            Severity · observed {clock(weather?.observed_at)}
          </div>
          <div className="mt-1 flex items-center gap-2">
            <BandChip band={SEVERITY_BAND[current?.severity || 'calm']} />
            <span className="text-xs font-semibold uppercase text-slate-300">{current?.severity || '—'}</span>
          </div>
          <div className="mt-0.5 text-[10px] text-slate-400">
            impact confidence {impact?.confidence || '—'} · refreshes every{' '}
            {minutes(weather?.refresh_interval_sec)}
          </div>
        </div>
      </div>

      {/* --- 4. the causal chain ---------------------------------------------- */}
      <section className="panel mt-3 p-3">
        <h2 className="panel-title mb-2">
          How weather reaches the crowd
          <span className="ml-2 font-normal normal-case text-[10px] text-slate-400">
            {impact?.model_kind === 'rule_based'
              ? 'rule-based — documented coefficients on measured values, not a learned model'
              : impact?.model_kind}
            {impact?.model ? ` · ${impact.model}` : ''}
          </span>
        </h2>
        <ol className="grid gap-2 lg:grid-cols-6">
          {(impact?.causal_chain || []).map((link, i) => (
            <li
              key={link.stage}
              className="relative rounded border border-surface-700/60 bg-surface-850 p-2 text-[11px]"
            >
              <div className="flex items-center gap-1 font-semibold text-slate-200">
                <span className="text-[9px] text-slate-500">{i + 1}</span>
                {link.label}
              </div>
              <p className="mt-1 text-[10px] leading-snug text-slate-400">{link.detail}</p>
              {i < (impact?.causal_chain || []).length - 1 && (
                <ArrowRight className="absolute -right-2.5 top-1/2 hidden h-3 w-3 -translate-y-1/2 text-slate-600 lg:block" />
              )}
            </li>
          ))}
        </ol>
        {!impact?.causal_chain?.length && (
          <p className="text-xs text-slate-400">The causal chain is reported with the impact vector.</p>
        )}
      </section>

      {/* --- 5. the impact vector, with uncertainty --------------------------- */}
      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <section className="panel overflow-x-auto p-3">
          <h2 className="panel-title mb-1">Weather impact on the twin</h2>
          <p className="mb-2 text-[10px] text-slate-400">
            {impact?.applied
              ? 'What the current reading changes in the city model. Ranges come from each rule’s ' +
                'configured coefficient range, widened by forecast horizon — not from an assumed confidence.'
              : impact?.note || 'Nothing is applied at the moment.'}
          </p>
          <table className="w-full text-left text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-slate-400">
              <tr>
                <th className="pb-1">Quantity</th>
                <th className="pb-1 text-right">Effect (range)</th>
                <th className="pb-1 pl-3">Driven by</th>
              </tr>
            </thead>
            <tbody>
              {[
                ['travel_time_mult', '×'],
                ['dwell_mult', '×'],
                ['service_rate_mult', '×'],
                ['attendance_mult', '×'],
                ['arrival_spread_mult', '×'],
                ['arrival_shift_min', 'min'],
                ['emergency_gain_mult', '×'],
                ['capacity_mult', '×'],
              ].map(([key, suffix]) => {
                const band = impact?.bands?.[key];
                const value = key === 'capacity_mult' ? band?.value : impact?.[key];
                return (
                  <tr key={key} className="border-t border-surface-700/40 align-top">
                    <td className="py-1 text-slate-300">{FIELD_LABELS[key] || key}</td>
                    <td className="py-1 text-right">
                      <Multiplier value={value} band={band} suffix={suffix} />
                    </td>
                    <td className="py-1 pl-3 text-[10px] text-slate-400">
                      {(band?.rules || []).map((r) => r.rule.replace(/_/g, ' ')).join(', ') || '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {Object.keys(impact?.type_capacity_mult || {}).length > 0 && (
            <p className="mt-2 text-[10px] text-slate-400">
              Capacity by entity type:{' '}
              {Object.entries(impact.type_capacity_mult)
                .map(([type, mult]) => `${type.replace(/_/g, ' ')} ×${mult.toFixed(3)}`)
                .join(' · ')}
              . Applied by <b>type</b>, never to named ids — so a generated OSM world behaves the same way.
            </p>
          )}
          {impact?.closed_entity_ids?.length > 0 && (
            <p className="mt-1 text-[10px] text-red-600">
              Closed by flooding (named explicitly by the operator):{' '}
              {impact.closed_entity_ids.map(name).join(', ')}
            </p>
          )}
          {impact?.bands?.travel_time_mult?.rules?.length > 0 && (
            <details className="mt-2">
              <summary className="cursor-pointer text-[10px] uppercase tracking-wider text-slate-400">
                Where these coefficients come from
              </summary>
              <ul className="mt-1 space-y-0.5 text-[10px] text-slate-400">
                {Object.values(impact.bands)
                  .flatMap((b) => b.rules || [])
                  .filter((r, i, all) => all.findIndex((x) => x.rule === r.rule) === i)
                  .map((r) => (
                    <li key={r.rule}>
                      <b className="text-slate-300">{r.rule.replace(/_/g, ' ')}</b> — {r.basis} (k={r.coefficient})
                    </li>
                  ))}
              </ul>
            </details>
          )}
        </section>

        {/* --- 6. the forecast track ----------------------------------------- */}
        <section className="panel p-3">
          <h2 className="panel-title mb-1">Forecast pressure, next {forecastChart.length} hours</h2>
          <p className="mb-2 text-[10px] text-slate-400">
            Added travel time implied by each forecast hour, with its uncertainty band. The band widens with
            distance because a forecast is less certain than an observation.
          </p>
          <div className="h-44">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={forecastChart} margin={{ top: 6, right: 12, bottom: 0, left: -20 }}>
                <CartesianGrid stroke="#E2E8F0" strokeDasharray="3 3" />
                <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#64748B' }} />
                <YAxis tick={{ fontSize: 9, fill: '#64748B' }} unit="%" />
                <Tooltip contentStyle={{ fontSize: 10 }} />
                <Legend wrapperStyle={{ fontSize: 10 }} />
                <Area type="monotone" dataKey="upper" name="upper" stroke="none" fill="#0D9488" fillOpacity={0.15} isAnimationActive={false} />
                <Area type="monotone" dataKey="lower" name="lower" stroke="none" fill="#FFFFFF" fillOpacity={1} isAnimationActive={false} />
                <Line type="monotone" dataKey="travel" name="added travel time %" stroke="#0D9488" strokeWidth={2} dot={false} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <div className="mt-1 grid grid-cols-3 gap-2 text-[10px] text-slate-400">
            <span className="flex items-center gap-1"><Droplets className="h-3 w-3" /> rainfall drives travel & queues</span>
            <span className="flex items-center gap-1"><Thermometer className="h-3 w-3" /> heat drives dwell & incidents</span>
            <span className="flex items-center gap-1"><Wind className="h-3 w-3" /> wind drives open-air service</span>
          </div>
        </section>
      </div>

      {/* --- 7. weather what-if --------------------------------------------- */}
      <section className="panel mt-3 p-3">
        <h2 className="panel-title mb-1">Weather what-if</h2>
        <p className="mb-2 text-[11px] text-slate-400">
          Runs on two clones of the live city — one unchanged, one with these conditions — plus two more at the
          impact model&apos;s mild and severe rule coefficients, which is where the outcome range comes from.{' '}
          <b>The live city is never modified by a what-if.</b>{' '}
          Unspecified fields keep their real observed value.
        </p>
        {mockMode && (
          <p className="mb-2 text-xs text-amber-800">
            Replay mode shows one <b>recorded</b> weather scenario. Composing and running a new one needs the
            live backend (<code>npm run dev:live</code>).
          </p>
        )}
        <fieldset disabled={mockMode} className="disabled:opacity-60">
          <div className="flex flex-wrap items-center gap-1.5">
            {PRESETS.map((preset) => (
              <button
                key={preset.key}
                type="button"
                className="btn-secondary"
                onClick={() => setParams(preset.params)}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <div className="mt-2 flex flex-wrap items-end gap-3">
            {[
              ['rain_mm_per_hr', 'Rainfall (mm/hr)', 0, 200, 0.5],
              ['temp_c', 'Temperature (°C)', -30, 60, 0.5],
              ['wind_kph', 'Wind (km/h)', 0, 300, 1],
              ['flood_severity', 'Flood severity (0–1)', 0, 1, 0.05],
              ['storm_duration_min', 'Storm duration (min)', 0, 1440, 5],
            ].map(([key, label, min, max, step]) => (
              <label key={key} className="flex flex-col gap-0.5 text-[10px] text-slate-400">
                {label}
                <input
                  type="number"
                  min={min}
                  max={max}
                  step={step}
                  className="w-28 rounded border border-surface-700 bg-surface-850 px-1.5 py-1 text-xs"
                  value={params[key] ?? ''}
                  onChange={(e) =>
                    setParams((prev) => {
                      const next = { ...prev };
                      if (e.target.value === '') delete next[key];
                      else next[key] = Number(e.target.value);
                      return next;
                    })
                  }
                />
              </label>
            ))}
            <button
              type="button"
              className="btn-primary"
              disabled={weatherScenario.status === 'running' || Object.keys(params).length === 0}
              onClick={runScenario}
            >
              <Play className="h-3.5 w-3.5" />
              {weatherScenario.status === 'running' ? 'Simulating…' : 'Run scenario'}
            </button>
          </div>
        </fieldset>
        {weatherScenario.status === 'failed' && (
          <p className="mt-2 text-xs text-red-600">The scenario did not complete. Check the values and retry.</p>
        )}
      </section>

      {/* --- 8. baseline vs scenario --------------------------------------- */}
      {result && baseline && scenario && (
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <section className="panel overflow-x-auto p-3">
            <h2 className="panel-title mb-1">Baseline vs weather scenario</h2>
            <p className="mb-2 text-[10px] text-slate-400">
              {result.weather?.scenario_conditions && (
                <>
                  Simulated:{' '}
                  {decimals(result.weather.scenario_conditions.precipitation_mm_per_hr, 1)} mm/hr,{' '}
                  {decimals(result.weather.scenario_conditions.apparent_temp_c, 1)} °C felt,{' '}
                  {decimals(result.weather.scenario_conditions.wind_kph, 0)} km/h ·{' '}
                  <b>{result.weather.scenario_conditions.severity}</b>
                  {' · layered on the '}
                  {AVAILABILITY[result.weather.live_reading?.availability]?.label?.toLowerCase() ||
                    'current reading'}
                </>
              )}
            </p>
            <table className="w-full text-left text-xs">
              <thead className="text-[10px] uppercase tracking-wider text-slate-400">
                <tr>
                  <th className="pb-1">Metric</th>
                  <th className="pb-1 text-right">Baseline</th>
                  <th className="pb-1 text-right">Scenario</th>
                  <th className="pb-1 text-right">Range</th>
                </tr>
              </thead>
              <tbody>
                {COMPARE.map(([key, label, fmt]) => {
                  const a = baseline[key] ?? 0;
                  const b = scenario[key] ?? 0;
                  const same = Math.abs(b - a) < 1e-6;
                  const band = uncertainty?.metrics?.[key];
                  return (
                    <tr key={key} className="border-t border-surface-700/40">
                      <td className="py-1 text-slate-300">{label}</td>
                      <td className="py-1 text-right tabular-nums text-slate-400">{fmt(a)}</td>
                      <td
                        className={`py-1 text-right font-semibold tabular-nums ${
                          same ? 'text-slate-200' : b > a ? 'text-red-600' : 'text-emerald-700'
                        }`}
                      >
                        {fmt(b)}
                      </td>
                      <td className="py-1 text-right text-[10px] tabular-nums text-slate-400">
                        {band ? `${fmt(band.lower)} – ${fmt(band.upper)}` : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {uncertainty && (
              <p className="mt-2 text-[10px] text-slate-400">
                Range: {uncertainty.method}. Confidence <b>{uncertainty.confidence}</b> ({uncertainty.kind}).
              </p>
            )}
            {result.delta?.new_critical_entities?.length > 0 && (
              <p className="mt-1 text-[11px] text-red-600">
                Newly critical under this weather ({result.delta.new_critical_entities.length}):{' '}
                {result.delta.new_critical_entities.slice(0, 6).map(name).join(', ')}
                {result.delta.new_critical_entities.length > 6 &&
                  ` and ${result.delta.new_critical_entities.length - 6} more`}
              </p>
            )}
          </section>

          <section className="panel p-3">
            <h2 className="panel-title mb-2">Peak utilisation over the horizon (%)</h2>
            <div className="h-44">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={timeline} margin={{ top: 6, right: 12, bottom: 0, left: -18 }}>
                  <CartesianGrid stroke="#E2E8F0" strokeDasharray="3 3" />
                  <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#64748B' }} />
                  <YAxis tick={{ fontSize: 9, fill: '#64748B' }} />
                  <Tooltip contentStyle={{ fontSize: 10 }} />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                  <Line type="monotone" dataKey="baseline" stroke="#64748B" dot={false} isAnimationActive={false} />
                  <Line type="monotone" dataKey="scenario" stroke="#0D9488" strokeWidth={2} dot={false} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <h3 className="panel-title mb-1 mt-3">Where it bites hardest</h3>
            {!result.top_changes?.length && (
              <p className="text-xs text-slate-400">No location&apos;s peak moves by 2 points or more.</p>
            )}
            <table className="w-full text-left text-xs">
              <tbody>
                {(result.top_changes || []).slice(0, 8).map((c) => (
                  <tr key={c.entity_id} className="border-t border-surface-700/40">
                    <td className="py-1 text-slate-200">{c.display_name}</td>
                    <td className="text-[10px] text-slate-400">{c.entity_type.replace(/_/g, ' ')}</td>
                    <td className="text-right tabular-nums text-slate-400">{percent(c.baseline_peak)}</td>
                    <td
                      className={`text-right font-semibold tabular-nums ${
                        c.scenario_peak > c.baseline_peak ? 'text-red-600' : 'text-emerald-700'
                      }`}
                    >
                      {percent(c.scenario_peak)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {result.candidate_interventions?.length > 0 && (
              <>
                <h3 className="panel-title mb-1 mt-3">Contingency actions for this weather</h3>
                <ul className="space-y-1">
                  {result.candidate_interventions.map((i) => (
                    <li key={i.intervention_id} className="rounded border border-surface-700/60 p-1.5 text-[11px]">
                      <div className="flex justify-between gap-2">
                        <span className="font-semibold text-slate-100">{i.title}</span>
                        <span className="font-mono text-[10px] text-slate-400">{i.certificate?.verdict}</span>
                      </div>
                      <div className="text-[10px] text-slate-400">
                        simulated relief {i.estimated_relief_pct.toFixed(1)}%
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        </div>
      )}

      {/* --- 9. public signals ---------------------------------------------- */}
      <section className="panel mt-3 p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h2 className="panel-title">Public signals</h2>
            <p className="mt-0.5 text-[11px] text-slate-400">
              Publicly posted reports of conditions, read from keyless public timelines. Post text is the
              platform&apos;s own; the type and relevance labels are ours (keyword match
              {socialSignals?.classifier ? `, ${socialSignals.classifier}` : ''}).{' '}
              <b>Nothing here feeds the simulation</b> — the twin is driven by the weather provider alone.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <Chip kind={socialSignals?.availability || 'unavailable'} />
            {socialSignals?.summary && (
              <span className="chip border border-slate-500/40 bg-slate-500/10 text-[10px] text-slate-400">
                {socialSignals.summary.local_count} local · {socialSignals.summary.global_count} elsewhere
              </span>
            )}
          </div>
        </div>
        {socialSignals?.queried_tags?.length > 0 && (
          <p className="mt-1.5 text-[10px] text-slate-400">
            Searched {socialSignals.queried_tags.join(' ')} for this world&apos;s venue
            {socialSignals.topics?.length ? ` (${socialSignals.topics.join(', ')})` : ''} over the last{' '}
            {minutes(socialSignals.window_sec)}.
          </p>
        )}
        {socialSignals?.detail && <p className="mt-1 text-[11px] text-amber-800">{socialSignals.detail}</p>}
        {socialSignals && socialSignals.signals.length === 0 && (
          <p className="mt-2 text-xs text-slate-400">
            No public post in the window mentioned conditions for this venue. That is a real result, not a
            failure — nothing is invented to fill the space.
          </p>
        )}
        <ul className="mt-2 grid gap-1.5 lg:grid-cols-2">
          {(socialSignals?.signals || []).map((s) => (
            <li key={s.signal_id} className="rounded border border-surface-700/60 bg-surface-850 p-2 text-[11px]">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="chip border border-slate-500/40 bg-slate-500/10 text-[9px] uppercase text-slate-300">
                  {s.signal_type.replace(/_/g, ' ')}
                </span>
                <span
                  className={`chip border text-[9px] uppercase ${
                    s.scope === 'local'
                      ? 'border-teal-500/40 bg-teal-500/10 text-teal-800'
                      : 'border-slate-500/40 bg-slate-500/10 text-slate-500'
                  }`}
                >
                  {s.scope}
                </span>
                {!s.is_real_post && (
                  <span className="chip border border-sky-500/40 bg-sky-500/10 text-[9px] uppercase text-sky-800">
                    test data — not a real post
                  </span>
                )}
                <span className="ml-auto text-[9px] text-slate-500">{clock(s.posted_at)}</span>
              </div>
              {/* Third-party text, rendered as TEXT. Never as markup. */}
              <p className="mt-1 leading-snug text-slate-300">{s.text}</p>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-[9px] text-slate-500">
                <span>{s.source_detail}</span>
                {s.author && <span>{s.author}</span>}
                <span>relevance {s.relevance.toFixed(2)}</span>
                {s.url && (
                  <a
                    href={s.url}
                    target="_blank"
                    rel="noreferrer noopener nofollow"
                    className="flex items-center gap-0.5 text-teal-700 hover:underline"
                  >
                    source <ExternalLink className="h-2.5 w-2.5" />
                  </a>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>

      {/* --- 10. live twin context ------------------------------------------ */}
      <section className="panel mt-3 flex flex-wrap items-center gap-4 p-3 text-[11px] text-slate-400">
        <span className="flex items-center gap-1.5 font-semibold text-slate-300">
          <CloudRain className="h-3.5 w-3.5" /> Live twin right now
        </span>
        <span>
          overall risk <BandChip band={summary.overall_risk_band} /> {summary.overall_risk_score}
        </span>
        <span>critical {summary.critical_count}</span>
        <span>high {summary.high_count}</span>
        <span>load variance {decimals(summary.load_variance)}</span>
        <span className="ml-auto">
          This is the same state the Command Centre shows — one twin, one store.
        </span>
      </section>
    </PageShell>
  );
}
