/**
 * Toasts — 02_FRONTEND_CONTRACT.md §9.
 *
 * Note what does NOT arrive here: `MODEL_NOT_READY` and `INSUFFICIENT_HISTORY`.
 * Those are expected in the first ~90 seconds of a run and render as skeletons
 * in the owning component, never as an error.
 */
import { useEffect } from 'react';
import { AlertTriangle, CheckCircle2, Info, X } from 'lucide-react';

import { useStore } from '../store/useStore.js';

const TONE = {
  error: {
    border: 'border-red-500/50 bg-surface-900/95 text-red-200 shadow-red-950/40',
    icon: AlertTriangle,
    iconColor: 'text-red-400',
  },
  success: {
    border: 'border-emerald-500/50 bg-surface-900/95 text-emerald-200 shadow-emerald-950/40',
    icon: CheckCircle2,
    iconColor: 'text-emerald-400',
  },
  info: {
    border: 'border-teal-500/50 bg-surface-900/95 text-teal-200 shadow-teal-950/40',
    icon: Info,
    iconColor: 'text-teal-400',
  },
};

function Toast({ toast, onDismiss }) {
  useEffect(() => {
    const handle = setTimeout(() => onDismiss(toast.id), 5000);
    return () => clearTimeout(handle);
  }, [toast.id, onDismiss]);

  const config = TONE[toast.tone] || TONE.info;
  const IconComponent = config.icon;

  return (
    <div
      className={`reveal flex items-start gap-2.5 rounded-md border p-3 text-xs shadow-2xl backdrop-blur-md transition-all duration-200 ${config.border}`}
      role="alert"
    >
      <IconComponent className={`h-4 w-4 shrink-0 mt-0.5 ${config.iconColor}`} />
      <span className="flex-1 font-medium leading-relaxed">{toast.message}</span>
      <button
        type="button"
        onClick={() => onDismiss(toast.id)}
        className="shrink-0 rounded p-0.5 text-slate-400 hover:bg-surface-800 hover:text-white transition-colors"
        aria-label="Dismiss notification"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export default function Toasts() {
  const { toasts, dismissToast } = useStore();
  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
      {toasts.map((toast) => (
        <div key={toast.id} className="pointer-events-auto">
          <Toast toast={toast} onDismiss={dismissToast} />
        </div>
      ))}
    </div>
  );
}

