"use client";

import { useState } from "react";
import type { Api, Row } from "@/lib/api";
import { ErrorBox, Json, ResourceStatus, StatusBadge, clip, placeholders, plural, rows, score, time, useAction, useResource } from "@/components/common";

const SCORERS = [
  ["exact_match", "Exact match", "Passes when the output equals expected exactly."],
  ["contains", "Contains", "Passes when the output contains expected."],
  ["regex", "Regular expression", "Expected is a regular expression searched for in the output."],
  ["json_schema", "JSON Schema", "Expected is a JSON Schema; the output must be JSON that satisfies it."],
  ["llm_judge", "LLM judge", "The selected model scores the output against expected from 0 to 1."],
] as const;
const EXAMPLE = '[\n  { "input": "", "expected": "" }\n]';
const isCase = (row: unknown) => Boolean(row) && typeof row === "object" && !Array.isArray(row) && typeof (row as Row).input === "string" && "expected" in (row as Row);

function describe(item: Row, agentNames: Map<string, string>) {
  const config = (item.config && typeof item.config === "object" ? item.config : {}) as Row;
  const scorer = SCORERS.find(([id]) => id === config.scorer)?.[1];
  const target = config.agent_id ? `Agent ${agentNames.get(String(config.agent_id)) ?? String(config.agent_id)}`
    : config.prompt_name ? `Prompt ${String(config.prompt_name)}${config.prompt_version ? ` v${String(config.prompt_version)}` : ""}` : "";
  return [scorer, target, typeof config.cases === "number" ? plural(config.cases, "case") : ""].filter(Boolean).join(" · ") || `Evaluation ${String(item.id)}`;
}

function Cases({ results }: { results: Row[] }) {
  return <div className="table-wrap"><table>
    <thead><tr><th className="fit">#</th><th className="share">Input</th><th className="share">Expected</th><th>Output</th><th className="fit">Score</th><th className="fit">Result</th></tr></thead>
    <tbody>{results.map((result, i) => <tr key={i}>
      <td className="fit">{Number(result.index ?? i) + 1}</td><td>{clip(result.input, 200)}</td><td>{clip(result.expected, 200)}</td>
      <td>{result.error ? <span className="run-error">{clip(result.error, 200)}</span> : clip(result.output, 200)}</td>
      <td className="fit">{score(result.score)}</td><td className="fit"><span className={`badge ${result.passed ? "status-completed" : "status-failed"}`}>{result.passed ? "Pass" : "Fail"}</span></td>
    </tr>)}</tbody>
  </table></div>;
}

function Outcome({ item }: { item: Row }) {
  const results = rows(item.results);
  return <><p className="score">Score <strong>{score(item.score)}</strong> <span>{results.filter(result => result.passed).length} / {results.length} passed</span></p><Cases results={results} /></>;
}

