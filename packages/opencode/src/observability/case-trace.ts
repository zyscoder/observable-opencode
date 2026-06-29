import crypto from "crypto"
import fs from "fs"
import path from "path"
import { Global } from "@opencode-ai/core/global"
import { renderProvenanceTraceHtml } from "./causal-trace-viewer"
import { renderCaseTraceHtml } from "./case-trace-html"

export type TraceStatus = "running" | "success" | "error" | "cancelled"

export type TraceComponent =
  | "run"
  | "runtime"
  | "prompt"
  | "context"
  | "llm"
  | "processor"
  | "tool"
  | "skill"
  | "task"
  | "mcp"
  | "plugin"
  | "result"
  | "trace"

export type TraceFieldSummary = {
  type: string
  length?: number
  hash?: string
  preview?: string
  value?: string | number | boolean | null
  keys?: string[]
  artifact_id?: string
}

export type TraceArtifact = {
  artifact_id: string
  kind: "text" | "json"
  label?: string
  path: string
  length: number
  hash: string
  preview: string
  created_at: string
  dedupe_key?: string
  occurrences?: number
}

export type TraceTokenUsage = {
  input?: number
  output?: number
  reasoning?: number
  cached_input?: number
  cache_write?: number
  total?: number
  cost?: number
}

export type TraceError = {
  name?: string
  message: string
  stack?: string
  component?: string
  span_id?: string
}

export type TraceSpan = {
  span_id: string
  parent_span_id?: string
  component: TraceComponent
  operation: string
  name?: string
  status: TraceStatus
  start_time: string
  end_time?: string
  start_ms: number
  end_ms?: number
  duration_ms?: number
  input_summary?: unknown
  output_summary?: unknown
  token_usage?: TraceTokenUsage
  metadata?: Record<string, unknown>
  error?: TraceError
}

export type TraceEvent = {
  event_id: string
  span_id?: string
  component: TraceComponent
  event_type: string
  timestamp: string
  time_ms: number
  data?: unknown
}

export type TraceRef = {
  type: string
  id: string
  label?: string
}

export type TraceContextSnapshot = {
  snapshot_id: string
  span_id?: string
  phase: "llm_request" | "compaction" | "other"
  provider_id?: string
  model_id?: string
  agent?: string
  message_count?: number
  system_count?: number
  tool_count?: number
  token_estimate?: number
  messages?: TraceFieldSummary
  system?: TraceFieldSummary
  tools?: TraceFieldSummary
  metadata?: Record<string, unknown>
}

