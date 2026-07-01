import crypto from "crypto"
import fs from "fs"
import path from "path"
import { Global } from "@opencode-ai/core/global"
import { renderProvenanceTraceHtml } from "./causal-trace-viewer"
import {
  TRACE_VERSION,
  isFormalRecordType,
  normalizeRelation,
  shouldPromoteRuntimeEvent,
} from "./trace-semantic-contract"

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

export type TraceSourceLocation = {
  uri?: string
  path?: string
  line_start?: number
  line_end?: number
  content_hash?: string
  snippet_preview?: string
}

export type TraceContextLedger = {
  token_estimate_before?: number
  token_estimate_after?: number
  retained_message_ids?: string[]
  dropped_message_ids?: string[]
  retained_fact_refs?: string[]
  dropped_fact_refs?: string[]
  summary_artifact_id?: string
  auto_continue_prompt_ref?: string
  quality_flags?: string[]
  algorithm?: string
  ledger_id_quality?: "concrete" | "estimated" | "unknown" | string
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
  context_ledger?: TraceContextLedger
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
  response_role?: "final_answer" | "intermediate_summary" | "subagent_result" | "auto_continue_summary" | string
  visibility?: "user_visible" | "internal_continue" | "compaction_followup" | "debug" | string
  turn_index?: number
  is_final_for_case?: boolean
  direct_evidence_refs?: string[]
  context_refs?: string[]
  execution_refs?: string[]
  source_refs?: string[]
  source_locations?: TraceSourceLocation[]
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

export type TraceLlmTurnRecord = {
  turn_id: string
  span_id?: string
  session_id?: string
  parent_session_id?: string
  message_id?: string
  agent?: string
  agent_role?: "main" | "subagent" | "compaction" | "title" | "background" | string
  provider_id?: string
  model_id?: string
  input_context_refs?: string[]
  prompt_transform_refs?: string[]
  tool_schema_ref?: string
  request_id?: string
  status?: TraceStatus
  duration_ms?: number
  finish_reason?: string
  stop_reason?: string
  token_usage?: TraceTokenUsage
  is_background?: boolean
  source_refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceAgentLifecycleRecord = {
  lifecycle_id: string
  span_id?: string
  session_id?: string
  message_id?: string
  agent?: string
  phase: string
  status?: TraceStatus | TraceVerificationRecord["status"]
  summary?: TraceFieldSummary
  source_refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceExitGateRecord = {
  gate_id: string
  span_id?: string
  session_id?: string
  message_id?: string
  has_final_answer?: boolean
  needs_compaction?: boolean
  auto_continue?: boolean
  synthetic_continue?: boolean
  continuation_source?: "user" | "compaction" | "system" | "none" | string
  decision: "continue" | "exit" | "wait" | "cancel" | string
  reason?: string
  source_refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceEvidenceFact = {
  fact_id: string
  span_id?: string
  source: string
  category?: string
  summary: TraceFieldSummary
  data?: TraceFieldSummary
  fact_kind?: string
  canonical_subject?: string
  claim?: string
  structured_claim?: TraceStructuredClaim
  support_level?: "direct" | "context" | "execution" | "weak" | string
  quality_flags?: string[]
  confidence?: "observed" | "inferred" | string
  source_refs?: string[]
  source_locations?: TraceSourceLocation[]
  metadata?: Record<string, unknown>
}

export type TraceStructuredClaim = {
  subject?: string
  predicate?: string
  value?: string | number | boolean | null
  qualifier?: string
  source_span?: TraceSourceLocation
  extraction_method:
    | "explicit_structured_data"
    | "mcp_json_text"
    | "source_location_fields"
    | "verification_output"
    | "summary_sentence"
    | "fallback_summary"
    | string
  raw_artifact_ref?: string
}

export type TraceResponseClaimRecord = {
  claim_id: string
  response_segment_id?: string
  text: TraceFieldSummary
  claim_index: number
  direct_evidence_refs: string[]
  context_refs: string[]
  execution_refs: string[]
  legacy_context_refs?: string[]
  matched_evidence_refs?: string[]
  candidate_evidence_refs?: string[]
  match_strategy?: string
  match_score?: number
  match_reasons?: string[]
  original_direct_evidence_refs?: string[]
  source_refs?: string[]
  source_locations?: TraceSourceLocation[]
  support_level: "direct" | "contextual" | "execution_only" | "unsupported" | string
  quality_flags: string[]
  metadata?: Record<string, unknown>
}

export type TraceCompactionCheckRecord = {
  check_id: string
  span_id?: string
  session_id?: string
  message_id?: string
  provider_id?: string
  model_id?: string
  token_usage?: TraceTokenUsage
  token_estimate?: number
  context_limit?: number
  reserved_tokens?: number
  overflow: boolean
  selected_algorithm?: string
  trigger_reason: string
  source_refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceHealthIssue = {
  kind: string
  severity: "info" | "warning" | "error"
  message: string
  record_id?: string
  event_type?: string
  count?: number
  refs?: string[]
  metadata?: Record<string, unknown>
}

export type TraceHealthMetrics = {
  circular_reference_markers: number
  open_records: number
  finalized_open_records: number
  expected_lifecycle_finalized_records?: number
  unexpected_missing_close_records?: number
  llm_turns_missing_token_usage: number
  llm_turns_missing_finish_reason: number
  compaction_quality_flags: Record<string, number>
  empty_subagent_results: number
  broad_response_refs: number
  duplicate_evidence_facts: number
  generic_evidence_facts?: number
  unsupported_response_claims?: number
  context_only_response_claims?: number
  payload_duplication_groups?: number
  compaction_check_missing?: number
  broken_claim_fragments?: number
  over_attributed_claims?: number
  generic_mcp_facts?: number
  path_only_evidence_facts?: number
  non_final_response_claims?: number
  weak_evidence_matches?: number
  mcp_json_parse_shadowed?: number
  skill_request_unresolved?: number
  background_llm_turns?: number
  legacy_context_ref_claims?: number
  issues: TraceHealthIssue[]
}

export type CausalNodeKind =
  | "run.start"
  | "task.loop"
  | "prompt.assembly"
  | "context.transform"
  | "context.pack"
  | "context.compaction_check"
  | "context.compaction"
  | "llm.call"
  | "llm.turn"
  | "agent.lifecycle"
  | "exit.gate"
  | "decision"
  | "tool.call"
  | "mcp.call"
  | "skill.load"
  | "subagent.call"
  | "loop.decision"
  | "observation"
  | "evidence.fact"
  | "change"
  | "verification"
  | "response.output"
  | "response.claim"

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
  source_locations?: TraceSourceLocation[]
  typed_resources?: Record<string, unknown>[]
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
  trace_version: typeof TRACE_VERSION
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
    trace: string
    legacy_trace: string
    provenance_trace?: string
    trace_html: string
    records: string
    raw_events: string
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
  source_locations?: TraceSourceLocation[]
  typed_resources?: Record<string, unknown>[]
  artifact_refs?: string[]
  data?: Record<string, unknown>
  metadata?: Record<string, unknown>
}

export type DataflowEdge = {
  edge_id: string
  from: TraceRef
  to: TraceRef
  relation:
    | "selected_into_context"
    | "prompted"
    | "produced"
    | "consumed"
    | "compressed_from"
    | "compressed_to"
    | "spawned"
    | "continued_from"
    | "derived_from"
    | "verified_by"
    | "modified_by"
    | "failed_before"
    | "read_from"
    | "returned_by"
    | "submitted"
    | "assembled"
    | "transformed_to"
    | "resolved_to"
    | "used_as_context"
    | "selected_by"
    | "called"
    | "returned_to"
    | "delegated_to"
    | "reported_to"
    | "supported_response"
    | "claimed_by"
    | "supports_claim"
    | "contextualizes_claim"
    | "executed_for_claim"
  label?: string
  metadata?: Record<string, unknown>
}

export type ProvenanceTraceSummary = {
  trace_version: typeof TRACE_VERSION
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
    trace_health: TraceHealthMetrics
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
  context_ledger?: TraceContextLedger
  metadata?: Record<string, unknown>
}

type SemanticDecisionInput = Omit<TraceSemanticDecision, "decision_id" | "rationale" | "metadata" | "source_refs"> & {
  decision_id?: string
  rationale?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
  source_locations?: TraceSourceLocation[]
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

type ResponseOutputInput = Omit<TraceResponseSegment, "segment_id" | "text" | "source_refs" | "source_locations"> & {
  segment_id?: string
  text: unknown
  response_role?: TraceResponseSegment["response_role"]
  source_refs?: string[]
  source_locations?: TraceSourceLocation[]
  evidence_refs?: string[]
}

type ResponseClaimInput = Omit<
  TraceResponseClaimRecord,
  | "claim_id"
  | "text"
  | "direct_evidence_refs"
  | "context_refs"
  | "execution_refs"
  | "source_refs"
  | "source_locations"
  | "support_level"
  | "quality_flags"
> & {
  claim_id?: string
  text: unknown
  source_refs?: string[]
  source_locations?: TraceSourceLocation[]
  evidence_refs?: string[]
  support_level?: TraceResponseClaimRecord["support_level"]
  quality_flags?: string[]
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

type LlmTurnInput = Omit<TraceLlmTurnRecord, "turn_id" | "token_usage" | "source_refs"> & {
  turn_id?: string
  token_usage?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
}

type AgentLifecycleInput = Omit<TraceAgentLifecycleRecord, "lifecycle_id" | "summary" | "source_refs"> & {
  lifecycle_id?: string
  summary?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
}

type ExitGateInput = Omit<TraceExitGateRecord, "gate_id" | "source_refs"> & {
  gate_id?: string
  source_refs?: string[]
  evidence_refs?: string[]
}

type EvidenceFactInput = Omit<TraceEvidenceFact, "fact_id" | "summary" | "data" | "source_refs"> & {
  fact_id?: string
  summary: unknown
  data?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
}

type CausalNodeInput = Omit<
  CausalNode,
  "node_id" | "timestamp" | "time_ms" | "data" | "artifact_refs" | "source_refs"
> & {
  node_id?: string
  data?: Record<string, unknown>
  source_refs?: string[]
  source_locations?: TraceSourceLocation[]
  typed_resources?: Record<string, unknown>[]
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
  source_locations?: TraceSourceLocation[]
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
  context_ledger?: TraceContextLedger
  after_context_refs?: string[]
  span_id?: string
  source_refs?: string[]
  evidence_refs?: string[]
  metadata?: Record<string, unknown>
}

type CompactionCheckInput = Omit<
  TraceCompactionCheckRecord,
  "check_id" | "token_usage" | "source_refs" | "metadata"
> & {
  check_id?: string
  token_usage?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
  metadata?: Record<string, unknown>
}

type PromptAssemblyStage =
  | "initial_user_request"
  | "resolved_parts"
  | "user_message_created"
  | "subagent_prompt"
  | "command_prompt"
  | (string & {})

type PromptAssemblyInput = {
  stage: PromptAssemblyStage
  session_id?: string
  message_id?: string
  agent?: string
  model?: unknown
  input?: unknown
  output?: unknown
  parts?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
  source_locations?: TraceSourceLocation[]
  metadata?: Record<string, unknown>
}

type ContextTransformStage =
  | "session_messages_before_plugin"
  | "session_messages_after_plugin"
  | "model_messages_built"
  | "llm_request_ready"
  | "provider_message_transform"
  | "compaction_prompt_built"
  | (string & {})

type ContextTransformInput = {
  stage: ContextTransformStage
  session_id?: string
  message_id?: string
  step?: number
  agent?: string
  provider_id?: string
  model_id?: string
  input?: unknown
  output?: unknown
  transforms?: unknown
  source_refs?: string[]
  evidence_refs?: string[]
  source_locations?: TraceSourceLocation[]
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

function sanitizeForJson(input: unknown, key = "", stack = new WeakSet<object>()): unknown {
  if (isSensitiveKey(key)) return "[REDACTED]"
  if (typeof input === "bigint") return String(input)
  if (typeof input === "function") return `[Function ${input.name || "anonymous"}]`
  if (input instanceof Error) return errorInfo(input)
  if (input instanceof URL) return input.toString()
  if (typeof input === "string") return redactText(input)
  if (!input || typeof input !== "object") return input
  if (stack.has(input)) return "[Circular]"
  stack.add(input)
  try {
    if (Array.isArray(input)) return input.map((item, index) => sanitizeForJson(item, String(index), stack))
    const output: Record<string, unknown> = {}
    for (const [childKey, value] of Object.entries(input as Record<string, unknown>)) {
      output[childKey] = sanitizeForJson(value, childKey, stack)
    }
    return output
  } finally {
    stack.delete(input)
  }
}

function json(input: unknown) {
  return JSON.stringify(sanitizeForJson(input), undefined, 0)
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

function cloneTokenUsage(input: TraceTokenUsage | undefined): TraceTokenUsage | undefined {
  if (!input) return undefined
  return {
    input: safeNumber(input.input),
    output: safeNumber(input.output),
    reasoning: safeNumber(input.reasoning),
    cached_input: safeNumber(input.cached_input),
    cache_write: safeNumber(input.cache_write),
    total: safeNumber(input.total),
    cost: safeNumber(input.cost),
  }
}

function stringField(input: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = input[key]
    if (typeof value === "string" && value.trim()) return value
  }
  return undefined
}

function numberField(input: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = optionalNumber(input[key])
    if (value !== undefined) return value
  }
  return undefined
}

function booleanField(input: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = input[key]
    if (typeof value === "boolean") return value
  }
  return undefined
}

function stringArrayField(input: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = input[key]
    if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string")
  }
  return undefined
}

function loopDecisionFromRuntimeEvent(
  component: TraceComponent,
  eventType: string,
  data: unknown,
  state: { has_user_visible_response: boolean; has_final_answer: boolean },
) {
  if (component !== "processor") return undefined
  if (
    !/step\.finish|finish\.step|loop\.decision|loop\.finish|message\.finish|message\.completed|process\.result/.test(
      eventType,
    )
  )
    return undefined
  if (!data || typeof data !== "object" || Array.isArray(data)) return undefined
  const record = data as Record<string, unknown>
  const reason = stringField(record, ["reason", "stop_reason", "finish_reason"]) ?? eventType
  const syntheticContinue =
    booleanField(record, ["synthetic_continue", "synthetic", "auto"]) ??
    stringField(record, ["synthetic_continue"]) === "true"
  const compactionContinue =
    booleanField(record, ["compaction_continue"]) ??
    (record.metadata && typeof record.metadata === "object" && !Array.isArray(record.metadata)
      ? booleanField(record.metadata as Record<string, unknown>, ["compaction_continue"])
      : undefined) ??
    false
  const decision = (() => {
    if (compactionContinue || syntheticContinue) return "auto_continue"
    if (/stop|complete|done|end/i.test(reason)) return "stop"
    if (/tool|continue|next/i.test(reason)) return "continue"
    return "unknown"
  })()
  return {
    decision,
    reason,
    agent: stringField(record, ["agent"]),
    message_id: stringField(record, ["message_id", "messageID", "id"]),
    part_count: numberField(record, ["part_count", "partCount"]),
    part_types: stringArrayField(record, ["part_types", "partTypes"]),
    has_user_visible_response: state.has_user_visible_response,
    has_final_answer: state.has_final_answer,
    synthetic_continue: Boolean(syntheticContinue),
    compaction_continue: Boolean(compactionContinue),
    runtime_refs: {
      session_id: stringField(record, ["session_id", "sessionID"]),
      message_id: stringField(record, ["message_id", "messageID", "id"]),
    },
  }
}

function normalizeSourcePath(input: string) {
  const trimmed = input
    .trim()
    .replace(/^["'(<]+/, "")
    .replace(/[)"'>,，。；;]+$/, "")
  if (!trimmed) return undefined
  if (/[`*]/.test(trimmed)) return undefined
  if (/\s/.test(trimmed)) return undefined
  if (/^[a-z]+:\/\//i.test(trimmed)) return trimmed
  return trimmed.replace(/^file:\/\//, "")
}

function isStandaloneSourcePath(input: string) {
  const sourcePath = normalizeSourcePath(input)
  if (!sourcePath) return false
  if (/^[a-z]+:\/\//i.test(sourcePath)) return true
  return /^(?:\.{0,2}\/|\/|~\/|[\w@+.-]+\/)?[\w@+.-]+\.(?:mjs|js|ts|tsx|jsx|json|md|txt|py|go|rs|java|c|cc|cpp|h|hpp|swift|kt|sh|yaml|yml)$/i.test(
    sourcePath,
  )
}

function sourceLocationFromRecord(input: Record<string, unknown>): TraceSourceLocation | undefined {
  const candidate = stringField(input, ["path", "file", "filePath", "filepath", "filename", "uri", "url"])
  if (!candidate) return undefined
  const sourcePath = normalizeSourcePath(candidate)
  if (!sourcePath) return undefined
  const snippet = stringField(input, ["snippet", "snippet_preview", "preview", "content", "text"])
  return {
    uri: /^[a-z]+:\/\//i.test(sourcePath) ? sourcePath : undefined,
    path: /^[a-z]+:\/\//i.test(sourcePath) ? undefined : sourcePath,
    line_start: numberField(input, ["line_start", "lineStart", "startLine", "start", "line", "line_number"]),
    line_end: numberField(input, ["line_end", "lineEnd", "endLine", "end"]),
    content_hash: snippet ? `sha256:${hash(snippet)}` : undefined,
    snippet_preview: snippet?.slice(0, 240),
  }
}

function sourceLocationsFromText(input: string): TraceSourceLocation[] {
  const output: TraceSourceLocation[] = []
  const pathPattern =
    /((?:file:\/\/)?(?:[./~\w@+-][^\s:'")\]]*\/)?[^\s:'")\]]+\.(?:mjs|js|ts|tsx|jsx|json|md|txt|py|go|rs|java|c|cc|cpp|h|hpp|swift|kt|sh|yaml|yml))(?::(\d+))?(?::(\d+))?/gi
  for (const match of input.matchAll(pathPattern)) {
    const raw = match[1]
    if (!raw) continue
    const sourcePath = normalizeSourcePath(raw)
    if (!sourcePath) continue
    output.push({
      uri: /^[a-z]+:\/\//i.test(sourcePath) ? sourcePath : undefined,
      path: /^[a-z]+:\/\//i.test(sourcePath) ? undefined : sourcePath,
      line_start: optionalNumber(match[2]),
      line_end: optionalNumber(match[2]),
      snippet_preview: lineExcerpt(input, match.index ?? 0),
    })
    if (output.length >= 40) break
  }
  return dedupeSourceLocations(output)
}

function lineExcerpt(input: string, index: number) {
  const start = input.lastIndexOf("\n", Math.max(0, index - 1)) + 1
  const endIndex = input.indexOf("\n", index)
  const end = endIndex === -1 ? input.length : endIndex
  return input.slice(start, end).trim().slice(0, 240) || undefined
}

function collectSourceLocations(input: unknown, output: TraceSourceLocation[] = [], depth = 0): TraceSourceLocation[] {
  if (depth > 4 || input === undefined || input === null) return output
  if (typeof input === "string") {
    output.push(...sourceLocationsFromText(input))
    const sourcePath = normalizeSourcePath(input)
    if (sourcePath && isStandaloneSourcePath(input)) {
      output.push({
        uri: /^[a-z]+:\/\//i.test(sourcePath) ? sourcePath : undefined,
        path: /^[a-z]+:\/\//i.test(sourcePath) ? undefined : sourcePath,
      })
    }
    return output
  }
  if (Array.isArray(input)) {
    for (const item of input.slice(0, 40)) collectSourceLocations(item, output, depth + 1)
    return dedupeSourceLocations(output)
  }
  if (typeof input === "object") {
    const record = input as Record<string, unknown>
    const location = sourceLocationFromRecord(record)
    if (location) output.push(location)
    for (const key of ["files", "locations", "source_locations", "metadata", "data", "args", "content"]) {
      if (record[key] !== undefined) collectSourceLocations(record[key], output, depth + 1)
    }
  }
  return dedupeSourceLocations(output)
}

function dedupeSourceLocations(input: TraceSourceLocation[]) {
  const seen = new Set<string>()
  return input.filter((location) => {
    const key = [location.uri, location.path, location.line_start, location.line_end].join(":")
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function traceContextLedger(input: {
  input_tokens?: number
  context_limit?: number
  selected_head_messages?: number
  selected_tail_messages?: number
  hidden_compaction_messages?: number
  output_summary?: unknown
  auto_continue?: boolean
  context_ledger?: TraceContextLedger
  metadata?: Record<string, unknown>
}): TraceContextLedger {
  const metadata = input.metadata ?? {}
  const retainedCount = safeNumber(input.selected_head_messages) + safeNumber(input.selected_tail_messages)
  const droppedCount = safeNumber(input.hidden_compaction_messages)
  const hasConcreteIDs = Boolean(
    input.context_ledger?.retained_message_ids?.length ||
      input.context_ledger?.dropped_message_ids?.length ||
      input.context_ledger?.retained_fact_refs?.length ||
      input.context_ledger?.dropped_fact_refs?.length,
  )
  const hasEstimatedIDs = retainedCount > 0 || droppedCount > 0
  const qualityFlags = new Set<string>(input.context_ledger?.quality_flags ?? [])
  if (!input.input_tokens && !input.context_ledger?.token_estimate_before) qualityFlags.add("missing_token_estimate")
  if (!hasConcreteIDs) qualityFlags.add("message_ids_estimated_or_missing")
  if (input.auto_continue) qualityFlags.add("auto_continue_enabled")
  if (!input.context_ledger?.summary_artifact_id && input.output_summary !== undefined)
    qualityFlags.add("summary_artifact_pending")
  return {
    algorithm:
      stringField(metadata, ["algorithm", "compaction_algorithm"]) ??
      input.context_ledger?.algorithm ??
      "head-tail-summary",
    token_estimate_before: input.context_ledger?.token_estimate_before ?? input.input_tokens,
    token_estimate_after:
      input.context_ledger?.token_estimate_after ??
      (input.context_limit === undefined
        ? undefined
        : Math.min(safeNumber(input.input_tokens), safeNumber(input.context_limit))),
    retained_message_ids:
      input.context_ledger?.retained_message_ids ??
      Array.from({ length: retainedCount }, (_, index) => `retained:${index + 1}`),
    dropped_message_ids:
      input.context_ledger?.dropped_message_ids ??
      Array.from({ length: droppedCount }, (_, index) => `dropped:${index + 1}`),
    retained_fact_refs: input.context_ledger?.retained_fact_refs ?? [],
    dropped_fact_refs: input.context_ledger?.dropped_fact_refs ?? [],
    summary_artifact_id: input.context_ledger?.summary_artifact_id,
    auto_continue_prompt_ref:
      input.context_ledger?.auto_continue_prompt_ref ?? stringField(metadata, ["auto_continue_prompt_ref"]),
    quality_flags: [...qualityFlags],
    ledger_id_quality:
      input.context_ledger?.ledger_id_quality ??
      (hasConcreteIDs ? "concrete" : hasEstimatedIDs ? "estimated" : "unknown"),
  }
}

function childTraceDir(caseDir: string, childSessionID: string) {
  return path.join(caseDir, "subtraces", childSessionID)
}

function childTraceRelativeDir(childSessionID: string) {
  return `subtraces/${childSessionID}`
}

function childTraceAvailable(caseDir: string, childSessionID: string) {
  return fs.existsSync(path.join(childTraceDir(caseDir, childSessionID), "trace.json"))
}

function enrichSubagentOutput(output: unknown, parentRecordID: string, caseDir: string): unknown {
  if (!output || typeof output !== "object" || Array.isArray(output)) return output
  const record = output as Record<string, unknown>
  const childSessionID = stringField(record, ["child_session_id", "task_id", "session_id"])
  if (!childSessionID) return output
  const available = childTraceAvailable(caseDir, childSessionID)
  return {
    ...record,
    trace_ref: {
      parent_record_id: parentRecordID,
      child_session_id: childSessionID,
      child_status: "success",
      child_trace_available: available,
      ...(available ? { child_trace_dir: childTraceRelativeDir(childSessionID) } : {}),
    },
  }
}

function subagentTraceRef(output: unknown, parentRecordID: string, caseDir: string) {
  if (!output || typeof output !== "object" || Array.isArray(output)) return undefined
  const record = output as Record<string, unknown>
  const childSessionID = stringField(record, ["child_session_id", "task_id", "session_id"])
  if (!childSessionID) return undefined
  const available = childTraceAvailable(caseDir, childSessionID)
  return {
    parent_record_id: parentRecordID,
    child_session_id: childSessionID,
    child_status: stringField(record, ["child_status", "status"]) ?? "success",
    child_trace_available: available,
    ...(available ? { child_trace_dir: childTraceRelativeDir(childSessionID) } : {}),
  }
}

function parsedJsonObject(input: string): Record<string, unknown> | undefined {
  try {
    const parsed = JSON.parse(input)
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>
  } catch {
    return undefined
  }
}

function mcpContentItems(data: unknown): unknown[] {
  if (!data || typeof data !== "object" || Array.isArray(data)) return []
  const record = data as Record<string, unknown>
  if (Array.isArray(record.content)) return record.content
  const output = record.output
  if (
    output &&
    typeof output === "object" &&
    !Array.isArray(output) &&
    Array.isArray((output as Record<string, unknown>).content)
  ) {
    return (output as Record<string, unknown>).content as unknown[]
  }
  return []
}

function mcpSemanticExtras(data: unknown): {
  typed_resources: Record<string, unknown>[]
  source_locations: TraceSourceLocation[]
} {
  const typedResources: Record<string, unknown>[] = []
  const sourceLocations: TraceSourceLocation[] = []
  for (const [index, item] of mcpContentItems(data).slice(0, 20).entries()) {
    const value = item && typeof item === "object" ? (item as Record<string, unknown>) : {}
    const text = typeof value.text === "string" ? value.text : undefined
    const parsed = text ? parsedJsonObject(text) : undefined
    if (parsed && (parsed.fact || parsed.key || parsed.path || parsed.file || parsed.uri)) {
      const sourceLocation = sourceLocationFromRecord(parsed)
      if (sourceLocation) sourceLocations.push({ ...sourceLocation })
      typedResources.push({
        index,
        type: "repo_fact",
        key: stringField(parsed, ["key"]),
        fact: stringField(parsed, ["fact", "summary", "text"]),
        source_location: sourceLocation ? { ...sourceLocation } : undefined,
        text_hash: text ? `sha256:${hash(text)}` : undefined,
      })
      continue
    }
    typedResources.push({
      index,
      type: typeof value.type === "string" ? value.type : typeof item,
      uri: typeof value.uri === "string" ? value.uri : undefined,
      text_hash: text ? `sha256:${hash(text)}` : undefined,
      preview: text?.slice(0, 240),
    })
  }
  return { typed_resources: typedResources, source_locations: dedupeSourceLocations(sourceLocations) }
}

function observationSemanticExtras(
  source: string,
  data: unknown,
): {
  typed_resources?: Record<string, unknown>[]
  source_locations?: TraceSourceLocation[]
} {
  if (!data || typeof data !== "object" || Array.isArray(data)) return {}
  const record = data as Record<string, unknown>
  if (/mcp/i.test(source)) {
    return mcpSemanticExtras(record)
  }
  if (/skill/i.test(source)) {
    return {
      typed_resources: [
        {
          type: "skill",
          name: stringField(record, ["name"]),
          path: stringField(record, ["dir"]),
          sampled_files_hash:
            typeof record.sampled_files === "string" ? `sha256:${hash(record.sampled_files)}` : undefined,
        },
      ],
    }
  }
  return {}
}

function typedResourcesForSpan(
  component: TraceComponent,
  operation: string,
  name: string | undefined,
  payload: unknown,
) {
  const record =
    payload && typeof payload === "object" && !Array.isArray(payload) ? (payload as Record<string, unknown>) : {}
  const input =
    record.input && typeof record.input === "object" && !Array.isArray(record.input)
      ? (record.input as Record<string, unknown>)
      : record
  if (component === "llm") {
    const model =
      input.model && typeof input.model === "object" && !Array.isArray(input.model)
        ? (input.model as Record<string, unknown>)
        : {}
    return [
      {
        type: "llm_request",
        agent: stringField(input, ["agent"]),
        provider_id:
          stringField(model, ["providerID", "provider_id"]) ?? stringField(input, ["providerID", "provider_id"]),
        model_id: stringField(model, ["id", "modelID", "model_id"]) ?? stringField(input, ["modelID", "model_id"]),
        message_count: numberField(input, ["message_count", "messageCount"]),
        tool_count: numberField(input, ["tool_count", "toolCount"]),
      },
    ]
  }
  if (component === "tool") {
    const args =
      input.args && typeof input.args === "object" && !Array.isArray(input.args)
        ? (input.args as Record<string, unknown>)
        : {}
    return [
      {
        type: "tool_call",
        tool_name: name ?? operation,
        call_id: stringField(input, ["callID", "call_id"]),
        intent_hint: stringField(args, ["description", "query", "pattern", "command"]),
        command: stringField(args, ["command"]),
        path: stringField(args, ["path", "filePath", "filepath"]),
        workdir: stringField(args, ["workdir", "cwd"]),
      },
    ]
  }
  if (component === "task") {
    return [
      {
        type: "subagent_task",
        subagent_type: stringField(input, ["subagent_type"]) ?? name,
        description: stringField(input, ["description"]),
        parent_session_id: stringField(input, ["parent_session_id", "sessionID", "session_id"]),
        child_session_id: stringField(record, ["task_id", "child_session_id", "sessionId", "session_id"]),
      },
    ]
  }
  if (component === "skill") {
    return [
      {
        type: "skill",
        name: stringField(input, ["name"]) ?? name,
        path: stringField(record, ["dir"]),
      },
    ]
  }
  if (component === "mcp") {
    return [
      {
        type: "mcp_call",
        server: stringField(input, ["server"]) ?? name?.split(":")[0],
        tool_name: stringField(input, ["tool"]) ?? name?.split(":").slice(1).join(":"),
      },
    ]
  }
  return undefined
}

function mergeTypedResources(
  current: Record<string, unknown>[] | undefined,
  next: Record<string, unknown>[] | undefined,
) {
  const merged = [...(current ?? []), ...(next ?? [])]
  if (!merged.length) return undefined
  const seen = new Set<string>()
  return merged.filter((item) => {
    const key = json(item)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function mergeRefs(current: string[] | undefined, next: string[] | undefined) {
  const merged = [...(current ?? []), ...(next ?? [])]
  return merged.length ? merged.filter((item, index, array) => array.indexOf(item) === index) : undefined
}

function omitUndefined(input: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(input).filter(([, value]) => value !== undefined))
}

function classifySourceRefs(input: string[] | undefined) {
  const direct_evidence_refs: string[] = []
  const context_refs: string[] = []
  const execution_refs: string[] = []
  for (const ref of input ?? []) {
    const separator = ref.indexOf(":")
    const type = separator === -1 ? "" : ref.slice(0, separator)
    if (type === "evidence") {
      direct_evidence_refs.push(ref)
      continue
    }
    if (["prompt", "context", "context_snapshot", "llm"].includes(type)) {
      context_refs.push(ref)
      continue
    }
    execution_refs.push(ref)
  }
  return {
    direct_evidence_refs: dedupeStrings(direct_evidence_refs),
    context_refs: dedupeStrings(context_refs),
    execution_refs: dedupeStrings(execution_refs),
  }
}

function splitResponseClaims(input: unknown): string[] {
  const text = typeof input === "string" ? input : stringPreview(input, 8000)
  if (!text.trim()) return []
  const protectedText = protectClaimSegments(text)
  const normalized = protectedText.text
    .replace(/\r\n/g, "\n")
    .split(/\n+|(?:^|\n)\s*(?:[-*]|\d+\.)\s+/)
    .flatMap((part) => part.match(/[^。！？.!?；;]+[。！？.!?]?/g) ?? [part])
    .map((part) => restoreClaimSegments(part.trim(), protectedText.segments))
    .filter(Boolean)
  const seen = new Set<string>()
  return normalized
    .map((claim) => claim.replace(/\s+/g, " ").trim())
    .filter((claim) => {
      const textLength = claim.replace(/\s/g, "").length
      const hasFactSignal = /\d|[/\\][\w.-]+|[A-Za-z_$][\w$]*\(|[A-Za-z_$][\w$]*\.[A-Za-z_$]/.test(claim)
      if (textLength < 6 && !hasFactSignal) return false
      if (isNonFactualResponseClaim(claim)) return false
      const key = claim.toLowerCase()
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    .slice(0, 50)
}

function isNonFactualResponseClaim(input: string) {
  const normalized = input
    .trim()
    .replace(/^#+\s*/, "")
    .replace(/^[-*]\s*/, "")
    .replace(/\*\*/g, "")
    .replace(/^["'`]+|["'`]+$/g, "")
    .replace(/[。.!?；;:：]+$/g, "")
    .trim()
    .toLowerCase()
  if (!normalized) return true
  if (/^(好的|可以|下面|因此|总结|结论)$/.test(normalized)) return true
  if (/^(summary|here'?s the summary|final summary|result summary)$/.test(normalized)) return true
  if (
    /^(goal|constraints?\s*&?\s*preferences?|progress|done|in progress|blocked|key decisions|next steps|critical context|relevant files)$/.test(
      normalized,
    )
  )
    return true
  if (/^(no further steps needed|nothing else needed|no next steps needed)$/.test(normalized)) return true
  if (/^(以下是|下面是|这里是).*(总结|结论)$/.test(normalized)) return true
  return false
}

function protectClaimSegments(input: string) {
  const segments: string[] = []
  let text = input
  const protect = (pattern: RegExp) => {
    text = text.replace(pattern, (match) => {
      const token = `__TRACE_PROTECTED_${segments.length}__`
      segments.push(match)
      return token
    })
  }
  protect(/`[^`\n]+`/g)
  protect(/\b\d+\.\d+%?/g)
  protect(/\b\d+\s*percent\b/gi)
  protect(/(?:^|[\s('"，。；;：:])((?:\.{0,2}\/|\/)?[\w@~-]+(?:\/[\w@~.-]+)+)(?=$|[\s)'",，。；;：:])/g)
  protect(/\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b/g)
  protect(/\b[A-Za-z_$][\w$]*\([^。\n]*?\)/g)
  return { text, segments }
}

function restoreClaimSegments(input: string, segments: string[]) {
  let output = input
  segments.forEach((segment, index) => {
    output = output.replaceAll(`__TRACE_PROTECTED_${index}__`, segment)
  })
  return output
}

function isBrokenClaimFragment(input: unknown) {
  const text = typeof input === "string" ? input.trim() : stringPreview(input, 200).trim()
  if (!text) return false
  if (/^\d+[%)]?[。.!?；;]?$/.test(text)) return true
  if (/^(?:\.\d+|[A-Za-z0-9_$-]+\.)$/.test(text)) return true
  if (/^(?:mjs|ts|tsx|js|jsx|json|md|yaml|yml|go|rs|py|java|cc|cpp|h|hpp)\b[。.!?；;)]?$/i.test(text)) return true
  if (/^[,，。.!?；;:：)\]]+$/.test(text)) return true
  return false
}

function responseClaimSupportLevel(classifiedRefs: ReturnType<typeof classifySourceRefs>) {
  if (classifiedRefs.direct_evidence_refs.length) return "direct"
  if (classifiedRefs.context_refs.length) return "contextual"
  if (classifiedRefs.execution_refs.length) return "execution_only"
  return "unsupported"
}

function responseClaimQualityFlags(classifiedRefs: ReturnType<typeof classifySourceRefs>) {
  const flags: string[] = []
  if (!classifiedRefs.direct_evidence_refs.length) {
    if (classifiedRefs.context_refs.length) flags.push("context_only_claim")
    else if (classifiedRefs.execution_refs.length) flags.push("execution_only_claim")
    else flags.push("unsupported_response_claim")
  }
  return flags
}

function normalizeMatchText(input: unknown) {
  return stringPreview(input, 6000)
    .toLowerCase()
    .replace(/15\s*%|15\s+percent/g, "0.15")
    .replace(/_/g, "-")
    .replace(/\s+/g, " ")
    .trim()
}

function matchTerms(input: unknown) {
  const normalized = normalizeMatchText(input)
  const terms = new Set<string>()
  for (const token of normalized.match(/[a-z0-9_$./%-]+/g) ?? []) {
    const trimmed = token.replace(/^[./-]+|[./-]+$/g, "")
    if (trimmed.length < 3 && !/\d/.test(trimmed)) continue
    if (
      [
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "observed",
        "source",
        "result",
        "output",
        "value",
        "claim",
        "text",
      ].includes(trimmed)
    ) {
      continue
    }
    terms.add(trimmed)
    if (trimmed.includes("/")) terms.add(trimmed.split("/").at(-1) ?? trimmed)
    if (trimmed.includes("-")) terms.add(trimmed.replace(/-/g, "_"))
    if (trimmed.includes("_")) terms.add(trimmed.replace(/_/g, "-"))
  }
  return [...terms]
}

function evidenceMatchStrings(data: Record<string, unknown> | undefined) {
  if (!data) return []
  const output: string[] = []
  const structured = recordFromUnknown(data.structured_claim)
  for (const key of ["canonical_subject", "claim", "fact_kind", "category", "source"]) {
    const value = data[key]
    if (value !== undefined) output.push(stringPreview(value, 1000))
  }
  if (structured) {
    for (const key of ["subject", "predicate", "value", "qualifier", "extraction_method"]) {
      const value = structured[key]
      if (value !== undefined) output.push(stringPreview(value, 1000))
    }
    const span = recordFromUnknown(structured.source_span)
    for (const key of ["path", "uri", "snippet_preview"]) {
      const value = span?.[key]
      if (value !== undefined) output.push(stringPreview(value, 1000))
    }
  }
  for (const location of Array.isArray(data.source_locations) ? data.source_locations : []) {
    if (!recordFromUnknown(location)) continue
    output.push(stringPreview(location, 1000))
  }
  return dedupeStrings(output.filter(Boolean))
}

function isVerificationClaimText(input: string) {
  return /test|测试|passed|failed|pass|fail|assert|断言|exit|退出码|验证|pricing tests|expected|actual|通过|失败|\b48000\b|\b51000\b/i.test(
    input,
  )
}

function predicateMatchesClaim(predicate: string | undefined, claim: string) {
  if (!predicate) return false
  if (claim.includes(predicate)) return true
  if (predicate === "owner" && /owner|owned|team|负责|归属/.test(claim)) return true
  if (predicate === "discount-cap" && /discount|折扣|cap|capped|limit|upper|上限|封顶|0\.15|15/.test(claim)) return true
  if (predicate === "implementation-entry" && /entry|入口|实现|implementation|src\/|\.mjs/.test(claim)) return true
  if (predicate === "exit-status" && isVerificationClaimText(claim)) return true
  return false
}

function evidenceMatchAnalysis(claimText: unknown, evidenceData: Record<string, unknown> | undefined) {
  const reasons: string[] = []
  if (!evidenceData) return { score: 0, reasons, weak: false }
  const claim = normalizeMatchText(claimText)
  if (!claim) return { score: 0, reasons, weak: false }
  const strings = evidenceMatchStrings(evidenceData)
  const haystack = normalizeMatchText(strings.join(" "))
  if (!haystack) return { score: 0, reasons, weak: false }
  const factKind = typeof evidenceData.fact_kind === "string" ? evidenceData.fact_kind : ""
  if (factKind === "verification_output" && !isVerificationClaimText(claim)) {
    return { score: 0, reasons: ["incompatible_verification_fact"], weak: false }
  }
  const structured = recordFromUnknown(evidenceData.structured_claim)
  let score = 0
  const structuredValue = structured?.value === undefined ? undefined : normalizeMatchText(structured.value)
  const structuredSubject = structured?.subject === undefined ? undefined : normalizeMatchText(structured.subject)
  const structuredPredicate = structured?.predicate === undefined ? undefined : normalizeMatchText(structured.predicate)
  if (structuredValue && claim.includes(structuredValue)) {
    score += 0.55
    reasons.push("structured_value")
  }
  if (structuredSubject && claim.includes(structuredSubject)) {
    score += 0.25
    reasons.push("structured_subject")
  }
  if (predicateMatchesClaim(structuredPredicate, claim)) {
    score += 0.2
    reasons.push("structured_predicate")
  }
  const span = recordFromUnknown(structured?.source_span)
  const sourcePath = typeof span?.path === "string" ? normalizeMatchText(span.path) : undefined
  if (sourcePath) {
    const basename = sourcePath.split("/").at(-1)
    if (claim.includes(sourcePath) || (basename && claim.includes(basename))) {
      score += 0.35
      reasons.push("source_path")
    }
  }
  const claimTerms = new Set(matchTerms(claim))
  const evidenceTerms = new Set(matchTerms(haystack))
  const shared = [...claimTerms].filter((term) => evidenceTerms.has(term))
  if (shared.length) {
    score += Math.min(0.36, shared.length * 0.09)
    reasons.push("shared_terms")
  }
  if (/0\.15/.test(claim) && /0\.15/.test(haystack)) {
    score += 0.45
    reasons.push("discount_cap_value")
  }
  if (/billing-platform/.test(claim) && /billing-platform/.test(haystack)) {
    score += 0.45
    reasons.push("owner_value")
  }
  if (factKind === "verification_output" && isVerificationClaimText(claim)) {
    score += 0.5
    reasons.push("verification_result")
    if (
      /(pass|passed|通过|成功)/i.test(claim) &&
      /(pass|passed|exit[_ -]?code.{0,20}0|退出码.{0,20}0|通过|成功)/i.test(haystack)
    ) {
      score += 0.25
      reasons.push("verification_passed")
    }
    if (
      /(fail|failed|失败|断言|assert)/i.test(claim) &&
      /(fail|failed|assert|expected|actual|失败|断言)/i.test(haystack)
    ) {
      score += 0.25
      reasons.push("verification_failed")
    }
  }
  const finalScore = Math.min(1, Number(score.toFixed(2)))
  const strongReasons = reasons.filter((reason) => reason !== "shared_terms")
  return {
    score: finalScore,
    reasons: dedupeStrings(reasons),
    weak: finalScore > 0 && finalScore < 0.45 && strongReasons.length === 0,
  }
}

function dedupeStrings(input: string[]) {
  return input.filter((item, index, array) => array.indexOf(item) === index)
}

function stringPreview(input: unknown, limit = 400) {
  if (typeof input === "string") return input.slice(0, limit)
  if (input === undefined || input === null) return ""
  try {
    return JSON.stringify(input).slice(0, limit)
  } catch {
    return String(input).slice(0, limit)
  }
}

function fieldSummaryText(input: unknown) {
  const record = recordFromUnknown(input)
  if (!record) return stringPreview(input, 8000)
  const value = record.value
  if (typeof value === "string") return value
  const preview = record.preview
  if (typeof preview === "string") return preview
  return stringPreview(input, 8000)
}

function requestedSkillNames(input: unknown) {
  const names = new Set<string>()
  const text = collectTextCandidates(input).join("\n")
  for (const match of text.matchAll(/\b(?:use|load|call|调用|使用|加载)\s+([a-zA-Z][\w-]{1,80})\s+skill\b/gi)) {
    if (match[1]) names.add(match[1])
  }
  for (const match of text.matchAll(/\b([a-zA-Z][\w-]{1,80})\s+skill\b/gi)) {
    const name = match[1]
    if (!name || ["the", "this", "a", "an"].includes(name.toLowerCase())) continue
    names.add(name)
  }
  return [...names]
}

function availableSkillNames(input: unknown) {
  const names = new Set<string>()
  const text = stringPreview(input, 200000)
  for (const match of text.matchAll(/<name>\s*([^<\s]+)\s*<\/name>/gi)) {
    if (match[1]) names.add(match[1])
  }
  for (const match of text.matchAll(/"name"\s*:\s*"([^"]+)"/gi)) {
    if (match[1]) names.add(match[1])
  }
  return [...names]
}

function availableSkillNamesFromError(input: string) {
  const match = input.match(/available skills?:\s*([^\n.]+)/i)
  if (!match?.[1]) return []
  return match[1]
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function objectField(input: unknown, key: string) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined
  return (input as Record<string, unknown>)[key]
}

function firstStringField(input: unknown, keys: string[]) {
  for (const key of keys) {
    const value = objectField(input, key)
    if (typeof value === "string" && value.trim()) return value
  }
  return undefined
}

function isEmptySubagentOutput(input: unknown) {
  const text = stringPreview(input, 2000)
  return /<task_result>\s*<\/task_result>/i.test(text)
}

function evidenceQualityFlags(input: EvidenceFactInput) {
  const flags: string[] = []
  if (/subagent|task/i.test(input.source) && isEmptySubagentOutput(input.data ?? input.summary)) {
    flags.push("empty_subagent_result")
  }
  return flags
}

function recordFromUnknown(input: unknown): Record<string, unknown> | undefined {
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined
  return input as Record<string, unknown>
}

function primitiveClaimValue(input: unknown): string | number | boolean | null | undefined {
  if (input === null) return null
  if (typeof input === "string" || typeof input === "number" || typeof input === "boolean") return input
  if (input === undefined) return undefined
  try {
    return JSON.stringify(input)
  } catch {
    return String(input)
  }
}

function firstPresentField(input: unknown, keys: string[]) {
  const record = recordFromUnknown(input)
  if (!record) return undefined
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== "") return record[key]
  }
  return undefined
}

function parseJsonObjectText(input: string): Record<string, unknown> | undefined {
  const trimmed = input.trim()
  if (!trimmed) return undefined
  const candidates = [trimmed]
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i)
  if (fenced?.[1]) candidates.push(fenced[1].trim())
  const objectMatch = trimmed.match(/\{[\s\S]*\}/)
  if (objectMatch?.[0]) candidates.push(objectMatch[0])
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate)
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>
    } catch {
      continue
    }
  }
  return undefined
}

function jsonLikePayload(input: unknown): Record<string, unknown> | undefined {
  if (recordFromUnknown(input)) return recordFromUnknown(input)
  if (typeof input !== "string") return undefined
  return parseJsonObjectText(input)
}

function textPayloadFromRecord(input: unknown) {
  const record = recordFromUnknown(input)
  if (!record) return undefined
  for (const key of ["text", "content", "fact", "claim", "summary", "value", "output"]) {
    const value = record[key]
    if (typeof value === "string" && value.trim()) return value
  }
  return undefined
}

type StructuredClaimCandidate = {
  record: Record<string, unknown>
  extraction_method: TraceStructuredClaim["extraction_method"]
}

function looksStructuredClaimLike(input: Record<string, unknown>) {
  return Boolean(
    input.subject !== undefined ||
      input.predicate !== undefined ||
      input.value !== undefined ||
      input.fact !== undefined ||
      input.claim !== undefined ||
      input.owner !== undefined ||
      input.symbol !== undefined ||
      input.path !== undefined ||
      input.file !== undefined,
  )
}

function collectStructuredClaimCandidates(
  input: unknown,
  output: StructuredClaimCandidate[] = [],
  depth = 0,
  extractionMethod: TraceStructuredClaim["extraction_method"] = "explicit_structured_data",
): StructuredClaimCandidate[] {
  if (depth > 6 || input === undefined || input === null) return output
  if (typeof input === "string") {
    const parsed = parseJsonObjectText(input)
    if (parsed) collectStructuredClaimCandidates(parsed, output, depth + 1, "mcp_json_text")
    return output
  }
  if (Array.isArray(input)) {
    for (const item of input.slice(0, 60)) collectStructuredClaimCandidates(item, output, depth + 1, extractionMethod)
    return output
  }
  if (typeof input !== "object") return output
  const record = input as Record<string, unknown>
  if (looksStructuredClaimLike(record)) output.push({ record, extraction_method: extractionMethod })
  for (const key of [
    "content",
    "output",
    "result",
    "results",
    "data",
    "metadata",
    "typed_resources",
    "resources",
    "args",
    "message",
    "messages",
  ]) {
    if (record[key] !== undefined) collectStructuredClaimCandidates(record[key], output, depth + 1, extractionMethod)
  }
  for (const key of ["text", "fact", "claim", "summary", "value", "snippet"]) {
    const value = record[key]
    if (typeof value === "string") collectStructuredClaimCandidates(value, output, depth + 1, "mcp_json_text")
  }
  return output
}

function collectTextCandidates(input: unknown, output: string[] = [], depth = 0): string[] {
  if (depth > 5 || input === undefined || input === null || output.length >= 80) return output
  if (typeof input === "string") {
    if (input.trim()) output.push(input)
    const parsed = parseJsonObjectText(input)
    if (parsed) collectTextCandidates(parsed, output, depth + 1)
    return output
  }
  if (Array.isArray(input)) {
    for (const item of input.slice(0, 60)) collectTextCandidates(item, output, depth + 1)
    return output
  }
  if (typeof input !== "object") return output
  const record = input as Record<string, unknown>
  for (const key of [
    "text",
    "content",
    "fact",
    "claim",
    "summary",
    "value",
    "output",
    "snippet",
    "stdout",
    "stderr",
    "result",
    "data",
  ]) {
    if (record[key] !== undefined) collectTextCandidates(record[key], output, depth + 1)
  }
  return dedupeStrings(output)
}

function structuredCandidateScore(candidate: StructuredClaimCandidate) {
  const record = candidate.record
  let score = candidate.extraction_method === "mcp_json_text" ? 100 : 0
  const hasSubject = record.subject !== undefined || record.symbol !== undefined || record.name !== undefined
  const hasPredicate = record.predicate !== undefined || record.relation !== undefined || record.property !== undefined
  const hasValue =
    record.value !== undefined ||
    record.fact !== undefined ||
    record.claim !== undefined ||
    record.text !== undefined ||
    record.owner !== undefined ||
    record.status !== undefined
  const structuredCount = [hasSubject, hasPredicate, hasValue].filter(Boolean).length
  score += structuredCount * 30
  if (sourceLocationFromRecord(record)) score += 10
  const onlyLocation =
    structuredCount === 0 &&
    (record.path !== undefined ||
      record.file !== undefined ||
      record.filePath !== undefined ||
      record.uri !== undefined)
  if (onlyLocation) score -= 100
  return score
}

function sortedStructuredClaimCandidates(input: unknown) {
  return collectStructuredClaimCandidates(input).sort(
    (a, b) => structuredCandidateScore(b) - structuredCandidateScore(a),
  )
}

function strongStructuredClaimCandidates(input: unknown) {
  return sortedStructuredClaimCandidates(input).filter((candidate) => structuredCandidateScore(candidate) >= 70)
}

function claimSourceSpan(input: unknown, sourceLocations: TraceSourceLocation[]) {
  const record = recordFromUnknown(input)
  const fromRecord = record ? sourceLocationFromRecord(record) : undefined
  return fromRecord ?? sourceLocations[0]
}

function rawArtifactRef(input: unknown) {
  const record = recordFromUnknown(input)
  if (!record) return undefined
  const direct = record.artifact_id
  if (typeof direct === "string") return direct
  for (const value of Object.values(record)) {
    if (recordFromUnknown(value) && typeof (value as Record<string, unknown>).artifact_id === "string") {
      return (value as Record<string, unknown>).artifact_id as string
    }
  }
  return undefined
}

function isGenericClaimText(input: string) {
  const text = input.trim()
  if (!text) return true
  if (/returned\s+\d+\s+content\s+item/i.test(text)) return true
  if (/^(result|output|response|summary|file observed|pricing file observed)\b/i.test(text)) return true
  if (text.length < 12 && !/\d/.test(text)) return true
  return false
}

function sourcePathFromText(input: string) {
  const pathTag = input.match(/<path>\s*([^<]+?)\s*<\/path>/i)
  if (pathTag?.[1]) return normalizeSourcePath(pathTag[1].trim())
  return sourceLocationsFromText(input).find((location) => location.path || location.uri)?.path
}

function lineSourceSpanFromText(
  input: string,
  lineText: string,
  lineNumber: number | undefined,
  fallback?: TraceSourceLocation,
) {
  const sourcePath = sourcePathFromText(input) ?? fallback?.path
  return {
    uri: fallback?.uri,
    path: sourcePath,
    line_start: lineNumber ?? fallback?.line_start,
    line_end: lineNumber ?? fallback?.line_end,
    snippet_preview: lineText.trim().slice(0, 240),
  } satisfies TraceSourceLocation
}

function structuredClaimFromLineText(
  input: string,
  sourceSpan?: TraceSourceLocation,
): TraceStructuredClaim | undefined {
  const text = input.trim()
  if (!text) return undefined
  const normalized = text.toLowerCase()
  if (
    /(discount|折扣)/i.test(text) &&
    /(cap|capped|limit|maximum|max|上限|封顶|math\.min|0\.15|15\s*%|15\s+percent)/i.test(text)
  ) {
    const value = /0\.15/.test(text) ? "0.15" : /15\s*%/.test(text) ? "15%" : "15 percent"
    return {
      subject: /renewalquote/i.test(text) ? "renewalQuote" : "discount",
      predicate: "discount_cap",
      value,
      source_span: sourceSpan,
      extraction_method: "source_line_pattern",
    }
  }
  if (/billing-platform/i.test(text) && /(owner|owned|负责|归属|quoteowner)/i.test(text)) {
    return {
      subject: /renewalquote/i.test(text) ? "renewalQuote" : /quoteowner/i.test(text) ? "quoteOwner" : "owner",
      predicate: "owner",
      value: "billing-platform",
      source_span: sourceSpan,
      extraction_method: "source_line_pattern",
    }
  }
  if (/entry point|入口|export function renewalQuote|renewalQuote\(input\)/i.test(text)) {
    return {
      subject: "renewalQuote",
      predicate: "implementation_entry",
      value: sourceSpan?.path ?? "renewalQuote(input)",
      source_span: sourceSpan,
      extraction_method: "source_line_pattern",
    }
  }
  const returnValue = text.match(/return\s+["']([^"']+)["']/)
  if (returnValue?.[1]) {
    return {
      subject: /quoteowner/i.test(text) ? "quoteOwner" : "function_return",
      predicate: "return_value",
      value: returnValue[1],
      source_span: sourceSpan,
      extraction_method: "source_line_pattern",
    }
  }
  if (normalized.includes("math.min") && /0\.15/.test(text)) {
    return {
      subject: "renewalQuote",
      predicate: "discount_cap",
      value: "0.15",
      source_span: sourceSpan,
      extraction_method: "source_line_pattern",
    }
  }
  return undefined
}

function structuredLineClaimsFromText(input: string, fallback?: TraceSourceLocation) {
  const claims: TraceStructuredClaim[] = []
  const lines = input.split(/\r?\n/)
  for (const line of lines) {
    const match = line.match(/^\s*(\d+):\s?(.*)$/)
    const lineNumber = optionalNumber(match?.[1])
    const lineText = (match?.[2] ?? line).trim()
    if (!lineText) continue
    const sourceSpan = lineSourceSpanFromText(input, lineText, lineNumber, fallback)
    const claim = structuredClaimFromLineText(lineText, sourceSpan)
    if (claim) claims.push(claim)
    if (claims.length >= 20) break
  }
  if (!claims.length && input.length < 2000) {
    const claim = structuredClaimFromLineText(input, fallback)
    if (claim) claims.push(claim)
  }
  return claims
}

function structuredClaimFromRecord(
  record: Record<string, unknown>,
  extractionMethod: TraceStructuredClaim["extraction_method"],
  sourceSpan?: TraceSourceLocation,
): TraceStructuredClaim | undefined {
  const subject = primitiveClaimValue(firstPresentField(record, ["subject", "symbol", "name", "key", "path", "file"]))
  const predicate = primitiveClaimValue(
    firstPresentField(record, ["predicate", "relation", "property", "type", "category"]),
  )
  const value = primitiveClaimValue(firstPresentField(record, ["value", "fact", "claim", "text", "owner", "status"]))
  const qualifier = primitiveClaimValue(firstPresentField(record, ["qualifier", "reason", "confidence", "stage"]))
  if (subject === undefined && predicate === undefined && value === undefined) return undefined
  return {
    subject: subject === undefined ? undefined : String(subject),
    predicate: predicate === undefined ? undefined : String(predicate),
    value,
    qualifier: qualifier === undefined ? undefined : String(qualifier),
    source_span: sourceSpan,
    extraction_method: extractionMethod,
    raw_artifact_ref: rawArtifactRef(record),
  }
}

function structuredClaimFromEvidence(
  input: EvidenceFactInput,
  sourceLocations: TraceSourceLocation[],
): { structured_claim: TraceStructuredClaim; quality_flags: string[] } {
  const flags: string[] = []
  const sourceSpan = claimSourceSpan(input.data, sourceLocations)
  const dataRecord = recordFromUnknown(input.data)
  const source = input.source.toLowerCase()
  const category = input.category?.toLowerCase() ?? ""

  if (dataRecord) {
    const explicit = structuredClaimFromRecord(dataRecord, "explicit_structured_data", sourceSpan)
    if (
      explicit &&
      (dataRecord.subject !== undefined || dataRecord.predicate !== undefined || dataRecord.value !== undefined)
    ) {
      if (!explicit.source_span) flags.push("missing_source_span")
      return { structured_claim: explicit, quality_flags: flags }
    }
    const textPayload = textPayloadFromRecord(dataRecord)
    const parsed = textPayload ? parseJsonObjectText(textPayload) : undefined
    if (parsed) {
      const parsedSourceSpan = sourceLocationFromRecord(parsed) ?? sourceSpan
      const parsedClaim = structuredClaimFromRecord(
        parsed,
        source.includes("mcp") ? "mcp_json_text" : "explicit_structured_data",
        parsedSourceSpan,
      )
      if (parsedClaim) {
        if (!parsedClaim.source_span) flags.push("missing_source_span")
        if (parsedClaim.extraction_method === "mcp_json_text") flags.push("mcp_json_fact_extracted")
        return { structured_claim: parsedClaim, quality_flags: flags }
      }
    } else if (source.includes("mcp") && textPayload) {
      flags.push("unparsed_mcp_text")
    }
    for (const candidate of strongStructuredClaimCandidates(dataRecord)) {
      const candidateSourceSpan = sourceLocationFromRecord(candidate.record) ?? sourceSpan
      const candidateClaim = structuredClaimFromRecord(
        candidate.record,
        source.includes("mcp") ? "mcp_json_text" : candidate.extraction_method,
        candidateSourceSpan,
      )
      if (candidateClaim) {
        if (!candidateClaim.source_span) flags.push("missing_source_span")
        if (candidateClaim.extraction_method === "mcp_json_text") flags.push("mcp_json_fact_extracted")
        return { structured_claim: candidateClaim, quality_flags: flags }
      }
    }
    for (const textCandidate of collectTextCandidates(dataRecord)) {
      const lineClaim = structuredLineClaimsFromText(textCandidate, sourceSpan)[0]
      if (lineClaim) {
        flags.push("line_fact_extracted")
        if (!lineClaim.source_span) flags.push("missing_source_span")
        return { structured_claim: lineClaim, quality_flags: flags }
      }
    }
    const locationClaim = structuredClaimFromRecord(dataRecord, "source_location_fields", sourceSpan)
    if (
      locationClaim &&
      (dataRecord.path !== undefined || dataRecord.file !== undefined || dataRecord.symbol !== undefined)
    ) {
      if (!locationClaim.predicate) locationClaim.predicate = "located_at"
      if (!locationClaim.value && sourceSpan?.path) locationClaim.value = sourceSpan.path
      if (!locationClaim.source_span) flags.push("missing_source_span")
      flags.push("path_only_evidence_fact")
      return { structured_claim: locationClaim, quality_flags: flags }
    }
    if (/verification|test|command|bash/.test(source) || /verification|test|command/.test(category)) {
      const verificationClaim = structuredClaimFromRecord(
        {
          subject:
            firstPresentField(dataRecord, ["command", "cmd", "tool_name", "name"]) ?? input.category ?? input.source,
          predicate: "exit_status",
          value: firstPresentField(dataRecord, ["status", "exit_code", "exitCode", "result"]),
          reason: firstPresentField(dataRecord, ["message", "stderr", "stdout"]),
        },
        "verification_output",
        sourceSpan,
      )
      flags.push("weak_verification_claim")
      if (verificationClaim) {
        if (!verificationClaim.source_span) flags.push("missing_source_span")
        return { structured_claim: verificationClaim, quality_flags: flags }
      }
    }
  }

  const structuredCandidates = sortedStructuredClaimCandidates(input.data)
  for (const candidate of structuredCandidates) {
    const candidateSourceSpan = sourceLocationFromRecord(candidate.record) ?? sourceSpan
    const candidateClaim = structuredClaimFromRecord(
      candidate.record,
      source.includes("mcp") ? "mcp_json_text" : candidate.extraction_method,
      candidateSourceSpan,
    )
    if (candidateClaim) {
      if (!candidateClaim.source_span) flags.push("missing_source_span")
      if (candidateClaim.extraction_method === "mcp_json_text") flags.push("mcp_json_fact_extracted")
      return { structured_claim: candidateClaim, quality_flags: flags }
    }
  }

  const textRecord = jsonLikePayload(input.data)
  if (textRecord) {
    const jsonClaim = structuredClaimFromRecord(
      textRecord,
      source.includes("mcp") ? "mcp_json_text" : "explicit_structured_data",
      sourceSpan,
    )
    if (jsonClaim) {
      if (!jsonClaim.source_span) flags.push("missing_source_span")
      return { structured_claim: jsonClaim, quality_flags: flags }
    }
  }

  for (const textCandidate of collectTextCandidates(input.data)) {
    const lineClaims = structuredLineClaimsFromText(textCandidate, sourceSpan)
    const lineClaim = lineClaims[0]
    if (lineClaim) {
      flags.push("line_fact_extracted")
      if (!lineClaim.source_span) flags.push("missing_source_span")
      return { structured_claim: lineClaim, quality_flags: flags }
    }
  }

  const summary = stringPreview(input.summary, 1000)
  const extractionMethod: TraceStructuredClaim["extraction_method"] = isGenericClaimText(summary)
    ? "fallback_summary"
    : "summary_sentence"
  if (extractionMethod === "fallback_summary") {
    flags.push("generic_claim", "fallback_summary_claim")
  }
  if (source.includes("mcp") && flags.includes("generic_claim")) flags.push("generic_mcp_fact")
  if (!sourceSpan) flags.push("missing_source_span")
  if (rawArtifactRef(input.summary) || rawArtifactRef(input.data)) flags.push("artifact_only_claim")
  if (
    (/file|read|grep|code/.test(source) || /file|read|grep|code/.test(category)) &&
    sourceSpan?.path &&
    isGenericClaimText(summary)
  ) {
    flags.push("path_only_evidence_fact")
  }
  return {
    structured_claim: {
      subject: input.canonical_subject ?? input.category ?? input.source,
      predicate: "observed",
      value: summary,
      source_span: sourceSpan,
      extraction_method: extractionMethod,
      raw_artifact_ref: rawArtifactRef(input.data) ?? rawArtifactRef(input.summary),
    },
    quality_flags: flags,
  }
}

function canonicalEvidence(input: EvidenceFactInput, sourceLocations: TraceSourceLocation[]) {
  const structured = structuredClaimFromEvidence(input, sourceLocations)
  const qualityFlags = dedupeStrings([
    ...(input.quality_flags ?? []),
    ...evidenceQualityFlags(input),
    ...structured.quality_flags,
  ])
  const path =
    sourceLocations.find((location) => location.path)?.path ??
    firstStringField(input.data, ["path", "file", "filePath", "filepath"])
  const symbol = firstStringField(input.data, ["symbol", "name", "key"])
  const factText = firstStringField(input.data, ["fact", "claim", "summary", "text"])
  const subject =
    input.canonical_subject ?? structured.structured_claim.subject ?? symbol ?? path ?? input.category ?? input.source
  const summary = stringPreview(input.summary, 500)
  const structuredValue =
    structured.structured_claim.value === undefined ? undefined : String(structured.structured_claim.value)
  const claim = factText ?? structuredValue ?? summary
  const source = input.source.toLowerCase()
  const category = input.category?.toLowerCase() ?? ""
  const factKind = (() => {
    if (source.includes("mcp")) return "mcp_fact"
    if (source.includes("skill")) return "skill_instruction"
    if (source.includes("subagent") || source.includes("task")) return "subagent_result"
    if (/verification|test|command|bash/.test(source) || /verification|test|command/.test(category)) {
      return "verification_output"
    }
    if (/file|read|grep|code/.test(source) || /file|read|grep|code/.test(category)) return "code_reference"
    return "observation"
  })()
  return {
    fact_kind: input.fact_kind ?? factKind,
    canonical_subject: subject,
    claim: input.claim ?? claim,
    structured_claim: structured.structured_claim,
    support_level: input.support_level ?? (qualityFlags.includes("empty_subagent_result") ? "weak" : "direct"),
    quality_flags: qualityFlags,
  }
}

function countCircularMarkers(input: unknown, stack = new WeakSet<object>()): number {
  if (input === "[Circular]") return 1
  if (!input || typeof input !== "object") return 0
  if (stack.has(input)) return 1
  stack.add(input)
  try {
    let count = 0
    for (const value of Object.values(input as Record<string, unknown>)) count += countCircularMarkers(value, stack)
    return count
  } finally {
    stack.delete(input)
  }
}

function aggregateFlags(target: Record<string, number>, flags: unknown) {
  if (!Array.isArray(flags)) return
  for (const flag of flags) {
    if (typeof flag !== "string" || !flag) continue
    target[flag] = (target[flag] ?? 0) + 1
  }
}

function isWeakObservation(input: ObservationInput) {
  if (input.category !== "tool_output") return false
  if (!/tool/i.test(input.source)) return false
  const summary = typeof input.summary === "string" ? input.summary : ""
  const data =
    input.data && typeof input.data === "object" && !Array.isArray(input.data)
      ? (input.data as Record<string, unknown>)
      : {}
  const keys = Object.keys(data)
  const onlyPathData = keys.length > 0 && keys.every((key) => ["path", "file", "filePath", "uri"].includes(key))
  const pathLikeSummary = /(^|\/)[^/\s]+\.[a-z0-9]+$/i.test(summary) || summary.includes("/private/")
  return onlyPathData && pathLikeSummary
}

function isEvidenceWorthyObservation(input: ObservationInput) {
  if (isWeakObservation(input)) return false
  if (/tool|mcp|skill|subagent|task|verification|grep|read|bash|file/i.test(input.source)) return true
  if (input.category && /tool|mcp|skill|subagent|verification|test|file|grep|read/i.test(input.category)) return true
  return false
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
  readonly legacyTraceFile: string
  readonly htmlFile: string
  readonly manifestFile: string
  readonly provenanceTraceFile: string
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
  private claimedResponseSegmentIDs = new Set<string>()
  private designRecords: TraceDesignRecord[] = []
  private recentFailedVerificationID: string | undefined
  private recentChangeID: string | undefined
  private recentContextSnapshotIDs: string[] = []
  private recentPromptNodeIDs: string[] = []
  private recentContextNodeIDs: string[] = []
  private recentLLMNodeIDs: string[] = []
  private recentEvidenceNodeIDs: string[] = []
  private recentVerificationIDs: string[] = []
  private recentChangeIDs: string[] = []
  private recentToolSpanIDs: string[] = []
  private requestedSkillNames = new Set<string>()
  private recordedSkillRequestNames = new Set<string>()
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
    this.legacyTraceFile = path.join(this.caseDir, "legacy-trace.json")
    this.htmlFile = path.join(this.caseDir, "trace.html")
    this.manifestFile = path.join(this.caseDir, "manifest.json")
    this.provenanceTraceFile = path.join(this.caseDir, "provenance-trace.json")
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
        data: this.spanStartData(input),
        source_locations: collectSourceLocations(input.input),
        typed_resources: typedResourcesForSpan(input.component, input.operation, input.name, input.input),
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
        const output =
          span.component === "task" ? enrichSubagentOutput(input?.output, nodeID, this.caseDir) : input?.output
        const summarizedOutput =
          output === undefined
            ? node.data?.output
            : this.summarizeCausalValue(output, `${span.component}.${span.operation}.output`)
        const spanEndData = this.spanEndData(span, input, output, summarizedOutput, usage, error, nodeID)
        node.status = span.status
        node.data = {
          ...(node.data ?? {}),
          ...spanEndData,
        }
        if (span.component === "mcp") {
          const extras = mcpSemanticExtras(input?.output)
          node.typed_resources = extras.typed_resources.length ? extras.typed_resources : node.typed_resources
          node.source_locations = dedupeSourceLocations([...(node.source_locations ?? []), ...extras.source_locations])
        }
        this.applySkillToolClosure(node, span, output, error)
        node.typed_resources = mergeTypedResources(
          node.typed_resources,
          typedResourcesForSpan(span.component, span.operation, span.name, {
            input: span.input_summary,
            output,
            ...(output && typeof output === "object" && !Array.isArray(output)
              ? (output as Record<string, unknown>)
              : {}),
          }),
        )
        node.source_locations = dedupeSourceLocations([
          ...(node.source_locations ?? []),
          ...collectSourceLocations(output),
          ...collectSourceLocations(input?.metadata),
        ])
        node.artifact_refs = this.collectArtifactRefs(node.data)
        this.writeRecord("node.update", node)
        this.writePartial()
      }
    }
  }

  private spanStartData(input: StartSpanInput): Record<string, unknown> {
    const base: Record<string, unknown> = {
      operation: input.operation,
      input: input.input,
    }
    if (input.component !== "llm") return base
    const record =
      input.input && typeof input.input === "object" && !Array.isArray(input.input)
        ? (input.input as Record<string, unknown>)
        : {}
    const model =
      record.model && typeof record.model === "object" && !Array.isArray(record.model)
        ? (record.model as Record<string, unknown>)
        : {}
    const [nameProvider, nameModel] =
      typeof input.name === "string" && input.name.includes("/") ? input.name.split("/", 2) : []
    return {
      ...base,
      request_id: stringField(record, ["request_id", "response_id", "id"]),
      agent: stringField(record, ["agent"]),
      provider_id:
        stringField(model, ["providerID", "provider_id"]) ??
        stringField(record, ["providerID", "provider_id"]) ??
        nameProvider,
      model_id:
        stringField(model, ["id", "modelID", "model_id"]) ?? stringField(record, ["modelID", "model_id"]) ?? nameModel,
      message_count: numberField(record, ["message_count", "messageCount"]),
      system_count: numberField(record, ["system_count", "systemCount"]),
      tool_count: numberField(record, ["tool_count", "toolCount"]),
      tool_choice: record.tool_choice,
      input_context_snapshot_ref: stringField(record, ["input_context_snapshot_ref", "context_snapshot_ref"]),
    }
  }

  private spanEndData(
    span: TraceSpan,
    input: EndSpanInput | undefined,
    rawOutput: unknown,
    summarizedOutput: unknown,
    usage: TraceTokenUsage | undefined,
    error: TraceError | undefined,
    nodeID: string,
  ): Record<string, unknown> {
    const data: Record<string, unknown> = {
      duration_ms: span.duration_ms,
      output: summarizedOutput,
      token_usage: cloneTokenUsage(usage),
      error,
    }
    if (span.component === "llm") {
      const output =
        rawOutput && typeof rawOutput === "object" && !Array.isArray(rawOutput)
          ? (rawOutput as Record<string, unknown>)
          : {}
      data.finish_reason = stringField(output, ["finish_reason", "finishReason"])
      data.stop_reason = stringField(output, ["stop_reason", "stopReason"])
      data.cache_usage = usage
        ? {
            cached_input: usage.cached_input,
            cache_write: usage.cache_write,
          }
        : undefined
    }
    if (span.component === "task") {
      const traceRef = subagentTraceRef(rawOutput, nodeID, this.caseDir)
      if (traceRef) {
        data.trace_ref = traceRef
        data.child_session_id = traceRef.child_session_id
        data.child_status = traceRef.child_status
        data.child_trace_available = traceRef.child_trace_available
        if (traceRef.child_trace_dir) data.child_trace_dir = traceRef.child_trace_dir
      }
      if (isEmptySubagentOutput(rawOutput)) data.quality_flags = ["empty_subagent_result"]
      const artifactID = this.collectArtifactRefs(summarizedOutput)[0]
      if (artifactID) data.output_artifact_id = artifactID
    }
    return data
  }

  private applySkillToolClosure(node: CausalNode, span: TraceSpan, rawOutput: unknown, error: TraceError | undefined) {
    if (span.component !== "tool") return
    if ((span.name ?? "").toLowerCase() !== "skill") return
    if (!error) return
    const input = recordFromUnknown(node.data?.input)
    const args = recordFromUnknown(input?.args)
    const skillName = firstStringField(args, ["name"]) ?? firstStringField(input, ["name"]) ?? "unknown"
    const message = [error.message, stringPreview(rawOutput, 1000)].filter(Boolean).join("\n")
    const availableSkillNames = availableSkillNamesFromError(message)
    const requestStatus = /not found|missing|unknown skill|not available/i.test(message) ? "missing" : "error"
    const qualityFlags = ["skill_request_unresolved"]
    node.status = "error"
    node.data = {
      ...(node.data ?? {}),
      skill_name: skillName,
      request_status: requestStatus,
      available_skill_names: availableSkillNames,
      quality_flags: qualityFlags,
    }
    const callID = firstStringField(input, ["callID", "call_id"])
    const skillNode = this.causalNodes.find((item) => {
      if (item.kind !== "skill.load") return false
      if (item.status !== "running") return false
      if (item.title === skillName) return true
      const itemInput = recordFromUnknown(item.data?.input)
      return (
        firstStringField(itemInput, ["name"]) === skillName ||
        (callID !== undefined && firstStringField(itemInput, ["callID", "call_id"]) === callID)
      )
    })
    if (!skillNode) return
    skillNode.status = "error"
    skillNode.data = {
      ...(skillNode.data ?? {}),
      output: this.summarizeCausalValue(rawOutput, "skill.load.output"),
      error,
      skill_name: skillName,
      request_status: requestStatus,
      available_skill_names: availableSkillNames,
      quality_flags: qualityFlags,
    }
    if (skillNode.span_id) {
      const skillSpan = this.spans.get(skillNode.span_id)
      if (skillSpan?.status === "running") {
        const ended = Date.now()
        skillSpan.status = "error"
        skillSpan.end_time = new Date(ended).toISOString()
        skillSpan.end_ms = ended - this.startedAt
        skillSpan.duration_ms = Math.max(0, skillSpan.end_ms - skillSpan.start_ms)
        skillSpan.output_summary = this.summarizeJson(rawOutput, "skill.load.output")
        skillSpan.error = error
      }
    }
    skillNode.artifact_refs = this.collectArtifactRefs(skillNode.data)
    this.writeRecord("node.update", skillNode)
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
    const loopDecision = loopDecisionFromRuntimeEvent(input.component, input.event_type, input.data, {
      has_user_visible_response: this.responseSegments.some((segment) => segment.visibility === "user_visible"),
      has_final_answer: this.responseSegments.some((segment) => segment.response_role === "final_answer"),
    })
    if (loopDecision) {
      this.node({
        kind: "loop.decision",
        component: input.component,
        span_id: input.span_id,
        title: loopDecision.decision,
        status: "success",
        data: loopDecision,
      })
    }
    if (shouldPromoteRuntimeEvent(input.component, input.event_type, input.data)) {
      this.node({
        kind: "task.loop",
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
      context_ledger: input.context_ledger,
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
        context_ledger: input.context_ledger,
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
    const sourceRefs = input.source_refs ?? input.evidence_refs
    const decision: TraceSemanticDecision = {
      decision_id: input.decision_id ?? semanticID("dec", this.semanticDecisions.length + 1),
      span_id: input.span_id,
      component: input.component,
      decision_type: input.decision_type,
      intent: input.intent,
      chosen_action: input.chosen_action,
      rationale:
        input.rationale === undefined ? undefined : this.summarizeText(input.rationale, "semantic.decision.rationale"),
      source_refs: sourceRefs,
      metadata: input.metadata,
    }
    this.semanticDecisions.push(decision)
    this.write("semantic.decision", decision)
    this.node({
      node_id: `decisionnode_${decision.decision_id}`,
      kind: "decision",
      component: input.component,
      span_id: input.span_id,
      title: input.chosen_action ?? input.decision_type,
      status: "success",
      data: {
        decision_id: decision.decision_id,
        decision_type: input.decision_type,
        intent: input.intent,
        chosen_action: input.chosen_action,
        rationale: input.rationale,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      source_locations: dedupeSourceLocations([
        ...(input.source_locations ?? []),
        ...collectSourceLocations(input.metadata),
      ]),
      metadata: input.metadata,
    })
    return decision
  }

  promptAssembly(input: PromptAssemblyInput) {
    const sourceRefs = input.source_refs ?? input.evidence_refs
    const node = this.node({
      kind: "prompt.assembly",
      component: "prompt",
      title: input.stage,
      status: "success",
      data: {
        stage: input.stage,
        session_id: input.session_id,
        message_id: input.message_id,
        agent: input.agent,
        model: input.model,
        input: input.input,
        output: input.output,
        parts: input.parts,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      source_locations: dedupeSourceLocations([
        ...(input.source_locations ?? []),
        ...collectSourceLocations(input.input),
        ...collectSourceLocations(input.output),
        ...collectSourceLocations(input.parts),
      ]),
      metadata: input.metadata,
    })
    for (const ref of sourceRefs ?? []) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "node", id: node.node_id, label: "prompt.assembly" },
        relation: "prompt_to_message",
        label: "Prompt assembly consumed source record",
      })
    }
    for (const name of requestedSkillNames([input.input, input.parts])) this.requestedSkillNames.add(name)
    return node
  }

  contextTransform(input: ContextTransformInput) {
    const sourceRefs = input.source_refs ?? input.evidence_refs
    const node = this.node({
      kind: "context.transform",
      component: "context",
      title: input.stage,
      status: "success",
      data: {
        stage: input.stage,
        session_id: input.session_id,
        message_id: input.message_id,
        step: input.step,
        agent: input.agent,
        provider_id: input.provider_id,
        model_id: input.model_id,
        input: input.input,
        output: input.output,
        transforms: input.transforms,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      source_locations: dedupeSourceLocations([
        ...(input.source_locations ?? []),
        ...collectSourceLocations(input.input),
        ...collectSourceLocations(input.output),
        ...collectSourceLocations(input.transforms),
      ]),
      metadata: input.metadata,
    })
    for (const ref of sourceRefs ?? []) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "node", id: node.node_id, label: "context.transform" },
        relation: "context_transform",
        label: "Context transform consumed source record",
      })
    }
    if (input.stage === "model_messages_built" || input.stage === "llm_request_ready") {
      this.recordRequestedSkillAvailability(input.output, node.node_id)
    }
    return node
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
    const sourceLocations = dedupeSourceLocations(
      parsed
        .filter((failure) => failure.file)
        .map((failure) => ({
          uri: failure.file?.startsWith("file://") ? failure.file : undefined,
          path: failure.file?.startsWith("file://") ? undefined : failure.file,
          line_start: failure.line,
          line_end: failure.line,
        })),
    )
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
      source_locations: sourceLocations,
      source_refs: sourceLocations
        .map((location) => location.uri ?? location.path)
        .filter((item): item is string => Boolean(item)),
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
      source_locations: collectSourceLocations(input.files),
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
    const classifiedRefs = classifySourceRefs(sourceRefs)
    const visibility = input.visibility ?? (input.metadata?.visibility as string | undefined) ?? "user_visible"
    const turnIndex = input.turn_index ?? optionalNumber(input.metadata?.turn_index) ?? this.responseSegments.length + 1
    const responseRole =
      input.response_role ??
      (input.metadata?.response_role as TraceResponseSegment["response_role"] | undefined) ??
      this.inferResponseRole(visibility)
    const isFinalForCase =
      input.is_final_for_case ??
      (typeof input.metadata?.is_final_for_case === "boolean"
        ? input.metadata.is_final_for_case
        : responseRole === "final_answer")
    const sourceLocations = dedupeSourceLocations([
      ...(input.source_locations ?? []),
      ...collectSourceLocations(input.metadata),
    ])
    const metadata = {
      ...(input.metadata ?? {}),
      visibility,
      turn_index: turnIndex,
      response_role: responseRole,
      is_final_for_case: isFinalForCase,
      direct_evidence_refs: classifiedRefs.direct_evidence_refs,
      context_refs: classifiedRefs.context_refs,
      execution_refs: classifiedRefs.execution_refs,
    }
    const segment: TraceResponseSegment = {
      segment_id: input.segment_id ?? semanticID("segment", this.responseSegments.length + 1),
      response_artifact: input.response_artifact,
      text: this.summarizeText(input.text, "result.response.output"),
      response_role: responseRole,
      visibility,
      turn_index: turnIndex,
      is_final_for_case: isFinalForCase,
      direct_evidence_refs: classifiedRefs.direct_evidence_refs,
      context_refs: classifiedRefs.context_refs,
      execution_refs: classifiedRefs.execution_refs,
      source_refs: sourceRefs,
      source_locations: sourceLocations,
      metadata,
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
        response_role: responseRole,
        visibility,
        turn_index: turnIndex,
        is_final_for_case: isFinalForCase,
        direct_evidence_refs: classifiedRefs.direct_evidence_refs,
        context_refs: classifiedRefs.context_refs,
        execution_refs: classifiedRefs.execution_refs,
        source_locations: sourceLocations,
        metadata,
      },
      source_refs: sourceRefs,
      source_locations: sourceLocations,
    })
    for (const ref of classifiedRefs.direct_evidence_refs) {
      this.linkSourceToResponse(ref, record.node_id)
    }
    return segment
  }

  responseClaim(input: ResponseClaimInput) {
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const classifiedRefs = classifySourceRefs(sourceRefs)
    const evidenceMatch = this.matchEvidenceForClaim(input.text, classifiedRefs.direct_evidence_refs)
    const effectiveDirectEvidenceRefs = evidenceMatch.refs.length
      ? evidenceMatch.refs
      : classifiedRefs.direct_evidence_refs.length <= 1
        ? classifiedRefs.direct_evidence_refs
        : []
    const legacyContextRefs = sourceRefs.filter((ref) => !effectiveDirectEvidenceRefs.includes(ref))
    const effectiveClassifiedRefs = {
      ...classifiedRefs,
      direct_evidence_refs: effectiveDirectEvidenceRefs,
    }
    const sourceLocations = dedupeSourceLocations([
      ...(input.source_locations ?? []),
      ...collectSourceLocations(input.text),
      ...collectSourceLocations(input.metadata),
    ])
    const supportLevel = input.support_level ?? responseClaimSupportLevel(effectiveClassifiedRefs)
    const qualityFlags = dedupeStrings([
      ...(input.quality_flags ?? []),
      ...responseClaimQualityFlags(effectiveClassifiedRefs),
      ...(isBrokenClaimFragment(input.text) ? ["broken_claim_fragment"] : []),
      ...(evidenceMatch.weak ? ["weak_evidence_match"] : []),
      ...(classifiedRefs.direct_evidence_refs.length && !evidenceMatch.refs.length
        ? ["unmatched_direct_evidence_refs"]
        : []),
    ])
    const claim: TraceResponseClaimRecord = {
      claim_id: input.claim_id ?? semanticID("claim", this.causalNodes.length + 1),
      response_segment_id: input.response_segment_id,
      text: this.summarizeText(input.text, "result.response.claim"),
      claim_index: input.claim_index,
      direct_evidence_refs: effectiveDirectEvidenceRefs,
      context_refs: classifiedRefs.context_refs,
      execution_refs: classifiedRefs.execution_refs,
      legacy_context_refs: legacyContextRefs,
      matched_evidence_refs: evidenceMatch.refs,
      candidate_evidence_refs: evidenceMatch.candidateRefs,
      match_strategy: evidenceMatch.strategy,
      match_score: evidenceMatch.score,
      match_reasons: evidenceMatch.reasons,
      original_direct_evidence_refs: classifiedRefs.direct_evidence_refs,
      source_refs: sourceRefs,
      source_locations: sourceLocations,
      support_level: supportLevel,
      quality_flags: qualityFlags,
      metadata: omitUndefined({
        ...(input.metadata ?? {}),
        original_direct_evidence_refs: classifiedRefs.direct_evidence_refs,
      }),
    }
    const node = this.node({
      node_id: `responseclaim_${claim.claim_id}`,
      kind: "response.claim",
      component: "result",
      title: `Response claim ${claim.claim_index}`,
      status: "success",
      data: {
        claim_id: claim.claim_id,
        response_segment_id: claim.response_segment_id,
        text: input.text,
        claim_index: claim.claim_index,
        direct_evidence_refs: claim.direct_evidence_refs,
        context_refs: claim.context_refs,
        execution_refs: claim.execution_refs,
        legacy_context_refs: claim.legacy_context_refs,
        matched_evidence_refs: claim.matched_evidence_refs,
        candidate_evidence_refs: claim.candidate_evidence_refs,
        match_strategy: claim.match_strategy,
        match_score: claim.match_score,
        match_reasons: claim.match_reasons,
        original_direct_evidence_refs: claim.original_direct_evidence_refs,
        support_level: claim.support_level,
        quality_flags: claim.quality_flags,
        source_locations: sourceLocations,
        metadata: claim.metadata,
      },
      source_refs: sourceRefs,
      source_locations: sourceLocations,
      metadata: claim.metadata,
    })
    const responseNodeID =
      input.metadata && typeof input.metadata.response_node_id === "string"
        ? input.metadata.response_node_id
        : undefined
    if (responseNodeID) {
      this.causalEdge({
        from: { type: "node", id: responseNodeID, label: "response.output" },
        to: { type: "response_claim", id: node.node_id, label: "response.claim" },
        relation: "response_to_claim",
        label: "Response output was split into a claim",
      })
    }
    for (const ref of claim.direct_evidence_refs) this.linkSourceToClaim(ref, node.node_id, "evidence_to_claim")
    for (const ref of claim.context_refs) this.linkSourceToClaim(ref, node.node_id, "context_to_claim")
    for (const ref of claim.execution_refs) this.linkSourceToClaim(ref, node.node_id, "execution_to_claim")
    return claim
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

  private inferResponseRole(visibility: string | undefined): TraceResponseSegment["response_role"] {
    if (visibility === "internal_continue") return "intermediate_summary"
    if (visibility === "compaction_followup") return "auto_continue_summary"
    return "final_answer"
  }

  private normalizeFinalResponseSegments() {
    const finalSegments = this.responseSegments.filter((segment) => segment.response_role === "final_answer")
    const finalSegment = finalSegments.at(-1)
    for (const segment of this.responseSegments) {
      const shouldBeFinal = segment === finalSegment
      if (segment.response_role !== "final_answer") {
        segment.is_final_for_case = false
      } else if (!shouldBeFinal) {
        segment.response_role = "intermediate_summary"
        segment.is_final_for_case = false
      } else {
        segment.is_final_for_case = shouldBeFinal
      }
      segment.metadata = {
        ...(segment.metadata ?? {}),
        response_role: segment.response_role,
        is_final_for_case: segment.is_final_for_case,
      }
      const node = this.causalNodes.find((item) => item.node_id === `responsenode_${segment.segment_id}`)
      if (!node?.data) continue
      node.data.response_role = segment.response_role
      node.data.is_final_for_case = segment.is_final_for_case
      const metadata =
        node.data.metadata && typeof node.data.metadata === "object"
          ? (node.data.metadata as Record<string, unknown>)
          : {}
      node.data.metadata = {
        ...metadata,
        response_role: segment.response_role,
        is_final_for_case: segment.is_final_for_case,
      }
    }
  }

  private emitFinalResponseClaims() {
    for (const segment of this.responseSegments) {
      if (this.claimedResponseSegmentIDs.has(segment.segment_id)) continue
      if (segment.response_role !== "final_answer") continue
      if (segment.visibility !== "user_visible") continue
      if (segment.is_final_for_case !== true) continue
      const responseNodeID = `responsenode_${segment.segment_id}`
      const responseNode = this.causalNodes.find((item) => item.node_id === responseNodeID)
      const responseText = responseNode?.data?.text ?? fieldSummaryText(segment.text)
      const claims = splitResponseClaims(responseText)
      claims.forEach((claim, index) => {
        this.responseClaim({
          response_segment_id: segment.segment_id,
          text: claim,
          claim_index: index + 1,
          source_refs: segment.source_refs,
          source_locations: segment.source_locations,
          metadata: {
            response_node_id: responseNodeID,
            response_role: segment.response_role,
            turn_index: segment.turn_index,
            is_final_for_case: segment.is_final_for_case,
          },
        })
      })
      this.claimedResponseSegmentIDs.add(segment.segment_id)
    }
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

  llmTurn(input: LlmTurnInput) {
    const turnID = input.turn_id ?? input.span_id ?? semanticID("llmturn", this.causalNodes.length + 1)
    const usage = input.token_usage ? normalizeTokenUsage(input.token_usage) : undefined
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const data: Record<string, unknown> = omitUndefined({
      turn_id: turnID,
      span_id: input.span_id,
      session_id: input.session_id,
      parent_session_id: input.parent_session_id,
      message_id: input.message_id,
      agent: input.agent,
      agent_role: input.agent_role,
      provider_id: input.provider_id,
      model_id: input.model_id,
      input_context_refs: input.input_context_refs,
      prompt_transform_refs: input.prompt_transform_refs,
      tool_schema_ref: input.tool_schema_ref,
      request_id: input.request_id,
      status: input.status,
      duration_ms: input.duration_ms,
      finish_reason: input.finish_reason,
      stop_reason: input.stop_reason,
      token_usage: cloneTokenUsage(usage),
      is_background: input.is_background,
      metadata: input.metadata,
    })
    const nodeID = `llmturn_${turnID}`
    const existing = this.causalNodes.find((item) => item.node_id === nodeID)
    if (existing) {
      existing.status = input.status ?? existing.status
      existing.data = {
        ...(existing.data ?? {}),
        ...data,
      }
      existing.source_refs = mergeRefs(existing.source_refs, sourceRefs)
      existing.artifact_refs = this.collectArtifactRefs(existing.data)
      this.writeRecord("node.update", existing)
      this.writePartial()
      return existing
    }
    const node = this.node({
      node_id: nodeID,
      kind: "llm.turn",
      component: "llm",
      span_id: input.span_id,
      title: `${input.agent ?? "agent"} ${input.provider_id ?? "provider"}/${input.model_id ?? "model"}`,
      status: input.status ?? "running",
      data,
      source_refs: sourceRefs,
      metadata: input.metadata,
    })
    if (input.span_id) {
      this.causalEdge({
        from: { type: "span", id: input.span_id, label: "llm.call" },
        to: { type: "llm_turn", id: turnID, label: "llm.turn" },
        relation: "derived_from",
        label: "LLM turn normalized from provider span",
      })
    }
    return node
  }

  agentLifecycle(input: AgentLifecycleInput) {
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    return this.node({
      node_id: `lifecycle_${input.lifecycle_id ?? semanticID("life", this.causalNodes.length + 1)}`,
      kind: "agent.lifecycle",
      component: "processor",
      span_id: input.span_id,
      title: input.phase,
      status: input.status,
      data: {
        lifecycle_id: input.lifecycle_id,
        session_id: input.session_id,
        message_id: input.message_id,
        agent: input.agent,
        phase: input.phase,
        status: input.status,
        summary:
          input.summary === undefined ? undefined : this.summarizeCausalValue(input.summary, "agent.lifecycle.summary"),
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      metadata: input.metadata,
    })
  }

  exitGate(input: ExitGateInput) {
    const gateID = input.gate_id ?? semanticID("gate", this.causalNodes.length + 1)
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    return this.node({
      node_id: `exitgate_${gateID}`,
      kind: "exit.gate",
      component: "processor",
      span_id: input.span_id,
      title: input.decision,
      status: input.decision === "cancel" ? "cancelled" : "success",
      data: {
        gate_id: gateID,
        session_id: input.session_id,
        message_id: input.message_id,
        has_final_answer: input.has_final_answer,
        needs_compaction: input.needs_compaction,
        auto_continue: input.auto_continue,
        synthetic_continue: input.synthetic_continue,
        continuation_source: input.continuation_source,
        decision: input.decision,
        reason: input.reason,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      metadata: input.metadata,
    })
  }

  evidenceFact(input: EvidenceFactInput) {
    const factID = input.fact_id ?? semanticID("fact", this.causalNodes.length + 1)
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const sourceLocations = dedupeSourceLocations([
      ...(input.source_locations ?? []),
      ...collectSourceLocations(input.data),
      ...collectSourceLocations(input.metadata),
    ])
    const canonical = canonicalEvidence(input, sourceLocations)
    const node = this.node({
      node_id: `evidence_${factID}`,
      kind: "evidence.fact",
      component: this.componentForObservationSource(input.source),
      span_id: input.span_id,
      title: input.category ?? input.source,
      status: "success",
      data: {
        fact_id: factID,
        source: input.source,
        category: input.category,
        summary: input.summary,
        data: input.data,
        fact_kind: canonical.fact_kind,
        canonical_subject: canonical.canonical_subject,
        claim: canonical.claim,
        structured_claim: canonical.structured_claim,
        support_level: canonical.support_level,
        quality_flags: canonical.quality_flags,
        confidence: input.confidence ?? "observed",
        source_locations: sourceLocations,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      source_locations: sourceLocations,
      metadata: input.metadata,
    })
    this.remember(this.recentEvidenceNodeIDs, node.node_id)
    for (const ref of sourceRefs ?? []) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "evidence", id: node.node_id, label: "evidence.fact" },
        relation: "derived_from",
        label: "Evidence fact derived from source record",
      })
    }
    return node
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
      source_locations: input.source_locations,
      typed_resources: input.typed_resources,
      artifact_refs: [],
      metadata: input.metadata,
    }
    node.artifact_refs = this.collectArtifactRefs(node.data)
    this.causalNodes.push(node)
    if (node.kind === "prompt.assembly") this.remember(this.recentPromptNodeIDs, node.node_id)
    if (node.kind === "context.pack" || node.kind === "context.transform")
      this.remember(this.recentContextNodeIDs, node.node_id)
    if (node.kind === "llm.call") this.remember(this.recentLLMNodeIDs, node.node_id)
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
    if (isWeakObservation(input)) {
      this.writeRecord("observation.suppressed", {
        source: input.source,
        category: input.category,
        reason: "path-only tool_output observation has no independent attribution value",
      })
      return undefined
    }
    const sourceRefs = input.source_refs ?? input.evidence_refs
    const semanticExtras = observationSemanticExtras(input.source, input.data)
    const sourceLocations = dedupeSourceLocations([
      ...(input.source_locations ?? []),
      ...collectSourceLocations(input.data),
      ...collectSourceLocations(input.metadata),
      ...(semanticExtras.source_locations ?? []),
    ])
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
        source_locations: sourceLocations,
        typed_resources: semanticExtras.typed_resources,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      source_locations: sourceLocations,
      typed_resources: semanticExtras.typed_resources,
      metadata: input.metadata,
    })
    for (const ref of sourceRefs ?? []) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "node", id: node.node_id, label: "observation" },
        relation:
          parsed.type === "compaction" || parsed.id.startsWith("compaction")
            ? "compaction_to_observation"
            : "source_to_observation",
        label: "Observation produced from source record",
      })
    }
    if (isEvidenceWorthyObservation(input)) {
      this.evidenceFact({
        source: input.source,
        category: input.category,
        summary: input.summary,
        data: input.data,
        span_id: input.span_id,
        source_refs: [`observation:${node.node_id}`],
        source_locations: sourceLocations,
        confidence: "observed",
        metadata: input.metadata,
      })
    }
    return node
  }

  compaction(input: CompactionRecordInput) {
    const sourceRefs = input.source_refs ?? input.evidence_refs
    const contextLedger = traceContextLedger(input)
    const previousSummary =
      input.previous_summary === undefined
        ? undefined
        : this.summarizeCausalValue(input.previous_summary, "context.compaction.previous_summary")
    const serializedTail =
      input.serialized_tail === undefined
        ? undefined
        : this.summarizeCausalValue(input.serialized_tail, "context.compaction.serialized_tail")
    const outputSummary =
      input.output_summary === undefined
        ? undefined
        : this.summarizeCausalValue(input.output_summary, "context.compaction.output_summary")
    contextLedger.summary_artifact_id = contextLedger.summary_artifact_id ?? this.collectArtifactRefs(outputSummary)[0]
    if (contextLedger.summary_artifact_id && contextLedger.quality_flags?.includes("summary_artifact_pending")) {
      contextLedger.quality_flags = contextLedger.quality_flags.filter((flag) => flag !== "summary_artifact_pending")
    }
    const serializedTailArtifactRef = this.collectArtifactRefs(serializedTail)[0]
    const summaryArtifactRef = contextLedger.summary_artifact_id ?? this.collectArtifactRefs(outputSummary)[0]
    const metadata = input.metadata ?? {}
    const afterContextRefs = dedupeStrings([
      ...(input.after_context_refs ?? []),
      ...(stringArrayField(metadata, ["after_context_refs", "afterContextRefs"]) ?? []),
    ])
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
        previous_summary: previousSummary,
        serialized_tail: serializedTail,
        output_summary: outputSummary,
        algorithm: contextLedger.algorithm,
        before_context_refs: sourceRefs ?? [],
        after_context_refs: afterContextRefs,
        serialized_tail_artifact_ref: serializedTailArtifactRef,
        summary_artifact_ref: summaryArtifactRef,
        retained_message_ids: contextLedger.retained_message_ids,
        dropped_message_ids: contextLedger.dropped_message_ids,
        retained_fact_refs: contextLedger.retained_fact_refs,
        dropped_fact_refs: contextLedger.dropped_fact_refs,
        token_estimate_before: contextLedger.token_estimate_before,
        token_estimate_after: contextLedger.token_estimate_after,
        auto_continue_prompt_ref: contextLedger.auto_continue_prompt_ref,
        context_ledger: contextLedger,
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
      ...this.recentPromptNodeIDs.slice(-2).map((id) => `prompt:${id}`),
      ...this.recentContextNodeIDs.slice(-3).map((id) => `context:${id}`),
      ...this.recentContextSnapshotIDs.slice(-2).map((id) => `context_snapshot:${id}`),
      ...this.recentLLMNodeIDs.slice(-2).map((id) => `llm:${id}`),
      ...this.recentEvidenceNodeIDs.slice(-6).map((id) => `evidence:${id}`),
      ...this.recentToolSpanIDs.slice(-3).map((id) => `tool_span:${id}`),
      ...this.recentVerificationIDs.slice(-3).map((id) => `verification:${id}`),
      ...this.recentChangeIDs.slice(-3).map((id) => `change:${id}`),
    ].filter((item, index, array) => array.indexOf(item) === index)
  }

  finish(input?: FinishTraceInput) {
    if (this.finished) return
    this.evaluateConstraints()
    this.normalizeFinalResponseSegments()
    this.emitFinalResponseClaims()
    const error = input?.error ? errorInfo(input.error) : undefined
    if (error) this.errors.push(error)
    this.result = input?.result ?? this.result
    const status = input?.status ?? (error ? "error" : "success")
    this.finalizeOpenRecords(status)
    this.finished = true
    const summary = this.summary(status)
    const provenance = this.provenanceSummary(summary.status)
    this.write("trace.finish", summary)
    this.writeRecord("finish", provenance.manifest)
    this.safeWrite(this.manifestFile, jsonPretty(provenance.manifest))
    this.safeWrite(this.provenanceTraceFile, jsonPretty(provenance))
    this.writePartial(true, provenance)
    this.safeWrite(this.traceFile, jsonPretty(provenance))
    this.safeWrite(this.legacyTraceFile, jsonPretty(summary))
    this.safeWrite(this.htmlFile, renderProvenanceTraceHtml(provenance))
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
      token_usage: cloneTokenUsage(this.tokenUsage) ?? {},
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
      trace_version: TRACE_VERSION,
      case_id: this.caseID,
      run_id: this.runID,
      session_id: this.sessionID,
      started_at: this.startedIso,
      ended_at: new Date(ended).toISOString(),
      duration_ms: Math.max(0, ended - this.startedAt),
      status,
      input: this.input,
      environment: this.environment,
      token_usage: cloneTokenUsage(this.tokenUsage) ?? {},
      result: this.result,
      files: {
        trace: "trace.json",
        legacy_trace: "legacy-trace.json",
        provenance_trace: "provenance-trace.json",
        trace_html: "trace.html",
        records: "records.jsonl",
        raw_events: "raw-events.jsonl",
        partial_latest: "partial/latest.json",
      },
    }
  }

  private provenanceSummary(status: TraceStatus): ProvenanceTraceSummary {
    const manifest = this.manifest(status)
    const records = this.provenanceRecords()
    const dataflowEdges = this.provenanceDataflowEdges()
    const traceHealth = this.traceHealth(records)
    return {
      trace_version: TRACE_VERSION,
      manifest,
      records,
      dataflow_edges: dataflowEdges,
      artifacts: this.artifacts,
      metrics: {
        spans: this.spans.size,
        events: this.events.length,
        records: records.length,
        dataflow_edges: dataflowEdges.length,
        artifacts: this.artifacts.length,
        token_usage: cloneTokenUsage(this.tokenUsage) ?? {},
        trace_health: traceHealth,
      },
    }
  }

  private finalizeOpenRecords(status: TraceStatus) {
    const finalStatus: TraceStatus = status === "running" ? "cancelled" : status
    const finalizedReason =
      finalStatus === "cancelled" ? "trace_cancelled" : finalStatus === "error" ? "trace_error" : "trace_finished"
    const finalizedAt = new Date().toISOString()
    for (const span of this.spans.values()) {
      if (span.status !== "running") continue
      span.status = finalStatus
      span.end_time = finalizedAt
      span.end_ms = Date.now() - this.startedAt
      span.duration_ms = Math.max(0, span.end_ms - span.start_ms)
      span.metadata = {
        ...(span.metadata ?? {}),
        finalized_status: "finalized_without_close",
        finalized_reason: finalizedReason,
      }
    }
    for (const node of this.causalNodes) {
      if (node.status !== "running") continue
      node.status = finalStatus
      node.data = {
        ...(node.data ?? {}),
        finalized_status: "finalized_without_close",
        finalized_reason: finalizedReason,
        finalized_at: finalizedAt,
        original_status: "running",
      }
      node.artifact_refs = this.collectArtifactRefs(node.data)
      this.writeRecord("node.update", node)
    }
  }

  private traceHealth(records: ProvenanceRecord[]): TraceHealthMetrics {
    const issues: TraceHealthIssue[] = []
    const circularReferenceMarkers = countCircularMarkers(records)
    const openRecords = records.filter((record) => record.status === "running")
    const finalizedOpenRecords = records.filter((record) => record.data?.finalized_status === "finalized_without_close")
    const expectedFinalizedTypes = new Set([
      "run.start",
      "task.loop",
      "llm.call",
      "llm.turn",
      "agent.lifecycle",
      "prompt.assembly",
      "context.transform",
    ])
    const expectedLifecycleFinalizedRecords = finalizedOpenRecords.filter(
      (record) => record.status === "success" && expectedFinalizedTypes.has(record.event_type),
    )
    const unexpectedMissingCloseRecords = finalizedOpenRecords.filter(
      (record) => !expectedLifecycleFinalizedRecords.includes(record),
    )
    for (const record of unexpectedMissingCloseRecords.slice(0, 20)) {
      issues.push({
        kind: "unexpected_missing_close_record",
        severity: "warning",
        message: "Record was open at trace finish and was finalized without an expected lifecycle close policy.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of openRecords.slice(0, 20)) {
      issues.push({
        kind: "open_record",
        severity: "warning",
        message: "Record is still running after trace finalization.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    const llmTurns = records.filter((record) => record.event_type === "llm.turn")
    const backgroundLlmTurns = llmTurns.filter(
      (record) =>
        record.data?.agent_role === "title" ||
        record.data?.agent_role === "background" ||
        record.data?.is_background === true,
    )
    const llmTurnsMissingTokenUsage = llmTurns.filter(
      (record) => record.data?.agent_role !== "title" && !record.token_usage?.total,
    )
    const llmTurnsMissingFinishReason = llmTurns.filter(
      (record) => record.data?.agent_role !== "title" && !record.data?.finish_reason,
    )
    for (const record of llmTurnsMissingTokenUsage.slice(0, 20)) {
      issues.push({
        kind: "llm_turn_missing_token_usage",
        severity: "warning",
        message: "LLM turn has no token usage.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of llmTurnsMissingFinishReason.slice(0, 20)) {
      issues.push({
        kind: "llm_turn_missing_finish_reason",
        severity: "info",
        message: "LLM turn has no finish reason.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    const compactionQualityFlags: Record<string, number> = {}
    for (const record of records.filter((item) => item.event_type === "context.compaction")) {
      const contextLedger = objectField(record.data, "context_ledger")
      aggregateFlags(compactionQualityFlags, objectField(contextLedger, "quality_flags"))
    }
    for (const [flag, count] of Object.entries(compactionQualityFlags)) {
      issues.push({
        kind: "compaction_quality_flag",
        severity: flag.includes("missing") || flag.includes("pending") ? "warning" : "info",
        message: `Compaction quality flag observed: ${flag}.`,
        count,
        metadata: { flag },
      })
    }
    const emptySubagentRecords = records.filter((record) => {
      if (record.event_type !== "subagent.call" && record.event_type !== "evidence.fact") return false
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("empty_subagent_result")
    })
    for (const record of emptySubagentRecords.slice(0, 20)) {
      issues.push({
        kind: "empty_subagent_result",
        severity: "warning",
        message: "Subagent returned no substantive task result.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    const broadResponses = records.filter((record) => {
      if (record.event_type !== "response.output") return false
      const sourceCount = record.source_refs?.length ?? 0
      const directCount = Array.isArray(record.data?.direct_evidence_refs) ? record.data.direct_evidence_refs.length : 0
      return sourceCount > 8 && directCount < sourceCount
    })
    for (const record of broadResponses.slice(0, 20)) {
      issues.push({
        kind: "broad_response_refs",
        severity: "info",
        message: "Response has broad legacy source refs; prefer direct_evidence_refs for attribution.",
        record_id: record.record_id,
        event_type: record.event_type,
        count: record.source_refs?.length,
      })
    }
    const evidenceKeys = new Map<string, number>()
    for (const record of records.filter((item) => item.event_type === "evidence.fact")) {
      const key = [record.data?.source, record.data?.category, record.data?.canonical_subject, record.data?.claim].join(
        "|",
      )
      evidenceKeys.set(key, (evidenceKeys.get(key) ?? 0) + 1)
    }
    const duplicateEvidenceFacts = [...evidenceKeys.values()]
      .filter((count) => count > 1)
      .reduce((a, b) => a + b - 1, 0)
    if (duplicateEvidenceFacts) {
      issues.push({
        kind: "duplicate_evidence_facts",
        severity: "info",
        message: "Duplicate evidence facts were observed.",
        count: duplicateEvidenceFacts,
      })
    }
    const evidenceFacts = records.filter((record) => record.event_type === "evidence.fact")
    const genericEvidenceFacts = evidenceFacts.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && (flags.includes("generic_claim") || flags.includes("fallback_summary_claim"))
    })
    const genericMcpFacts = evidenceFacts.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("generic_mcp_fact")
    })
    const pathOnlyEvidenceFacts = evidenceFacts.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("path_only_evidence_fact")
    })
    const mcpJsonParseShadowed = evidenceFacts.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("mcp_json_parse_shadowed")
    })
    for (const record of genericEvidenceFacts.slice(0, 20)) {
      issues.push({
        kind: "generic_evidence_fact",
        severity: "info",
        message: "Evidence fact used a generic or fallback claim; offline attribution should inspect its raw artifact.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of genericMcpFacts.slice(0, 20)) {
      issues.push({
        kind: "generic_mcp_fact",
        severity: "warning",
        message: "MCP evidence was not converted into a structured fact.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of pathOnlyEvidenceFacts.slice(0, 20)) {
      issues.push({
        kind: "path_only_evidence_fact",
        severity: "info",
        message: "File evidence only identified a path without a line-level semantic fact.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    const responseClaims = records.filter((record) => record.event_type === "response.claim")
    const unsupportedResponseClaims = responseClaims.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("unsupported_response_claim")
    })
    const contextOnlyResponseClaims = responseClaims.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("context_only_claim")
    })
    const brokenClaimFragments = responseClaims.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("broken_claim_fragment")
    })
    const overAttributedClaims = responseClaims.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("over_attributed_claim")
    })
    const legacyContextRefClaims = responseClaims.filter((record) => {
      const legacyRefs = record.data?.legacy_context_refs
      return Array.isArray(legacyRefs) && legacyRefs.length > 0
    })
    const weakEvidenceMatches = responseClaims.filter((record) => {
      const flags = record.data?.quality_flags
      return Array.isArray(flags) && flags.includes("weak_evidence_match")
    })
    const responseSegments = new Map(
      records
        .filter((record) => record.event_type === "response.output")
        .map((record) => [String(record.data?.segment_id ?? ""), record]),
    )
    const nonFinalResponseClaims = responseClaims.filter((record) => {
      const segment = responseSegments.get(String(record.data?.response_segment_id ?? ""))
      return (
        !segment ||
        segment.data?.response_role !== "final_answer" ||
        segment.data?.visibility !== "user_visible" ||
        segment.data?.is_final_for_case !== true
      )
    })
    const unresolvedSkillRequests = records.filter((record) => {
      if (record.event_type !== "skill.load") return false
      const flags = record.data?.quality_flags
      return (
        (Array.isArray(flags) && flags.includes("skill_request_unresolved")) ||
        record.data?.request_status === "missing" ||
        record.data?.request_status === "requested"
      )
    })
    for (const record of unsupportedResponseClaims.slice(0, 20)) {
      issues.push({
        kind: "unsupported_response_claim",
        severity: "warning",
        message: "Response claim has no direct evidence, context, or execution refs.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of brokenClaimFragments.slice(0, 20)) {
      issues.push({
        kind: "broken_claim_fragment",
        severity: "warning",
        message: "Response claim appears to be a fragment produced by sentence splitting.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of overAttributedClaims.slice(0, 20)) {
      issues.push({
        kind: "over_attributed_claim",
        severity: "info",
        message: "Response claim had broader source evidence than the matched direct evidence refs.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of nonFinalResponseClaims.slice(0, 20)) {
      issues.push({
        kind: "non_final_response_claim",
        severity: "warning",
        message: "Response claim is attached to a response segment that is not the final user-visible answer.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of weakEvidenceMatches.slice(0, 20)) {
      issues.push({
        kind: "weak_evidence_match",
        severity: "info",
        message: "Response claim was supported only by a weak evidence overlap.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    for (const record of unresolvedSkillRequests.slice(0, 20)) {
      issues.push({
        kind: "skill_request_unresolved",
        severity: "info",
        message: "User requested a skill that was not observed as loaded in the model context.",
        record_id: record.record_id,
        event_type: record.event_type,
      })
    }
    const payloadDuplicationGroups = this.artifacts.filter((artifact) => (artifact.occurrences ?? 1) > 1).length
    const compactionRecords = records.filter((record) => record.event_type === "context.compaction")
    const compactionCheckRecords = records.filter((record) => record.event_type === "context.compaction_check")
    const compactionCheckMissing =
      compactionRecords.length && !compactionCheckRecords.length ? compactionRecords.length : 0
    if (compactionCheckMissing) {
      issues.push({
        kind: "compaction_check_missing",
        severity: "warning",
        message: "Compaction record was observed without a preceding compaction check record.",
        count: compactionCheckMissing,
      })
    }
    return {
      circular_reference_markers: circularReferenceMarkers,
      open_records: openRecords.length,
      finalized_open_records: finalizedOpenRecords.length,
      expected_lifecycle_finalized_records: expectedLifecycleFinalizedRecords.length,
      unexpected_missing_close_records: unexpectedMissingCloseRecords.length,
      llm_turns_missing_token_usage: llmTurnsMissingTokenUsage.length,
      llm_turns_missing_finish_reason: llmTurnsMissingFinishReason.length,
      compaction_quality_flags: compactionQualityFlags,
      empty_subagent_results: emptySubagentRecords.length,
      broad_response_refs: broadResponses.length,
      duplicate_evidence_facts: duplicateEvidenceFacts,
      generic_evidence_facts: genericEvidenceFacts.length,
      unsupported_response_claims: unsupportedResponseClaims.length,
      context_only_response_claims: contextOnlyResponseClaims.length,
      payload_duplication_groups: payloadDuplicationGroups,
      compaction_check_missing: compactionCheckMissing,
      broken_claim_fragments: brokenClaimFragments.length,
      over_attributed_claims: overAttributedClaims.length,
      generic_mcp_facts: genericMcpFacts.length,
      path_only_evidence_facts: pathOnlyEvidenceFacts.length,
      non_final_response_claims: nonFinalResponseClaims.length,
      weak_evidence_matches: weakEvidenceMatches.length,
      mcp_json_parse_shadowed: mcpJsonParseShadowed.length,
      skill_request_unresolved: unresolvedSkillRequests.length,
      background_llm_turns: backgroundLlmTurns.length,
      legacy_context_ref_claims: legacyContextRefClaims.length,
      issues,
    }
  }

  private provenanceRecords(): ProvenanceRecord[] {
    return this.causalNodes
      .map((node) => ({
        node,
        eventType: node.kind === "final.claim" ? "response.output" : node.kind,
      }))
      .filter((item) => isFormalRecordType(item.eventType))
      .map(({ node, eventType }) => ({
        record_id: node.node_id,
        component: node.component,
        event_type: eventType,
        span_id: node.span_id,
        timestamp: node.timestamp,
        time_ms: node.time_ms,
        title: node.title,
        status: node.status,
        duration_ms: optionalNumber(node.data?.duration_ms),
        token_usage: cloneTokenUsage(node.data?.token_usage as TraceTokenUsage | undefined),
        error: node.data?.error,
        source_refs: node.source_refs,
        source_locations: node.source_locations,
        typed_resources: node.typed_resources,
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
      relation: normalizeRelation(edge.relation),
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
        for (const verification of this.verificationRecords)
          sourceRefs.add(`verification:${verification.verification_id}`)
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

  private recordRequestedSkillAvailability(contextOutput: unknown, sourceNodeID: string) {
    if (!this.requestedSkillNames.size) return
    const available = availableSkillNames(contextOutput)
    const availableSet = new Set(available)
    for (const skillName of this.requestedSkillNames) {
      if (this.recordedSkillRequestNames.has(skillName)) continue
      this.recordedSkillRequestNames.add(skillName)
      const requestStatus = availableSet.has(skillName) ? "requested" : "missing"
      this.node({
        node_id: `skillrequest_${hash(skillName).slice(0, 8)}`,
        kind: "skill.load",
        component: "skill",
        title: `Skill request: ${skillName}`,
        status: "success",
        data: {
          skill_name: skillName,
          request_source: "user_prompt",
          request_status: requestStatus,
          available_skill_names: available,
          quality_flags: requestStatus === "missing" ? ["skill_request_unresolved"] : [],
        },
        source_refs: [`context:${sourceNodeID}`],
      })
    }
  }

  compactionCheck(input: CompactionCheckInput) {
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const checkID = input.check_id ?? semanticID("compactioncheck", this.causalNodes.length + 1)
    const usage = input.token_usage ? normalizeTokenUsage(input.token_usage) : undefined
    const data = omitUndefined({
      check_id: checkID,
      session_id: input.session_id,
      message_id: input.message_id,
      provider_id: input.provider_id,
      model_id: input.model_id,
      token_usage: usage,
      token_estimate: input.token_estimate,
      context_limit: input.context_limit,
      reserved_tokens: input.reserved_tokens,
      overflow: input.overflow,
      selected_algorithm: input.selected_algorithm,
      trigger_reason: input.trigger_reason,
      metadata: input.metadata,
    })
    const node = this.node({
      node_id: `compactioncheck_${checkID}`,
      kind: "context.compaction_check",
      component: "context",
      span_id: input.span_id,
      title: input.overflow ? "Compaction check overflow" : "Compaction check",
      status: "success",
      data,
      source_refs: sourceRefs,
      metadata: input.metadata,
    })
    for (const ref of sourceRefs ?? []) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "compaction_check", id: node.node_id, label: "context.compaction_check" },
        relation: "derived_from",
        label: "Compaction check used source record",
      })
    }
    return node
  }

  private parseSourceRef(ref: string): TraceRef | undefined {
    const index = ref.indexOf(":")
    if (index === -1) return undefined
    return {
      type: ref.slice(0, index),
      id: ref.slice(index + 1),
    }
  }

  private evidenceNodeForRef(ref: string) {
    const parsed = this.parseSourceRef(ref)
    if (!parsed || parsed.type !== "evidence") return undefined
    return this.causalNodes.find((node) => node.node_id === parsed.id || node.node_id === `evidence_${parsed.id}`)
  }

  private matchEvidenceForClaim(claimText: unknown, evidenceRefs: string[]) {
    const scored = evidenceRefs
      .map((ref) => {
        const node = this.evidenceNodeForRef(ref)
        const analysis = evidenceMatchAnalysis(claimText, node?.data)
        return {
          ref,
          score: analysis.score,
          reasons: analysis.reasons,
          weak: analysis.weak,
          factKind: typeof node?.data?.fact_kind === "string" ? node.data.fact_kind : undefined,
        }
      })
      .filter((item) => item.score >= 0.35)
      .sort((a, b) => b.score - a.score)
    const isVerificationClaim = isVerificationClaimText(stringPreview(claimText, 2000))
    const preferred =
      isVerificationClaim && scored.some((item) => item.factKind === "verification_output")
        ? scored.filter((item) => item.factKind === "verification_output")
        : scored
    const refs = preferred.slice(0, 3).map((item) => item.ref)
    const score = preferred[0]?.score ?? 0
    const reasons = dedupeStrings(preferred.flatMap((item) => item.reasons))
    const weak = preferred.some((item) => item.weak)
    return {
      refs,
      score,
      reasons,
      weak,
      candidateRefs: evidenceRefs,
      strategy: refs.length ? "structured_text_overlap" : evidenceRefs.length ? "no_direct_match" : "no_evidence_refs",
    }
  }

  private linkSourceToResponse(ref: string, responseNodeID: string) {
    const parsed = this.parseSourceRef(ref)
    if (!parsed) return
    this.causalEdge({
      from: parsed,
      to: { type: "node", id: responseNodeID, label: "response.output" },
      relation: parsed.type === "evidence" ? "evidence_to_response" : "source_to_response",
      label:
        parsed.type === "evidence"
          ? "Evidence fact supported response output"
          : "Response output consumed source record",
    })
  }

  private linkSourceToClaim(
    ref: string,
    claimNodeID: string,
    relation: "evidence_to_claim" | "context_to_claim" | "execution_to_claim",
  ) {
    const parsed = this.parseSourceRef(ref)
    if (!parsed) return
    this.causalEdge({
      from: parsed,
      to: { type: "response_claim", id: claimNodeID, label: "response.claim" },
      relation,
      label:
        relation === "evidence_to_claim"
          ? "Evidence fact supports response claim"
          : relation === "context_to_claim"
            ? "Context record contextualizes response claim"
            : "Execution record was used for response claim",
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
    return normalizeRelation(relation)
  }

  private provenanceLabel(label: string | undefined) {
    if (!label) return undefined
    return label.replace(/final response evidence/gi, "response output").replace(/final claim/gi, "response output")
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
        trace_version: TRACE_VERSION,
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
  return JSON.stringify(sanitizeForJson(input), undefined, 2)
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

  export function promptAssembly(input: PromptAssemblyInput) {
    return get()?.promptAssembly(input)
  }

  export function contextTransform(input: ContextTransformInput) {
    return get()?.contextTransform(input)
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

  export function llmTurn(input: LlmTurnInput) {
    return get()?.llmTurn(input)
  }

  export function agentLifecycle(input: AgentLifecycleInput) {
    return get()?.agentLifecycle(input)
  }

  export function exitGate(input: ExitGateInput) {
    return get()?.exitGate(input)
  }

  export function evidenceFact(input: EvidenceFactInput) {
    return get()?.evidenceFact(input)
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

  export function compactionCheck(input: CompactionCheckInput) {
    return get()?.compactionCheck(input)
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
