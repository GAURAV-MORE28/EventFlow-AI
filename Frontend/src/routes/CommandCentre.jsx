/**
 * Command Centre — map-first operational screen.
 *
 * Layout:
 *   TopBar
 *   NavBar
 *   ┌──────────────────────────────────────────────┐
 *   │  LIVE NETWORK MAP (all remaining space)       │
 *   │  entity detail panel + action strip overlay   │
 *   └──────────────────────────────────────────────┘
 *
 * Commander, What-If, the intervention queue, the pressure timeline and the
 * digital-twin gauge live on their own navbar pages (/commander, /whatif,
 * /interventions, /metrics); they are not duplicated here.
 */
import ActionHUD from '../components/ActionHUD.jsx';
import EntityDetailPanel from '../components/EntityDetailPanel.jsx';
import MapCanvas from '../components/MapCanvas.jsx';
import NavBar from '../components/NavBar.jsx';
import { ReplayBanner } from '../components/PageShell.jsx';
import TopBar from '../components/TopBar.jsx';
import { useActionTracking } from '../lib/useActionTracking.js';
import { useStore } from '../store/useStore.js';

/** Live operational readout from the simulator (tick payload), no fixed labels. */
function OpsLine() {
  const ops = useStore((st) => st.operations);
  const speed = useStore((st) => st.speed);
  if (!ops) return null;
  return (
    <div className="pointer-events-none absolute right-3 top-3 z-10 flex items-center gap-3 rounded border border-surface-700/60 bg-surface-900/90 px-2 py-1 text-[10px] font-mono text-slate-400 shadow-panel">
      <span>{Math.round(ops.arrivals_per_min || 0)} arriving/min</span>
      <span>·</span>
      <span>{Math.round(ops.queued_people || 0).toLocaleString('en-IN')} queued</span>
      <span>·</span>
      <span>{Math.round(ops.rooms_available || 0).toLocaleString('en-IN')} hotel rooms free</span>
      {speed && (<><span>·</span><span>{speed}× speed</span></>)}
    </div>
  );
}

export default function CommandCentre() {
  useActionTracking();

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-surface-950 font-sans">
      <TopBar />
      <NavBar />
      <main className="flex min-h-0 flex-1 flex-col overflow-hidden p-2">
        <ReplayBanner className="mb-2 shrink-0" />
        <section className="relative min-h-0 flex-1 overflow-hidden rounded-md border border-surface-700/60 bg-surface-950 shadow-panel">
          <MapCanvas />
          <OpsLine />
          <EntityDetailPanel />
          <ActionHUD />
        </section>
      </main>
    </div>
  );
}
