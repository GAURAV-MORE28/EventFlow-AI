/**
 * Transport and Crowd & Venues pages — the same live board over different
 * domains of the city.
 */
import DomainBoard, { DisruptionsPanel } from '../components/DomainBoard.jsx';
import PageShell, { Stat } from '../components/PageShell.jsx';
import { integer, minutes } from '../lib/format.js';
import { useStore } from '../store/useStore.js';

function OpsStats() {
  const ops = useStore((s) => s.operations);
  if (!ops) return null;
  return (
    <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
      <Stat label="Arriving now" value={`${integer(ops.arrivals_per_min)}/min`} />
      <Stat label="Leaving now" value={`${integer(ops.egress_per_min)}/min`} />
      <Stat label="Queued at stations & gates" value={integer(ops.queued_people)} tone={ops.queued_people > 500 ? 'text-orange-600' : 'text-slate-100'} />
      <Stat label="Current trip time" value={minutes(ops.current_travel_time_sec)} sub="access node to venue, incl. queues" />
      <Stat label="Entered after start" value={integer(ops.late_entries)} sub="visitors delayed past kick-off" />
    </div>
  );
}

export function Transport() {
  return (
    <PageShell
      title="Transportation"
      subtitle="Stations, hubs, lines, roads and parking, with live load, 30-minute forecast and queue delay. Report an outage or capacity cut and riders re-route to substitutes in the simulation."
    >
      <OpsStats />
      <div className="mb-3"><DisruptionsPanel /></div>
      <DomainBoard domains={['transport', 'roads', 'parking']} />
    </PageShell>
  );
}

export function Crowd() {
  return (
    <PageShell
      title="Crowd & venues"
      subtitle="Venues, entry gates, crowd zones and emergency posts. Closing a gate sends its arrivals to the other gates of the venue; queues past a gate's capacity spill onto the adjacent roads."
    >
      <OpsStats />
      <div className="mb-3"><DisruptionsPanel /></div>
      <DomainBoard domains={['venues', 'crowd', 'emergency', 'hospitality']} />
    </PageShell>
  );
}
