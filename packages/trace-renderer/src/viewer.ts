import type { ProvenanceTraceView, ProvenanceRecord, TraceArtifact, TraceTokenUsage } from "opencode/observability/case-trace"
import { TRACE_VERSION } from "opencode/observability/trace-semantic-contract"

export type ProvenanceTraceHtmlChunkOptions = {
  maxChunkBytes?: number
  artifactSnapshotPaths?: ReadonlyMap<string, string>
}

const artifactPathPolicies = new WeakMap<Map<string, TraceArtifact>, ReadonlyMap<string, string>>()

export const VIEWER_STRING_BUDGET = 8192

export function escapeViewerHtml(input: unknown, limit = VIEWER_STRING_BUDGET) {
  return boundedViewerText(input, limit)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;")
}

const escapeHtml = escapeViewerHtml

function preview(input: unknown, limit = 260): string {
  if (input === undefined || input === null) return ""
  if (typeof input === "string") return input.slice(0, limit)
  if (typeof input === "number" || typeof input === "boolean") return String(input)
  if (typeof input === "object") {
    const value = input as Record<string, unknown>
    if (typeof value.preview === "string") return value.preview.slice(0, limit)
    if (value.value !== undefined) return boundedViewerText(value.value, limit)
  }
  return pretty(input, limit).replaceAll("\n", " ").slice(0, limit)
}

function pretty(input: unknown, limit = 4200): string {
  if (input === undefined || input === null || input === "") return "-"
  if (typeof input === "string") return trimText(input, limit)
  if (typeof input === "number" || typeof input === "boolean") return String(input)
  const budget = Math.max(32, Math.floor(limit))
  const parts: string[] = []
  let length = 0
  let truncated = false
  const stack = new WeakSet<object>()
  const append = (value: string) => {
    if (!value || length >= budget) {
      if (value) truncated = true
      return
    }
    const available = budget - length
    if (value.length > available) {
      parts.push(value.slice(0, available))
      length = budget
      truncated = true
      return
    }
    parts.push(value)
    length += value.length
  }
  const quoted = (value: string) => {
    append('"')
    for (let index = 0; index < value.length && length < budget - 1; index++) {
      const code = value.charCodeAt(index)
      if (code === 0x22) append('\\"')
      else if (code === 0x5c) append("\\\\")
      else if (code === 0x08) append("\\b")
      else if (code === 0x0c) append("\\f")
      else if (code === 0x0a) append("\\n")
      else if (code === 0x0d) append("\\r")
      else if (code === 0x09) append("\\t")
      else if (code < 0x20) append(`\\u${code.toString(16).padStart(4, "0")}`)
      else if (code >= 0xd800 && code <= 0xdbff) {
        const next = value.charCodeAt(index + 1)
        if (next >= 0xdc00 && next <= 0xdfff) {
          append(value.slice(index, index + 2))
          index += 1
        } else {
          append(`\\u${code.toString(16).padStart(4, "0")}`)
        }
      } else if (code >= 0xdc00 && code <= 0xdfff) {
        append(`\\u${code.toString(16).padStart(4, "0")}`)
      } else append(value[index]!)
      if (length >= budget) truncated = true
    }
    if (length < budget) append('"')
    if (length >= budget || value.length > Math.max(0, budget - length)) truncated = true
  }
  const visit = (value: unknown, depth: number) => {
    if (length >= budget) {
      truncated = true
      return
    }
    if (value === null) return append("null")
    if (value === undefined) return append("undefined")
    if (typeof value === "string") return quoted(value)
    if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint")
      return append(String(value))
    if (typeof value === "function") return append(`[Function ${value.name || "anonymous"}]`)
    if (typeof value === "symbol") return append(String(value))
    if (depth >= 12) {
      truncated = true
      return append("[Max depth]")
    }
    if (stack.has(value)) return append("[Circular]")
    stack.add(value)
    try {
      if (Array.isArray(value)) {
        append("[")
        for (let index = 0; index < value.length && length < budget; index++) {
          if (index) append(", ")
          visit(value[index], depth + 1)
        }
        if (length < budget) append("]")
        else if (value.length) truncated = true
        return
      }
      append("{")
      let first = true
      for (const key in value as Record<string, unknown>) {
        if (!Object.prototype.hasOwnProperty.call(value, key)) continue
        if (length >= budget) {
          truncated = true
          break
        }
        if (!first) append(", ")
        first = false
        quoted(key)
        append(": ")
        try {
          visit((value as Record<string, unknown>)[key], depth + 1)
        } catch {
          append("[Unavailable]")
        }
      }
      if (length < budget) append("}")
    } finally {
      stack.delete(value)
    }
  }
  visit(input, 0)
  const text = parts.join("")
  return truncated ? `${text}\n... [truncated by preview budget]` : text
}

function trimText(input: string, limit: number) {
  if (input.length <= limit) return input
  return `${input.slice(0, limit)}\n... [truncated ${input.length - limit} chars]`
}

export function boundedViewerText(input: unknown, limit = VIEWER_STRING_BUDGET): string {
  const budget = Math.max(1, Math.floor(limit))
  if (input === undefined || input === null) return ""
  if (typeof input === "string") return trimText(input, budget)
  if (typeof input === "number" || typeof input === "boolean" || typeof input === "bigint") return String(input)
  return pretty(input, budget)
}

export function boundedViewerJoin(
  values: readonly unknown[],
  separator = ", ",
  limit = VIEWER_STRING_BUDGET,
): string {
  const budget = Math.max(1, Math.floor(limit))
  const parts: string[] = []
  let length = 0
  for (const value of values) {
    if (value === undefined || value === null || value === "") continue
    const prefix = parts.length ? separator : ""
    const available = budget - length - prefix.length
    if (available <= 0) break
    const part = boundedViewerText(value, available)
    parts.push(prefix + part)
    length += prefix.length + part.length
    if (length >= budget || part.includes("... [truncated")) break
  }
  return parts.join("")
}

export function safeArtifactRelativePath(input: unknown) {
  if (
    typeof input !== "string" ||
    !input ||
    input.length > VIEWER_STRING_BUDGET ||
    input.includes("\0")
  )
    return undefined
  const candidate = input.replaceAll("\\", "/")
  if (candidate.startsWith("/") || /^[a-zA-Z]:\//.test(candidate)) return undefined
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(candidate)) return undefined
  if (/%(?:2e|2f|5c)/i.test(candidate)) return undefined
  const segments = candidate.split("/")
  if (segments.some((segment) => !segment || segment === "." || segment === "..")) return undefined
  if (segments[0] !== "artifacts" || segments.length < 2) return undefined
  return segments.join("/")
}

