"use client";

import { useEffect, useRef, useState } from "react";
import { streamChat, type Api } from "@/lib/api";
import { ErrorBox, ResourceStatus, useResource } from "@/components/common";

type Message = { role: "user" | "assistant"; content: string };

export function Playground({ api }: { api: Api }) {
  const models = useResource(api, "/v1/models");
  const [model, setModel] = useState("");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | null>(null);
  const selected = model || String(models.rows[0]?.id || "");
  useEffect(() => () => abort.current?.abort(), []);
  async function send() {
    if (!input.trim() || !selected || busy) return;
    const history: Message[] = [...messages, { role: "user", content: input.trim() }];
    setMessages([...history, { role: "assistant", content: "" }]); setInput(""); setBusy(true); setError("");
    const controller = new AbortController(); abort.current = controller;
    try {
      await streamChat(api, { model: selected, messages: history, stream: true }, controller.signal, chunk => {
        setMessages(current => current.map((m, i) => i === current.length - 1 ? { ...m, content: m.content + chunk } : m));
      });
    } catch (e) { if ((e as Error).name !== "AbortError") setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="panel chat">
    <div className="panel-heading"><div><span className="status-dot" /><strong>Chat playground</strong></div><label>Model <select aria-label="Model" value={selected} onChange={e => setModel(e.target.value)} disabled={busy || models.loading}>{!models.rows.length && <option value="">No models available</option>}{models.rows.map(m => <option key={String(m.id)} value={String(m.id)}>{String(m.id)}</option>)}</select></label></div>
    <ResourceStatus {...models} /><ErrorBox error={error} />
    <div className="messages" role="log" aria-label="Conversation" aria-live="polite">
      {!messages.length && <div className="chat-empty"><span className="orb" aria-hidden="true">✳</span><h2>What will you build today?</h2><p className="muted">Talk to a model. Test an idea. Find a new direction.</p><div className="chips">{["Explain a complex idea", "Help me plan a project", "Explore a new perspective"].map(s => <button key={s} onClick={() => setInput(s)}>{s} ↗</button>)}</div></div>}
      {messages.map((message, i) => <article className={`message ${message.role}`} key={i}><span className="message-avatar">{message.role === "user" ? "You" : "SI"}</span><div><strong>{message.role === "user" ? "You" : selected}</strong><p>{message.content || (busy ? "Thinking…" : "No text returned.")}</p></div></article>)}
    </div>
    <form className="composer" onSubmit={e => { e.preventDefault(); void send(); }}><label className="sr-only" htmlFor="chat-input">Message</label><textarea id="chat-input" placeholder="Send a message to your model…" value={input} onChange={e => setInput(e.target.value)} disabled={busy} rows={3} /><div><small>Live streaming · Your conversation stays on this page</small>{busy ? <button type="button" onClick={() => abort.current?.abort()}>Stop generation</button> : <button className="primary" disabled={!selected || !input.trim()} type="submit">Send message ↑</button>}</div></form>
  </div>;
}
