/**
 * Command Centre — 02_FRONTEND_CONTRACT.md §4.
 *
 * Layout:
 *   TopBar
 *   ┌──────────────────────────┬──────────┐
 *   │  MAP (flex-1)            │ Commander│  right sidebar 300px
 *   │                          │ WhatIf   │
 *   └──────────────────────────┴──────────┘
 *   [Ops Intelligence label]
 *   ┌──────────────┬──────────────┬────────┐  bottom panels, items-start
 *   │ Pressure TL  │ Intervention │ Twin   │  PT/IQ fixed 248px, Twin auto
 *   └──────────────┴──────────────┴────────┘
 *
 * TwinFidelityGauge is items-start so collapsing it actually shrinks the row.
 */
import ActionHUD from '../components/ActionHUD.jsx';
import CommanderBar from '../components/CommanderBar.jsx';
import EntityDetailPanel from '../components/EntityDetailPanel.jsx';
import InterventionQueue from '../components/InterventionQueue.jsx';
import MapCanvas from '../components/MapCanvas.jsx';
import PressureTimeline from '../components/PressureTimeline.jsx';
import TopBar from '../components/TopBar.jsx';
import TwinFidelityGauge from '../components/TwinFidelityGauge.jsx';
import WhatIfPanel from '../components/WhatIfPanel.jsx';
import { useActionTracking } from '../lib/useActionTracking.js';

export default function CommandCentre() {
  useActionTracking();

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-surface-900">
      <TopBar />

      <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-hidden p-1.5">

        {/* ── Top row: map (flex-1) + right sidebar (300px) ───────────────── */}
        <div className="grid min-h-0 flex-1 grid-cols-[1fr_300px] gap-1.5">

          {/* Live venue map */}
          <section className="relative min-h-0">
            <MapCanvas />
            <EntityDetailPanel />
            <ActionHUD />
          </section>

          {/* Right sidebar: Commander fills top, What-If sits at bottom */}
          <aside className="grid min-h-0 grid-rows-[1fr_auto] gap-1.5">
            <CommanderBar />
            <WhatIfPanel />
          </aside>
        </div>

        {/* ── Bottom: Operations Intelligence (3 panels) ───────────────────── */}
        <div className="shrink-0">
          <div className="mb-1 flex items-center gap-2 px-0.5">
            <span className="text-[9px] font-semibold uppercase tracking-[0.15em] text-slate-600">
              Operations Intelligence
            </span>
            <div className="h-px flex-1 bg-surface-700" />
          </div>

          {/*
            items-start: each flex child sizes to its OWN height.
            PT and IQ are wrapped in fixed-height (248px) divs so their
            internal overflow-y-auto works. TwinFidelityGauge has no height
            wrapper — when collapsed it's ~44px (header only), not 248px.
          */}
          <div className="grid h-[248px] grid-cols-3 gap-1.5">
            <PressureTimeline />
            <InterventionQueue />
            <div className="self-start">
              <TwinFidelityGauge />
            </div>
          </div>
        </div>

      </div>
    </div>
  );
}
