/**
 * REST snapshots that stay current with the live city.
 *
 * Pages that need more than the WebSocket stream carries (hotel catalogue,
 * schedule, domain boards) fetch through `api` and refetch every
 * `everyCycles` simulation cycles, and immediately whenever the world changes
 * outside the cycle (an approval, a schedule change, a disruption). The last
 * good payload stays on screen while a refetch is in flight — never blank.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { useStore } from '../store/useStore.js';

export function useLiveQuery(fetcher, deps = [], { everyCycles = 4, enabled = true } = {}) {
  const cycle = useStore((s) => s.cycleNumber);
  const world = useStore((s) => s.worldVersion);
  const bucket = Math.floor(cycle / Math.max(1, everyCycles));
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [manual, setManual] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    setLoading(true);
    fetcherRef
      .current()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bucket, world, manual, enabled, ...deps]);

  const reload = useCallback(() => setManual((n) => n + 1), []);
  return { data, error, loading, reload };
}
