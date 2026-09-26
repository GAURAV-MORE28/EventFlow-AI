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
import { useEffect, useRef } from 'react';
import { Route, Routes } from 'react-router-dom';

import CommandCentre from './routes/CommandCentre.jsx';
import Attendee from './routes/Attendee.jsx';
import Metrics from './routes/Metrics.jsx';
import Events from './routes/Events.jsx';
import Accommodation from './routes/Accommodation.jsx';
import Interventions from './routes/Interventions.jsx';
import WhatIf from './routes/WhatIf.jsx';
import Commander from './routes/Commander.jsx';
import VenueSetup from './routes/VenueSetup.jsx';
import { Crowd, Transport } from './routes/Network.jsx';
import Toasts from './components/Toasts.jsx';
import { MOCK_MODE, api } from './lib/api.js';
import { startMockDriver } from './lib/mocks.js';
import { connectWebSocket } from './lib/ws.js';
import { useStore } from './store/useStore.js';

/** Graph + primary event + schedule of the ACTIVE world (refetched when it changes). */
async function loadTopology(store) {
  const [event, graph, events] = await Promise.all([
    api.event(),
    api.graph(),
    api.events().catch(() => ({ events: [] })),
  ]);
  store.setEvent(event);
  store.setGraph(graph);
  store.setEvents(events.events);
  if (graph.world) store.setWorld(graph.world);
  return graph.world;
}

export default function App() {
  const store = useStore();
  const loadedWorld = useRef(null);
  const worldKey = useStore((s) => (s.world ? `${s.world.world_id}:${s.world.run_id}` : null));
  const worldId = useStore((s) => s.world?.world_id ?? null);

  // A resync that names a different world (blueprint activation, or back to the
  // demo) cleared the old graph in the store; fetch the new world's topology.
  useEffect(() => {
    if (MOCK_MODE || !worldId || loadedWorld.current === null || loadedWorld.current === worldId) return;
    loadedWorld.current = worldId;
    loadTopology(store).catch((error) => store.toast(`Could not load the new network: ${error.message}`, 'error'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [worldId, worldKey]);

  useEffect(() => {
    let disconnect = () => {};
    let cancelled = false;

    async function bootstrap() {
      try {
        const world = await loadTopology(store);
        if (cancelled) return;
        loadedWorld.current = world?.world_id ?? '';
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
          const [state, timeline, interventions, cascades, overview] = await Promise.all([
            api.state(),
            api.pressureTimeline().catch(() => ({ items: [] })),
            api.interventions('proposed', 10).catch(() => ({ interventions: [] })),
            api.activeCascades().catch(() => ({ cascades: [] })),
            api.overview().catch(() => null),
          ]);
          if (cancelled) return;
          if (overview) store.setSimControl({ speed: overview.speed_multiplier, paused: overview.paused });
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
        <Route path="/events" element={<Events />} />
        <Route path="/accommodation" element={<Accommodation />} />
        <Route path="/transport" element={<Transport />} />
        <Route path="/crowd" element={<Crowd />} />
        <Route path="/interventions" element={<Interventions />} />
        <Route path="/whatif" element={<WhatIf />} />
        <Route path="/commander" element={<Commander />} />
        <Route path="/venue" element={<VenueSetup />} />
      </Routes>
      <Toasts />
    </>
  );
}