function artifactPathMarkup(artifact: TraceArtifact, artifacts: Map<string, TraceArtifact>) {
  const safePath = safeArtifactRelativePath(artifact.path)
  const snapshotPath = safePath ? artifactPathPolicies.get(artifacts)?.get(safePath) : undefined
  const id = boundedViewerText(artifact.artifact_id, 520)
  if (!snapshotPath) {
    return `<span class="muted" title="Artifact unavailable">Artifact unavailable: <code>${escapeHtml(id)}</code></span>`
  }
  return `<a href="${escapeHtml(snapshotPath)}"><code>${escapeHtml(id)}</code></a>`
}

function artifactLinks(ids: string[] | undefined, artifacts: Map<string, TraceArtifact>) {
  if (!ids?.length) return `<span class="muted">-</span>`
  return ids
    .map((id) => {
      const boundedID = boundedViewerText(id, 520)
      const artifact = artifacts.get(boundedID)
      if (!artifact) return `<code>${escapeHtml(boundedID)}</code>`
      return artifactPathMarkup(artifact, artifacts)
    })
    .join(" ")
}

function sourceLocationLabel(location: Record<string, unknown>) {
  const target = boundedViewerText(
    typeof location.uri === "string" ? location.uri : typeof location.path === "string" ? location.path : "",
    640,
  )
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
      const target = boundedViewerText(location.uri ?? location.path ?? "-", 640)
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
      const label = boundedViewerJoin([resource.type, resource.key, resource.name, resource.uri], " ", 520)
      const location =
        resource.source_location && typeof resource.source_location === "object"
          ? sourceLocationLabel(resource.source_location as Record<string, unknown>)
          : ""
      return `<div class="typed-resource">
        <code>${escapeHtml(label || pretty(resource, 520))}</code>
        ${resource.fact ? `<div class="fact-text">${escapeHtml(resource.fact)}</div>` : ""}
        ${location ? `<div class="muted">${escapeHtml(location)}</div>` : ""}
      </div>`
    })
    .join("")
}

function structuredClaim(record: ProvenanceRecord) {
  const claim = record.data?.structured_claim
  if (!claim || typeof claim !== "object" || Array.isArray(claim)) return `<span class="muted">-</span>`
  const value = claim as Record<string, unknown>
  const sourceSpan =
    value.source_span && typeof value.source_span === "object" && !Array.isArray(value.source_span)
      ? sourceLocationLabel(value.source_span as Record<string, unknown>)
      : ""
  return `<div class="structured-claim">
    <div><span class="label">Subject</span><strong>${escapeHtml(value.subject ?? "-")}</strong></div>
    <div><span class="label">Predicate</span><strong>${escapeHtml(value.predicate ?? "-")}</strong></div>
    <div><span class="label">Value</span><code>${escapeHtml(value.value ?? "-")}</code></div>
    <div><span class="label">Method</span><code>${escapeHtml(value.extraction_method ?? "-")}</code></div>
    ${sourceSpan ? `<div class="claim-span"><span class="label">Source Span</span><code>${escapeHtml(sourceSpan)}</code></div>` : ""}
  </div>`
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
  return boundedViewerText(
    record.title || record.data?.tool_name || record.data?.model_id || record.data?.response_role || record.record_id,
    520,
  )
}

function summaryField(prefix: string, value: unknown, limit = 160) {
  if (value === undefined || value === null || value === "") return ""
  return prefix + boundedViewerText(value, Math.max(1, limit - prefix.length))
}

function summaryParts(values: readonly unknown[]) {
  return boundedViewerJoin(values, " | ", 520)
}

function recordSummary(record: ProvenanceRecord) {
  const data = record.data ?? {}
  if (record.event_type === "decision") {
    return summaryParts([
      summaryField("type=", data.decision_type),
      summaryField("intent=", data.intent),
      summaryField("action=", data.chosen_action),
      preview(data.rationale, 160),
    ])
  }
  if (record.event_type === "prompt.assembly") {
    return summaryParts([summaryField("stage=", data.stage), preview(data.output ?? data.parts ?? data.input, 180)])
  }
  if (record.event_type === "context.transform") {
    return summaryParts([
      summaryField("stage=", data.stage),
      summaryField("agent=", data.agent),
      summaryField("model=", data.model_id),
      preview(data.transforms, 160),
    ])
  }
  if (record.event_type === "loop.decision") {
    return summaryParts([
      summaryField("decision=", data.decision),
      summaryField("reason=", data.reason),
      summaryField("agent=", data.agent),
      summaryField("parts=", data.part_count),
    ])
  }
  if (record.event_type === "response.output") {
    return summaryParts([summaryField("role=", data.response_role), preview(data.text, 160)])
  }
  if (record.event_type === "response.claim") {
    return summaryParts([
      summaryField("format=", data.claim_format),
      summaryField("support=", data.support_level),
      Array.isArray(data.quality_flags)
        ? summaryField("flags=", boundedViewerJoin(data.quality_flags, ",", 160))
        : "",
      preview(data.text, 160),
    ])
  }
  if (record.event_type === "claim.support_assessment") {
    return summaryParts([
      summaryField("support=", data.support_level),
      Array.isArray(data.quality_flags)
        ? summaryField("flags=", boundedViewerJoin(data.quality_flags, ",", 160))
        : "",
      Array.isArray(data.tool_failure_dependency_refs) && data.tool_failure_dependency_refs.length
        ? summaryField("tool_failures=", boundedViewerJoin(data.tool_failure_dependency_refs, ",", 160))
        : "",
      summaryField("match=", data.match_score),
    ])
  }
  if (record.event_type === "llm.call") {
    return summaryParts([data.provider_id, data.model_id, formatTokens(record.token_usage)])
  }
  if (record.event_type === "llm.turn") {
    return summaryParts([
      summaryField("role=", data.agent_role),
      summaryField("agent=", data.agent),
      data.provider_id && data.model_id ? boundedViewerJoin([data.provider_id, data.model_id], "/", 160) : "",
      summaryField("finish=", data.finish_reason),
      formatTokens(record.token_usage),
    ])
  }
  if (record.event_type === "agent.lifecycle") {
    return summaryParts([summaryField("phase=", data.phase), preview(data.summary, 180)])
  }
  if (record.event_type === "exit.gate") {
    return summaryParts([
      summaryField("decision=", data.decision),
      summaryField("reason=", data.reason),
      summaryField("source=", data.continuation_source),
      summaryField("compact=", data.needs_compaction),
    ])
  }
  if (record.event_type === "evidence.semantic_fact" || record.event_type === "evidence.fact") {
    return summaryParts([data.source, data.category, preview(data.summary, 160)])
  }
  if (record.event_type === "execution.observation") {
    return summaryParts([data.source, data.category, preview(data.summary, 160)])
  }
  if (record.event_type === "task.plan_state") {
    return summaryParts([data.source, data.category, preview(data.plan_items, 160)])
  }
  if (record.event_type === "tool.call" || record.event_type === "tool.result" || record.event_type === "mcp.call") {
    return summaryParts([data.tool_name, data.server, data.name, data.request_status, preview(data.output, 120)])
  }
  if (record.event_type === "tool.error") {
    return summaryParts([data.tool_name, data.error_kind, preview(data.error_message ?? data.error, 160)])
  }
  if (record.event_type === "context.compaction") {
    return summaryParts([
      data.algorithm,
      data.reason,
      summaryField("before=", data.token_estimate_before),
      summaryField("after=", data.token_estimate_after),
      summaryField("retention=", data.retention_ratio),
      Array.isArray(data.compression_loss_risks) && data.compression_loss_risks.length
        ? summaryField("risks=", boundedViewerJoin(data.compression_loss_risks, ",", 160))
        : "",
    ])
  }
  if (record.event_type === "context.compaction_check") {
    return summaryParts([
      summaryField("reason=", data.trigger_reason),
      summaryField("overflow=", data.overflow),
      summaryField("tokens=", data.token_estimate),
      summaryField("limit=", data.context_limit),
      summaryField("algorithm=", data.selected_algorithm),
    ])
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
      [
        "prompt.assembly",
        "case.completed",
        "case.failed",
        "context.transform",
        "context.pack",
        "context.compaction",
        "decision",
        "llm.turn",
        "agent.lifecycle",
        "exit.gate",
        "evidence.semantic_fact",
        "evidence.fact",
        "execution.observation",
        "task.plan_state",
        "change",
        "verification",
        "response.output",
        "loop.decision",
      ].includes(record.event_type),
  )
}