export type TraceSemanticDecision = {
  decision_id: string
  span_id?: string
  component: TraceComponent
  decision_type: string
  intent?: string
  chosen_action?: string
  rationale?: TraceFieldSummary
  source_refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceSemanticEdge = {
  edge_id: string
  from: TraceRef
  to: TraceRef
  relation: string
  label?: string
  metadata?: Record<string, unknown>
}

export type TraceParsedFailure = {
  message?: string
  expected?: string
  actual?: string
  file?: string
  line?: number
  column?: number
}

export type TraceVerificationRecord = {
  verification_id: string
  span_id?: string
  tool_call_id?: string
  command?: string
  cwd?: string
  purpose?: string
  stage?: "baseline" | "post_change" | "exploration" | "unknown"
  exit_code?: number
  status: "passed" | "failed" | "unknown"
  parsed_failures: TraceParsedFailure[]
  stdout?: TraceFieldSummary
  stderr?: TraceFieldSummary
  metadata?: Record<string, unknown>
}

export type TraceChangeRecord = {
  change_id: string
  span_id?: string
  tool_call_id?: string
  files: string[]
  intent?: string
  diff?: TraceFieldSummary
  source_refs?: string[]
  verification_refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceConstraintRecord = {
  constraint_id: string
  source: "user" | "system" | "runtime" | string
  constraint: string
  status: "observed_satisfied" | "observed_violated" | "unknown"
  source_refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceResponseSegment = {
  segment_id: string
  response_artifact?: string
  text: TraceFieldSummary
  source_refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceDesignRecord = {
  design_id: string
  span_id?: string
  source?: "final_response" | "context" | "manual" | string
  requirement_summary?: TraceFieldSummary
  existing_boundaries?: TraceFieldSummary
  design_constraints?: TraceFieldSummary
  candidate_solutions?: TraceFieldSummary
  selected_solution?: TraceFieldSummary
  tradeoffs?: TraceFieldSummary
  risks?: TraceFieldSummary
  test_strategy?: TraceFieldSummary
  source_refs?: string[]
  metadata?: Record<string, unknown>
}

export type CausalNodeKind =
  | "run.start"
  | "task.loop"
  | "context.pack"
  | "context.compaction"
  | "llm.call"
  | "tool.call"
  | "mcp.call"
  | "skill.load"
  | "subagent.call"
  | "observation"
  | "change"
  | "verification"
  | "response.output"
  | "runtime.event"

export type CausalNode = {
  node_id: string
  kind: CausalNodeKind | string
  component?: TraceComponent
  span_id?: string
  timestamp: string
  time_ms: number
  title?: string
  status?: TraceStatus | TraceVerificationRecord["status"]
  data?: Record<string, unknown>
  source_refs?: string[]
  artifact_refs?: string[]
  metadata?: Record<string, unknown>
}

export type CausalEdge = {
  edge_id: string
  from: TraceRef
  to: TraceRef
  relation: string
  label?: string
  metadata?: Record<string, unknown>
}

export type TraceManifest = {
  trace_version: "3.0"
  case_id: string
  run_id: string
  session_id?: string
  started_at: string
  ended_at?: string
  duration_ms: number
  status: TraceStatus
  input?: Record<string, unknown>
  environment: Record<string, unknown>
  token_usage: TraceTokenUsage
  result?: Record<string, unknown>
  files: {
    provenance_trace: string
    records: string
    raw_events: string
    viewer: string
    partial_latest: string
  }
}

export type ProvenanceRecord = {
  record_id: string
  component?: TraceComponent
  event_type: string
  span_id?: string
  parent_span_id?: string
  timestamp: string
  time_ms: number
  title?: string
  status?: TraceStatus | TraceVerificationRecord["status"]
  duration_ms?: number
  token_usage?: TraceTokenUsage
  error?: unknown
  input_refs?: string[]
  output_refs?: string[]
  source_refs?: string[]
  artifact_refs?: string[]
  data?: Record<string, unknown>
  metadata?: Record<string, unknown>
}

export type DataflowEdge = {
  edge_id: string
  from: TraceRef
  to: TraceRef
  relation:
    | "produced"
    | "consumed"
    | "prompted"
    | "returned"
    | "selected_into_context"
    | "compressed_from"
    | "compressed_to"
    | "spawned"
    | "continued_from"
    | "wrote_artifact"
    | string
  label?: string
  metadata?: Record<string, unknown>
}

export type ProvenanceTraceSummary = {
  trace_version: "3.0"
  manifest: TraceManifest
  records: ProvenanceRecord[]
  dataflow_edges: DataflowEdge[]
  artifacts: TraceArtifact[]
  metrics: {
    spans: number
    events: number
    records: number
    dataflow_edges: number
    artifacts: number
    token_usage: TraceTokenUsage
  }
}

export type TraceSummary = {
  trace_version: "1.0" | "1.1" | "1.2" | "1.3"
  case_id: string
  run_id: string
  session_id?: string
  started_at: string
  ended_at?: string
  duration_ms: number
  status: TraceStatus
  input?: Record<string, unknown>
  environment: Record<string, unknown>
  token_usage: TraceTokenUsage
  spans: TraceSpan[]
  events: TraceEvent[]
  artifacts?: TraceArtifact[]
  errors: TraceError[]
  result?: Record<string, unknown>
  context_snapshots?: TraceContextSnapshot[]
  semantic_decisions?: TraceSemanticDecision[]
  dataflow_edges?: TraceSemanticEdge[]
  verification_records?: TraceVerificationRecord[]
  change_records?: TraceChangeRecord[]
  constraint_records?: TraceConstraintRecord[]
  response_segments?: TraceResponseSegment[]
  design_records?: TraceDesignRecord[]
}

type CaseTraceConfig = {
  caseID?: string
  traceDir?: string
  input?: Record<string, unknown>
  environment?: Record<string, unknown>
}

type StartSpanInput = {
  component: TraceComponent
  operation: string
  name?: string
  parentSpanID?: string
  input?: unknown
  metadata?: Record<string, unknown>
}

type EndSpanInput = {
  status?: Exclude<TraceStatus, "running">
  output?: unknown
  metadata?: Record<string, unknown>
  tokenUsage?: unknown
  error?: unknown
}

type TraceEventInput = {
  component: TraceComponent
  event_type: string
  span_id?: string
  data?: unknown
}

type FinishTraceInput = {
  status?: Exclude<TraceStatus, "running">
  result?: Record<string, unknown>
  error?: unknown
}

type ContextSnapshotInput = Omit<TraceContextSnapshot, "snapshot_id" | "messages" | "system" | "tools" | "metadata"> & {
  snapshot_id?: string
  messages?: unknown
  system?: unknown
  tools?: unknown
  metadata?: Record<string, unknown>
}

type SemanticDecisionInput = Omit<TraceSemanticDecision, "decision_id" | "rationale" | "metadata" | "source_refs"> & {
  decision_id?: string
  rationale?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
  metadata?: Record<string, unknown>
}

type SemanticEdgeInput = Omit<TraceSemanticEdge, "edge_id"> & {
  edge_id?: string
}

type VerificationRecordInput = Omit<
  TraceVerificationRecord,
  "verification_id" | "parsed_failures" | "stdout" | "stderr" | "status"
> & {
  verification_id?: string
  status?: TraceVerificationRecord["status"]
  parsed_failures?: TraceParsedFailure[]
  stdout?: unknown
  stderr?: unknown
}

type ChangeRecordInput = Omit<TraceChangeRecord, "change_id" | "diff" | "source_refs"> & {
  change_id?: string
  diff?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
}

type ConstraintRecordInput = Omit<TraceConstraintRecord, "constraint_id" | "source_refs"> & {
  constraint_id?: string
  source_refs?: string[]
  evidence_refs?: string[]
}

type ResponseOutputInput = Omit<TraceResponseSegment, "segment_id" | "text" | "source_refs"> & {
  segment_id?: string
  text: unknown
  source_refs?: string[]
  evidence_refs?: string[]
}

type FinalResponseEvidenceInput = Omit<ResponseOutputInput, "segment_id" | "text"> & {
  claim_id?: string
  claim: unknown
  confidence?: "low" | "medium" | "high" | string
}

type DesignRecordInput = Omit<
  TraceDesignRecord,
  | "design_id"
  | "requirement_summary"
  | "existing_boundaries"
  | "design_constraints"
  | "candidate_solutions"
  | "selected_solution"
  | "tradeoffs"
  | "risks"
  | "test_strategy"
  | "source_refs"
> & {
  design_id?: string
  requirement_summary?: unknown
  existing_boundaries?: unknown
  design_constraints?: unknown
  candidate_solutions?: unknown
  selected_solution?: unknown
  tradeoffs?: unknown
  risks?: unknown
  test_strategy?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
}

type CausalNodeInput = Omit<CausalNode, "node_id" | "timestamp" | "time_ms" | "data" | "artifact_refs" | "source_refs"> & {
  node_id?: string
  data?: Record<string, unknown>
  source_refs?: string[]
  evidence_refs?: string[]
}

type CausalEdgeInput = Omit<CausalEdge, "edge_id"> & {
  edge_id?: string
}

type ObservationInput = {
  source: string
  category?: string
  summary: unknown
  data?: unknown
  span_id?: string
  source_refs?: string[]
  evidence_refs?: string[]
  confidence?: "low" | "medium" | "high" | string
  metadata?: Record<string, unknown>
}

type CompactionRecordInput = {
  trigger: "manual" | "auto" | "overflow" | string
  provider_id?: string
  model_id?: string
  input_tokens?: number
  context_limit?: number
  reserved_output_tokens?: number
  selected_head_messages?: number
  selected_tail_messages?: number
  hidden_compaction_messages?: number
  previous_summary?: unknown
  serialized_tail?: unknown
  output_summary?: unknown
  auto_continue?: boolean
  result?: string
  span_id?: string
  source_refs?: string[]
  evidence_refs?: string[]
  metadata?: Record<string, unknown>
}

export type ActiveSpan = {
  id: string
  event(input: Omit<TraceEventInput, "span_id" | "component"> & { component?: TraceComponent }): void
  end(input?: EndSpanInput): void
}

const truthy = new Set(["1", "true", "yes", "on"])
const secretTextPatterns = [/\bsk-[a-zA-Z0-9_-]{8,}\b/g, /\bBearer\s+[a-zA-Z0-9._~+/=-]+\b/gi]
let active: ActiveCaseTrace | false | undefined
let processFinalizerInstalled = false

function nowIso() {
  return new Date().toISOString()
}

function enabledFromEnv() {
  const value = process.env.OPENCODE_CASE_TRACE
  return value ? truthy.has(value.toLowerCase()) : false
}

function stamp() {
  return new Date()
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d+Z$/, "Z")
}

function safeCaseID(input: string) {
  const trimmed = input.trim() || `case-${stamp()}-${process.pid}`
  return trimmed.replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 160)
}

function safeNumber(input: unknown) {
  const value = Number(input ?? 0)
  if (!Number.isFinite(value)) return 0
  return Math.max(0, value)
}

function optionalNumber(input: unknown) {
  if (input === undefined || input === null) return undefined
  const value = Number(input)
  if (!Number.isFinite(value)) return undefined
  return value
}

function defaultTraceDir() {
  return process.env.OPENCODE_CASE_TRACE_DIR || path.join(Global.Path.data, "case-traces")
}

function json(input: unknown) {
  const seen = new WeakSet<object>()
  return JSON.stringify(
    input,
    (_key, value) => {
      const key = String(_key ?? "")
      if (isSensitiveKey(key)) return "[REDACTED]"
      if (typeof value === "bigint") return String(value)
      if (typeof value === "function") return `[Function ${value.name || "anonymous"}]`
      if (value instanceof Error) {
        return errorInfo(value)
      }
      if (value instanceof URL) return value.toString()
      if (typeof value === "string") return redactText(value)
      if (value && typeof value === "object") {
        if (seen.has(value)) return "[Circular]"
        seen.add(value)
      }
      return value
    },
    0,
  )
}

function normalizeKey(input: string) {
  return input
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
}

function isSensitiveKey(input: string) {
  if (!input) return false
  const key = normalizeKey(input)
  if (!key) return false
  const safeMetricKeys = new Set([
    "token",
    "tokens",
    "token_usage",
    "token_estimate",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "input_token_details",
    "output_token_details",
  ])
  if (safeMetricKeys.has(key)) return false
  if (/^(api_key|authorization|cookie|password|passwd|credential|secret)$/.test(key)) return true
  if (/^(access_token|refresh_token|auth_token|id_token)$/.test(key)) return true
  if (/(^|_)(authorization|cookie|password|passwd|credential|secret)($|_)/.test(key)) return true
  if (/(^|_)api_key($|_)/.test(key)) return true
  if (/(^|_)(access_token|refresh_token|auth_token|id_token)($|_)/.test(key)) return true
  return false
}

function hash(input: string) {
  return crypto.createHash("sha256").update(input).digest("hex").slice(0, 16)
}

function redactText(input: string) {
  let output = input
  for (const pattern of secretTextPatterns) output = output.replace(pattern, "[REDACTED]")
  return output
}

function maxFieldLength() {
  return safeNumber(process.env.OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH || 2048) || 2048
}

function summarizeScalar(input: unknown): TraceFieldSummary {
  if (input === null) return { type: "null", value: null }
  if (typeof input === "string") return summarizeText(input)
  if (typeof input === "number" || typeof input === "boolean") return { type: typeof input, value: input }
  if (typeof input === "undefined") return { type: "undefined" }
  return { type: typeof input, value: String(input) }
}

export function summarizeText(input: unknown): TraceFieldSummary {
  const text = redactText(String(input ?? ""))
  const limit = maxFieldLength()
  return {
    type: "text",
    length: text.length,
    hash: hash(text),
    preview: text.slice(0, limit),
  }
}

export function summarizeJson(input: unknown): TraceFieldSummary {
  if (input === null || typeof input !== "object") return summarizeScalar(input)
  const serialized = json(input)
  const keys = Array.isArray(input) ? undefined : Object.keys(input as Record<string, unknown>).slice(0, 50)
  return {
    type: Array.isArray(input) ? "array" : "object",
    length: serialized.length,
    hash: hash(serialized),
    keys,
    preview: serialized.slice(0, maxFieldLength()),
  }
}

function semanticID(prefix: string, sequence: number) {
  return `${prefix}_${sequence}_${crypto.randomUUID().slice(0, 8)}`
}

function parseVerificationFailures(input: { stdout?: unknown; stderr?: unknown }): TraceParsedFailure[] {
  const text = [input.stdout, input.stderr]
    .map((item) => String(item ?? ""))
    .filter(Boolean)
    .join("\n")
  const failures: TraceParsedFailure[] = []
  const expectedCandidates = text
    .split(/\r?\n|\\n/)
    .map((line, index) => {
      if (line.includes("${")) return undefined
      const expected = line.match(/^\s*(?:(?:[A-Za-z]*Error):\s*)?expected\s+([^,\n]+),\s*got\s+([^\n]+)\s*$/i)
      if (!expected) return undefined
      const hasErrorPrefix = /^\s*[A-Za-z]*Error:\s*/i.test(line)
      return {
        index,
        score: hasErrorPrefix ? 2 : 1,
        message: expected[0].trim(),
        expected: expected[1]?.trim(),
        actual: expected[2]?.trim(),
      }
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item))
    .toSorted((a, b) => b.score - a.score || a.index - b.index)
  const expected = expectedCandidates[0]
  if (expected) {
    failures.push({
      message: expected.message,
      expected: expected.expected,
      actual: expected.actual,
    })
  }

  const locations = [...text.matchAll(/((?:file:\/\/)?[^\s:]+):(\d+)(?::(\d+))?/g)].toSorted((a, b) => {
    const aHasColumn = a[3] ? 1 : 0
    const bHasColumn = b[3] ? 1 : 0
    return bHasColumn - aHasColumn || a.index - b.index
  })
  const location = locations[0]
  if (location) {
    const target = failures[0] ?? {}
    target.file = location[1]
    target.line = optionalNumber(location[2])
    target.column = optionalNumber(location[3])
    if (!failures.length) failures.push(target)
  }

  return failures
}

function isTestLikeCommand(command: string | undefined) {
  return Boolean(
    command &&
      /\b(test|pytest|jest|vitest|mocha)\b|bun test|npm test|pnpm test|yarn test|go test|cargo test|node .*test|xcodebuild/i.test(
        command,
      ),
  )
}

export function summarizeMessages(input: unknown): TraceFieldSummary {
  if (!Array.isArray(input)) return summarizeJson(input)
  const roles = input
    .map((item) => (item && typeof item === "object" && "role" in item ? String(item.role) : "unknown"))
    .slice(0, 40)
  const summary = {
    count: input.length,
    roles,
  }
  return summarizeJson(summary)
}

export function errorInfo(input: unknown): TraceError {
  if (input instanceof Error) {
    return {
      name: input.name,
      message: input.message || input.name,
      stack: input.stack,
    }
  }
  return {
    message: typeof input === "string" ? input : json(input),
  }
}

export function normalizeTokenUsage(input: unknown): TraceTokenUsage {
  if (!input || typeof input !== "object") return {}
  const value = input as Record<string, any>
  const cache = value.cache && typeof value.cache === "object" ? value.cache : {}
  return {
    input: safeNumber(value.input ?? value.inputTokens),
    output: safeNumber(value.output ?? value.outputTokens),
    reasoning: safeNumber(value.reasoning ?? value.reasoningTokens ?? value.outputTokenDetails?.reasoningTokens),
    cached_input: safeNumber(
      value.cached_input ?? value.cachedInputTokens ?? value.inputTokenDetails?.cacheReadTokens ?? cache.read,
    ),
    cache_write: safeNumber(value.cache_write ?? value.inputTokenDetails?.cacheWriteTokens ?? cache.write),
    total: safeNumber(value.total ?? value.totalTokens),
    cost: safeNumber(value.cost),
  }
}

function mergeUsage(target: TraceTokenUsage, next: TraceTokenUsage) {
  target.input = safeNumber(target.input) + safeNumber(next.input)
  target.output = safeNumber(target.output) + safeNumber(next.output)
  target.reasoning = safeNumber(target.reasoning) + safeNumber(next.reasoning)
  target.cached_input = safeNumber(target.cached_input) + safeNumber(next.cached_input)
  target.cache_write = safeNumber(target.cache_write) + safeNumber(next.cache_write)
  target.total = safeNumber(target.total) + safeNumber(next.total)
  target.cost = safeNumber(target.cost) + safeNumber(next.cost)
}

class ActiveCaseTrace {
  readonly caseID: string
  readonly runID = crypto.randomUUID()
  readonly rootDir: string
  readonly caseDir: string
  readonly artifactDir: string
  readonly eventsFile: string
  readonly rawEventsFile: string
  readonly recordsFile: string
  readonly traceFile: string
  readonly htmlFile: string
  readonly manifestFile: string
  readonly provenanceTraceFile: string
  readonly viewerFile: string
  readonly partialDir: string
  readonly partialFile: string
  readonly startedAt = Date.now()
  readonly startedIso = nowIso()
  private sequence = 0
  private artifactSequence = 0
  private finished = false
  private sessionID: string | undefined
  private input: Record<string, unknown> | undefined
  private result: Record<string, unknown> | undefined
  private environment: Record<string, unknown>
  private spans = new Map<string, TraceSpan>()
  private spanNodeIDs = new Map<string, string>()
  private events: TraceEvent[] = []
  private artifacts: TraceArtifact[] = []
  private artifactByDedupeKey = new Map<string, TraceArtifact>()
  private causalNodes: CausalNode[] = []
  private causalEdges: CausalEdge[] = []
  private errors: TraceError[] = []
  private contextSnapshots: TraceContextSnapshot[] = []
  private semanticDecisions: TraceSemanticDecision[] = []
  private semanticEdges: TraceSemanticEdge[] = []
  private verificationRecords: TraceVerificationRecord[] = []
  private changeRecords: TraceChangeRecord[] = []
  private constraintRecords: TraceConstraintRecord[] = []
  private responseSegments: TraceResponseSegment[] = []
  private designRecords: TraceDesignRecord[] = []
  private recentFailedVerificationID: string | undefined
  private recentChangeID: string | undefined
  private recentContextSnapshotIDs: string[] = []
  private recentVerificationIDs: string[] = []
  private recentChangeIDs: string[] = []
  private recentToolSpanIDs: string[] = []
  private tokenUsage: TraceTokenUsage = {}
  private writable = true
  private nextPartialWrite = 0

  constructor(config: CaseTraceConfig) {
    this.caseID = safeCaseID(config.caseID ?? process.env.OPENCODE_CASE_ID ?? "")
    this.rootDir = config.traceDir ?? defaultTraceDir()
    this.caseDir = path.join(this.rootDir, this.caseID)
    this.artifactDir = path.join(this.caseDir, "artifacts")
    this.eventsFile = path.join(this.caseDir, "events.jsonl")
    this.rawEventsFile = path.join(this.caseDir, "raw-events.jsonl")
    this.recordsFile = path.join(this.caseDir, "records.jsonl")
    this.traceFile = path.join(this.caseDir, "trace.json")
    this.htmlFile = path.join(this.caseDir, "trace.html")
    this.manifestFile = path.join(this.caseDir, "manifest.json")
    this.provenanceTraceFile = path.join(this.caseDir, "provenance-trace.json")
    this.viewerFile = path.join(this.caseDir, "viewer.html")
    this.partialDir = path.join(this.caseDir, "partial")
    this.partialFile = path.join(this.partialDir, "latest.json")
    this.input = config.input
    this.environment = {
      cwd: process.cwd(),
      argv: process.argv.slice(2),
      pid: process.pid,
      ...config.environment,
    }
    this.open()
  }

  setSessionID(sessionID: string | undefined) {
    if (!sessionID) return
    this.sessionID = sessionID
    this.write("trace.session", { session_id: sessionID })
  }

  setInput(input: Record<string, unknown>) {
    this.input = {
      ...(this.input ?? {}),
      ...input,
    }
    this.write("trace.input", this.input)
  }

  setEnvironment(input: Record<string, unknown>) {
    this.environment = {
      ...this.environment,
      ...input,
    }
    this.write("trace.environment", this.environment)
  }

  startSpan(input: StartSpanInput): ActiveSpan {
    const id = `span_${++this.sequence}_${crypto.randomUUID().slice(0, 8)}`
    const started = Date.now()
    const span: TraceSpan = {
      span_id: id,
      parent_span_id: input.parentSpanID,
      component: input.component,
      operation: input.operation,
      name: input.name,
      status: "running",
      start_time: new Date(started).toISOString(),
      start_ms: started - this.startedAt,
      input_summary:
        input.input === undefined
          ? undefined
          : this.summarizeJson(input.input, `${input.component}.${input.operation}.input`),
      metadata: input.metadata,
    }
    this.spans.set(id, span)
    if (["tool", "skill", "task", "mcp"].includes(input.component)) this.remember(this.recentToolSpanIDs, id)
    this.write("span.start", span)
    const nodeKind = this.nodeKindForSpan(input.component)
    if (nodeKind) {
      const node = this.node({
        kind: nodeKind,
        component: input.component,
        span_id: id,
        title: input.name ?? input.operation,
        status: "running",
        data: {
          operation: input.operation,
          input: input.input,
        },
        metadata: input.metadata,
      })
      this.spanNodeIDs.set(id, node.node_id)
    }
    let ended = false
    return {
      id,
      event: (event) =>
        this.event({
          span_id: id,
          component: event.component ?? input.component,
          event_type: event.event_type,
          data: event.data,
        }),
      end: (endInput) => {
        if (ended) return
        ended = true
        this.endSpan(id, endInput)
      },
    }
  }

  endSpan(id: string, input?: EndSpanInput) {
    const span = this.spans.get(id)
    if (!span) return
    const ended = Date.now()
    const usage = input?.tokenUsage ? normalizeTokenUsage(input.tokenUsage) : undefined
    const error = input?.error ? { ...errorInfo(input.error), component: span.component, span_id: id } : undefined
    span.status = input?.status ?? (error ? "error" : "success")
    span.end_time = new Date(ended).toISOString()
    span.end_ms = ended - this.startedAt
    span.duration_ms = Math.max(0, ended - (this.startedAt + span.start_ms))
    span.output_summary =
      input?.output === undefined
        ? span.output_summary
        : this.summarizeJson(input.output, `${span.component}.${span.operation}.output`)
    span.metadata = {
      ...(span.metadata ?? {}),
      ...(input?.metadata ?? {}),
    }
    if (usage) {
      span.token_usage = usage
      mergeUsage(this.tokenUsage, usage)
    }
    if (error) {
      span.error = error
      this.errors.push(error)
    }
    this.write("span.end", span)
    const nodeID = this.spanNodeIDs.get(id)
    if (nodeID) {
      const node = this.causalNodes.find((item) => item.node_id === nodeID)
      if (node) {
        node.status = span.status
        node.data = {
          ...(node.data ?? {}),
          duration_ms: span.duration_ms,
          output: input?.output === undefined ? node.data?.output : this.summarizeCausalValue(input.output, `${span.component}.${span.operation}.output`),
          token_usage: usage,
          error: error,
        }
        this.writeRecord("node.update", node)
        this.writePartial()
      }
    }
  }

  event(input: TraceEventInput) {
    const time = Date.now()
    const event: TraceEvent = {
      event_id: `evt_${++this.sequence}_${crypto.randomUUID().slice(0, 8)}`,
      span_id: input.span_id,
      component: input.component,
      event_type: input.event_type,
      timestamp: new Date(time).toISOString(),
      time_ms: time - this.startedAt,
      data:
        input.data === undefined
          ? undefined
          : this.summarizeJson(input.data, `${input.component}.${input.event_type}.data`),
    }
    this.events.push(event)
    this.write("event", event)
    if (input.component === "runtime" || input.component === "prompt") {
      this.node({
        kind: "runtime.event",
        component: input.component,
        title: input.event_type,
        data: input.data === undefined ? undefined : { payload: input.data },
      })
    }
  }

  usage(input: unknown, spanID?: string) {
    const usage = normalizeTokenUsage(input)
    mergeUsage(this.tokenUsage, usage)
    this.event({
      component: "llm",
      event_type: "usage",
      span_id: spanID,
      data: usage,
    })
  }

  contextSnapshot(input: ContextSnapshotInput) {
    const snapshot: TraceContextSnapshot = {
      snapshot_id: input.snapshot_id ?? semanticID("ctx", this.contextSnapshots.length + 1),
      span_id: input.span_id,
      phase: input.phase,
      provider_id: input.provider_id,
      model_id: input.model_id,
      agent: input.agent,
      message_count: input.message_count,
      system_count: input.system_count,
      tool_count: input.tool_count,
      token_estimate: input.token_estimate,
      messages:
        input.messages === undefined
          ? undefined
          : this.summarizeJson(input.messages, `context.${input.phase}.messages`),
      system:
        input.system === undefined ? undefined : this.summarizeJson(input.system, `context.${input.phase}.system`),
      tools: input.tools === undefined ? undefined : this.summarizeJson(input.tools, `context.${input.phase}.tools`),
      metadata: input.metadata,
    }
    this.contextSnapshots.push(snapshot)
    this.remember(this.recentContextSnapshotIDs, snapshot.snapshot_id)
    this.write("semantic.context_snapshot", snapshot)
    const node = this.node({
      node_id: `ctxnode_${snapshot.snapshot_id}`,
      kind: "context.pack",
      component: "context",
      span_id: input.span_id,
      title: `${input.agent ?? "agent"} context package`,
      data: {
        snapshot_id: snapshot.snapshot_id,
        phase: input.phase,
        provider_id: input.provider_id,
        model_id: input.model_id,
        agent: input.agent,
        message_count: input.message_count,
        system_count: input.system_count,
        tool_count: input.tool_count,
        token_estimate: input.token_estimate,
        messages: input.messages,
        system: input.system,
        tools: input.tools,
        metadata: input.metadata,
      },
    })
    if (input.span_id) {
      this.causalEdge({
        from: { type: "node", id: node.node_id, label: "context.pack" },
        to: { type: "span", id: input.span_id, label: "llm.call" },
        relation: input.phase === "compaction" ? "compaction_to_context" : "context_to_llm",
        label: "Context package prepared for model call",
      })
    }
    return snapshot
  }

  decision(input: SemanticDecisionInput) {
    const decision: TraceSemanticDecision = {
      decision_id: input.decision_id ?? semanticID("dec", this.semanticDecisions.length + 1),
      span_id: input.span_id,
      component: input.component,
      decision_type: input.decision_type,
      intent: input.intent,
      chosen_action: input.chosen_action,
      rationale:
        input.rationale === undefined ? undefined : this.summarizeText(input.rationale, "semantic.decision.rationale"),
      source_refs: input.source_refs ?? input.evidence_refs,
      metadata: input.metadata,
    }
    this.semanticDecisions.push(decision)
    this.write("semantic.decision", decision)
    return decision
  }

  edge(input: SemanticEdgeInput) {
    const edge: TraceSemanticEdge = {
      edge_id: input.edge_id ?? semanticID("edge", this.semanticEdges.length + 1),
      from: input.from,
      to: input.to,
      relation: input.relation,
      label: input.label,
      metadata: input.metadata,
    }
    this.semanticEdges.push(edge)
    this.write("semantic.edge", edge)
    this.causalEdge({
      from: input.from,
      to: input.to,
      relation: input.relation,
      label: input.label,
      metadata: input.metadata,
    })
    return edge
  }

  verification(input: VerificationRecordInput) {
    const parsed = input.parsed_failures ?? parseVerificationFailures({ stdout: input.stdout, stderr: input.stderr })
    const exitCode = optionalNumber(input.exit_code)
    const status = input.status ?? (exitCode === undefined ? "unknown" : exitCode === 0 ? "passed" : "failed")
    const verification: TraceVerificationRecord = {
      verification_id: input.verification_id ?? semanticID("ver", this.verificationRecords.length + 1),
      span_id: input.span_id,
      tool_call_id: input.tool_call_id,
      command: input.command,
      cwd: input.cwd,
      purpose: input.purpose,
      stage: input.stage ?? "unknown",
      exit_code: exitCode,
      status,
      parsed_failures: parsed,
      stdout: input.stdout === undefined ? undefined : this.summarizeText(input.stdout, "verification.stdout"),
      stderr: input.stderr === undefined ? undefined : this.summarizeText(input.stderr, "verification.stderr"),
      metadata: input.metadata,
    }
    this.verificationRecords.push(verification)
    this.remember(this.recentVerificationIDs, verification.verification_id)
    this.write("semantic.verification", verification)
    this.node({
      node_id: `vernode_${verification.verification_id}`,
      kind: "verification",
      component: "tool",
      span_id: input.span_id,
      title: input.command ?? input.purpose ?? "verification",
      status: verification.status,
      data: {
        verification_id: verification.verification_id,
        command: input.command,
        cwd: input.cwd,
        purpose: input.purpose,
        stage: verification.stage,
        exit_code: verification.exit_code,
        parsed_failures: verification.parsed_failures,
        stdout: input.stdout,
        stderr: input.stderr,
      },
    })
    if (verification.status === "failed") this.recentFailedVerificationID = verification.verification_id
    if (verification.status === "passed" && this.recentChangeID) {
      this.edge({
        from: { type: "change", id: this.recentChangeID },
        to: { type: "verification", id: verification.verification_id },
        relation: "change_to_verification",
        label: "Verification ran after repository change",
      })
    }
    return verification
  }

  change(input: ChangeRecordInput) {
    const sourceRefs =
      input.source_refs ??
      input.evidence_refs ??
      (this.recentFailedVerificationID ? [`verification:${this.recentFailedVerificationID}`] : undefined)
    const change: TraceChangeRecord = {
      change_id: input.change_id ?? semanticID("chg", this.changeRecords.length + 1),
      span_id: input.span_id,
      tool_call_id: input.tool_call_id,
      files: input.files,
      intent: input.intent,
      diff: input.diff === undefined ? undefined : this.summarizeText(input.diff, "change.diff"),
      source_refs: sourceRefs,
      verification_refs: input.verification_refs,
      metadata: input.metadata,
    }
    this.changeRecords.push(change)
    this.remember(this.recentChangeIDs, change.change_id)
    this.write("semantic.change", change)
    this.node({
      node_id: `chgnode_${change.change_id}`,
      kind: "change",
      component: "tool",
      span_id: input.span_id,
      title: input.intent ?? "repository change",
      data: {
        change_id: change.change_id,
        files: input.files,
        intent: input.intent,
        diff: input.diff,
        source_refs: sourceRefs,
        verification_refs: input.verification_refs,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
    })
    this.recentChangeID = change.change_id
    if (this.recentFailedVerificationID) {
      this.edge({
        from: { type: "verification", id: this.recentFailedVerificationID },
        to: { type: "change", id: change.change_id },
        relation: "failure_to_change",
        label: "Change followed a failed verification record",
      })
    }
    return change
  }

  constraint(input: ConstraintRecordInput) {
    const constraint: TraceConstraintRecord = {
      constraint_id: input.constraint_id ?? semanticID("constraint", this.constraintRecords.length + 1),
      source: input.source,
      constraint: redactText(input.constraint),
      status: input.status,
      source_refs: input.source_refs ?? input.evidence_refs,
      metadata: input.metadata,
    }
    this.constraintRecords.push(constraint)
    this.write("semantic.constraint", constraint)
    return constraint
  }

  responseOutput(input: ResponseOutputInput) {
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const segment: TraceResponseSegment = {
      segment_id: input.segment_id ?? semanticID("segment", this.responseSegments.length + 1),
      response_artifact: input.response_artifact,
      text: this.summarizeText(input.text, "result.response.output"),
      source_refs: sourceRefs,
      metadata: input.metadata,
    }
    this.responseSegments.push(segment)
    this.write("semantic.response_output", segment)
    const record = this.node({
      node_id: `responsenode_${segment.segment_id}`,
      kind: "response.output",
      component: "result",
      title: `Response output ${this.responseSegments.length}`,
      data: {
        segment_id: segment.segment_id,
        response_artifact: input.response_artifact,
        text: input.text,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
    })
    for (const ref of sourceRefs ?? []) {
      this.linkSourceToResponse(ref, record.node_id)
    }
    return segment
  }

  finalEvidence(input: FinalResponseEvidenceInput) {
    return this.responseOutput({
      segment_id: input.claim_id,
      response_artifact: input.response_artifact,
      text: input.claim,
      source_refs: input.source_refs ?? input.evidence_refs,
      metadata: input.metadata,
    })
  }

  designRecord(input: DesignRecordInput) {
    const design: TraceDesignRecord = {
      design_id: input.design_id ?? semanticID("design", this.designRecords.length + 1),
      span_id: input.span_id,
      source: input.source,
      requirement_summary: this.summarizeDesignField(input.requirement_summary, "design.requirement_summary"),
      existing_boundaries: this.summarizeDesignField(input.existing_boundaries, "design.existing_boundaries"),
      design_constraints: this.summarizeDesignField(input.design_constraints, "design.design_constraints"),
      candidate_solutions: this.summarizeDesignField(input.candidate_solutions, "design.candidate_solutions"),
      selected_solution: this.summarizeDesignField(input.selected_solution, "design.selected_solution"),
      tradeoffs: this.summarizeDesignField(input.tradeoffs, "design.tradeoffs"),
      risks: this.summarizeDesignField(input.risks, "design.risks"),
      test_strategy: this.summarizeDesignField(input.test_strategy, "design.test_strategy"),
      source_refs: this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs),
      metadata: input.metadata,
    }
    this.designRecords.push(design)
    this.write("semantic.design_record", design)
    return design
  }

  node(input: CausalNodeInput) {
    const node: CausalNode = {
      node_id: input.node_id ?? semanticID("node", this.causalNodes.length + 1),
      kind: input.kind,
      component: input.component,
      span_id: input.span_id,
      timestamp: nowIso(),
      time_ms: Math.max(0, Date.now() - this.startedAt),
      title: input.title,
      status: input.status,
      data: input.data === undefined ? undefined : this.summarizeCausalObject(input.data, `${input.kind}.data`),
      source_refs: input.source_refs ?? input.evidence_refs,
      artifact_refs: [],
      metadata: input.metadata,
    }
    node.artifact_refs = this.collectArtifactRefs(node.data)
    this.causalNodes.push(node)
    this.writeRecord("node", node)
    this.writePartial()
    return node
  }

  causalEdge(input: CausalEdgeInput) {
    const edge: CausalEdge = {
      edge_id: input.edge_id ?? semanticID("cedge", this.causalEdges.length + 1),
      from: input.from,
      to: input.to,
      relation: input.relation,
      label: input.label,
      metadata: input.metadata,
    }
    this.causalEdges.push(edge)
    this.writeRecord("edge", edge)
    this.writePartial()
    return edge
  }

  observation(input: ObservationInput) {
    const sourceRefs = input.source_refs ?? input.evidence_refs
    const node = this.node({
      kind: "observation",
      component: this.componentForObservationSource(input.source),
      span_id: input.span_id,
      title: input.category ?? input.source,
      status: "success",
      data: {
        source: input.source,
        category: input.category,
        summary: input.summary,
        data: input.data,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      metadata: input.metadata,
    })
    for (const ref of sourceRefs ?? []) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "node", id: node.node_id, label: "observation" },
        relation: parsed.type === "compaction" || parsed.id.startsWith("compaction") ? "compaction_to_observation" : "source_to_observation",
        label: "Observation produced from source record",
      })
    }
    return node
  }

  compaction(input: CompactionRecordInput) {
    const sourceRefs = input.source_refs ?? input.evidence_refs
    const node = this.node({
      kind: "context.compaction",
      component: "context",
      span_id: input.span_id,
      title: `${input.trigger} compaction`,
      status: input.result === "error" ? "error" : "success",
      data: {
        trigger: input.trigger,
        provider_id: input.provider_id,
        model_id: input.model_id,
        input_tokens: input.input_tokens,
        context_limit: input.context_limit,
        reserved_output_tokens: input.reserved_output_tokens,
        selected_head_messages: input.selected_head_messages,
        selected_tail_messages: input.selected_tail_messages,
        hidden_compaction_messages: input.hidden_compaction_messages,
        previous_summary: input.previous_summary,
        serialized_tail: input.serialized_tail,
        output_summary: input.output_summary,
        auto_continue: input.auto_continue,
        result: input.result,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      metadata: input.metadata,
    })
    for (const ref of sourceRefs ?? []) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "compaction", id: node.node_id, label: "context.compaction" },
        relation: "source_to_compaction",
        label: "Compaction consumed source context",
      })
    }
    return node
  }

  currentSourceRefs() {
    return [
      ...this.recentContextSnapshotIDs.slice(-2).map((id) => `context_snapshot:${id}`),
      ...this.recentToolSpanIDs.slice(-3).map((id) => `tool_span:${id}`),
      ...this.recentVerificationIDs.slice(-3).map((id) => `verification:${id}`),
      ...this.recentChangeIDs.slice(-3).map((id) => `change:${id}`),
    ].filter((item, index, array) => array.indexOf(item) === index)
  }

  finish(input?: FinishTraceInput) {
    if (this.finished) return
    this.evaluateConstraints()
    this.finished = true
    const error = input?.error ? errorInfo(input.error) : undefined
    if (error) this.errors.push(error)
    this.result = input?.result ?? this.result
    const summary = this.summary(input?.status ?? (error ? "error" : "success"))
    const provenance = this.provenanceSummary(summary.status)
    this.write("trace.finish", summary)
    this.writeRecord("finish", provenance.manifest)
    this.safeWrite(this.manifestFile, jsonPretty(provenance.manifest))
    this.safeWrite(this.provenanceTraceFile, jsonPretty(provenance))
    this.writePartial(true, provenance)
    this.safeWrite(this.viewerFile, renderProvenanceTraceHtml(provenance))
    this.safeWrite(this.traceFile, jsonPretty(summary))
    this.safeWrite(this.htmlFile, renderCaseTraceHtml(summary, { artifactDir: this.caseDir }))
  }

  private summary(status: TraceStatus): TraceSummary {
    const ended = Date.now()
    return {
      trace_version: "1.3",
      case_id: this.caseID,
      run_id: this.runID,
      session_id: this.sessionID,
      started_at: this.startedIso,
      ended_at: new Date(ended).toISOString(),
      duration_ms: Math.max(0, ended - this.startedAt),
      status,
      input: this.input,
      environment: this.environment,
      token_usage: this.tokenUsage,
      spans: [...this.spans.values()],
      events: this.events,
      artifacts: this.artifacts,
      errors: this.errors,
      result: this.result,
      context_snapshots: this.contextSnapshots,
      semantic_decisions: this.semanticDecisions,
      dataflow_edges: this.semanticEdges,
      verification_records: this.verificationRecords,
      change_records: this.changeRecords,
      constraint_records: this.constraintRecords,
      response_segments: this.responseSegments,
      design_records: this.designRecords,
    }
  }

  private manifest(status: TraceStatus): TraceManifest {
    const ended = Date.now()
    return {
      trace_version: "3.0",
      case_id: this.caseID,
      run_id: this.runID,
      session_id: this.sessionID,
      started_at: this.startedIso,
      ended_at: new Date(ended).toISOString(),
      duration_ms: Math.max(0, ended - this.startedAt),
      status,
      input: this.input,
      environment: this.environment,
      token_usage: this.tokenUsage,
      result: this.result,
      files: {
        provenance_trace: "provenance-trace.json",
        records: "records.jsonl",
        raw_events: "raw-events.jsonl",
        viewer: "viewer.html",
        partial_latest: "partial/latest.json",
      },
    }
  }

  private provenanceSummary(status: TraceStatus): ProvenanceTraceSummary {
    const manifest = this.manifest(status)
    return {
      trace_version: "3.0",
      manifest,
      records: this.provenanceRecords(),
      dataflow_edges: this.provenanceDataflowEdges(),
      artifacts: this.artifacts,
      metrics: {
        spans: this.spans.size,
        events: this.events.length,
        records: this.causalNodes.length,
        dataflow_edges: this.causalEdges.length,
        artifacts: this.artifacts.length,
        token_usage: this.tokenUsage,
      },
    }
  }

  private provenanceRecords(): ProvenanceRecord[] {
    return this.causalNodes.map((node) => ({
      record_id: node.node_id,
      component: node.component,
      event_type: node.kind === "final.claim" ? "response.output" : node.kind,
      span_id: node.span_id,
      timestamp: node.timestamp,
      time_ms: node.time_ms,
      title: node.title,
      status: node.status,
      duration_ms: optionalNumber(node.data?.duration_ms),
      token_usage: node.data?.token_usage as TraceTokenUsage | undefined,
      error: node.data?.error,
      source_refs: node.source_refs,
      artifact_refs: node.artifact_refs,
      data: node.data,
      metadata: node.metadata,
    }))
  }

  private provenanceDataflowEdges(): DataflowEdge[] {
    return this.causalEdges.map((edge) => ({
      edge_id: edge.edge_id,
      from: this.provenanceRef(edge.from),
      to: this.provenanceRef(edge.to),
      relation: this.provenanceRelation(edge.relation),
      label: this.provenanceLabel(edge.label),
      metadata: edge.metadata,
    }))
  }

  summarizeText(input: unknown, label = "text"): TraceFieldSummary {
    const text = redactText(String(input ?? ""))
    const summary: TraceFieldSummary = {
      type: "text",
      length: text.length,
      hash: hash(text),
      preview: text.slice(0, maxFieldLength()),
    }
    if (text.length <= maxFieldLength()) return summary
    const artifact = this.writeArtifact("text", label, text)
    return {
      ...summary,
      artifact_id: artifact.artifact_id,
    }
  }

  summarizeJson(input: unknown, label = "json"): TraceFieldSummary {
    if (input === null || typeof input !== "object") return summarizeScalar(input)
    const serialized = json(input)
    const keys = Array.isArray(input) ? undefined : Object.keys(input as Record<string, unknown>).slice(0, 50)
    const summary: TraceFieldSummary = {
      type: Array.isArray(input) ? "array" : "object",
      length: serialized.length,
      hash: hash(serialized),
      keys,
      preview: serialized.slice(0, maxFieldLength()),
    }
    if (serialized.length <= maxFieldLength()) return summary
    const artifact = this.writeArtifact("json", label, prettyJsonString(serialized))
    return {
      ...summary,
      artifact_id: artifact.artifact_id,
    }
  }

  private summarizeDesignField(input: unknown, label: string) {
    if (input === undefined) return undefined
    if (input === null || typeof input !== "object") return this.summarizeText(input, label)
    return this.summarizeJson(input, label)
  }

  private summarizeCausalObject(input: Record<string, unknown>, label: string): Record<string, unknown> {
    const output: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(input)) {
      output[key] = this.summarizeCausalValue(value, `${label}.${key}`)
    }
    return output
  }

  private summarizeCausalValue(input: unknown, label: string): unknown {
    if (input === undefined) return undefined
    if (input === null) return null
    if (typeof input === "string") {
      return input.length > maxFieldLength() ? this.summarizeText(input, this.causalArtifactLabel(label)) : input
    }
    if (typeof input === "number" || typeof input === "boolean") return input
    if (Array.isArray(input)) {
      const serialized = json(input)
      if (serialized.length > maxFieldLength()) return this.summarizeJson(input, this.causalArtifactLabel(label))
      return input.map((item, index) => this.summarizeCausalValue(item, `${label}.${index}`))
    }
    if (typeof input === "object") {
      const serialized = json(input)
      if (serialized.length > maxFieldLength()) return this.summarizeJson(input, this.causalArtifactLabel(label))
      return this.summarizeCausalObject(input as Record<string, unknown>, label)
    }
    return String(input)
  }

  private causalArtifactLabel(label: string) {
    if (/observation\.data\.summary$/.test(label) || /observation\.summary/.test(label)) return "observation.summary"
    if (/observation\.data/.test(label)) return "observation.data"
    if (/context\.compaction.*previous_summary/.test(label)) return "compaction.previous_summary"
    if (/context\.compaction.*serialized_tail/.test(label)) return "compaction.serialized_tail"
    if (/context\.compaction.*output_summary/.test(label)) return "compaction.output_summary"
    if (/context\.pack/.test(label)) return "context.pack"
    return label.replace(/\.data\./, ".")
  }

  private collectArtifactRefs(input: unknown): string[] {
    const refs = new Set<string>()
    const visit = (value: unknown) => {
      if (!value || typeof value !== "object") return
      const summary = value as Partial<TraceFieldSummary>
      if (typeof summary.artifact_id === "string") refs.add(summary.artifact_id)
      for (const child of Object.values(value as Record<string, unknown>)) visit(child)
    }
    visit(input)
    return [...refs]
  }

  private normalizeSourceRefs(input: string[] | undefined) {
    const provided = input ?? []
    const concreteProvided = provided.filter((item) => !item.startsWith("recent_"))
    if (provided.length && concreteProvided.length === provided.length) return concreteProvided
    return [...concreteProvided, ...this.currentSourceRefs()].filter(
      (item, index, array) => array.indexOf(item) === index,
    )
  }

  private remember(target: string[], id: string, limit = 12) {
    target.push(id)
    if (target.length > limit) target.splice(0, target.length - limit)
  }

  private evaluateConstraints() {
    for (const constraint of this.constraintRecords) {
      if (constraint.status !== "unknown") continue
      const text = constraint.constraint.toLowerCase()
      const sourceRefs = new Set(constraint.source_refs ?? [])
      if (/do not modify|read[- ]?only|只读|不修改|不要修改/.test(text)) {
        if (this.changeRecords.length) {
          constraint.status = "observed_violated"
          for (const change of this.changeRecords) sourceRefs.add(`change:${change.change_id}`)
        } else {
          constraint.status = "observed_satisfied"
        }
      } else if (/run verification tests|run tests|执行测试|运行测试/.test(text)) {
        const verifications = this.verificationRecords.filter((item) => isTestLikeCommand(item.command))
        constraint.status = verifications.length ? "observed_satisfied" : "observed_violated"
        for (const verification of verifications) sourceRefs.add(`verification:${verification.verification_id}`)
      } else if (/only make necessary changes|only necessary|minimal change|只改必要|最小修改/.test(text)) {
        if (!this.changeRecords.length) {
          constraint.status = "observed_satisfied"
        } else {
          const hasLinkedChange = this.changeRecords.some(
            (change) => (change.source_refs?.length ?? 0) > 0 || (change.verification_refs?.length ?? 0) > 0,
          )
          const hasVerification = this.verificationRecords.length > 0
          if (hasLinkedChange || hasVerification) constraint.status = "observed_satisfied"
        }
        for (const change of this.changeRecords) sourceRefs.add(`change:${change.change_id}`)
        for (const verification of this.verificationRecords) sourceRefs.add(`verification:${verification.verification_id}`)
      }
      constraint.source_refs = [...sourceRefs]
      this.write("semantic.constraint_evaluated", constraint)
    }
  }

  private nodeKindForSpan(component: TraceComponent): CausalNodeKind | undefined {
    if (component === "llm") return "llm.call"
    if (component === "tool") return "tool.call"
    if (component === "mcp") return "mcp.call"
    if (component === "skill") return "skill.load"
    if (component === "task") return "subagent.call"
    if (component === "runtime" || component === "prompt" || component === "processor") return "task.loop"
    return undefined
  }

  private componentForObservationSource(source: string): TraceComponent {
    if (/mcp/i.test(source)) return "mcp"
    if (/skill/i.test(source)) return "skill"
    if (/task|subagent/i.test(source)) return "task"
    if (/compaction|context/i.test(source)) return "context"
    if (/verification|change|tool|file|grep|bash|read|edit/i.test(source)) return "tool"
    return "result"
  }

  private parseSourceRef(ref: string): TraceRef | undefined {
    const index = ref.indexOf(":")
    if (index === -1) return undefined
    return {
      type: ref.slice(0, index),
      id: ref.slice(index + 1),
    }
  }

  private linkSourceToResponse(ref: string, responseNodeID: string) {
    const parsed = this.parseSourceRef(ref)
    if (!parsed) return
    this.causalEdge({
      from: parsed,
      to: { type: "node", id: responseNodeID, label: "response.output" },
      relation: "source_to_response",
      label: "Response output consumed source record",
    })
  }

  private provenanceRef(ref: TraceRef): TraceRef {
    const type = ref.type === "final_response_evidence" ? "response_segment" : ref.type
    const label = ref.label === "final.claim" ? "response.output" : ref.label
    return {
      ...ref,
      type,
      label,
    }
  }

  private provenanceRelation(relation: string): DataflowEdge["relation"] {
    if (relation === "context_to_llm") return "selected_into_context"
    if (relation === "compaction_to_context") return "compressed_to"
    if (relation === "source_to_compaction" || relation === "evidence_to_compaction") return "compressed_from"
    if (relation === "context_to_compaction_summary") return "compressed_to"
    if (relation === "llm_to_tool") return "prompted"
    if (relation === "source_to_response" || /_to_claim$/.test(relation)) return "consumed"
    if (relation === "source_to_observation" || relation === "evidence_to_observation" || relation === "tool_to_observation" || relation === "compaction_to_observation") return "produced"
    if (relation === "processor_to_final_response") return "produced"
    if (relation === "final_response_to_design_record") return "produced"
    if (relation === "change_to_verification" || relation === "failure_to_change") return "continued_from"
    return relation
  }

  private provenanceLabel(label: string | undefined) {
    if (!label) return undefined
    return label
      .replace(/final response evidence/gi, "response output")
      .replace(/final claim/gi, "response output")
      .replace(/claim/gi, "response")
      .replace(/evidence/gi, "source record")
  }

  private writeArtifact(kind: TraceArtifact["kind"], label: string, content: string): TraceArtifact {
    const redacted = redactText(content)
    const contentHash = hash(redacted)
    const dedupeKey = `${kind}:${contentHash}`
    const existing = this.artifactByDedupeKey.get(dedupeKey)
    if (existing) {
      existing.occurrences = (existing.occurrences ?? 1) + 1
      this.writeRecord("artifact.reuse", existing)
      this.writePartial()
      return existing
    }
    const artifactID = `artifact_${++this.artifactSequence}_${contentHash}`
    const safeLabel = (label || kind).replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 80)
    const filename = `${contentHash}_${safeLabel}.${kind === "json" ? "json" : "txt"}`
    const relativePath = `artifacts/sha256/${filename}`
    const artifact: TraceArtifact = {
      artifact_id: artifactID,
      kind,
      label,
      path: relativePath,
      length: redacted.length,
      hash: contentHash,
      preview: redacted.slice(0, maxFieldLength()),
      created_at: nowIso(),
      dedupe_key: dedupeKey,
      occurrences: 1,
    }
    this.artifacts.push(artifact)
    this.artifactByDedupeKey.set(dedupeKey, artifact)
    try {
      fs.mkdirSync(path.dirname(path.join(this.caseDir, relativePath)), { recursive: true })
      fs.writeFileSync(path.join(this.caseDir, relativePath), redacted)
      this.write("artifact.write", artifact)
      this.writeRecord("artifact", artifact)
    } catch {}
    return artifact
  }

  private open() {
    try {
      fs.mkdirSync(this.caseDir, { recursive: true })
      fs.mkdirSync(this.partialDir, { recursive: true })
      fs.writeFileSync(this.eventsFile, "")
      fs.writeFileSync(this.rawEventsFile, "")
      fs.writeFileSync(this.recordsFile, "")
      this.write("trace.start", {
        trace_version: "1.3",
        case_id: this.caseID,
        run_id: this.runID,
        started_at: this.startedIso,
        root_dir: this.rootDir,
        case_dir: this.caseDir,
        input: this.input,
        environment: this.environment,
      })
      this.node({
        kind: "run.start",
        component: "run",
        title: this.caseID,
        status: "running",
        data: {
          case_id: this.caseID,
          run_id: this.runID,
          input: this.input,
          environment: this.environment,
        },
      })
      this.writePartial(true)
    } catch {
      this.writable = false
    }
  }

  private write(type: string, data: unknown) {
    if (!this.writable) return
    if (this.finished && type !== "trace.finish") return
    try {
      fs.appendFileSync(
        this.eventsFile,
        json({
          time: nowIso(),
          type,
          data,
        }) + "\n",
      )
      fs.appendFileSync(
        this.rawEventsFile,
        json({
          time: nowIso(),
          type,
          data,
        }) + "\n",
      )
    } catch {}
  }

  private writeRecord(recordType: string, data: unknown) {
    if (!this.writable) return
    try {
      fs.appendFileSync(
        this.recordsFile,
        json({
          time: nowIso(),
          record_type: recordType,
          data,
        }) + "\n",
      )
    } catch {}
  }

  private writePartial(force = false, summary?: ProvenanceTraceSummary) {
    if (!this.writable) return
    const now = Date.now()
    const interval = safeNumber(process.env.OPENCODE_CASE_TRACE_PARTIAL_INTERVAL_MS || 5000) || 5000
    if (!force && now < this.nextPartialWrite) return
    this.nextPartialWrite = now + interval
    this.safeWrite(this.partialFile, jsonPretty(summary ?? this.provenanceSummary("running")))
  }

  private safeWrite(target: string, content: string) {
    try {
      fs.mkdirSync(path.dirname(target), { recursive: true })
      fs.writeFileSync(target, content)
    } catch {}
  }
}

