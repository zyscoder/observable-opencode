import type { TraceSummary } from "./case-trace"

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

function tokenTotal(input: TraceSummary["token_usage"]) {
  return (
    (input.input ?? 0) +
    (input.output ?? 0) +
    (input.reasoning ?? 0) +
    (input.cached_input ?? 0) +
    (input.cache_write ?? 0)
  )
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
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #18202a;
      --muted: #667085;
      --border: #d9dee7;
      --run: #2563eb;
      --prompt: #0f766e;
      --llm: #7c3aed;
      --processor: #c2410c;
      --tool: #047857;
      --skill: #b45309;
      --task: #be123c;
      --mcp: #0369a1;
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
    h1, h2 { margin: 0; letter-spacing: 0; }
    h1 { font-size: 24px; }
    h2 { font-size: 16px; margin-bottom: 12px; }
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
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }
    .card {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfe;
    }
    .label { color: var(--muted); font-size: 12px; }
    .value { font-size: 18px; font-weight: 650; margin-top: 4px; word-break: break-word; }
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
    .bar.prompt { background: var(--prompt); }
    .bar.llm { background: var(--llm); }
    .bar.processor { background: var(--processor); }
    .bar.tool { background: var(--tool); }
    .bar.skill { background: var(--skill); }
    .bar.task { background: var(--task); }
    .bar.mcp { background: var(--mcp); }
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
      background: #0f172a;
      color: #e2e8f0;
      border-radius: 8px;
      padding: 12px;
      max-height: 360px;
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
    }
    .empty { color: var(--muted); font-size: 13px; }
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
      <h2>组件时间线</h2>
      ${
        spans.length
          ? `<div class="timeline">
          ${spans
            .map((span) => {
              const left = Math.max(0, Math.min(100, (span.start_ms / duration) * 100))
              const width = Math.max(0.3, (((span.end_ms ?? span.start_ms) - span.start_ms) / duration) * 100)
              const cls = span.error || span.status === "error" ? "error" : span.component
              return `<div class="row" title="${escapeHtml(span.operation)}">
                <div class="name"><span class="pill">${escapeHtml(span.component)}</span> ${escapeHtml(span.name ?? span.operation)}</div>
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
        <thead><tr><th>input</th><th>output</th><th>reasoning</th><th>cached_input</th><th>cache_write</th><th>cost</th></tr></thead>
        <tbody>
          <tr>
            <td>${trace.token_usage.input ?? 0}</td>
            <td>${trace.token_usage.output ?? 0}</td>
            <td>${trace.token_usage.reasoning ?? 0}</td>
            <td>${trace.token_usage.cached_input ?? 0}</td>
            <td>${trace.token_usage.cache_write ?? 0}</td>
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
                <td><code>${escapeHtml(JSON.stringify(span.input_summary ?? {}))}</code></td>
                <td><code>${escapeHtml(JSON.stringify(span.output_summary ?? {}))}</code></td>
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
      <h2>原始 Trace JSON</h2>
      <pre id="raw"></pre>
    </section>
  </main>
  <script>
    const trace = ${jsonScript(trace)};
    document.getElementById("raw").textContent = JSON.stringify(trace, null, 2);
  </script>
</body>
</html>`
}
