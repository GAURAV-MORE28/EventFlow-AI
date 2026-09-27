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
import { useEffect, useRef, useState } from 'react';
import { Route, Routes } from 'react-router-dom';

import CommandCentre from './routes/CommandCentre.jsx';
import Attendee from './routes/Attendee.jsx';
import Metrics from './routes/Metrics.jsx';
import Events from './routes/Events.jsx';
import Accommodation from './routes/Accommodation.jsx';
import Interventions from './routes/Interventions.jsx';
import WhatIf from './routes/WhatIf.jsx';
import DigitalTwin from './routes/DigitalTwin.jsx';
import Commander from './routes/Commander.jsx';
import VenueSetup from './routes/VenueSetup.jsx';
import { Crowd, Transport } from './routes/Network.jsx';
import Toasts from './components/Toasts.jsx';
import { MOCK_MODE, api } from './lib/api.js';
import { startMockDriver } from './lib/mocks.js';
import { connectWebSocket } from './lib/ws.js';
import { useStore } from './store/useStore.js';
import NavigationWrapper from './components/NavigationWrapper.jsx';
import Preloader from './components/Preloader.jsx';

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
  // The weather reading and the public signals are for THIS world's venue, so
  // they are refetched with the topology whenever the world changes.
  loadEnvironment(store);
  return graph.world;
}

/**
 * Weather + public signals. Both are advisory context: a failure leaves the
 * store's previous value in place and shows nothing rather than a fake reading,
 * and neither one can stop the shell from mounting.
 */
function loadEnvironment(store) {
  api
    .weather()
    .then((weather) => store.setWeather(weather))
    .catch(() => {});
  api
    .socialSignals()
    .then((signals) => store.setSocialSignals(signals))
    .catch(() => {});
}

export default function App() {
  const store = useStore();
  const loadedWorld = useRef(null);
  const [isBootstrapping, setIsBootstrapping] = useState(true);
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

      if (!cancelled) {
        // Add a slight minimum delay so the slick animation is always seen
        setTimeout(() => setIsBootstrapping(false), 800);
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
      <Preloader show={isBootstrapping} />
      <NavigationWrapper>
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
        <Route path="/twin" element={<DigitalTwin />} />
        <Route path="/commander" element={<Commander />} />
        <Route path="/venue" element={<VenueSetup />} />
      </Routes>
        <Toasts />
      </NavigationWrapper>
    </>
  );
}