function jsonPretty(input: unknown) {
  const seen = new WeakSet<object>()
  return JSON.stringify(
    input,
    (key, value) => {
      if (isSensitiveKey(key)) return "[REDACTED]"
      if (typeof value === "bigint") return String(value)
      if (typeof value === "string") return redactText(value)
      if (value && typeof value === "object") {
        if (seen.has(value)) return "[Circular]"
        seen.add(value)
      }
      return value
    },
    2,
  )
}

function prettyJsonString(input: string) {
  try {
    return JSON.stringify(JSON.parse(input), null, 2)
  } catch {
    return input
  }
}

const summarizeTextField = summarizeText
const summarizeJsonField = summarizeJson

function finishActiveFromProcessExit(code: number | undefined) {
  const current = active || undefined
  if (!current) return
  current.finish({
    status: code && code !== 0 ? "error" : "success",
    result: {
      exit_code: code ?? process.exitCode ?? 0,
      reason: "process.exit",
    },
  })
  active = false
}

function finishActiveFromSignal(signal: NodeJS.Signals) {
  const current = active || undefined
  if (!current) return
  current.finish({
    status: "cancelled",
    result: {
      reason: signal,
      signal,
    },
  })
  active = false
}

function finishActiveFromError(reason: string, error: unknown) {
  const current = active || undefined
  if (!current) return
  current.finish({
    status: "error",
    error,
    result: {
      reason,
    },
  })
  active = false
}

