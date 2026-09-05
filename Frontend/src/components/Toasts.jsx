/**
 * Toasts — 02_FRONTEND_CONTRACT.md §9.
 *
 * Note what does NOT arrive here: `MODEL_NOT_READY` and `INSUFFICIENT_HISTORY`.
 * Those are expected in the first ~90 seconds of a run and render as skeletons
 * in the owning component, never as an error.
 */
import { useEffect } from 'react';

import { useStore } from '../store/useStore.js';

const TONE = {
  error: 'border-red-500/40 bg-red-500/15 text-red-200',
  success: 'border-green-500/40 bg-green-500/15 text-green-200',
  info: 'border-sky-500/40 bg-sky-500/15 text-sky-200',
};

function Toast({ toast, onDismiss }) {
  useEffect(() => {
    const handle = setTimeout(() => onDismiss(toast.id), 5000);
    return () => clearTimeout(handle);
  }, [toast.id, onDismiss]);

  return (
    <div className={`reveal rounded-md border px-3 py-2 text-xs shadow-lg ${TONE[toast.tone] || TONE.info}`}>
      <div className="flex items-start gap-2">
        <span className="flex-1">{toast.message}</span>
        <button type="button" onClick={() => onDismiss(toast.id)} className="opacity-60 hover:opacity-100">
          ✕
        </button>
      </div>
    </div>
  );
}

export default function Toasts() {
  const { toasts, dismissToast } = useStore();
  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-72 flex-col gap-2">
      {toasts.map((toast) => (
        <div key={toast.id} className="pointer-events-auto">
          <Toast toast={toast} onDismiss={dismissToast} />
        </div>
      ))}
    </div>
  );
}
