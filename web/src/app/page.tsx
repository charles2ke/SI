"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { Api } from "@/lib/api";
import { Empty } from "@/components/common";
import { Playground } from "@/components/playground";
import { Agents } from "@/components/agents";
import { Knowledge } from "@/components/knowledge";
import { Prompts } from "@/components/prompts";
import { Traces } from "@/components/traces";
import { Evals } from "@/components/evals";

const tabs = ["Playground", "Agents", "Knowledge", "Prompts", "Traces", "Evals"] as const;
type Tab = (typeof tabs)[number];
const icons: Record<Tab, string> = { Playground: "◈", Agents: "⌘", Knowledge: "▤", Prompts: "¶", Traces: "≋", Evals: "◎" };
const descriptions: Record<Tab, string> = {
  Playground: "A space to explore your models. Start a conversation.",
  Agents: "Give your models a purpose, tools, and room to work.",
  Knowledge: "Ground your intelligence in your own knowledge.",
  Prompts: "Version your prompts. Render them before you rely on them.",
  Traces: "Follow every step. Understand every outcome.",
  Evals: "Measure what matters before you ship.",
};

export default function Home() {
  const [key, setKey] = useState("");
  const [activeKey, setActiveKey] = useState("");
  const [tab, setTab] = useState<Tab>("Playground");
  const [traceRun, setTraceRun] = useState("");
  const api = useMemo(() => new Api(activeKey), [activeKey]);
  function open(next: Tab, runId = "") { setTab(next); setTraceRun(runId); }

  return <div className="shell">
    <aside className="sidebar">
      <Link className="brand" href="/"><span className="brand-icon">si</span><span>SI<span className="muted"> / console</span></span></Link>
      <div className="workspace"><span className="workspace-icon">W</span><div>My workspace<small>Intelligence, connected.</small></div></div>
      <p className="eyebrow">WORKSPACE</p>
      <nav aria-label="Workspace">{tabs.map(item => <button key={item} aria-current={tab === item ? "page" : undefined} onClick={() => open(item)}><span aria-hidden="true">{icons[item]}</span>{item}</button>)}</nav>
      <div className="sidebar-bottom"><span className="status-dot" /> Your infrastructure. Your intelligence.<small>SI · Developer console</small></div>
    </aside>
    <div className="content">
      <header><span>Workspace <span className="muted">/ {tab}</span></span><span className="environment">LOCAL WORKSPACE</span></header>
      <main>
        <div className="page-heading"><div><p className="eyebrow">BUILD WITH INTELLIGENCE</p><h1>{tab}</h1><p className="muted">{descriptions[tab]}</p></div><span className="section-number">0{tabs.indexOf(tab) + 1} / 0{tabs.length}</span></div>
        <form className="connection panel" onSubmit={e => { e.preventDefault(); setActiveKey(key.trim()); }}>
          <div><strong>API connection</strong><small>{api.base} · Key stays in memory for this page only.</small></div>
          <label className="sr-only" htmlFor="api-key">API key</label><input id="api-key" type="password" autoComplete="off" placeholder="Enter your API key" value={key} onChange={e => setKey(e.target.value)} required />
          <button className="primary" type="submit">{activeKey ? "Reconnect" : "Connect"}</button>
          <button type="button" className="subtle" onClick={() => { setKey("si-local-development"); setActiveKey("si-local-development"); }}>Use local dev key</button>
        </form>
        {!activeKey ? <Empty title="Your workspace is ready" detail="Connect with an API key to load live models, agents, and knowledge. For the local bootstrap server, choose “Use local dev key”." /> :
          <section key={`${activeKey}-${tab}-${traceRun}`} aria-label={`${tab} workspace`}>
            {tab === "Playground" && <Playground api={api} />}
            {tab === "Agents" && <Agents api={api} onTraces={runId => open("Traces", runId)} />}
            {tab === "Knowledge" && <Knowledge api={api} />}
            {tab === "Prompts" && <Prompts api={api} />}
            {tab === "Traces" && <Traces api={api} runId={traceRun} />}
            {tab === "Evals" && <Evals api={api} />}
          </section>}
        <footer>Built for the way you think.<span>SI / OPEN INTELLIGENCE</span></footer>
      </main>
    </div>
  </div>;
}
