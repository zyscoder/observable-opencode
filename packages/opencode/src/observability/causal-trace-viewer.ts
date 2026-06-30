import type { ProvenanceTraceSummary, ProvenanceRecord, TraceArtifact, TraceTokenUsage } from "./case-trace"

function escapeHtml(input: unknown) {
  return String(input ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;")
}

function preview(input: unknown, limit = 260): string {
  if (input === undefined || input === null) return ""
  if (typeof input === "string") return input.slice(0, limit)
  if (typeof input === "number" || typeof input === "boolean") return String(input)
  if (typeof input === "object") {
    const value = input as Record<string, unknown>
    if (typeof value.preview === "string") return value.preview.slice(0, limit)
    if (value.value !== undefined) return String(value.value).slice(0, limit)
  }
  try {
    return JSON.stringify(input).slice(0, limit)
  } catch {
    return String(input).slice(0, limit)
  }
}

function pretty(input: unknown, limit = 4200): string {
  if (input === undefined || input === null || input === "") return "-"
  if (typeof input === "string") return trimText(input, limit)
  if (typeof input === "number" || typeof input === "boolean") return String(input)
  try {
    const seen = new WeakSet<object>()
    const text = JSON.stringify(
      input,
      (_key, value) => {
        if (typeof value === "object" && value !== null) {
          if (seen.has(value)) return "[Circular]"
          seen.add(value)
        }
        return value
      },
      2,
    )
    return trimText(text, limit)
  } catch {
    return trimText(String(input), limit)
  }
}

function trimText(input: string, limit: number) {
  if (input.length <= limit) return input
  return `${input.slice(0, limit)}\n... [truncated ${input.length - limit} chars]`
}

function artifactLinks(ids: string[] | undefined, artifacts: Map<string, TraceArtifact>) {
  if (!ids?.length) return `<span class="muted">-</span>`
  return ids
    .map((id) => {
      const artifact = artifacts.get(id)
      if (!artifact) return `<code>${escapeHtml(id)}</code>`
      return `<a href="${escapeHtml(artifact.path)}"><code>${escapeHtml(id)}</code></a>`
    })
    .join(" ")
}

function sourceLocationLabel(location: Record<string, unknown>) {
  const target = typeof location.uri === "string" ? location.uri : typeof location.path === "string" ? location.path : ""
  const start = typeof location.line_start === "number" ? location.line_start : undefined
  const end = typeof location.line_end === "number" ? location.line_end : undefined
  if (!target) return ""
  if (start === undefined) return target
  return end && end !== start ? `${target}:${start}-${end}` : `${target}:${start}`
}

function sourceLocations(record: ProvenanceRecord) {
  if (!record.source_locations?.length) return `<span class="muted">-</span>`
  return record.source_locations
    .map((location) => {
      const target = location.uri ?? location.path ?? "-"
      const line =
        location.line_start === undefined
          ? ""
          : location.line_end && location.line_end !== location.line_start
            ? `:${location.line_start}-${location.line_end}`
            : `:${location.line_start}`
      return `<div class="source-location"><code>${escapeHtml(`${target}${line}`)}</code>${
        location.snippet_preview ? `<pre>${escapeHtml(location.snippet_preview)}</pre>` : ""
      }</div>`
    })
    .join("")
}

function typedResources(record: ProvenanceRecord) {
  const resources =
    record.typed_resources ??
    (record.data?.typed_resources && Array.isArray(record.data.typed_resources)
      ? (record.data.typed_resources as Record<string, unknown>[])
      : [])
  if (!resources.length) return `<span class="muted">-</span>`
  return resources
    .map((resource) => {
      const label = [resource.type, resource.key, resource.name, resource.uri].filter(Boolean).join(" ")
      const location =
        resource.source_location && typeof resource.source_location === "object"
          ? sourceLocationLabel(resource.source_location as Record<string, unknown>)
          : ""
      return `<div class="typed-resource">
        <code>${escapeHtml(label || JSON.stringify(resource))}</code>
        ${resource.fact ? `<div class="fact-text">${escapeHtml(resource.fact)}</div>` : ""}
        ${location ? `<div class="muted">${escapeHtml(location)}</div>` : ""}
      </div>`
    })
    .join("")
}

function formatDuration(ms: number | undefined) {
  if (ms === undefined) return "-"
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`
  return `${ms}ms`
}

function formatTokens(usage: TraceTokenUsage | undefined) {
  if (!usage) return "-"
  const parts = [
    usage.input === undefined ? "" : `in ${usage.input}`,
    usage.output === undefined ? "" : `out ${usage.output}`,
    usage.reasoning === undefined ? "" : `reasoning ${usage.reasoning}`,
    usage.cached_input === undefined ? "" : `cached ${usage.cached_input}`,
    usage.total === undefined ? "" : `total ${usage.total}`,
  ].filter(Boolean)
  return parts.length ? parts.join(" / ") : "-"
}

function recordLabel(record: ProvenanceRecord) {
  return record.title || record.data?.tool_name || record.data?.model_id || record.data?.response_role || record.record_id
}

function recordSummary(record: ProvenanceRecord) {
  const data = record.data ?? {}
  if (record.event_type === "loop.decision") {
    return [
      data.decision ? `decision=${String(data.decision)}` : "",
      data.reason ? `reason=${String(data.reason)}` : "",
      data.agent ? `agent=${String(data.agent)}` : "",
      data.part_count !== undefined ? `parts=${String(data.part_count)}` : "",
    ]
      .filter(Boolean)
      .join(" | ")
  }
  if (record.event_type === "response.output") {
    return [data.response_role ? `role=${String(data.response_role)}` : "", preview(data.text, 160)].filter(Boolean).join(" | ")
  }
  if (record.event_type === "llm.call") {
    return [data.provider_id, data.model_id, formatTokens(record.token_usage)].filter(Boolean).join(" | ")
  }
  if (record.event_type === "tool.call" || record.event_type === "mcp.call") {
    return [data.tool_name, data.server, data.name, preview(data.output, 120)].filter(Boolean).join(" | ")
  }
  if (record.event_type === "context.compaction") {
    return [data.algorithm, data.reason, data.before_tokens, data.after_tokens].filter((item) => item !== undefined).join(" | ")
  }
  return preview(data.summary ?? data.output ?? data.text ?? data, 180)
}

function recordInput(record: ProvenanceRecord) {
  const data = record.data ?? {}
  if (data.input !== undefined) return data.input
  if (data.prompt !== undefined) return { prompt: data.prompt }
  if (data.messages !== undefined) return { messages: data.messages }
  if (record.input_refs?.length) return { input_refs: record.input_refs }
  return undefined
}

function recordOutput(record: ProvenanceRecord) {
  const data = record.data ?? {}
  if (data.output !== undefined) return data.output
  if (data.result !== undefined) return data.result
  if (data.response !== undefined) return data.response
  if (data.text !== undefined) return data.text
  if (record.output_refs?.length) return { output_refs: record.output_refs }
  if (record.error !== undefined) return { error: record.error }
  return undefined
}

function hasSemanticFacts(record: ProvenanceRecord) {
  return Boolean(
    record.typed_resources?.length ||
      record.source_locations?.length ||
      record.source_refs?.length ||
      record.artifact_refs?.length ||
      ["observation", "change", "verification", "response.output", "loop.decision"].includes(record.event_type),
  )
}

function componentStats(trace: ProvenanceTraceSummary) {
  const stats = new Map<string, { records: number; duration: number; tokens: number }>()
  for (const record of trace.records) {
    const key = record.component ?? "unknown"
    const current = stats.get(key) ?? { records: 0, duration: 0, tokens: 0 }
    current.records += 1
    current.duration += record.duration_ms ?? 0
    current.tokens += record.token_usage?.total ?? 0
    stats.set(key, current)
  }
  return [...stats.entries()].sort((a, b) => b[1].records - a[1].records)
}

function renderOverview(trace: ProvenanceTraceSummary) {
  const statusClass = trace.manifest.status === "success" ? "ok" : trace.manifest.status === "running" ? "running" : "bad"
  return `<section id="overview">
    <div class="section-title">
      <h2>Overview</h2>
      <span class="status-pill ${statusClass}">${escapeHtml(trace.manifest.status)}</span>
    </div>
    <div class="overview-grid">
      <div><span class="label">Case</span><strong>${escapeHtml(trace.manifest.case_id)}</strong></div>
      <div><span class="label">Run</span><code>${escapeHtml(trace.manifest.run_id)}</code></div>
      <div><span class="label">Duration</span><strong>${formatDuration(trace.manifest.duration_ms)}</strong></div>
      <div><span class="label">Records</span><strong>${trace.metrics.records}</strong></div>
      <div><span class="label">Dataflow</span><strong>${trace.metrics.dataflow_edges}</strong></div>
      <div><span class="label">Artifacts</span><strong>${trace.metrics.artifacts}</strong></div>
      <div><span class="label">Tokens</span><strong>${escapeHtml(formatTokens(trace.metrics.token_usage))}</strong></div>
    </div>
    <div class="component-strip">
      ${componentStats(trace)
        .map(
          ([component, stat]) => `<div class="component-chip">
            <strong>${escapeHtml(component)}</strong>
            <span>${stat.records} records</span>
            <span>${formatDuration(stat.duration)}</span>
            <span>${stat.tokens ? `${stat.tokens} tokens` : "no token data"}</span>
          </div>`,
        )
        .join("")}
    </div>
  </section>`
}

function renderAgentFlow(trace: ProvenanceTraceSummary, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) return `<section id="agent-flow"><h2>Agent Flow</h2><div class="empty">No records.</div></section>`
  return `<section id="agent-flow">
    <div class="section-title">
      <h2>Agent Flow</h2>
      <span class="muted">All ${records.length} records are shown.</span>
    </div>
    <div class="flow-list">
      ${records
        .map(
          (record, index) => `<article class="flow-row">
            <div class="flow-index">
              <span>${index + 1}</span>
              <code>${escapeHtml(`${record.time_ms}ms`)}</code>
            </div>
            <div class="flow-main">
              <div class="flow-head">
                <span class="kind">${escapeHtml(record.event_type)}</span>
                ${record.component ? `<span class="component">${escapeHtml(record.component)}</span>` : ""}
                ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
                <strong>${escapeHtml(recordLabel(record))}</strong>
              </div>
              <div class="flow-summary">${escapeHtml(recordSummary(record))}</div>
              <div class="flow-meta">
                <span>id <code>${escapeHtml(record.record_id)}</code></span>
                ${record.span_id ? `<span>span <code>${escapeHtml(record.span_id)}</code></span>` : ""}
                <span>duration ${formatDuration(record.duration_ms)}</span>
                <span>tokens ${escapeHtml(formatTokens(record.token_usage))}</span>
                ${record.artifact_refs?.length ? `<span>artifacts ${artifactLinks(record.artifact_refs, artifacts)}</span>` : ""}
              </div>
            </div>
          </article>`,
        )
        .join("")}
    </div>
  </section>`
}

function renderDataflow(trace: ProvenanceTraceSummary) {
  if (!trace.dataflow_edges.length) return `<div class="empty">No dataflow edges.</div>`
  return `<div class="table-scroll"><table>
    <thead><tr><th>Relation</th><th>From</th><th>To</th><th>Label</th></tr></thead>
    <tbody>
      ${trace.dataflow_edges
        .map(
          (edge) => `<tr>
            <td>${escapeHtml(edge.relation)}</td>
            <td><code>${escapeHtml(edge.from.type)}:${escapeHtml(edge.from.id)}</code></td>
            <td><code>${escapeHtml(edge.to.type)}:${escapeHtml(edge.to.id)}</code></td>
            <td>${escapeHtml(edge.label ?? "")}</td>
          </tr>`,
        )
        .join("")}
    </tbody>
  </table></div>`
}

function renderIoInspector(trace: ProvenanceTraceSummary, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.filter((record) =>
    ["llm.call", "tool.call", "mcp.call", "skill.load", "subagent.call", "observation", "response.output"].includes(
      record.event_type,
    ),
  )
  if (!records.length) return `<section id="io-inspector"><h2>IO Inspector</h2><div class="empty">No IO records.</div></section>`
  return `<section id="io-inspector">
    <div class="section-title">
      <h2>IO Inspector</h2>
      <span class="muted">Inputs and outputs are scrollable so large payloads stay inspectable.</span>
    </div>
    <div class="io-list">
      ${records
        .map(
          (record) => `<article class="io-record">
            <div class="io-head">
              <span class="kind">${escapeHtml(record.event_type)}</span>
              ${record.component ? `<span class="component">${escapeHtml(record.component)}</span>` : ""}
              <strong>${escapeHtml(recordLabel(record))}</strong>
              ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
              <code>${escapeHtml(record.record_id)}</code>
            </div>
            <div class="io-grid">
              <div class="io-input">
                <div class="pane-title">Input</div>
                <pre>${escapeHtml(pretty(recordInput(record)))}</pre>
                <div class="refs">refs ${artifactLinks(record.input_refs, artifacts)}</div>
              </div>
              <div class="io-output">
                <div class="pane-title">Output</div>
                <pre>${escapeHtml(pretty(recordOutput(record)))}</pre>
                <div class="refs">refs ${artifactLinks(record.output_refs ?? record.artifact_refs, artifacts)}</div>
              </div>
            </div>
          </article>`,
        )
        .join("")}
    </div>
  </section>`
}

function renderSemanticFacts(trace: ProvenanceTraceSummary, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.filter(hasSemanticFacts)
  if (!records.length) return `<section id="semantic-facts"><h2>Semantic Facts</h2><div class="empty">No semantic facts.</div></section>`
  return `<section id="semantic-facts">
    <div class="section-title">
      <h2>Semantic Facts</h2>
      <span class="muted">Typed resources, source references, final response facts, and loop decisions.</span>
    </div>
    <div class="fact-list">
      ${records
        .map(
          (record) => `<article class="fact-card">
            <div class="fact-head">
              <span class="kind">${escapeHtml(record.event_type)}</span>
              <strong>${escapeHtml(recordLabel(record))}</strong>
              ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
              <code>${escapeHtml(record.record_id)}</code>
            </div>
            <div class="fact-grid">
              <div>
                <div class="label">Typed Resources</div>
                <div class="typed-resources">${typedResources(record)}</div>
              </div>
              <div>
                <div class="label">Source Locations</div>
                <div class="source-locations">${sourceLocations(record)}</div>
              </div>
              <div>
                <div class="label">Source Refs</div>
                <div class="refs">${escapeHtml((record.source_refs ?? []).join(", ") || "-")}</div>
              </div>
              <div>
                <div class="label">Artifacts</div>
                <div class="refs">${artifactLinks(record.artifact_refs, artifacts)}</div>
              </div>
            </div>
            <pre class="semantic-data">${escapeHtml(pretty(record.data, 1800))}</pre>
          </article>`,
        )
        .join("")}
    </div>
  </section>`
}

function renderContextAndCompaction(trace: ProvenanceTraceSummary, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.filter(
    (record) =>
      record.event_type === "context.pack" ||
      record.event_type === "context.compaction" ||
      record.component === "context",
  )
  if (!records.length)
    return `<section id="context-compaction"><h2>Context And Compaction</h2><div class="muted">Context Ledger</div><div class="empty">No context or compaction records.</div></section>`
  return `<section id="context-compaction">
    <div class="section-title">
      <h2>Context And Compaction</h2>
      <span class="muted">Context Ledger. Context construction, compaction inputs, outputs, and stored large payloads.</span>
    </div>
    <div class="context-list">
      ${records
        .map(
          (record) => `<article class="context-card">
            <div class="flow-head">
              <span class="kind">${escapeHtml(record.event_type)}</span>
              ${record.component ? `<span class="component">${escapeHtml(record.component)}</span>` : ""}
              <strong>${escapeHtml(recordLabel(record))}</strong>
              ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
            </div>
            <div class="flow-summary">${escapeHtml(recordSummary(record))}</div>
            <div class="io-grid">
              <div class="io-input">
                <div class="pane-title">Before / Input</div>
                <pre>${escapeHtml(pretty(recordInput(record)))}</pre>
                <div class="refs">refs ${artifactLinks(record.input_refs, artifacts)}</div>
              </div>
              <div class="io-output">
                <div class="pane-title">After / Output</div>
                <pre>${escapeHtml(pretty(recordOutput(record)))}</pre>
                <div class="refs">refs ${artifactLinks(record.output_refs ?? record.artifact_refs, artifacts)}</div>
              </div>
            </div>
          </article>`,
        )
        .join("")}
    </div>
  </section>`
}

function renderArtifacts(trace: ProvenanceTraceSummary) {
  if (!trace.artifacts.length) return `<div class="empty">No artifacts.</div>`
  return `<div class="table-scroll"><table>
    <thead><tr><th>Artifact</th><th>Label</th><th>Length</th><th>Occurrences</th><th>Path</th></tr></thead>
    <tbody>
      ${trace.artifacts
        .map(
          (artifact) => `<tr>
            <td><code>${escapeHtml(artifact.artifact_id)}</code></td>
            <td>${escapeHtml(artifact.label ?? "")}</td>
            <td>${artifact.length}</td>
            <td>${artifact.occurrences ?? 1}</td>
            <td><a href="${escapeHtml(artifact.path)}">${escapeHtml(artifact.path)}</a></td>
          </tr>`,
        )
        .join("")}
    </tbody>
  </table></div>`
}

export function renderProvenanceTraceHtml(trace: ProvenanceTraceSummary) {
  const artifacts = new Map(trace.artifacts.map((artifact) => [artifact.artifact_id, artifact]))

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trace v4.2 - ${escapeHtml(trace.manifest.case_id)}</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --panel-soft: #f9fafb;
      --text: #111827;
      --muted: #667085;
      --border: #d8dee8;
      --line: #e6eaf0;
      --accent: #0f766e;
      --accent-soft: #e6f4f1;
      --blue: #0b5cab;
      --amber: #a15c07;
      --bad: #b42318;
      --shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.45; }
    header { position: sticky; top: 0; z-index: 3; background: rgba(255, 255, 255, 0.96); border-bottom: 1px solid var(--border); backdrop-filter: blur(10px); }
    .header-inner { padding: 18px 28px 12px; }
    h1 { margin: 0; font-size: 24px; letter-spacing: 0; }
    h2 { margin: 0; font-size: 18px; letter-spacing: 0; }
    h3 { margin: 0; font-size: 14px; letter-spacing: 0; }
    main { padding: 22px 28px 48px; }
    nav { display: flex; gap: 8px; overflow-x: auto; margin-top: 14px; padding-bottom: 4px; }
    nav a { flex: 0 0 auto; color: var(--blue); text-decoration: none; border: 1px solid var(--border); border-radius: 999px; padding: 6px 10px; font-size: 12px; background: var(--panel-soft); }
    section { margin-bottom: 18px; padding: 18px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; box-shadow: var(--shadow); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 700; background: var(--panel-soft); }
    pre { overflow: auto; margin: 6px 0 0; padding: 10px; background: #0b1220; color: #dbeafe; border: 1px solid #1f2a44; border-radius: 6px; white-space: pre; word-break: normal; font-size: 12px; line-height: 1.5; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    a { color: var(--blue); }
    .meta { display: flex; flex-wrap: wrap; gap: 10px 14px; margin-top: 8px; color: var(--muted); font-size: 13px; }
    .section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
    .muted, .label { color: var(--muted); font-size: 12px; }
    .label { display: block; margin-bottom: 4px; font-weight: 700; text-transform: uppercase; }
    .status-pill, .status, .kind, .component { display: inline-flex; align-items: center; min-height: 22px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); font-size: 12px; font-weight: 700; background: #fff; }
    .status-pill.ok { color: var(--accent); background: var(--accent-soft); border-color: #a7d8d0; }
    .status-pill.bad { color: var(--bad); background: #fff1f0; border-color: #fecdca; }
    .status-pill.running { color: var(--amber); background: #fff7e6; border-color: #fedf89; }
    .kind { color: var(--accent); }
    .component { color: var(--blue); }
    .status { color: var(--muted); }
    .overview-grid { display: grid; grid-template-columns: repeat(7, minmax(120px, 1fr)); gap: 10px; }
    .overview-grid > div { min-width: 0; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-soft); }
    .overview-grid strong, .overview-grid code { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .component-strip { display: flex; gap: 10px; overflow-x: auto; margin-top: 12px; padding-bottom: 2px; }
    .component-chip { flex: 0 0 210px; display: grid; gap: 2px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; font-size: 12px; }
    .flow-list, .io-list, .fact-list, .context-list { display: grid; gap: 10px; }
    .flow-row { display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 12px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-soft); }
    .flow-index { display: grid; align-content: start; gap: 4px; color: var(--muted); }
    .flow-index span { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 50%; color: var(--accent); background: var(--accent-soft); font-weight: 800; }
    .flow-head, .io-head, .fact-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
    .flow-summary { margin-top: 6px; color: #344054; font-size: 13px; overflow-wrap: anywhere; }
    .flow-meta { display: flex; flex-wrap: wrap; gap: 8px 12px; margin-top: 8px; color: var(--muted); font-size: 12px; }
    .io-record, .fact-card, .context-card { padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-soft); }
    .io-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; margin-top: 10px; }
    .io-input, .io-output { min-width: 0; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
    .io-input pre, .io-output pre { max-height: 360px; overflow: auto; }
    .pane-title { color: var(--muted); font-size: 12px; font-weight: 800; text-transform: uppercase; }
    .refs { overflow-x: auto; white-space: nowrap; padding-top: 7px; color: var(--muted); font-size: 12px; }
    .fact-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
    .semantic-data { max-height: 220px; }
    .source-locations, .typed-resources { display: grid; gap: 6px; min-width: 0; }
    .source-location pre { max-height: 120px; background: #f8fafc; color: var(--text); border-color: var(--border); white-space: pre; }
    .typed-resource { padding: 8px; border: 1px solid var(--line); border-radius: 6px; background: #fff; }
    .fact-text { margin-top: 5px; color: #344054; font-size: 13px; }
    .table-scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
    .empty { color: var(--muted); font-size: 13px; }
    @media (max-width: 1100px) { .overview-grid { grid-template-columns: repeat(3, minmax(120px, 1fr)); } .fact-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { main, .header-inner { padding-left: 14px; padding-right: 14px; } .overview-grid, .flow-row, .io-grid, .fact-grid { grid-template-columns: 1fr; } header { position: static; } }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <h1>Trace v4.2</h1>
      <div class="muted">Trace Provenance</div>
      <div class="meta">
        <span>case <code>${escapeHtml(trace.manifest.case_id)}</code></span>
        <span>run <code>${escapeHtml(trace.manifest.run_id)}</code></span>
        <span>${escapeHtml(trace.manifest.started_at)}</span>
      </div>
      <nav>
        <a href="#overview">Overview</a>
        <a href="#agent-flow">Agent Flow</a>
        <a href="#component-dataflow">Component Dataflow</a>
        <a href="#io-inspector">IO Inspector</a>
        <a href="#semantic-facts">Semantic Facts</a>
        <a href="#context-compaction">Context And Compaction</a>
        <a href="#artifacts">Artifacts</a>
      </nav>
    </div>
  </header>
  <main>
    ${renderOverview(trace)}
    ${renderAgentFlow(trace, artifacts)}
    <section id="component-dataflow">
      <h2>Component Dataflow</h2>
      ${renderDataflow(trace)}
    </section>
    ${renderIoInspector(trace, artifacts)}
    ${renderSemanticFacts(trace, artifacts)}
    ${renderContextAndCompaction(trace, artifacts)}
    <section id="artifacts">
      <h2>Artifacts</h2>
      ${renderArtifacts(trace)}
    </section>
  </main>
</body>
</html>`
}