function installProcessFinalizer() {
  if (processFinalizerInstalled) return
  processFinalizerInstalled = true
  process.once("beforeExit", (code) => finishActiveFromProcessExit(code))
  process.once("exit", (code) => finishActiveFromProcessExit(code))
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as NodeJS.Signals[]) {
    process.once(signal, () => {
      finishActiveFromSignal(signal)
      const code = signal === "SIGINT" ? 130 : signal === "SIGTERM" ? 143 : 129
      process.exit(code)
    })
  }
  process.once("uncaughtException", (error) => {
    finishActiveFromError("uncaughtException", error)
    process.exit(1)
  })
  process.once("unhandledRejection", (error) => {
    finishActiveFromError("unhandledRejection", error)
    process.exit(1)
  })
}

export namespace CaseTrace {
  export function isEnabled() {
    return enabledFromEnv()
  }

  export function configure(input: CaseTraceConfig = {}) {
    if (!enabledFromEnv()) {
      active = false
      return undefined
    }
    const caseID = safeCaseID(input.caseID ?? process.env.OPENCODE_CASE_ID ?? "")
    if (active && active.caseID === caseID) {
      if (input.input) active.setInput(input.input)
      if (input.environment) active.setEnvironment(input.environment)
      return active
    }
    if (active) active.finish({ status: "cancelled", result: { reason: "reconfigured" } })
    active = new ActiveCaseTrace({ ...input, caseID })
    installProcessFinalizer()
    return active || undefined
  }

