'use client';

import { useState, useRef, useEffect } from 'react';
import TextareaAutosize from 'react-textarea-autosize';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { motion } from 'framer-motion';

// Mirrors negotiator/loop_control.py — shown as budget in the metrics strip.
const MAX_DRAFT_REVISIONS = 3;

// Sub-agent internals must never render in the vendor chat; they belong in
// the telemetry pane. Only the root orchestrator speaks to the user.
const SUB_AGENT_AUTHORS = ['Strategist', 'Drafter', 'Auditor'];

const INITIAL_METRICS = {
  draft_revisions: 0,
  audit_rejections: 0,
  guard_hits: 0,
  firewall_trips: 0,
  escalated: false,
  approved: false,
  tool_calls: 0,
  tactic: null,
  elapsed_ms: 0,
  status: 'idle',
};

let telemetrySeq = 0;
const uid = () => `${Date.now()}_${telemetrySeq++}`;

function parseMaybeJson(value) {
  if (value && typeof value === 'object') return value;
  if (typeof value !== 'string') return null;
  try {
    return JSON.parse(value);
  } catch {
    const unfenced = value.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
    try {
      return JSON.parse(unfenced);
    } catch {
      return null;
    }
  }
}

function statusLabel(metrics) {
  if (metrics.status === 'idle') return 'Idle';
  if (metrics.status === 'running') return 'Running';
  if (metrics.firewall_trips > 0) return 'Firewalled';
  if (metrics.escalated) return 'Escalated';
  if (metrics.approved) return 'Approved';
  return 'Completed';
}

function elapsedLabel(ms) {
  return ms > 0 ? `${(ms / 1000).toFixed(1)}s` : '—';
}

