import type { TraceComponent, TraceFieldSummary, TraceSummary } from "./case-trace"

function escapeHtml(input: unknown) {
  return String(input ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;")
}

function jsonScript(input: unknown) {
  return JSON.stringify(input).replaceAll("<", "\\u003c").replaceAll(">", "\\u003e").replaceAll("&", "\\u0026")
}

function formatMs(input: number | undefined) {
  if (!Number.isFinite(input)) return "0 ms"
  const value = Math.max(0, input ?? 0)
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  return `${Math.round(value)} ms`
}

function tokenTotal(input: TraceSummary["token_usage"] | undefined) {
  if (!input) return 0
  if (input.total) return input.total
  return (
    (input.input ?? 0) +
    (input.output ?? 0) +
    (input.reasoning ?? 0) +
    (input.cached_input ?? 0) +
    (input.cache_write ?? 0)
  )
}

const componentOrder: TraceComponent[] = [
  "run",
  "runtime",
  "prompt",
  "context",
  "llm",
  "processor",
  "tool",
  "skill",
  "task",
  "mcp",
  "plugin",
  "result",
  "trace",
]

const componentLabel: Record<TraceComponent, string> = {
  run: "任务入口",
  runtime: "任务循环",
  prompt: "Prompt/上下文",
  context: "上下文管理",
  llm: "模型调用",
  processor: "结果处理",
  tool: "工具调用",
  skill: "Skill 执行",
  task: "子任务 Agent",
  mcp: "MCP 执行",
  plugin: "插件",
  result: "最终结果",
  trace: "Trace",
}

function componentRank(component: string) {
  const index = componentOrder.indexOf(component as TraceComponent)
  return index === -1 ? componentOrder.length : index
}

function componentClass(component: string) {
  return component.replace(/[^a-z0-9_-]/gi, "-")
}

function preview(input: unknown, limit = 180): string {
  if (input === undefined || input === null) return ""
  if (typeof input === "string") return input.slice(0, limit)
  if (typeof input === "number" || typeof input === "boolean") return String(input)
  const summary = input as Partial<TraceFieldSummary>
  if (typeof summary.preview === "string") return summary.preview.slice(0, limit)
  if (summary.value !== undefined) return String(summary.value).slice(0, limit)
  try {
    return JSON.stringify(input).slice(0, limit)
  } catch {
    return String(input).slice(0, limit)
  }
}

function collectComponents(trace: TraceSummary) {
  const components = new Set<string>()
  for (const span of trace.spans) components.add(span.component)
  for (const event of trace.events) components.add(event.component)
  return [...components].sort((a, b) => componentRank(a) - componentRank(b) || a.localeCompare(b))
}

function componentMetrics(trace: TraceSummary) {
  return collectComponents(trace).map((component) => {
    const spans = trace.spans.filter((span) => span.component === component)
    const events = trace.events.filter((event) => event.component === component)
    return {
      component,
      spans: spans.length,
      events: events.length,
      duration: spans.reduce((sum, span) => sum + (span.duration_ms ?? 0), 0),
      tokens: spans.reduce((sum, span) => sum + tokenTotal(span.token_usage), 0),
    }
  })
}

type ProcessItem = {
  time: number
  component: TraceComponent
  kind: "span" | "event"
  title: string
  status?: string
  duration?: number
  input?: unknown
  output?: unknown
  data?: unknown
}

function processItems(trace: TraceSummary) {
  const items: ProcessItem[] = [
    ...trace.spans.map((span) => ({
      time: span.start_ms,
      component: span.component,
      kind: "span" as const,
      title: span.name ?? span.operation,
      status: span.status,
      duration: span.duration_ms,
      input: span.input_summary,
      output: span.output_summary,
    })),
    ...trace.events.map((event) => ({
      time: event.time_ms,
      component: event.component,
      kind: "event" as const,
      title: event.event_type,
      data: event.data,
    })),
  ]
  return items.toSorted((a, b) => a.time - b.time)
}

type FlowEdge = {
  from: TraceComponent
  to: TraceComponent
  count: number
  samples: string[]
}

function addEdge(edges: Map<string, FlowEdge>, from: TraceComponent, to: TraceComponent, sample: string) {
  if (from === to) return
  const key = `${from}->${to}`
  const edge = edges.get(key) ?? { from, to, count: 0, samples: [] }
  edge.count += 1
  if (edge.samples.length < 3 && sample) edge.samples.push(sample)
  edges.set(key, edge)
}

function flowEdges(trace: TraceSummary) {
  const edges = new Map<string, FlowEdge>()
  const points = [
    ...trace.spans.map((span) => ({
      time: span.start_ms,
      component: span.component,
      label: `${span.name ?? span.operation}:start`,
    })),
    ...trace.events.map((event) => ({
      time: event.time_ms,
      component: event.component,
      label: event.event_type,
    })),
    ...trace.spans.map((span) => ({
      time: span.end_ms ?? span.start_ms,
      component: span.component,
      label: `${span.name ?? span.operation}:end`,
    })),
  ].toSorted((a, b) => a.time - b.time)

  let previous: (typeof points)[number] | undefined
  for (const point of points) {
    if (previous) addEdge(edges, previous.component, point.component, `${previous.label} -> ${point.label}`)
    previous = point
  }

  const spanByID = new Map(trace.spans.map((span) => [span.span_id, span]))
  for (const span of trace.spans) {
    if (!span.parent_span_id) continue
    const parent = spanByID.get(span.parent_span_id)
    if (parent)
      addEdge(
        edges,
        parent.component,
        span.component,
        `parent ${parent.name ?? parent.operation} -> ${span.name ?? span.operation}`,
      )
  }

  return [...edges.values()].sort((a, b) => b.count - a.count || componentRank(a.from) - componentRank(b.from))
}

function renderSummary(input: unknown) {
  const text = preview(input, 260)
  return text ? `<code>${escapeHtml(text)}</code>` : `<span class="muted">-</span>`
}

function renderIoCell(label: string, input: unknown) {
  return `<div class="io-cell">
    <div class="io-label">${escapeHtml(label)}</div>
    <div class="io-scroll">${renderSummary(input)}</div>
  </div>`
}

function renderAgentProcess(trace: TraceSummary) {
  const items = processItems(trace)
  if (!items.length) return `<div class="empty">没有流程事件。</div>`
  return `<div class="process-scroll"><div class="process">
    ${items
      .map(
        (item) => `<div class="step">
          <div class="step-time">${escapeHtml(formatMs(item.time))}</div>
          <div class="step-dot ${escapeHtml(componentClass(item.component))}"></div>
          <div class="step-body">
            <div class="step-head">
              <span class="pill ${escapeHtml(componentClass(item.component))}">${escapeHtml(componentLabel[item.component] ?? item.component)}</span>
              <strong>${escapeHtml(item.title)}</strong>
              ${item.status ? `<span class="status">${escapeHtml(item.status)}</span>` : ""}
              ${item.duration !== undefined ? `<span class="muted">${escapeHtml(formatMs(item.duration))}</span>` : ""}
            </div>
            <div class="step-io">
              ${item.input !== undefined ? renderIoCell("输入", item.input) : ""}
              ${item.output !== undefined ? renderIoCell("输出", item.output) : ""}
              ${item.data !== undefined ? renderIoCell("事件", item.data) : ""}
            </div>
          </div>
        </div>`,
      )
      .join("")}
  </div></div>`
}

function renderFlow(trace: TraceSummary) {
  const metrics = componentMetrics(trace)
  const edges = flowEdges(trace)
  if (!metrics.length) return `<div class="empty">没有组件流转数据。</div>`
  return `<div class="flow">
    <div class="flow-nodes">
      ${metrics
        .map(
          (item) => `<div class="flow-node ${escapeHtml(componentClass(item.component))}">
            <div class="node-title">${escapeHtml(componentLabel[item.component as TraceComponent] ?? item.component)}</div>
            <div class="node-sub"><code>${escapeHtml(item.component)}</code></div>
            <div class="node-metrics">
              <span>${item.spans} span</span>
              <span>${item.events} event</span>
              <span>${escapeHtml(formatMs(item.duration))}</span>
              ${item.tokens ? `<span>${item.tokens} token</span>` : ""}
            </div>
          </div>`,
        )
        .join("")}
    </div>
    <div class="flow-edges">
      <h3>跨组件流转</h3>
      ${
        edges.length
          ? edges
              .map(
                (edge) => `<div class="edge">
                  <span class="pill ${escapeHtml(componentClass(edge.from))}">${escapeHtml(edge.from)}</span>
                  <span class="arrow">→</span>
                  <span class="pill ${escapeHtml(componentClass(edge.to))}">${escapeHtml(edge.to)}</span>
                  <span class="edge-count">${edge.count} 次</span>
                  <span class="edge-sample">${escapeHtml(edge.samples.join("；"))}</span>
                </div>`,
              )
              .join("")
          : `<div class="empty">没有跨组件跳转。</div>`
      }
    </div>
  </div>`
}

export function renderCaseTraceHtml(trace: TraceSummary) {
  const spans = trace.spans.toSorted((a, b) => a.start_ms - b.start_ms)
  const maxEnd = Math.max(trace.duration_ms, ...spans.map((span) => span.end_ms ?? span.start_ms))
  const duration = Math.max(1, maxEnd)
  const tools = trace.spans.filter((span) => ["tool", "skill", "task", "mcp"].includes(span.component))
  const errors = trace.errors.length
    ? trace.errors
    : trace.spans.flatMap((span) =>
        span.error ? [{ ...span.error, span_id: span.span_id, component: span.component }] : [],
      )

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Opencode Case Trace - ${escapeHtml(trace.case_id)}</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --text: #18202a;
      --muted: #667085;
      --border: #d9dee7;
      --soft: #f9fafb;
      --run: #2563eb;
      --runtime: #0f766e;
      --prompt: #7c3aed;
      --context: #4f46e5;
      --llm: #be123c;
      --processor: #c2410c;
      --tool: #047857;
      --skill: #b45309;
      --task: #9333ea;
      --mcp: #0369a1;
      --plugin: #475467;
      --result: #15803d;
      --trace: #64748b;
      --other: #475467;
      --error: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }
    header {
      padding: 28px 32px 18px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }
    h1, h2, h3 { margin: 0; letter-spacing: 0; }
    h1 { font-size: 24px; }
    h2 { font-size: 16px; margin-bottom: 12px; }
    h3 { font-size: 13px; margin: 0 0 10px; color: #344054; }
    main { padding: 24px 32px 40px; }
    section {
      margin-bottom: 20px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
    }
    .meta {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
    }
    .cards, .flow-nodes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }
    .card, .flow-node {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfe;
    }
    .label, .muted, .node-sub { color: var(--muted); font-size: 12px; }
    .value { font-size: 18px; font-weight: 650; margin-top: 4px; word-break: break-word; }
    .node-title { font-size: 14px; font-weight: 700; }
    .node-metrics {
      margin-top: 10px;
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      color: #344054;
      font-size: 12px;
    }
    .flow-node { border-top: 4px solid var(--other); }
    .flow-node.run, .pill.run, .step-dot.run, .bar.run { border-color: var(--run); background-color: #eff6ff; }
    .flow-node.runtime, .pill.runtime, .step-dot.runtime, .bar.runtime { border-color: var(--runtime); background-color: #ecfdf5; }
    .flow-node.prompt, .pill.prompt, .step-dot.prompt, .bar.prompt { border-color: var(--prompt); background-color: #f5f3ff; }
    .flow-node.context, .pill.context, .step-dot.context, .bar.context { border-color: var(--context); background-color: #eef2ff; }
    .flow-node.llm, .pill.llm, .step-dot.llm, .bar.llm { border-color: var(--llm); background-color: #fff1f2; }
    .flow-node.processor, .pill.processor, .step-dot.processor, .bar.processor { border-color: var(--processor); background-color: #fff7ed; }
    .flow-node.tool, .pill.tool, .step-dot.tool, .bar.tool { border-color: var(--tool); background-color: #ecfdf5; }
    .flow-node.skill, .pill.skill, .step-dot.skill, .bar.skill { border-color: var(--skill); background-color: #fffbeb; }
    .flow-node.task, .pill.task, .step-dot.task, .bar.task { border-color: var(--task); background-color: #faf5ff; }
    .flow-node.mcp, .pill.mcp, .step-dot.mcp, .bar.mcp { border-color: var(--mcp); background-color: #f0f9ff; }
    .flow-node.plugin, .pill.plugin, .step-dot.plugin, .bar.plugin { border-color: var(--plugin); background-color: #f8fafc; }
    .flow-node.result, .pill.result, .step-dot.result, .bar.result { border-color: var(--result); background-color: #f0fdf4; }
    .flow-node.trace, .pill.trace, .step-dot.trace, .bar.trace { border-color: var(--trace); background-color: #f8fafc; }
    .flow-edges { margin-top: 14px; }
    .edge {
      display: grid;
      grid-template-columns: auto 18px auto 64px minmax(180px, 1fr);
      gap: 8px;
      align-items: center;
      padding: 8px 0;
      border-top: 1px solid #edf0f5;
      font-size: 12px;
    }
    .edge:first-of-type { border-top: 0; }
    .arrow { color: #98a2b3; font-weight: 700; text-align: center; }
    .edge-count { color: #344054; font-weight: 650; }
    .edge-sample { color: var(--muted); overflow-wrap: anywhere; }
    .process-scroll {
      max-height: min(78vh, 860px);
      overflow-y: auto;
      padding-right: 8px;
      overscroll-behavior: contain;
    }
    .process { position: relative; min-width: 0; }
    .step {
      display: grid;
      grid-template-columns: 72px 18px minmax(0, 1fr);
      gap: 10px;
      padding: 10px 0;
      border-top: 1px solid #edf0f5;
    }
    .step:first-child { border-top: 0; }
    .step-time { color: var(--muted); font-size: 12px; padding-top: 2px; text-align: right; }
    .step-dot {
      width: 12px;
      height: 12px;
      margin-top: 4px;
      border-radius: 999px;
      border: 3px solid var(--other);
      background: #ffffff;
    }
    .step-head {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      font-size: 13px;
    }
    .step-io {
      margin-top: 5px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      min-width: 0;
    }
    .io-cell {
      min-width: 0;
      border: 1px solid #edf0f5;
      border-radius: 6px;
      background: #fcfcfd;
      padding: 6px 8px;
    }
    .io-label {
      margin-bottom: 4px;
      color: #475467;
      font-size: 11px;
      font-weight: 650;
    }
    .io-scroll {
      max-width: 100%;
      overflow-x: auto;
      overflow-y: hidden;
      padding-bottom: 2px;
      white-space: nowrap;
    }
    .io-scroll code {
      display: inline-block;
      min-width: max-content;
      white-space: pre;
    }
    .timeline {
      position: relative;
      overflow-x: auto;
      padding-bottom: 6px;
    }
    .row {
      display: grid;
      grid-template-columns: 170px minmax(520px, 1fr) 86px;
      min-height: 30px;
      align-items: center;
      gap: 10px;
      border-top: 1px solid #edf0f5;
      font-size: 12px;
    }
    .row:first-child { border-top: 0; }
    .name {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: #344054;
    }
    .bar-wrap {
      position: relative;
      height: 16px;
      border-left: 1px solid #eef2f7;
      border-right: 1px solid #eef2f7;
    }
    .bar {
      position: absolute;
      top: 3px;
      height: 10px;
      min-width: 2px;
      border-radius: 3px;
      background: var(--other);
    }
    .bar.run { background: var(--run); }
    .bar.runtime { background: var(--runtime); }
    .bar.prompt { background: var(--prompt); }
    .bar.context { background: var(--context); }
    .bar.llm { background: var(--llm); }
    .bar.processor { background: var(--processor); }
    .bar.tool { background: var(--tool); }
    .bar.skill { background: var(--skill); }
    .bar.task { background: var(--task); }
    .bar.mcp { background: var(--mcp); }
    .bar.plugin { background: var(--plugin); }
    .bar.result { background: var(--result); }
    .bar.trace { background: var(--trace); }
    .bar.error { background: var(--error); }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      text-align: left;
      padding: 8px 10px;
      border-top: 1px solid #edf0f5;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      border-top: 0;
    }
    code, pre {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: #111827;
      color: #e5e7eb;
      border-radius: 8px;
      padding: 12px;
      max-height: 420px;
      overflow: auto;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 2px 8px;
      color: #344054;
      background: #ffffff;
      font-size: 12px;
      white-space: nowrap;
    }
    .status {
      border-radius: 999px;
      padding: 2px 7px;
      background: #f2f4f7;
      color: #344054;
      font-size: 12px;
    }
    .empty, .more { color: var(--muted); font-size: 13px; }
    details summary { cursor: pointer; color: #344054; font-weight: 650; }
    @media (max-width: 760px) {
      header, main { padding-left: 16px; padding-right: 16px; }
      .edge { grid-template-columns: 1fr; }
      .step { grid-template-columns: 54px 14px minmax(0, 1fr); }
      .row { grid-template-columns: 140px minmax(360px, 1fr) 76px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Opencode Case Trace</h1>
    <div class="meta">
      <span>case_id: <code>${escapeHtml(trace.case_id)}</code></span>
      <span>run_id: <code>${escapeHtml(trace.run_id)}</code></span>
      <span>status: <strong>${escapeHtml(trace.status)}</strong></span>
      <span>started_at: ${escapeHtml(trace.started_at)}</span>
    </div>
  </header>
  <main>
    <section>
      <div class="cards">
        <div class="card"><div class="label">总耗时</div><div class="value">${escapeHtml(formatMs(trace.duration_ms))}</div></div>
        <div class="card"><div class="label">Span 数</div><div class="value">${trace.spans.length}</div></div>
        <div class="card"><div class="label">Event 数</div><div class="value">${trace.events.length}</div></div>
        <div class="card"><div class="label">Token 总量</div><div class="value">${tokenTotal(trace.token_usage)}</div></div>
        <div class="card"><div class="label">错误数</div><div class="value">${errors.length}</div></div>
      </div>
    </section>

    <section>
      <h2>组件数据流转</h2>
      ${renderFlow(trace)}
    </section>

    <section>
      <h2>Agent 运行流程</h2>
      ${renderAgentProcess(trace)}
    </section>

    <section>
      <h2>组件时间线</h2>
      ${
        spans.length
          ? `<div class="timeline">
          ${spans
            .map((span) => {
              const left = Math.max(0, Math.min(100, (span.start_ms / duration) * 100))
              const width = Math.max(0.3, (((span.end_ms ?? span.start_ms) - span.start_ms) / duration) * 100)
              const cls = span.error || span.status === "error" ? "error" : componentClass(span.component)
              return `<div class="row" title="${escapeHtml(span.operation)}">
                <div class="name"><span class="pill ${escapeHtml(componentClass(span.component))}">${escapeHtml(span.component)}</span> ${escapeHtml(span.name ?? span.operation)}</div>
                <div class="bar-wrap"><div class="bar ${escapeHtml(cls)}" style="left:${left}%;width:${width}%"></div></div>
                <div>${escapeHtml(formatMs(span.duration_ms))}</div>
              </div>`
            })
            .join("")}
        </div>`
          : `<div class="empty">没有 span 数据。</div>`
      }
    </section>

    <section>
      <h2>Token 汇总</h2>
      <table>
        <thead><tr><th>input</th><th>output</th><th>reasoning</th><th>cached_input</th><th>cache_write</th><th>total</th><th>cost</th></tr></thead>
        <tbody>
          <tr>
            <td>${trace.token_usage.input ?? 0}</td>
            <td>${trace.token_usage.output ?? 0}</td>
            <td>${trace.token_usage.reasoning ?? 0}</td>
            <td>${trace.token_usage.cached_input ?? 0}</td>
            <td>${trace.token_usage.cache_write ?? 0}</td>
            <td>${tokenTotal(trace.token_usage)}</td>
            <td>${trace.token_usage.cost ?? 0}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section>
      <h2>工具与外部调用</h2>
      ${
        tools.length
          ? `<table>
        <thead><tr><th>组件</th><th>名称</th><th>状态</th><th>耗时</th><th>输入摘要</th><th>输出摘要</th></tr></thead>
        <tbody>
          ${tools
            .map(
              (span) => `<tr>
                <td>${escapeHtml(span.component)}</td>
                <td><code>${escapeHtml(span.name ?? span.operation)}</code></td>
                <td>${escapeHtml(span.status)}</td>
                <td>${escapeHtml(formatMs(span.duration_ms))}</td>
                <td>${renderSummary(span.input_summary ?? {})}</td>
                <td>${renderSummary(span.output_summary ?? {})}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>`
          : `<div class="empty">没有工具、skill、task 或 MCP 调用。</div>`
      }
    </section>

    <section>
      <h2>错误</h2>
      ${
        errors.length
          ? `<table>
        <thead><tr><th>组件</th><th>Span</th><th>Message</th></tr></thead>
        <tbody>
          ${errors
            .map(
              (error) => `<tr>
                <td>${escapeHtml(error.component ?? "")}</td>
                <td><code>${escapeHtml(error.span_id ?? "")}</code></td>
                <td>${escapeHtml(error.message ?? JSON.stringify(error))}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>`
          : `<div class="empty">没有错误。</div>`
      }
    </section>

    <section>
      <details>
        <summary>原始 Trace JSON</summary>
        <pre id="raw"></pre>
      </details>
    </section>
  </main>
  <script>
    const trace = ${jsonScript(trace)};
    document.getElementById("raw").textContent = JSON.stringify(trace, null, 2);
  </script>
</body>
</html>`
}
