/**
 * Command Centre — the layout in 02_FRONTEND_CONTRACT.md §4.
 *
 * Single screen, no page scrolling. The map is dominant; the right rail carries
 * the pressure timeline (hero), the intervention queue, and the twin gauge.
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
  // Derives the action lifecycle from live state (TASK 2). No render output.
  useActionTracking();

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-surface-900">
      <TopBar />

      <main className="grid min-h-0 flex-1 grid-cols-[1fr_400px] gap-2 p-2">
        <section className="relative min-h-0">
          <MapCanvas />
          <EntityDetailPanel />
          <ActionHUD />
        </section>

        <aside className="grid min-h-0 grid-rows-[minmax(0,1.05fr)_minmax(0,1.35fr)_auto_auto] gap-2">
          <PressureTimeline />
          <InterventionQueue />
          <TwinFidelityGauge />
          <WhatIfPanel />
        </aside>
      </main>

      <div className="px-2 pb-2">
        <CommanderBar />
      </div>
    </div>
  );
}