export default function AgenticUI() {
  const [input, setInput] = useState('');
  const [chatHistory, setChatHistory] = useState([]);
  const [telemetry, setTelemetry] = useState([]);
  const [metrics, setMetrics] = useState(INITIAL_METRICS);
  const [isProcessing, setIsProcessing] = useState(false);

  const chatEndRef = useRef(null);
  const telemetryEndRef = useRef(null);
  const abortControllerRef = useRef(null);
  const accumulatedRef = useRef('');
  // One ADK session per negotiation (not per message) so the orchestrator
  // keeps cross-turn memory: prior offers, turn counter, prior strategies.
  const sessionRef = useRef(null);

  // Auto-scroll logic
  useEffect(() => {
    if (chatHistory.length > 0) {
      chatEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [chatHistory]);

  useEffect(() => {
    if (telemetry.length > 0) {
      telemetryEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [telemetry]);

  const handleStop = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
  };

  const handleNewNegotiation = () => {
    sessionRef.current = null;
    accumulatedRef.current = '';
    setChatHistory([]);
    setTelemetry([]);
    setMetrics(INITIAL_METRICS);
  };

  const pushTelemetry = (entry) => {
    setTelemetry((prev) => [...prev, { id: uid(), ...entry }]);
  };

  const handleSubmit = async (e) => {
    if (e) e.preventDefault();
    if (!input.trim()) return;

    const userMessage = input;
    setInput('');
    setIsProcessing(true);
    setMetrics({ ...INITIAL_METRICS, status: 'running' });
    accumulatedRef.current = '';

    abortControllerRef.current = new AbortController();
    const startedAt = Date.now();

    const appName = 'negotiator';
    const userId = 'portfolio_user';
    let sessionId = sessionRef.current;

    setChatHistory((prev) => [...prev, { role: 'user', content: userMessage }]);

    // ---- One SSE event -> one UI update -------------------------------
    const handleEvent = (event) => {
      // Backend loop metrics arrive as state deltas from loop_control's
      // callbacks — they are authoritative over client-side counts.
      const delta = event.actions?.state_delta;
      if (delta) {
        if (delta.run_metrics) {
          setMetrics((prev) => ({ ...prev, ...delta.run_metrics }));
        }
        if (Array.isArray(delta.guard_findings) && delta.guard_findings.length > 0) {
          pushTelemetry({ type: 'guard', data: delta.guard_findings });
        }
      }

      const content = event.content || {};
      const author = content.author || event.author || '';
      const parts = content.parts || [];

      for (const part of parts) {
        if (part.functionCall) {
          setMetrics((prev) => ({ ...prev, tool_calls: prev.tool_calls + 1 }));
          pushTelemetry({
            type: 'tool_call',
            agent: part.functionCall.name,
            data: part.functionCall.args,
          });
        } else if (part.functionResponse) {
          const name = part.functionResponse.name || '';
          const data = part.functionResponse.response;
          pushTelemetry({ type: 'tool_response', agent: name, data });

          const parsed = parseMaybeJson(data);
          if (parsed) {
            if (/auditor/i.test(name)) {
              const violations = Array.isArray(parsed.violations) ? parsed.violations : [];
              setMetrics((prev) => ({
                ...prev,
                approved: prev.approved || parsed.status === 'approved',
                escalated: prev.escalated || parsed.status === 'escalated',
                audit_rejections:
                  prev.audit_rejections + (parsed.status === 'rejected' ? 1 : 0),
                guard_hits: prev.guard_hits + violations.length,
              }));
            } else if (/strategist/i.test(name) && parsed.selected_tactic) {
              setMetrics((prev) => ({ ...prev, tactic: parsed.selected_tactic }));
            }
          }
        } else if (part.text) {
          if (SUB_AGENT_AUTHORS.includes(author)) {
            // Internal reasoning/drafts are telemetry, never chat.
            pushTelemetry({ type: 'internal', agent: author, data: part.text });
          } else {
            accumulatedRef.current += part.text;
            const snapshot = accumulatedRef.current;
            setChatHistory((prev) => {
              const last = prev[prev.length - 1];
              if (last && last.role === 'model' && last.streaming) {
                return [...prev.slice(0, -1), { ...last, content: snapshot }];
              }
              return [...prev, { role: 'model', content: snapshot, streaming: true }];
            });
          }
        }
      }
    };

    // Buffered line handling: SSE frames can split across network chunks,
    // so incomplete trailing lines must be carried over to the next read.
    const handleLine = (rawLine) => {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
      if (!line.startsWith('data:')) return;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') return;
      try {
        handleEvent(JSON.parse(payload));
      } catch {
        pushTelemetry({
          type: 'system',
          message: 'Dropped an undecodable SSE frame (partial or malformed).',
        });
      }
    };

    try {
      if (!sessionId) {
        sessionId = 's_' + Date.now();
        pushTelemetry({ type: 'system', message: 'Registering new ADK Session...' });
        const sessionResponse = await fetch(`/api/apps/${appName}/users/${userId}/sessions/${sessionId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
          signal: abortControllerRef.current.signal
        });

        if (!sessionResponse.ok) throw new Error(`Failed to create session: ${sessionResponse.status}`);
        sessionRef.current = sessionId;
        pushTelemetry({ type: 'system', message: 'Session created. Firing Orchestrator...' });
      } else {
        pushTelemetry({ type: 'system', message: `Resuming session ${sessionId} (cross-turn memory on). Firing Orchestrator...` });
      }

      const response = await fetch('/api/run_sse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          appName: appName,
          userId: userId,
          sessionId: sessionId,
          newMessage: { role: 'user', parts: [{ text: userMessage }] },
          streaming: false
        }),
        signal: abortControllerRef.current.signal
      });

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) handleLine(line);
      }
      buffer += decoder.decode();
      if (buffer) handleLine(buffer);
    } catch (error) {
      if (error.name === 'AbortError') {
        pushTelemetry({ type: 'system', message: 'Pipeline stopped by user.' });
      } else {
        console.error('Stream failed:', error);
        // A failed turn often means the server restarted — drop the session
        // so the next turn registers a fresh one.
        sessionRef.current = null;
        setChatHistory((prev) => [...prev, { role: 'error', content: 'Connection to ADK server failed.' }]);
      }
    } finally {
      setIsProcessing(false);
      abortControllerRef.current = null;
      setChatHistory((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'model' && last.streaming) {
          return [...prev.slice(0, -1), { ...last, streaming: false }];
        }
        return prev;
      });
      setMetrics((prev) => ({
        ...prev,
        elapsed_ms: Date.now() - startedAt,
        status: prev.status === 'running' ? 'completed' : prev.status,
      }));
      pushTelemetry({ type: 'system', message: 'Orchestrator pipeline complete.' });
    }
  };

  return (
    <div className="relative w-full h-[100dvh] bg-slate-950 text-slate-200 font-sans overflow-hidden selection:bg-indigo-500/30 flex flex-col p-4 md:p-6 lg:p-8">

      {/* GLOBAL CSS INJECTION */}
      <style dangerouslySetInnerHTML={{
        __html: `
        /* Premium Frosted Glass Panel */
        .glass-panel {
          backdrop-filter: blur(24px);
          -webkit-backdrop-filter: blur(24px);
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-top: 1px solid rgba(255, 255, 255, 0.15);
          border-left: 1px solid rgba(255, 255, 255, 0.15);
          box-shadow: 
            0 20px 40px -10px rgba(0, 0, 0, 0.5),
            0 8px 16px -4px rgba(0, 0, 0, 0.3),
            inset 0 1px 2px rgba(255, 255, 255, 0.05);
        }

        /* Custom Indigo Scrollbar */
        .lumen-scrollbar::-webkit-scrollbar {
          width: 6px;
          height: 6px;
        }
        .lumen-scrollbar::-webkit-scrollbar-track {
          background: transparent;
        }
        .lumen-scrollbar::-webkit-scrollbar-thumb {
          background: rgba(99, 102, 241, 0.2);
          border-radius: 10px;
          transition: background 0.2s ease;
        }
        .lumen-scrollbar::-webkit-scrollbar-thumb:hover {
          background: rgba(99, 102, 241, 0.6);
        }
        .lumen-scrollbar::-webkit-scrollbar-corner {
          background: transparent;
        }
      `}} />

      {/* LUMEN LOOPS VIDEO BACKGROUND */}
      <div className="absolute inset-0 w-full h-full overflow-hidden pointer-events-none z-0">
        <video
          autoPlay
          loop
          muted
          playsInline
          className="absolute inset-0 w-full h-full object-cover opacity-60 mix-blend-screen"
        >
          <source src="/videos/webm/grid_1.webm" type="video/webm" />
          <source src="/videos/mp4/grid_1.mp4" type="video/mp4" />
        </video>
        <div className="absolute inset-0 bg-gradient-to-br from-slate-950/80 via-slate-950/60 to-slate-950/90" />
      </div>

      {/* Main Content Wrapper */}
      <div className="relative z-10 flex flex-col lg:flex-row h-full w-full max-w-screen-2xl mx-auto gap-4 md:gap-6 min-h-0">

        {/* LEFT PANE: Vendor Chat */}
        <div className="flex-1 flex flex-col bg-[rgba(255,255,255,0.03)] glass-panel rounded-3xl overflow-hidden transition-all duration-300 min-h-0">

          <div className="shrink-0 px-5 md:px-6 py-4 md:py-5 border-b border-white/10 bg-white/5 flex justify-between items-center">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-indigo-500/30">
                <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 8h2a2 2 0 012 2v6a2 2 0 01-2 2h-2v4l-4-4H9a1.994 1.994 0 01-1.414-.586m0 0L11 14h4a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2v4l.586-.586z" />
                </svg>
              </div>
              <h1 className="font-semibold text-white tracking-wide">Vendor Portal</h1>
            </div>

            <button
              onClick={handleNewNegotiation}
              className="group flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-slate-400 hover:text-white transition-all text-xs font-medium border border-white/5"
            >
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              New negotiation
            </button>
          </div>

          <div className="flex-1 overflow-y-auto lumen-scrollbar p-4 md:p-6 space-y-6">
            {chatHistory.length === 0 && (
              <div className="h-full flex flex-col items-center justify-center text-slate-400 gap-4 opacity-60">
                <div className="w-16 h-16 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center">
                  <svg className="w-8 h-8 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                  </svg>
                </div>
                <p className="text-sm tracking-wide">Start the negotiation below</p>
              </div>
            )}

            {chatHistory.map((msg, i) => (
              <motion.div
                initial={{ opacity: 0, y: 10, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                transition={{ type: 'spring', bounce: 0.4, duration: 0.5 }}
                key={i}
                className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'} group`}
              >
                <div className={`
                  relative px-5 py-4 rounded-3xl max-w-[85%] text-sm leading-relaxed shadow-lg
                  ${msg.role === 'user'
                    ? 'bg-gradient-to-br from-indigo-500 to-blue-600 text-white rounded-br-sm shadow-[0_8px_16px_rgba(99,102,241,0.2)]'
                    : msg.role === 'error'
                      ? 'bg-red-500/10 border border-red-500/20 text-red-300 rounded-bl-sm'
                      : 'bg-white/10 border border-white/15 border-t-white/25 border-l-white/25 text-slate-100 rounded-bl-sm shadow-xl transition-all group-hover:bg-white/15'
                  }`}>
                  {msg.role === 'user' ? (
                    <div className="whitespace-pre-wrap">{msg.content}</div>
                  ) : (
                    // Move the className to a wrapper div!
                    <div className="prose prose-sm prose-invert max-w-none break-words [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {msg.content}
                      </ReactMarkdown>
                    </div>
                  )}
                </div>
              </motion.div>
            ))}

            {/* TYPING INDICATOR (Shows when agent is working but hasn't replied yet) */}
            {isProcessing && chatHistory[chatHistory.length - 1]?.role === 'user' && (
              <motion.div
                initial={{ opacity: 0, y: 10, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                transition={{ type: 'spring', bounce: 0.4, duration: 0.5 }}
                className="flex justify-start group"
              >
                <div className="relative px-5 py-4 rounded-3xl max-w-[85%] bg-white/10 border border-white/15 border-t-white/25 border-l-white/25 text-slate-100 rounded-bl-sm shadow-xl flex items-center gap-1.5 h-[52px]">
                  <motion.div className="w-1.5 h-1.5 rounded-full bg-slate-400" animate={{ y: [0, -4, 0] }} transition={{ duration: 0.6, repeat: Infinity, delay: 0 }} />
                  <motion.div className="w-1.5 h-1.5 rounded-full bg-slate-400" animate={{ y: [0, -4, 0] }} transition={{ duration: 0.6, repeat: Infinity, delay: 0.2 }} />
                  <motion.div className="w-1.5 h-1.5 rounded-full bg-slate-400" animate={{ y: [0, -4, 0] }} transition={{ duration: 0.6, repeat: Infinity, delay: 0.4 }} />
                </div>
              </motion.div>
            )}

            <div ref={chatEndRef} className="h-1" />
          </div>

          <form onSubmit={handleSubmit} className="shrink-0 p-3 md:p-4 bg-black/20 border-t border-white/5">
            <div className="relative flex items-end gap-3">
              <TextareaAutosize
                minRows={1}
                maxRows={5}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    handleSubmit(e);
                  }
                }}
                disabled={isProcessing}
                placeholder="Type your message... (Shift+Enter for new line)"
                className="flex-1 bg-white/5 border border-white/10 rounded-2xl pl-5 pr-14 py-3 md:py-4 text-sm text-white placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500/50 disabled:opacity-50 transition-all resize-none lumen-scrollbar shadow-inner"
              />

              {isProcessing ? (
                <button
                  type="button"
                  onClick={handleStop}
                  className="absolute right-2 bottom-2 md:bottom-2.5 w-10 h-10 rounded-xl bg-slate-700 hover:bg-red-500 text-white flex items-center justify-center transition-all shadow-lg"
                >
                  <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                    <rect x="6" y="6" width="12" height="12" rx="2" />
                  </svg>
                </button>
              ) : (
                <button
                  type="submit"
                  disabled={!input.trim()}
                  className="absolute right-2 bottom-2 md:bottom-2.5 w-10 h-10 rounded-xl bg-indigo-500 hover:bg-indigo-400 disabled:opacity-50 disabled:hover:bg-indigo-500 text-white flex items-center justify-center transition-all shadow-lg shadow-indigo-500/30"
                >
                  <svg className="w-4 h-4 translate-x-[1px] translate-y-[-1px]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                  </svg>
                </button>
              )}
            </div>
          </form>
        </div>

        {/* RIGHT PANE: Telemetry */}
        <div className="flex-1 flex flex-col bg-[rgba(15,23,42,0.45)] glass-panel rounded-3xl overflow-hidden min-h-0">

          <div className="shrink-0 px-5 md:px-6 py-4 md:py-5 border-b border-white/10 bg-black/20 flex justify-between items-center">
            <div className="flex items-center gap-3">
              <div className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_8px_rgba(52,211,153,0.8)]"></div>
              <h2 className="font-mono font-medium tracking-widest text-slate-300 text-[10px] md:text-xs uppercase">ADK Telemetry Stream</h2>
            </div>

            <button
              onClick={() => setTelemetry([])}
              className="group flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-slate-400 hover:text-white transition-all text-xs font-medium border border-white/5"
            >
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
              Clear
            </button>
          </div>

          {/* RUN METRICS STRIP — fed by loop_control state deltas + stream events */}
          <div className="shrink-0 px-4 md:px-6 py-3 border-b border-white/5 bg-black/10">
            <div className="grid grid-cols-3 lg:grid-cols-6 gap-2">
              <MetricChip
                label="Verdict"
                value={statusLabel(metrics)}
                tone={
                  metrics.status === 'idle' ? 'muted'
                    : metrics.firewall_trips > 0 || metrics.escalated ? 'danger'
                      : metrics.approved ? 'ok' : 'info'
                }
              />
              <MetricChip
                label="Draft revisions"
                value={`${metrics.draft_revisions}/${MAX_DRAFT_REVISIONS}`}
                tone={metrics.draft_revisions >= MAX_DRAFT_REVISIONS ? 'danger' : 'info'}
              />
              <MetricChip
                label="Guard hits"
                value={metrics.guard_hits}
                tone={metrics.guard_hits > 0 ? 'danger' : 'ok'}
              />
              <MetricChip label="Tool calls" value={metrics.tool_calls} tone="info" />
              <MetricChip
                label="Tactic"
                value={metrics.tactic ? metrics.tactic.replace(/_/g, ' ') : '—'}
                tone="muted"
              />
              <MetricChip label="Elapsed" value={elapsedLabel(metrics.elapsed_ms)} tone="info" />
            </div>
          </div>

          <div className="flex-1 overflow-y-auto lumen-scrollbar p-4 md:p-6 space-y-4 font-mono text-[11px] md:text-[13px]">
            {telemetry.length === 0 && (
              <div className="h-full flex items-center justify-center text-slate-600 opacity-50 font-mono text-sm">
                Waiting for streaming data...
              </div>
            )}

            {telemetry.map((log) => (
              <motion.div
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ type: 'spring', stiffness: 200, damping: 20 }}
                key={log.id}
                className="p-3 md:p-4 border border-white/5 rounded-2xl bg-black/40 shadow-inner group transition-all hover:bg-black/60"
              >
                <div className="flex items-center gap-2 mb-3">
                  {log.type === 'system' ? (
                    <span className="flex items-center gap-2 text-cyan-400">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                      <span className="font-semibold tracking-wide">SYSTEM</span>
                    </span>
                  ) : log.type === 'tool_call' ? (
                    <span className="flex items-center gap-2 text-amber-400">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
                      <span className="font-semibold tracking-wide">EXEC: {log.agent}</span>
                    </span>
                  ) : log.type === 'tool_response' ? (
                    <span className="flex items-center gap-2 text-fuchsia-400">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6" /></svg>
                      <span className="font-semibold tracking-wide">RESP: {log.agent}</span>
                    </span>
                  ) : log.type === 'guard' ? (
                    <span className="flex items-center gap-2 text-red-400">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M5.07 19h13.86a2 2 0 001.74-3L13.74 4a2 2 0 00-3.46 0L3.34 16a2 2 0 001.73 3z" /></svg>
                      <span className="font-semibold tracking-wide">GUARD: firewall finding</span>
                    </span>
                  ) : (
                    <span className="flex items-center gap-2 text-purple-400">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                      <span className="font-semibold tracking-wide">INTERNAL: {log.agent}</span>
                    </span>
                  )}
                </div>

                {log.data ? (
                  <div className="relative rounded-xl overflow-hidden bg-slate-950/80 border border-slate-800/80 shadow-inner">
                    <div className="absolute top-0 left-0 w-1 h-full bg-gradient-to-b from-white/10 to-transparent z-10"></div>
                    <pre className="relative whitespace-pre-wrap overflow-x-auto overflow-y-auto max-h-64 p-3 md:p-4 text-slate-300 leading-relaxed lumen-scrollbar">
                      {typeof log.data === 'string' ? log.data : JSON.stringify(log.data, null, 2)}
                    </pre>
                  </div>
                ) : (
                  <div className="text-slate-400 border-l-2 border-slate-700/50 pl-3 ml-1 py-1 italic tracking-wide">
                    {log.message}
                  </div>
                )}
              </motion.div>
            ))}
            <div ref={telemetryEndRef} className="h-1" />
          </div>
        </div>

      </div>
    </div>
  );
}

function MetricChip({ label, value, tone = 'info' }) {
  const tones = {
    ok: 'text-emerald-300 border-emerald-400/20 bg-emerald-400/5',
    danger: 'text-red-300 border-red-400/20 bg-red-400/5',
    info: 'text-indigo-300 border-indigo-400/20 bg-indigo-400/5',
    muted: 'text-slate-300 border-white/10 bg-white/5',
  };
  return (
    <div className={`rounded-xl border px-2.5 py-2 ${tones[tone] || tones.info}`}>
      <div className="text-[9px] uppercase tracking-widest opacity-70 font-mono">{label}</div>
      <div className="text-sm font-semibold capitalize truncate">{value}</div>
    </div>
  );
}
