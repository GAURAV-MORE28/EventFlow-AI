/**
 * CommanderBar — 02_FRONTEND_CONTRACT.md §5.7.
 *
 * **Never render an assistant message without the sources tray available.** The
 * visible grounding is the whole point: every number in an answer traces to a
 * tool call the backend actually made, and the tray is where a judge checks.
 */
import { useEffect, useRef, useState } from 'react';
import {
  Bot,
  User,
  Sparkles,
  ChevronDown,
  ChevronRight,
  Send,
  ShieldCheck,
  AlertTriangle,
  Code2,
  Terminal,
  Maximize2,
  Minimize2
} from 'lucide-react';

import { api } from '../lib/api.js';
import { useStore } from '../store/useStore.js';

const SCRIPTED = [
  'What is the biggest problem right now?',
  'Why is Metro B becoming critical?',
  'What happens if we do nothing?',
  'Which action gives the largest safety improvement?',
];

function SourcesTray({ toolCalls }) {
  const [open, setOpen] = useState(false);
  if (!toolCalls || toolCalls.length === 0) return null;

  return (
    <div className="mt-2 pt-1 border-t border-surface-700/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[10px] font-mono font-medium text-slate-400 hover:text-teal-300 transition-colors"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <Code2 className="h-3 w-3 text-teal-400" />
        <span>Audit Sources ({toolCalls.length})</span>
      </button>
      {open && (
        <ul className="mt-1.5 space-y-1.5 rounded border border-surface-700/80 bg-surface-950/80 p-2 font-mono text-[10px]">
          {toolCalls.map((call, index) => (
            <li key={index} className="leading-relaxed border-b border-surface-800/80 pb-1 last:border-0 last:pb-0">
              <div className="flex items-center gap-1 text-teal-400 font-semibold">
                <Terminal className="h-2.5 w-2.5 text-teal-400" />
                <span>{call.tool}</span>
              </div>
              <div className="text-slate-400 text-[9px] truncate">
                args: {JSON.stringify(call.args)}
              </div>
              <div className="text-emerald-400 text-[9px]">
                ↳ {call.result_digest}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Message({ message }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end gap-1.5 pl-4">
        <div className="rounded-lg border border-teal-500/30 bg-teal-950/40 px-3 py-2 text-xs text-teal-100 shadow-sm leading-relaxed max-w-[85%]">
          {message.text}
        </div>
        <div className="h-6 w-6 rounded-full bg-surface-700 flex items-center justify-center shrink-0 border border-surface-600">
          <User className="h-3 w-3 text-slate-300" />
        </div>
      </div>
    );
  }

  if (message.pending) {
    return (
      <div className="flex items-start gap-2 pr-4">
        <div className="h-6 w-6 rounded-full bg-teal-500/20 border border-teal-500/40 flex items-center justify-center shrink-0">
          <Bot className="h-3.5 w-3.5 text-teal-400 animate-pulse" />
        </div>
        <div className="w-full space-y-2 rounded-lg border border-surface-700/60 bg-surface-900/60 p-3">
          <div className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 rounded-full bg-teal-400 animate-ping" />
            <span className="text-[10px] font-mono text-teal-400">Synthesizing state & topological causal graph…</span>
          </div>
          <div className="skeleton h-2.5 w-3/4" />
          <div className="skeleton h-2.5 w-1/2" />
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-2 pr-2">
      <div className="h-6 w-6 rounded-full bg-teal-500/20 border border-teal-500/40 flex items-center justify-center shrink-0 mt-0.5">
        <Bot className="h-3.5 w-3.5 text-teal-400" />
      </div>
      <div className="flex-1 rounded-lg border border-surface-700/80 bg-surface-900/80 p-3 shadow-panel">
        <div className="text-xs leading-relaxed text-slate-200 selection:bg-teal-500/30">
          {message.text}
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {message.grounding?.passed && (
            <span className="chip border border-emerald-500/30 bg-emerald-500/10 text-emerald-300 text-[9px] font-medium">
              <ShieldCheck className="h-2.5 w-2.5" />
              TOOL GROUNDED
            </span>
          )}
          {message.is_cached && (
            <span className="chip border border-surface-700 bg-surface-800 text-slate-400 text-[9px]">
              CACHED INFERENCE
            </span>
          )}
          {message.grounding && !message.grounding.passed && (
            <span className="chip border border-amber-500/40 bg-amber-500/15 text-amber-300 text-[9px]">
              <AlertTriangle className="h-2.5 w-2.5" />
              UNGROUNDED VALUES REMOVED
            </span>
          )}
        </div>

        <SourcesTray toolCalls={message.tool_calls || []} />
      </div>
    </div>
  );
}

export default function CommanderBar() {
  const { commanderMessages, pushCommanderMessage, replaceLastCommanderMessage, toast } =
    useStore();
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [commanderMessages]);

  async function ask(query) {
    const text = query.trim();
    if (!text || busy) return;

    setBusy(true);
    setInput('');
    pushCommanderMessage({ role: 'user', text });
    pushCommanderMessage({ role: 'assistant', pending: true });

    try {
      const result = await api.commander(text);
      replaceLastCommanderMessage({
        role: 'assistant',
        text: result.response,
        tool_calls: result.tool_calls,
        grounding: result.grounding,
        is_cached: result.is_cached,
      });
    } catch (error) {
      replaceLastCommanderMessage({
        role: 'assistant',
        text: error.isWarmingUp
          ? 'Network model is initializing — re-querying in a few seconds.'
          : `Could not answer query: ${error.message}`,
        tool_calls: [],
        grounding: null,
        is_cached: false,
      });
      if (!error.isWarmingUp) toast(error.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel flex min-h-0 flex-col overflow-hidden">
      {/* Panel Header */}
      <div className="panel-header">
        <div className="flex items-center gap-2">
          <div className="flex h-5 w-5 items-center justify-center rounded bg-teal-500/15 text-teal-400">
            <Sparkles className="h-3 w-3" />
          </div>
          <div>
            <h2 className="panel-title">Commander</h2>
            <div className="text-[9px] font-mono text-slate-400 leading-none">
              AI OPERATIONS ASSISTANT
            </div>
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="flex items-center gap-1 rounded bg-emerald-500/10 px-1.5 py-0.5 text-[9px] font-medium text-emerald-400 border border-emerald-500/20">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            ONLINE
          </span>
          <button
            type="button"
            onClick={() => setCollapsed((v) => !v)}
            className="rounded p-1 text-slate-400 hover:bg-surface-700 hover:text-slate-200 transition-colors"
            title={collapsed ? 'Expand commander' : 'Collapse commander'}
          >
            {collapsed ? <Maximize2 className="h-3 w-3" /> : <Minimize2 className="h-3 w-3" />}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div
          ref={scrollRef}
          className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-2.5"
        >
          {commanderMessages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center p-4">
              <Bot className="h-8 w-8 text-slate-600 mb-2" />
              <p className="text-xs font-medium text-slate-300">
                Operational Assistant Ready
              </p>
              <p className="mt-1 text-[11px] text-slate-500 max-w-[220px]">
                Query any venue entity, forecasted surge, cascade propagation or intervention certificate.
              </p>
            </div>
          ) : (
            commanderMessages.map((message, index) => <Message key={index} message={message} />)
          )}
        </div>
      )}

      {/* Suggested question chips */}
      <div className="border-t border-surface-700/60 bg-surface-900/40 px-2.5 py-2">
        <div className="mb-1 text-[9px] font-bold uppercase tracking-wider text-slate-400">
          Suggested Queries
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {SCRIPTED.map((question) => (
            <button
              key={question}
              type="button"
              disabled={busy}
              onClick={() => ask(question)}
              className="rounded-full border border-surface-700/80 bg-surface-850 px-2 py-0.5 text-[10px] text-slate-300 transition-all hover:border-teal-500/50 hover:bg-teal-500/10 hover:text-teal-200 disabled:opacity-40"
            >
              {question}
            </button>
          ))}
        </div>
      </div>

      {/* Query input form */}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          ask(input);
        }}
        className="flex items-center gap-1.5 border-t border-surface-700/60 bg-surface-900/80 p-2"
      >
        <div className="relative flex-1">
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Ask commander…"
            className="w-full rounded bg-surface-950 px-3 py-1.5 text-xs text-slate-200 outline-none ring-1 ring-surface-700/80 focus:ring-teal-500 placeholder:text-slate-500"
          />
        </div>
        <button
          type="submit"
          className="btn-primary h-8 px-3 text-xs"
          disabled={busy || !input.trim()}
          title="Send query"
        >
          <Send className="h-3 w-3" />
          <span>Ask</span>
        </button>
      </form>
    </section>
  );
}