  export function get() {
    if (active === undefined && enabledFromEnv()) return configure()
    return active || undefined
  }

  export function setSessionID(sessionID: string | undefined) {
    get()?.setSessionID(sessionID)
  }

  export function event(input: TraceEventInput) {
    get()?.event(input)
  }

  export function usage(input: unknown, spanID?: string) {
    get()?.usage(input, spanID)
  }

  export function contextSnapshot(input: ContextSnapshotInput) {
    return get()?.contextSnapshot(input)
  }

  export function decision(input: SemanticDecisionInput) {
    return get()?.decision(input)
  }

  export function edge(input: SemanticEdgeInput) {
    return get()?.edge(input)
  }

  export function verification(input: VerificationRecordInput) {
    return get()?.verification(input)
  }

  export function change(input: ChangeRecordInput) {
    return get()?.change(input)
  }

  export function constraint(input: ConstraintRecordInput) {
    return get()?.constraint(input)
  }

  export function finalEvidence(input: FinalResponseEvidenceInput) {
    return get()?.finalEvidence(input)
  }

  export function responseOutput(input: ResponseOutputInput) {
    return get()?.responseOutput(input)
  }

  export function designRecord(input: DesignRecordInput) {
    return get()?.designRecord(input)
  }

  export function node(input: CausalNodeInput) {
    return get()?.node(input)
  }

  export function causalEdge(input: CausalEdgeInput) {
    return get()?.causalEdge(input)
  }

  export function observation(input: ObservationInput) {
    return get()?.observation(input)
  }

  export function compaction(input: CompactionRecordInput) {
    return get()?.compaction(input)
  }

  export function currentEvidenceRefs() {
    return get()?.currentSourceRefs() ?? []
  }

  export function currentSourceRefs() {
    return get()?.currentSourceRefs() ?? []
  }

  export function finish(input?: FinishTraceInput) {
    get()?.finish(input)
    active = false
  }

  export function summarizeText(input: unknown) {
    return active ? active.summarizeText(input) : summarizeTextField(input)
  }

  export function summarizeJson(input: unknown) {
    return active ? active.summarizeJson(input) : summarizeJsonField(input)
  }
}
