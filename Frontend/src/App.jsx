/**
 * Application shell — 02_FRONTEND_CONTRACT.md §4.
 *
 *   /          Command Centre
 *   /attendee  Attendee PWA (mobile viewport)
 *   /metrics   KPI panel (judging slide, full screen)
 *
 * Bootstrap happens once here: fetch the static topology, then either connect
 * the WebSocket or start the mock driver. No component fetches its own data.
 */
import { useEffect } from 'react';
import { Route, Routes } from 'react-router-dom';

import CommandCentre from './routes/CommandCentre.jsx';
import Attendee from './routes/Attendee.jsx';
import Metrics from './routes/Metrics.jsx';
import Toasts from './components/Toasts.jsx';
import { MOCK_MODE, api } from './lib/api.js';
import { startMockDriver } from './lib/mocks.js';
import { connectWebSocket } from './lib/ws.js';
import { useStore } from './store/useStore.js';

export default function App() {
  const store = useStore();

  useEffect(() => {
    let disconnect = () => {};
    let cancelled = false;

    async function bootstrap() {
      try {
        const [event, graph] = await Promise.all([api.event(), api.graph()]);
        if (cancelled) return;
        store.setEvent(event);
        store.setGraph(graph);
      } catch (error) {
        // The map can render from a cached graph; a topology failure is worth
        // surfacing, but it must not stop the rest of the shell from mounting.
        if (!error?.isWarmingUp) {
          store.toast(`Could not load topology: ${error.message}`, 'error');
        }
      }

      if (cancelled) return;

      if (MOCK_MODE) {
        disconnect = startMockDriver(store);
      } else {
        // Seed from REST so the first frame is populated, then let the socket
        // take over with deltas.
        try {
          const [state, timeline, interventions, cascades, demo] = await Promise.all([
            api.state(),
            api.pressureTimeline().catch(() => ({ items: [] })),
            api.interventions('proposed', 10).catch(() => ({ interventions: [] })),
            api.activeCascades().catch(() => ({ cascades: [] })),
            api.demoStatus().catch(() => null),
          ]);
          if (cancelled) return;
          store.setDemo(demo);
          store.setSummary(state.summary, state.sim_time, state.cycle_number);
          store.mergeEntities(state.entities);
          store.setPressureTimeline(timeline.items, timeline.active_source);
          store.setInterventions(interventions.interventions);
          store.setCascades(cascades.cascades);
        } catch (error) {
          if (!error?.isWarmingUp) {
            store.toast(error.message, 'error');
          }
        }
        disconnect = connectWebSocket(store, { client: 'command_centre' });
      }
    }

    bootstrap();
    return () => {
      cancelled = true;
      disconnect();
    };
    // Bootstrap runs exactly once; the store is a stable zustand singleton.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <Routes>
        <Route path="/" element={<CommandCentre />} />
        <Route path="/attendee" element={<Attendee />} />
        <Route path="/metrics" element={<Metrics />} />
      </Routes>
      <Toasts />
    </>
  );
}
