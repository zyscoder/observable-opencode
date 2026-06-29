import type { ProvenanceTraceSummary, ProvenanceRecord, TraceArtifact } from "./case-trace"

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

function renderRecord(record: ProvenanceRecord, artifacts: Map<string, TraceArtifact>) {
  return `<article class="node node-${escapeHtml(record.event_type.replace(/[^a-z0-9_-]/gi, "-"))}">
    <div class="node-head">
      <span class="kind">${escapeHtml(record.event_type)}</span>
      <strong>${escapeHtml(record.title ?? record.record_id)}</strong>
      ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
      <span class="muted">${escapeHtml(record.record_id)}</span>
    </div>
    <div class="node-grid">
      <div>
        <div class="label">Data</div>
        <pre>${escapeHtml(preview(record.data, 900))}</pre>
      </div>
      <div>
        <div class="label">Source Refs</div>
        <div class="refs">${escapeHtml((record.source_refs ?? []).join(", ") || "-")}</div>
        <div class="label">Source Locations</div>
        <div class="refs source-locations">${sourceLocations(record)}</div>
        <div class="label">Artifacts</div>
        <div class="refs">${artifactLinks(record.artifact_refs, artifacts)}</div>
      </div>
    </div>
  </article>`
}

function renderDataflow(trace: ProvenanceTraceSummary) {
  if (!trace.dataflow_edges.length) return `<div class="empty">No dataflow edges.</div>`
  return `<table>
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
  </table>`
}

function renderTimeline(trace: ProvenanceTraceSummary, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) return `<div class="empty">No timeline records.</div>`
  return `<div class="timeline">
    ${records
      .map(
        (record) => `<div class="timeline-row">
          <div class="time">${escapeHtml(`${record.time_ms} ms`)}</div>
          ${renderRecord(record, artifacts)}
        </div>`,
      )
      .join("")}
  </div>`
}

function renderIo(trace: ProvenanceTraceSummary, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.filter((record) =>
    ["llm.call", "tool.call", "mcp.call", "skill.load", "subagent.call", "observation", "response.output"].includes(
      record.event_type,
    ),
  )
  if (!records.length) return `<div class="empty">No IO records.</div>`
  return `<div class="claims">
    ${records.map((record) => renderRecord(record, artifacts)).join("")}
  </div>`
}

function renderContext(trace: ProvenanceTraceSummary, artifacts: Map<string, TraceArtifact>) {
  const contexts = trace.records.filter((record) => record.event_type === "context.pack" || record.event_type === "context.compaction")
  if (!contexts.length) return `<div class="empty">No context or compaction nodes.</div>`
  return `<div class="context-list">
    ${contexts.map((record) => renderRecord(record, artifacts)).join("")}
  </div>`
}

function renderArtifacts(trace: ProvenanceTraceSummary) {
  if (!trace.artifacts.length) return `<div class="empty">No artifacts.</div>`
  return `<table>
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
  </table>`
}

export function renderProvenanceTraceHtml(trace: ProvenanceTraceSummary) {
  const artifacts = new Map(trace.artifacts.map((artifact) => [artifact.artifact_id, artifact]))
  const statusClass = trace.manifest.status === "success" ? "ok" : trace.manifest.status === "running" ? "running" : "bad"

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trace Provenance - ${escapeHtml(trace.manifest.case_id)}</title>
  <style>
    :root { color-scheme: light; --bg:#f6f7f9; --panel:#fff; --text:#17202a; --muted:#667085; --border:#d9dee7; --accent:#0f766e; --bad:#b42318; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.45; }
    header { padding: 24px 32px; background: var(--panel); border-bottom: 1px solid var(--border); }
    main { padding: 24px 32px 48px; }
    h1 { margin: 0; font-size: 24px; }
    h2 { margin: 0 0 14px; font-size: 18px; }
    h3 { margin: 14px 0 8px; font-size: 14px; }
    section { margin-bottom: 22px; padding: 18px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 8px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
    pre { max-height: 260px; overflow: auto; margin: 6px 0 0; padding: 10px; background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; white-space: pre-wrap; word-break: break-word; }
    .meta { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; color: var(--muted); font-size: 13px; }
    .status-pill { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); font-size: 12px; }
    .status-pill.ok { color: var(--accent); }
    .status-pill.bad { color: var(--bad); }
    .status-pill.running { color: #b45309; }
    .node { margin-bottom: 10px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: #fbfcfe; }
    .node-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .kind { color: var(--accent); font-size: 12px; font-weight: 700; }
    .status { color: var(--muted); font-size: 12px; }
    .muted, .label { color: var(--muted); font-size: 12px; }
    .node-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(220px, 1fr); gap: 12px; margin-top: 10px; }
    .timeline-row { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 12px; }
    .time { padding-top: 12px; color: var(--muted); font-size: 12px; text-align: right; }
    .refs { overflow-x: auto; white-space: nowrap; padding: 6px 0; }
    .source-locations { white-space: normal; display: grid; gap: 8px; }
    .source-location pre { max-height: 120px; white-space: pre; }
    .empty { color: var(--muted); font-size: 13px; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    a { color: #0369a1; }
    @media (max-width: 760px) { main, header { padding-left: 14px; padding-right: 14px; } .node-grid, .timeline-row { grid-template-columns: 1fr; } .time { text-align: left; } }
  </style>
</head>
<body>
  <header>
    <h1>Trace Provenance</h1>
    <div class="meta">
      <span>case: <code>${escapeHtml(trace.manifest.case_id)}</code></span>
      <span>run: <code>${escapeHtml(trace.manifest.run_id)}</code></span>
      <span class="status-pill ${statusClass}">${escapeHtml(trace.manifest.status)}</span>
      <span>${trace.metrics.records} records</span>
      <span>${trace.metrics.dataflow_edges} dataflow edges</span>
      <span>${trace.metrics.artifacts} artifacts</span>
    </div>
  </header>
  <main>
    <section id="component-dataflow">
      <h2>Component Dataflow</h2>
      ${renderDataflow(trace)}
    </section>
    <section id="timeline">
      <h2>Execution Timeline</h2>
      ${renderTimeline(trace, artifacts)}
    </section>
    <section id="io-inspector">
      <h2>IO Inspector</h2>
      ${renderIo(trace, artifacts)}
    </section>
    <section id="context-ledger">
      <h2>Context Ledger</h2>
      ${renderContext(trace, artifacts)}
    </section>
    <section id="artifacts">
      <h2>Artifacts</h2>
      ${renderArtifacts(trace)}
    </section>
  </main>
</body>
</html>`
}
