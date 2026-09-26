/**
 * Command Centre — 02_FRONTEND_CONTRACT.md §4.
 *
 * Layout:
 *   TopBar
 *   ┌──────────────────────────┬──────────────┐
 *   │  MAP (flex-1)            │ Commander    │  right sidebar 320px
 *   │                          │ What-If      │
 *   └──────────────────────────┴──────────────┘
 *   [Operations Intelligence Workspace Header]
 *   ┌──────────────┬──────────────┬───────────┐  bottom panels
 *   │ Pressure TL  │ Intervention │ Twin      │  PT/IQ fixed 252px, Twin auto
 *   └──────────────┴──────────────┴───────────┘
 */
import { Activity, ShieldAlert, Cpu } from 'lucide-react';
import ActionHUD from '../components/ActionHUD.jsx';
import CommanderBar from '../components/CommanderBar.jsx';
import EntityDetailPanel from '../components/EntityDetailPanel.jsx';
import InterventionQueue from '../components/InterventionQueue.jsx';
import MapCanvas from '../components/MapCanvas.jsx';
import NavBar from '../components/NavBar.jsx';
import { ReplayBanner } from '../components/PageShell.jsx';
import PressureTimeline from '../components/PressureTimeline.jsx';
import TopBar from '../components/TopBar.jsx';
import TwinFidelityGauge from '../components/TwinFidelityGauge.jsx';
import WhatIfPanel from '../components/WhatIfPanel.jsx';
import { useActionTracking } from '../lib/useActionTracking.js';
import { useStore } from '../store/useStore.js';

/** Live operational readout from the simulator (tick payload), no fixed labels. */
function OpsLine() {
  const ops = useStore((st) => st.operations);
  const speed = useStore((st) => st.speed);
  if (!ops) return null;
  return (
    <div className="flex items-center gap-3 text-[10px] text-slate-400 font-mono">
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

      <main className="flex min-h-0 flex-1 flex-col gap-2 overflow-hidden p-2">
        <ReplayBanner className="shrink-0" />

        {/* ── Top row: Interactive Map (flex-1) + C2 Intelligence Sidebar ───────────────── */}
        <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[1fr_320px] xl:grid-cols-[1fr_340px] gap-2">

          {/* Live venue map viewport */}
          <section className="relative min-h-0 overflow-hidden rounded-md border border-surface-700/60 bg-surface-950 shadow-panel">
            <MapCanvas />
            <EntityDetailPanel />
            <ActionHUD />
          </section>

          {/* Right sidebar: AI Commander Assistant + What-If Simulation */}
          <aside className="grid min-h-0 grid-rows-[1fr_auto] gap-2 overflow-hidden">
            <CommanderBar />
            <WhatIfPanel />
          </aside>
        </div>

        {/* ── Bottom: Unified Operations Intelligence Workspace ─────────────────────────── */}
        <section className="shrink-0 flex flex-col">
          <div className="mb-1.5 flex items-center justify-between px-1">
            <div className="flex items-center gap-2">
              <span className="flex h-2 w-2 rounded-full bg-sky-500 animate-pulse" />
              <span className="text-[10px] font-bold uppercase tracking-[0.16em] text-slate-400">
                Operations Intelligence
              </span>
              <span className="rounded bg-surface-800/80 px-1.5 py-0.5 text-[9px] font-mono font-medium text-slate-400 border border-surface-700/60">
                DECISION SUPPORT & REASONING
              </span>
            </div>
            <OpsLine />
          </div>

          {/*
            Three panels side by side:
            - Pressure Timeline (fixed height for scrollable items)
            - Intervention Queue (fixed height for scrollable items)
            - Digital Twin Gauge (collapses gracefully)
          */}
          <div className="grid h-[252px] grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
            <PressureTimeline />
            <InterventionQueue />
            <div className="self-start h-full">
              <TwinFidelityGauge />
            </div>
          </div>
        </section>

      </main>
    </div>
  );
}