function componentStats(trace: ProvenanceTraceView) {
  const stats = new Map<string, { records: number; duration: number; tokens: number }>()
  for (const record of trace.records) {
    const key = boundedViewerText(record.component ?? "unknown", 80)
    const current = stats.get(key) ?? { records: 0, duration: 0, tokens: 0 }
    current.records += 1
    current.duration += record.duration_ms ?? 0
    current.tokens += record.token_usage?.total ?? 0
    stats.set(key, current)
  }
  return [...stats.entries()].sort((a, b) => b[1].records - a[1].records)
}

function renderOverview(trace: ProvenanceTraceView) {
  const caseStatus = trace.manifest.case_status ?? trace.manifest.status
  const statusClass = caseStatus === "success" ? "ok" : caseStatus === "running" ? "running" : "bad"
  const recoveryStatus = (trace.manifest as { recovery_status?: string }).recovery_status
  return `<section id="overview">
    <div class="section-title">
      <h2>Overview</h2>
      <span class="status-pill ${statusClass}">case ${escapeHtml(caseStatus)}</span>
    </div>
    ${
      recoveryStatus
        ? `<div class="recovery-banner"><strong>Incomplete journal recovery</strong><span>${escapeHtml(recoveryStatus)}</span></div>`
        : ""
    }
    <div class="overview-grid">
      <div><span class="label">Case</span><strong>${escapeHtml(trace.manifest.case_id)}</strong></div>
      <div><span class="label">Run</span><code>${escapeHtml(trace.manifest.run_id)}</code></div>
      <div><span class="label">Duration</span><strong>${formatDuration(trace.manifest.duration_ms)}</strong></div>
      <div><span class="label">Server Status</span><strong>${escapeHtml(trace.manifest.server_status ?? trace.manifest.status)}</strong></div>
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

function* renderTraceHealth(trace: ProvenanceTraceView) {
  const health = trace.metrics.trace_health
  const compactionFlags = Object.entries(health.compaction_quality_flags ?? {})
  const issueSeverity = health.issues.some((issue) => issue.severity === "error")
    ? "bad"
    : health.issues.some((issue) => issue.severity === "warning")
      ? "running"
      : "ok"
  const healthCards = [
    ["Circular Markers", health.circular_reference_markers, "Path-cycle markers left in structured JSON."],
    ["Open Records", health.open_records, "Records still running after finalization."],
    ["Finalized Open", health.finalized_open_records, "Open records closed by trace finalizer."],
    [
      "Expected Finalized",
      health.expected_lifecycle_finalized_records ?? 0,
      "Lifecycle records finalized by an expected close policy.",
    ],
    [
      "Unexpected Missing Close",
      health.unexpected_missing_close_records ?? 0,
      "Finalized records without an expected lifecycle close policy.",
    ],
    ["LLM Missing Tokens", health.llm_turns_missing_token_usage, "LLM turns without token usage."],
    ["LLM Missing Finish", health.llm_turns_missing_finish_reason, "LLM turns without finish reason."],
    ["Background LLM", health.background_llm_turns ?? 0, "Title/background model turns shown as secondary flow."],
    ["Empty Subagent", health.empty_subagent_results, "Subagent/task facts with empty returned result."],
    [
      "Broad Responses",
      health.broad_response_refs,
      "Responses whose legacy source_refs are wider than direct evidence.",
    ],
    [
      "Duplicate Semantic Evidence",
      health.duplicate_semantic_facts ?? health.duplicate_evidence_facts,
      "Repeated semantic facts after canonicalization.",
    ],
    [
      "Generic Semantic Evidence",
      health.generic_semantic_facts ?? health.generic_evidence_facts ?? 0,
      "Semantic evidence facts that fell back to generic claims.",
    ],
    [
      "Execution Observations",
      health.execution_observations ?? 0,
      "Routine execution observations kept out of evidence.",
    ],
    ["Plan States", health.task_plan_states ?? 0, "Todo and task-plan states kept out of evidence."],
    ["Generic MCP", health.generic_mcp_facts ?? 0, "MCP facts that still fell back to generic claims."],
    ["Path Only Facts", health.path_only_evidence_facts ?? 0, "File facts that did not reach line-level semantics."],
    ["Unsupported Claims", health.unsupported_response_claims ?? 0, "Response claims without any provenance refs."],
    [
      "Context Only Claims",
      health.context_only_response_claims ?? 0,
      "Response claims supported only by context refs.",
    ],
    ["Broken Claims", health.broken_claim_fragments ?? 0, "Claim fragments produced by sentence splitting."],
    [
      "Legacy Claim Refs",
      health.legacy_context_ref_claims ?? 0,
      "Claims that kept wider source refs as legacy context after direct evidence matching.",
    ],
    ["Non-final Claims", health.non_final_response_claims ?? 0, "Claims attached to non-final response segments."],
    ["Weak Matches", health.weak_evidence_matches ?? 0, "Claims supported only by weak evidence overlap."],
    ["MCP Shadowed", health.mcp_json_parse_shadowed ?? 0, "MCP JSON facts shadowed by weaker parsers."],
    ["Skill Unresolved", health.skill_request_unresolved ?? 0, "Requested skills not observed as loaded."],
    ["Payload Dup Groups", health.payload_duplication_groups ?? 0, "Artifact payloads reused multiple times."],
    ["Missing Compaction Check", health.compaction_check_missing ?? 0, "Compactions without a check record."],
  ] as const

  yield `<section id="trace-health">
    <div class="section-title">
      <h2>Trace Health</h2>
      <span class="status-pill ${issueSeverity}">${health.issues.length} issues</span>
    </div>
    <div class="health-grid">
      ${healthCards
        .map(
          ([label, value, hint]) => `<div class="health-card">
            <span class="label">${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
            <span class="muted">${escapeHtml(hint)}</span>
          </div>`,
        )
        .join("")}
    </div>
    <div class="health-details">
      <div>
        <h3>Compaction Quality Flags</h3>`
  if (compactionFlags.length) {
    yield `<div class="flag-list">`
    for (const [flag, count] of compactionFlags) {
      yield `<span class="flag"><code>${escapeHtml(flag)}</code><strong>${escapeHtml(count)}</strong></span>`
    }
    yield `</div>`
  } else {
    yield `<div class="empty">No compaction quality flags.</div>`
  }
  yield `</div>
      <div>
        <h3>Quality Issues</h3>`
  if (health.issues.length) {
    yield `<div class="issue-list">`
    for (const issue of health.issues) {
      yield `<article class="issue-row">
                    <div>
                      <span class="severity ${escapeHtml(issue.severity)}">${escapeHtml(issue.severity)}</span>
                      <strong>${escapeHtml(issue.kind)}</strong>
                    </div>
                    <div class="flow-summary">${escapeHtml(issue.message)}</div>
                    <div class="flow-meta">
                      ${issue.record_id ? `<span>record <code>${escapeHtml(issue.record_id)}</code></span>` : ""}
                      ${issue.event_type ? `<span>event <code>${escapeHtml(issue.event_type)}</code></span>` : ""}
                      ${issue.count !== undefined ? `<span>count ${escapeHtml(issue.count)}</span>` : ""}
                    </div>
                  </article>`
    }
    yield `</div>`
  } else {
    yield `<div class="empty">No trace health issues.</div>`
  }
  yield `
      </div>
    </div>
  </section>`
}

function* renderAgentFlow(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) {
    yield `<section id="agent-flow"><h2>Agent Flow</h2><div class="empty">No records.</div></section>`
    return
  }
  yield `<section id="agent-flow">
    <div class="section-title">
      <h2>Agent Flow</h2>
      <span class="muted">All ${records.length} records are shown.</span>
    </div>
    <div class="flow-list">`
  for (const [index, record] of records.entries()) {
    yield `<article class="flow-row">
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
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderDataflow(trace: ProvenanceTraceView) {
  if (!trace.dataflow_edges.length) {
    yield `<div class="empty">No dataflow edges.</div>`
    return
  }
  yield `<div class="table-scroll"><table>
    <thead><tr><th>Relation</th><th>From</th><th>To</th><th>Label</th></tr></thead>
    <tbody>`
  for (const edge of trace.dataflow_edges) {
    yield `<tr>
            <td>${escapeHtml(edge.relation)}</td>
            <td><code>${escapeHtml(edge.from.type)}:${escapeHtml(edge.from.id)}</code></td>
            <td><code>${escapeHtml(edge.to.type)}:${escapeHtml(edge.to.id)}</code></td>
            <td>${escapeHtml(edge.label ?? "")}</td>
          </tr>`
  }
  yield `</tbody>
  </table></div>`
}

function* renderSemanticPipeline(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const pipelineTypes = new Set([
    "run.start",
    "case.completed",
    "case.failed",
    "prompt.assembly",
    "context.transform",
    "context.pack",
    "context.compaction",
    "llm.call",
    "llm.turn",
    "agent.lifecycle",
    "exit.gate",
    "decision",
    "tool.call",
    "tool.result",
    "tool.error",
    "mcp.call",
    "skill.load",
    "subagent.call",
    "loop.decision",
    "task.plan_state",
    "execution.observation",
    "evidence.semantic_fact",
    "evidence.fact",
    "response.output",
    "response.claim",
    "claim.support_assessment",
  ])
  const records = trace.records
    .filter((record) => pipelineTypes.has(record.event_type))
    .toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) {
    yield `<section id="semantic-pipeline"><h2>Semantic Pipeline</h2><div class="empty">No semantic pipeline records.</div></section>`
    return
  }
  yield `<section id="semantic-pipeline">
    <div class="section-title">
      <h2>Semantic Pipeline</h2>
      <span class="muted">User request, context transformations, model calls, decisions, tools, subagents, and outputs.</span>
    </div>
    <div class="pipeline">`
  for (const [index, record] of records.entries()) {
    yield `<article class="pipeline-card">
            <div class="pipeline-index">${index + 1}</div>
            <div class="pipeline-body">
              <div class="flow-head">
                <span class="kind">${escapeHtml(record.event_type)}</span>
                ${record.component ? `<span class="component">${escapeHtml(record.component)}</span>` : ""}
                ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
                <strong>${escapeHtml(recordLabel(record))}</strong>
              </div>
              <div class="flow-summary">${escapeHtml(recordSummary(record))}</div>
              <div class="pipeline-io">
                <div>
                  <div class="pane-title">Input</div>
                  <pre>${escapeHtml(pretty(recordInput(record), 2200))}</pre>
                </div>
                <div>
                  <div class="pane-title">Output</div>
                  <pre>${escapeHtml(pretty(recordOutput(record), 2200))}</pre>
                </div>
              </div>
              <div class="flow-meta">
                <span>id <code>${escapeHtml(record.record_id)}</code></span>
                <span>${escapeHtml(`${record.time_ms}ms`)}</span>
                ${record.artifact_refs?.length ? `<span>artifacts ${artifactLinks(record.artifact_refs, artifacts)}</span>` : ""}
                ${record.source_refs?.length ? `<span>sources <code>${escapeHtml(boundedViewerJoin(record.source_refs))}</code></span>` : ""}
              </div>
            </div>
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderIoInspector(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.filter((record) =>
    [
      "prompt.assembly",
      "context.transform",
      "context.pack",
      "context.compaction",
      "llm.call",
      "llm.turn",
      "agent.lifecycle",
      "exit.gate",
      "decision",
      "tool.call",
      "tool.result",
      "tool.error",
      "mcp.call",
      "skill.load",
      "subagent.call",
      "task.plan_state",
      "execution.observation",
      "evidence.semantic_fact",
      "evidence.fact",
      "response.output",
      "response.claim",
      "claim.support_assessment",
    ].includes(record.event_type),
  )
  if (!records.length) {
    yield `<section id="io-inspector"><h2>IO Inspector</h2><div class="empty">No IO records.</div></section>`
    return
  }
  yield `<section id="io-inspector">
    <div class="section-title">
      <h2>IO Inspector</h2>
      <span class="muted">Inputs and outputs are scrollable so large payloads stay inspectable.</span>
    </div>
    <div class="io-list">`
  for (const record of records) {
    yield `<article class="io-record">
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
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderSemanticFacts(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.filter(hasSemanticFacts)
  if (!records.length) {
    yield `<section id="semantic-facts"><h2>Semantic Facts</h2><div class="empty">No semantic facts.</div></section>`
    return
  }
  yield `<section id="semantic-facts">
    <div class="section-title">
      <h2>Semantic Facts</h2>
      <span class="muted">Typed resources, source references, final response facts, and loop decisions.</span>
    </div>
    <div class="fact-list">`
  for (const record of records) {
    yield `<article class="fact-card">
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
                <div class="refs">${escapeHtml(boundedViewerJoin(record.source_refs ?? []) || "-")}</div>
              </div>
              <div>
                <div class="label">Artifacts</div>
                <div class="refs">${artifactLinks(record.artifact_refs, artifacts)}</div>
              </div>
            </div>
            <pre class="semantic-data">${escapeHtml(pretty(record.data, 1800))}</pre>
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderLlmTurns(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records
    .filter((record) => record.event_type === "llm.turn")
    .toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) {
    yield `<section id="llm-turns"><h2>LLM Turns</h2><div class="empty">No LLM turn records.</div></section>`
    return
  }
  yield `<section id="llm-turns">
    <div class="section-title">
      <h2>LLM Turns</h2>
      <span class="muted">Normalized provider turns. Title/background turns are marked by role.</span>
    </div>
    <div class="fact-list">`
  for (const record of records) {
    yield `<article class="fact-card">
            <div class="fact-head">
              <span class="kind">${escapeHtml(record.event_type)}</span>
              <span class="component">${escapeHtml(record.data?.agent_role ?? "unknown")}</span>
              ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
              <strong>${escapeHtml(recordLabel(record))}</strong>
              <code>${escapeHtml(record.record_id)}</code>
            </div>
            <div class="flow-summary">${escapeHtml(recordSummary(record))}</div>
            <div class="flow-meta">
              <span>duration ${formatDuration(record.duration_ms)}</span>
              <span>tokens ${escapeHtml(formatTokens(record.token_usage))}</span>
              ${record.artifact_refs?.length ? `<span>artifacts ${artifactLinks(record.artifact_refs, artifacts)}</span>` : ""}
            </div>
            <pre class="semantic-data">${escapeHtml(pretty(record.data, 2200))}</pre>
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderLifecycle(trace: ProvenanceTraceView) {
  const records = trace.records
    .filter((record) => record.event_type === "agent.lifecycle" || record.event_type === "exit.gate")
    .toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) {
    yield `<section id="lifecycle"><h2>Lifecycle And Exit Gates</h2><div class="empty">No lifecycle records.</div></section>`
    return
  }
  yield `<section id="lifecycle">
    <div class="section-title">
      <h2>Lifecycle And Exit Gates</h2>
      <span class="muted">Turn milestones, compaction decisions, and non-interactive exit decisions.</span>
    </div>
    <div class="flow-list">`
  for (const [index, record] of records.entries()) {
    yield `<article class="flow-row">
            <div class="flow-index"><span>${index + 1}</span><code>${escapeHtml(`${record.time_ms}ms`)}</code></div>
            <div class="flow-main">
              <div class="flow-head">
                <span class="kind">${escapeHtml(record.event_type)}</span>
                ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
                <strong>${escapeHtml(recordLabel(record))}</strong>
              </div>
              <div class="flow-summary">${escapeHtml(recordSummary(record))}</div>
              <pre class="semantic-data">${escapeHtml(pretty(record.data, 1800))}</pre>
            </div>
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderSubagents(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records
    .filter((record) => record.event_type === "subagent.call" || record.data?.agent_role === "subagent")
    .toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) {
    yield `<section id="subagents"><h2>Subagents</h2><div class="empty">No subagent records.</div></section>`
    return
  }
  yield `<section id="subagents">
    <div class="section-title">
      <h2>Subagents</h2>
      <span class="muted">Delegated prompts, child turns, returned output, and child trace refs.</span>
    </div>
    <div class="io-list">`
  for (const record of records) {
    yield `<article class="io-record">
            <div class="io-head">
              <span class="kind">${escapeHtml(record.event_type)}</span>
              ${record.status ? `<span class="status">${escapeHtml(record.status)}</span>` : ""}
              <strong>${escapeHtml(recordLabel(record))}</strong>
              <code>${escapeHtml(record.record_id)}</code>
            </div>
            <div class="io-grid">
              <div class="io-input">
                <div class="pane-title">Delegated / Input</div>
                <pre>${escapeHtml(pretty(recordInput(record)))}</pre>
              </div>
              <div class="io-output">
                <div class="pane-title">Returned / Output</div>
                ${
                  record.data?.child_trace_mode
                    ? `<div class="refs">child trace mode: <code>${escapeHtml(record.data.child_trace_mode)}</code>${
                        record.data.child_record_count !== undefined
                          ? ` <span class="muted">records=${escapeHtml(record.data.child_record_count)}</span>`
                          : ""
                      }</div>`
                    : ""
                }
                ${
                  record.data?.child_trace_unavailable_reason
                    ? `<div class="refs">child trace unavailable: <code>${escapeHtml(record.data.child_trace_unavailable_reason)}</code></div>`
                    : ""
                }
                <pre>${escapeHtml(pretty(recordOutput(record)))}</pre>
                <div class="refs">refs ${artifactLinks(record.output_refs ?? record.artifact_refs, artifacts)}</div>
              </div>
            </div>
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderEvidenceFacts(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records
    .filter((record) => record.event_type === "evidence.semantic_fact" || record.event_type === "evidence.fact")
    .toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) {
    yield `<section id="evidence-facts"><h2>Semantic Evidence</h2><div class="empty">No semantic evidence records.</div></section>`
    return
  }
  yield `<section id="evidence-facts">
    <div class="section-title">
      <h2>Semantic Evidence</h2>
      <span class="muted">Stable facts extracted from tools, MCP, skills, verification, and subagents.</span>
    </div>
    <div class="fact-list">`
  for (const record of records) {
    yield `<article class="fact-card">
            <div class="fact-head">
              <span class="kind">${escapeHtml(record.event_type)}</span>
              ${record.component ? `<span class="component">${escapeHtml(record.component)}</span>` : ""}
              <strong>${escapeHtml(recordLabel(record))}</strong>
              <code>${escapeHtml(record.record_id)}</code>
            </div>
            <div class="flow-summary">${escapeHtml(recordSummary(record))}</div>
            <div class="fact-grid">
              <div>
                <div class="label">Structured Claim</div>
                ${structuredClaim(record)}
              </div>
              <div>
                <div class="label">Source Refs</div>
                <div class="refs">${escapeHtml(boundedViewerJoin(record.source_refs ?? []) || "-")}</div>
              </div>
              <div>
                <div class="label">Source Locations</div>
                <div class="source-locations">${sourceLocations(record)}</div>
              </div>
              <div>
                <div class="label">Artifacts</div>
                <div class="refs">${artifactLinks(record.artifact_refs, artifacts)}</div>
              </div>
              <div>
                <div class="label">Tokens</div>
                <div class="refs">${escapeHtml(formatTokens(record.token_usage))}</div>
              </div>
            </div>
            <pre class="semantic-data">${escapeHtml(pretty(record.data, 2400))}</pre>
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderExecutionObservations(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records
    .filter((record) => record.event_type === "execution.observation" || record.event_type === "task.plan_state")
    .toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) {
    yield `<section id="execution-observations"><h2>Execution Observations</h2><div class="empty">No routine execution observations.</div></section>`
    return
  }
  yield `<section id="execution-observations">
    <div class="section-title">
      <h2>Execution Observations</h2>
      <span class="muted">Routine tool observations and plan states kept separate from semantic evidence.</span>
    </div>
    <div class="fact-list">`
  for (const record of records) {
    yield `<article class="fact-card">
            <div class="fact-head">
              <span class="kind">${escapeHtml(record.event_type)}</span>
              ${record.component ? `<span class="component">${escapeHtml(record.component)}</span>` : ""}
              <strong>${escapeHtml(recordLabel(record))}</strong>
              <code>${escapeHtml(record.record_id)}</code>
            </div>
            <div class="flow-summary">${escapeHtml(recordSummary(record))}</div>
            <div class="fact-grid">
              <div>
                <div class="label">Source Refs</div>
                <div class="refs">${escapeHtml(boundedViewerJoin(record.source_refs ?? []) || "-")}</div>
              </div>
              <div>
                <div class="label">Artifacts</div>
                <div class="refs">${artifactLinks(record.artifact_refs, artifacts)}</div>
              </div>
            </div>
            <pre class="semantic-data">${escapeHtml(pretty(record.data, 1800))}</pre>
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderClaimEvidenceMatrix(trace: ProvenanceTraceView) {
  const assessments = new Map(
    trace.records
      .filter((record) => record.event_type === "claim.support_assessment")
      .map((record) => [boundedViewerText(record.data?.claim_id ?? "", 520), record]),
  )
  const records = trace.records
    .filter((record) => record.event_type === "response.claim")
    .toSorted((a, b) => a.time_ms - b.time_ms)
  if (!records.length) {
    yield `<section id="claim-evidence-matrix"><h2>Claim Evidence Matrix</h2><div class="empty">No response claim records.</div></section>`
    return
  }
  yield `<section id="claim-evidence-matrix">
    <div class="section-title">
      <h2>Claim Evidence Matrix</h2>
      <span class="muted">Final-answer claims mapped to direct evidence, context, and execution refs.</span>
    </div>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Claim</th>
            <th>Format</th>
            <th>Support</th>
            <th>Quality Flags</th>
            <th>Matched Evidence</th>
            <th>Assessment</th>
            <th>Tool Failures</th>
            <th>Match</th>
            <th>Reasons</th>
            <th>Candidates</th>
            <th>Direct Evidence</th>
            <th>Legacy Refs</th>
            <th>Context</th>
            <th>Execution</th>
          </tr>
        </thead>
        <tbody>`
  for (const record of records) {
    const data = record.data ?? {}
              const direct = Array.isArray(data.direct_evidence_refs) ? data.direct_evidence_refs : []
              const matched = Array.isArray(data.matched_evidence_refs) ? data.matched_evidence_refs : []
              const reasons = Array.isArray(data.match_reasons) ? data.match_reasons : []
              const candidates = Array.isArray(data.candidate_evidence_refs) ? data.candidate_evidence_refs : []
              const legacy = Array.isArray(data.legacy_context_refs) ? data.legacy_context_refs : []
              const context = Array.isArray(data.context_refs) ? data.context_refs : []
              const execution = Array.isArray(data.execution_refs) ? data.execution_refs : []
              const flags = Array.isArray(data.quality_flags) ? data.quality_flags : []
              const assessment = assessments.get(boundedViewerText(data.claim_id ?? "", 520))
              const assessmentData = assessment?.data ?? {}
              const missingEvidence = Array.isArray(assessmentData.missing_evidence_types)
                ? assessmentData.missing_evidence_types
                : []
              const toolFailures = Array.isArray(assessmentData.tool_failure_dependency_refs)
                ? assessmentData.tool_failure_dependency_refs
                : []
              const canonical = data.canonical_text
                ? `<div class="muted">${escapeHtml(preview(data.canonical_text, 420))}</div>`
                : ""
              const tableSubject = data.table_subject ? `<br/><code>${escapeHtml(data.table_subject)}</code>` : ""
    yield `<tr>
                <td><div class="claim-text">${escapeHtml(preview(data.text, 520))}</div>${canonical}<code>${escapeHtml(record.record_id)}</code></td>
                <td><code>${escapeHtml(data.claim_format ?? "-")}</code>${tableSubject}</td>
                <td><span class="status">${escapeHtml(data.support_level ?? "-")}</span></td>
                <td>${flags.length ? flags.map((flag) => `<code>${escapeHtml(flag)}</code>`).join(" ") : `<span class="muted">-</span>`}</td>
                <td><div class="refs">${escapeHtml(boundedViewerJoin(matched) || "-")}</div></td>
                <td><code>${escapeHtml(assessment?.record_id ?? "-")}</code><br/><span class="muted">${escapeHtml(boundedViewerJoin(missingEvidence) || "-")}</span></td>
                <td><div class="refs">${escapeHtml(boundedViewerJoin(toolFailures) || "-")}</div></td>
                <td><code>${escapeHtml(data.match_strategy ?? "-")}</code><br/><span class="muted">${escapeHtml(data.match_score ?? "-")}</span></td>
                <td>${reasons.length ? reasons.map((reason) => `<code>${escapeHtml(reason)}</code>`).join(" ") : `<span class="muted">-</span>`}</td>
                <td><div class="refs">${escapeHtml(boundedViewerJoin(candidates) || "-")}</div></td>
                <td><div class="refs">${escapeHtml(boundedViewerJoin(direct) || "-")}</div></td>
                <td><div class="refs">${escapeHtml(boundedViewerJoin(legacy) || "-")}</div></td>
                <td><div class="refs">${escapeHtml(boundedViewerJoin(context) || "-")}</div></td>
                <td><div class="refs">${escapeHtml(boundedViewerJoin(execution) || "-")}</div></td>
              </tr>`
  }
  yield `</tbody>
      </table>
    </div>
  </section>`
}

function* renderContextAndCompaction(trace: ProvenanceTraceView, artifacts: Map<string, TraceArtifact>) {
  const records = trace.records.filter(
    (record) =>
      record.event_type === "context.pack" ||
      record.event_type === "context.compaction_check" ||
      record.event_type === "context.compaction" ||
      record.component === "context",
  )
  if (!records.length) {
    yield `<section id="context-compaction"><h2>Context And Compaction</h2><div class="muted">Context Ledger</div><div class="empty">No context or compaction records.</div></section>`
    return
  }
  yield `<section id="context-compaction">
    <div class="section-title">
      <h2>Context And Compaction</h2>
      <span class="muted">Context Ledger. Context construction, compaction inputs, outputs, and stored large payloads.</span>
    </div>
    <div class="context-list">`
  for (const record of records) {
    yield `<article class="context-card">
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
          </article>`
  }
  yield `</div>
  </section>`
}

function* renderArtifacts(trace: ProvenanceTraceView, artifactSnapshotPaths: ReadonlyMap<string, string>) {
  if (!trace.artifacts.length) {
    yield `<div class="empty">No artifacts.</div>`
    return
  }
  yield `<div class="table-scroll"><table>
    <thead><tr><th>Artifact</th><th>Label</th><th>Length</th><th>Occurrences</th><th>Path</th></tr></thead>
    <tbody>`
  for (const artifact of trace.artifacts) {
    const safePath = safeArtifactRelativePath(artifact.path)
    const snapshotPath = safePath ? artifactSnapshotPaths.get(safePath) : undefined
    yield `<tr>
            <td><code>${escapeHtml(boundedViewerText(artifact.artifact_id, 520))}</code></td>
            <td>${escapeHtml(artifact.label ?? "")}</td>
            <td>${artifact.length}</td>
            <td>${artifact.occurrences ?? 1}</td>
            <td>${snapshotPath ? `<a href="${escapeHtml(snapshotPath)}">${escapeHtml(snapshotPath)}</a>` : `<span class="muted" title="Artifact unavailable">Artifact unavailable</span>`}</td>
          </tr>`
  }
  yield `</tbody>
  </table></div>`
}

function* provenanceTraceHtmlSemanticChunks(
  trace: ProvenanceTraceView,
  options: ProvenanceTraceHtmlChunkOptions,
): Generator<string> {
  const artifacts = new Map<string, TraceArtifact>()
  for (const artifact of trace.artifacts) artifacts.set(boundedViewerText(artifact.artifact_id, 520), artifact)
  const artifactSnapshotPaths = options.artifactSnapshotPaths ?? new Map<string, string>()
  artifactPathPolicies.set(artifacts, artifactSnapshotPaths)

  yield `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trace v${escapeHtml(TRACE_VERSION)} - ${escapeHtml(trace.manifest.case_id)}</title>
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
    .recovery-banner { display: flex; gap: 8px; align-items: center; margin: 0 0 14px; padding: 10px 12px; color: var(--bad); background: #fff1f0; border: 1px solid #fecdca; font-size: 13px; }
    .kind { color: var(--accent); }
    .component { color: var(--blue); }
    .status { color: var(--muted); }
    .overview-grid { display: grid; grid-template-columns: repeat(7, minmax(120px, 1fr)); gap: 10px; }
    .overview-grid > div { min-width: 0; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-soft); }
    .overview-grid strong, .overview-grid code { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .component-strip { display: flex; gap: 10px; overflow-x: auto; margin-top: 12px; padding-bottom: 2px; }
    .component-chip { flex: 0 0 210px; display: grid; gap: 2px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; font-size: 12px; }
    .health-grid { display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 10px; }
    .health-card { min-width: 0; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-soft); }
    .health-card strong { display: block; margin: 2px 0 4px; font-size: 24px; line-height: 1.1; }
    .health-details { display: grid; grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr); gap: 12px; margin-top: 14px; }
    .flag-list, .issue-list { display: grid; gap: 8px; margin-top: 8px; }
    .flag { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
    .issue-row { padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
    .severity { display: inline-flex; align-items: center; min-height: 22px; margin-right: 8px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); font-size: 12px; font-weight: 800; text-transform: uppercase; }
    .severity.info { color: var(--blue); background: #eef6ff; border-color: #b2ddff; }
    .severity.warning { color: var(--amber); background: #fff7e6; border-color: #fedf89; }
    .severity.error { color: var(--bad); background: #fff1f0; border-color: #fecdca; }
    .flow-list, .io-list, .fact-list, .context-list { display: grid; gap: 10px; }
    .pipeline { display: grid; gap: 10px; }
    .pipeline-card { display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 12px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-soft); }
    .pipeline-index { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 50%; color: var(--accent); background: var(--accent-soft); font-weight: 800; }
    .pipeline-body { min-width: 0; }
    .pipeline-io { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; margin-top: 10px; }
    .pipeline-io > div { min-width: 0; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
    .pipeline-io pre { max-height: 260px; }
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
    .structured-claim { display: grid; gap: 6px; min-width: 220px; }
    .structured-claim strong, .structured-claim code { display: block; max-width: 100%; overflow-x: auto; white-space: nowrap; }
    .claim-span { grid-column: 1 / -1; }
    .claim-text { min-width: 260px; max-height: 140px; overflow: auto; white-space: pre-wrap; }
    .source-locations, .typed-resources { display: grid; gap: 6px; min-width: 0; }
    .source-location pre { max-height: 120px; background: #f8fafc; color: var(--text); border-color: var(--border); white-space: pre; }
    .typed-resource { padding: 8px; border: 1px solid var(--line); border-radius: 6px; background: #fff; }
    .fact-text { margin-top: 5px; color: #344054; font-size: 13px; }
    .table-scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
    .empty { color: var(--muted); font-size: 13px; }
    @media (max-width: 1100px) { .overview-grid { grid-template-columns: repeat(3, minmax(120px, 1fr)); } .fact-grid, .health-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .health-details { grid-template-columns: 1fr; } }
    @media (max-width: 760px) { main, .header-inner { padding-left: 14px; padding-right: 14px; } .overview-grid, .flow-row, .io-grid, .fact-grid, .pipeline-card, .pipeline-io, .health-grid { grid-template-columns: 1fr; } header { position: static; } }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <h1>Trace v${escapeHtml(TRACE_VERSION)}</h1>
      <div class="muted">Trace Provenance</div>
      <div class="meta">
        <span>case <code>${escapeHtml(trace.manifest.case_id)}</code></span>
        <span>run <code>${escapeHtml(trace.manifest.run_id)}</code></span>
        <span>${escapeHtml(trace.manifest.started_at)}</span>
      </div>
      <nav>
        <a href="#overview">Overview</a>
        <a href="#trace-health">Trace Health</a>
        <a href="#semantic-pipeline">Semantic Pipeline</a>
        <a href="#llm-turns">LLM Turns</a>
        <a href="#lifecycle">Lifecycle</a>
        <a href="#subagents">Subagents</a>
        <a href="#claim-evidence-matrix">Claim Matrix</a>
        <a href="#evidence-facts">Semantic Evidence</a>
        <a href="#execution-observations">Execution Observations</a>
        <a href="#agent-flow">Agent Flow</a>
        <a href="#component-dataflow">Component Dataflow</a>
        <a href="#io-inspector">IO Inspector</a>
        <a href="#semantic-facts">Semantic Facts</a>
        <a href="#context-compaction">Context And Compaction</a>
        <a href="#artifacts">Artifacts</a>
      </nav>
    </div>
  </header>
  <main>`
  yield renderOverview(trace)
  yield* renderTraceHealth(trace)
  yield* renderSemanticPipeline(trace, artifacts)
  yield* renderLlmTurns(trace, artifacts)
  yield* renderLifecycle(trace)
  yield* renderSubagents(trace, artifacts)
  yield* renderClaimEvidenceMatrix(trace)
  yield* renderEvidenceFacts(trace, artifacts)
  yield* renderExecutionObservations(trace, artifacts)
  yield* renderAgentFlow(trace, artifacts)
  yield `<section id="component-dataflow">
      <h2>Component Dataflow</h2>`
  yield* renderDataflow(trace)
  yield `</section>`
  yield* renderIoInspector(trace, artifacts)
  yield* renderSemanticFacts(trace, artifacts)
  yield* renderContextAndCompaction(trace, artifacts)
  yield `<section id="artifacts">
      <h2>Artifacts</h2>`
  yield* renderArtifacts(trace, artifactSnapshotPaths)
  yield `</section>`
  yield `
  </main>
</body>
</html>`
}

function utf8CodePointBytes(input: string) {
  const codePoint = input.codePointAt(0) ?? 0
  if (codePoint <= 0x7f) return 1
  if (codePoint <= 0x7ff) return 2
  if (codePoint <= 0xffff) return 3
  return 4
}

function* boundedUtf8Chunks(chunks: Iterable<string>, maxChunkBytes: number): Generator<string> {
  for (const chunk of chunks) {
    let output = ""
    let outputBytes = 0
    for (const character of chunk) {
      const characterBytes = utf8CodePointBytes(character)
      if (output && outputBytes + characterBytes > maxChunkBytes) {
        yield output
        output = ""
        outputBytes = 0
      }
      output += character
      outputBytes += characterBytes
    }
    if (output) yield output
  }
}

export function* provenanceTraceHtmlChunks(
  trace: ProvenanceTraceView,
  options: ProvenanceTraceHtmlChunkOptions = {},
): Generator<string> {
  const maxChunkBytes = Math.max(4, Math.floor(options.maxChunkBytes ?? 256 * 1024))
  yield* boundedUtf8Chunks(provenanceTraceHtmlSemanticChunks(trace, options), maxChunkBytes)
}

export function renderProvenanceTraceHtml(trace: ProvenanceTraceView) {
  return [...provenanceTraceHtmlChunks(trace)].join("")
}