export function Evals({ api }: { api: Api }) {
  const evals = useResource(api, "/v1/evals");
  const agents = useResource(api, "/v1/agents");
  const prompts = useResource(api, "/v1/prompts");
  const models = useResource(api, "/v1/models");
  const [target, setTarget] = useState("agent");
  const [agentId, setAgentId] = useState("");
  const [promptName, setPromptName] = useState("");
  const [version, setVersion] = useState("");
  const [model, setModel] = useState("");
  const [scorer, setScorer] = useState("exact_match");
  const [threshold, setThreshold] = useState("1");
  const [dataset, setDataset] = useState(EXAMPLE);
  const [result, setResult] = useState<Row | null>(null);
  const action = useAction();

  const agentChoice = agentId || String(agents.rows[0]?.id ?? "");
  const agentNames = new Map(agents.rows.map(row => [String(row.id), String(row.name)]));
  const promptNames = [...new Set(prompts.rows.map(row => String(row.name)))].sort();
  const promptChoice = promptName || promptNames[0] || "";
  const promptVersions = prompts.rows.filter(row => row.name === promptChoice).sort((a, b) => Number(b.version) - Number(a.version));
  const promptRow = version ? promptVersions.find(row => String(row.version) === version) : promptVersions[0];
  const otherFields = promptRow ? placeholders(String(promptRow.template)).filter(field => field !== "input") : [];
  const modelIds = models.rows.map(row => String(row.id));
  const modelChoice = model || modelIds[0] || "";
  const needsModel = target === "prompt" || scorer === "llm_judge";
  const ready = target === "agent" ? Boolean(agentChoice) : Boolean(promptChoice);

  function submit() {
    void action.perform(async () => {
      const parsed: unknown = JSON.parse(dataset);
      if (!Array.isArray(parsed) || !parsed.length || !parsed.every(isCase)) throw new Error("Dataset must be a non-empty JSON array of objects, each with a string input and an expected value.");
      const body = {
        dataset: parsed, scorer, threshold: Number(threshold),
        ...(target === "agent" ? { agent_id: agentChoice } : { prompt_name: promptChoice, ...(version ? { prompt_version: Number(version) } : {}) }),
        ...(needsModel && modelChoice ? { model: modelChoice } : {}),
      };
      setResult(await api.request("/v1/evals", { method: "POST", body: JSON.stringify(body) }) as Row);
      await evals.refresh();
    });
  }

  return <div className="split">
    <div className="panel padded">
      <h2>Run an evaluation</h2>
      <p className="muted">Real inputs. Expected outputs. Measurable quality.</p>
      <ErrorBox error={action.error} />
      <form onSubmit={e => { e.preventDefault(); submit(); }}>
        <label>Target type<select value={target} onChange={e => setTarget(e.target.value)}><option value="agent">Agent</option><option value="prompt">Prompt</option></select></label>
        {target === "agent" ? <label>Agent<select required value={agentChoice} disabled={agents.loading} onChange={e => setAgentId(e.target.value)}>
          {!agents.rows.length && <option value="">{agents.loading ? "Loading agents…" : "No agents yet"}</option>}
          {agents.rows.map(row => <option key={String(row.id)} value={String(row.id)}>{String(row.name)}</option>)}
        </select></label> : <div className="two-columns">
          <label>Prompt<select required value={promptChoice} disabled={prompts.loading} onChange={e => { setPromptName(e.target.value); setVersion(""); }}>
            {!promptNames.length && <option value="">{prompts.loading ? "Loading prompts…" : "No prompts yet"}</option>}
            {promptNames.map(name => <option key={name} value={name}>{name}</option>)}
          </select></label>
          <label>Version<select value={version} onChange={e => setVersion(e.target.value)}>
            <option value="">Latest</option>
            {promptVersions.map(row => <option key={String(row.version)} value={String(row.version)}>v{String(row.version)}</option>)}
          </select></label>
        </div>}
        {target === "prompt" && <p className={`hint ${otherFields.length ? "run-error" : "muted"}`}>Each case’s input fills the template’s {"{input}"} placeholder.{otherFields.length ? ` This version also uses ${otherFields.map(field => `{${field}}`).join(", ")}, so its cases will fail.` : ""}</p>}
        <ResourceStatus {...(target === "agent" ? agents : prompts)} />
        <div className="two-columns">
          <label>Scorer<select value={scorer} onChange={e => setScorer(e.target.value)}>{SCORERS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
          <label>Pass threshold<input type="number" min={0} max={1} step={0.05} required value={threshold} onChange={e => setThreshold(e.target.value)} /></label>
        </div>
        <p className="muted hint">{SCORERS.find(([id]) => id === scorer)?.[2]} Cases pass when their score is at least the threshold.</p>
        {needsModel && <label>Model <small>{target === "agent" ? "Judges each output. The agent itself runs on its own model." : scorer === "llm_judge" ? "Runs each rendered prompt and judges its output." : "Runs each rendered prompt."}</small><select required value={modelChoice} disabled={models.loading} onChange={e => setModel(e.target.value)}>
          {!modelIds.length && <option value="">No models available</option>}
          {modelIds.map(item => <option key={item} value={item}>{item}</option>)}
        </select></label>}
        <label>Dataset (JSON) <small>Array of {"{ \"input\": string, \"expected\": any JSON }"}; up to 1000 cases.</small><textarea className="code-input" rows={8} required value={dataset} onChange={e => setDataset(e.target.value)} spellCheck={false} /></label>
        <button className="primary" disabled={action.busy || !ready}>{action.busy ? "Evaluating…" : "Run evaluation →"}</button>
      </form>
    </div>
    <div className="panel padded">
      <div className="section-heading"><h2>Evaluation results</h2><button disabled={evals.loading} onClick={() => void evals.refresh()}>Refresh ↻</button></div>
      <ResourceStatus {...evals} />
      {result && <section className="run-section" aria-label="Latest result">
        <h3>Latest result</h3>
        <p className="muted">{describe(result, agentNames)}</p>
        <Outcome item={result} />
        <details className="raw"><summary>Raw result JSON</summary><Json value={result} /></details>
      </section>}
      {!evals.loading && !evals.rows.length && !result && <p className="muted">No evaluations yet. Run a dataset to establish your baseline.</p>}
      {evals.rows.map((item, i) => <details className="trace" key={String(item.id || i)}>
        <summary><strong>{describe(item, agentNames)}</strong><span className="badge">Score {score(item.score)}</span><StatusBadge status={item.status} /><small>{[time(item.created_at), String(item.id)].filter(Boolean).join(" · ")}</small></summary>
        <Outcome item={item} />
      </details>)}
    </div>
  </div>;
}
