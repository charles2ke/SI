"use client";

import { useState } from "react";
import type { Api, Row } from "@/lib/api";
import { Empty, Json, ResourceStatus, plural, time, useResource } from "@/components/common";

const KINDS = [["", "All kinds"], ["model", "Agent model calls"], ["tool", "Agent tool calls"], ["gateway", "Gateway calls"], ["embedding", "Embeddings"], ["request", "API requests"]];

function summary(trace: Row) {
  return [
    typeof trace.latency_ms === "number" ? `${trace.latency_ms.toFixed(0)} ms` : "",
    typeof trace.tokens === "number" && trace.tokens ? plural(trace.tokens, "token") : "",
    typeof trace.cost === "number" && trace.cost ? `$${trace.cost.toFixed(4)}` : "",
    time(trace.created_at),
    trace.run_id ? `run ${String(trace.run_id)}` : "",
  ].filter(Boolean).join(" · ");
}

export function Traces({ api, runId = "" }: { api: Api; runId?: string }) {
  const [filter, setFilter] = useState({ run: runId, kind: "" });
  const [draft, setDraft] = useState(runId);
  const query = new URLSearchParams(Object.entries({ run_id: filter.run, kind: filter.kind }).filter(([, value]) => value)).toString();
  const traces = useResource(api, `/v1/traces${query ? `?${query}` : ""}`);
  const kindLabel = filter.kind ? KINDS.find(([value]) => value === filter.kind)?.[1] : "";
  return <div className="panel padded">
    <div className="section-heading"><h2>Execution traces</h2><button disabled={traces.loading} onClick={() => void traces.refresh()}>Refresh ↻</button></div>
    <form className="filters" onSubmit={e => { e.preventDefault(); setFilter({ ...filter, run: draft.trim() }); }}>
      <label>Run ID<input value={draft} onChange={e => setDraft(e.target.value)} placeholder="All runs" /></label>
      <label>Kind<select value={filter.kind} onChange={e => setFilter({ ...filter, kind: e.target.value })}>{KINDS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <button>Apply filter</button>
      {(filter.run || filter.kind) && <button type="button" className="subtle" onClick={() => { setFilter({ run: "", kind: "" }); setDraft(""); }}>Clear</button>}
    </form>
    <p className="muted">Showing the latest 500 spans{kindLabel ? ` (${kindLabel})` : ""}{filter.run ? ` for run ${filter.run}` : ""}. Every authenticated API call records a request span.</p>
    <ResourceStatus {...traces} />
    {!traces.loading && !traces.error && !traces.rows.length && <Empty title="Nothing to trace. Yet." detail="Run a model or agent to see its execution history here." />}
    {traces.rows.map((trace, i) => <details className="trace" key={String(trace.id || i)}>
      <summary><span className="badge">{String(trace.kind || "span")}</span><strong>{String(trace.name || trace.id || `Trace ${i + 1}`)}</strong><small>{summary(trace)}</small></summary>
      <Json value={trace} />
    </details>)}
  </div>;
}
