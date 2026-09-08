/**
 * CommanderBar — 02_FRONTEND_CONTRACT.md §5.7.
 *
 * **Never render an assistant message without the sources tray available.** The
 * visible grounding is the whole point: every number in an answer traces to a
 * tool call the backend actually made, and the tray is where a judge checks.
 */
import { useEffect, useRef, useState } from 'react';

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
  return (
    <div className="mt-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[11px] text-slate-500 hover:text-slate-300"
      >
        {open ? '▾' : '▸'} Sources ({toolCalls.length})
      </button>
      {open && (
        <ul className="mt-1 space-y-1 rounded border border-surface-600 bg-surface-900/70 p-2">
          {toolCalls.length === 0 && (
            <li className="text-[11px] text-slate-500">No tool calls recorded.</li>
          )}
          {toolCalls.map((call, index) => (
            <li key={index} className="font-mono text-[10px] leading-relaxed">
              <span className="text-sky-400">{call.tool}</span>
              <span className="text-slate-500">({JSON.stringify(call.args)})</span>
              <span className="text-slate-400"> → {call.result_digest}</span>
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
      <div className="flex justify-end">
        <div className="max-w-[75%] rounded-lg bg-sky-600/20 px-3 py-1.5 text-sm text-sky-100">
          {message.text}
        </div>
      </div>
    );
  }

  if (message.pending) {
    return (
      <div className="max-w-[85%] space-y-1.5">
        <div className="skeleton h-3 w-3/4" />
        <div className="skeleton h-3 w-1/2" />
      </div>
    );
  }

  return (
    <div className="max-w-[85%]">
      <div className="rounded-lg bg-surface-700 px-3 py-2 text-sm leading-relaxed text-slate-200">
        {message.text}
      </div>
      <div className="mt-1 flex items-center gap-2">
        {message.is_cached && (
          <span className="chip bg-surface-600 text-slate-400">cached</span>
        )}
        {message.grounding && !message.grounding.passed && (
          <span className="chip border border-amber-500/30 bg-amber-500/15 text-amber-400">
            Some values could not be grounded and were removed.
          </span>
        )}
      </div>
      <SourcesTray toolCalls={message.tool_calls || []} />
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
          ? 'Still warming up — ask again in a few seconds.'
          : `Could not answer: ${error.message}`,
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
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-header">
        <h2 className="panel-title">Commander</h2>
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="text-[11px] text-slate-500 hover:text-slate-300"
        >
          {collapsed ? 'expand' : 'collapse'}
        </button>
      </div>

      {!collapsed && (
        <div
          ref={scrollRef}
          className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 py-2"
        >
          {commanderMessages.length === 0 ? (
            <p className="text-[11px] text-slate-500">
              Ask about any entity, forecast, cascade or certificate.
            </p>
          ) : (
            commanderMessages.map((message, index) => <Message key={index} message={message} />)
          )}
        </div>
      )}

      {/* Scripted prompts — compact pill buttons that wrap in narrow sidebar */}
      <div className="flex flex-wrap items-center gap-1 border-t border-surface-700 px-2.5 py-1.5">
        {SCRIPTED.map((question) => (
          <button
            key={question}
            type="button"
            disabled={busy}
            onClick={() => ask(question)}
            className="rounded-full border border-surface-600 px-2 py-0.5 text-[10px] text-slate-400 transition-colors hover:border-sky-500/40 hover:text-sky-300 disabled:opacity-40"
          >
            {question}
          </button>
        ))}
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          ask(input);
        }}
        className="flex gap-1.5 border-t border-surface-700 px-2.5 py-2"
      >
        <input
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="Ask the commander…"
          className="flex-1 rounded bg-surface-900 px-2.5 py-1.5 text-[12px] text-slate-200 outline-none ring-1 ring-surface-600 focus:ring-sky-500"
        />
        <button type="submit" className="btn-primary text-[11px]" disabled={busy || !input.trim()}>
          Ask
        </button>
      </form>
    </section>
  );
}
