import crypto from "crypto"
import fs from "fs"
import path from "path"
import { Global } from "@opencode-ai/core/global"
import {
  CausalIRStore,
  normalizeTemporalReferences,
  projectProvenanceTrace,
  type ArtifactLike,
  type CausalEdgeLike,
  type CausalIREdge,
  type CausalIRDerivation,
  type CausalIRCommitResult,
  type CausalIRDiagnosticLike,
  type CausalIRJournalEntry,
  type CausalIRJournalSummary,
  type CausalIRNode,
  type CausalIROrigin,
  type CausalIRRef,
  type CausalIRStoreSnapshot,
  type CausalNodeLike,
} from "./causal-ir"
import { renderProvenanceTraceHtml } from "./causal-trace-viewer"
import { atomizeResponseClaims } from "./claim-atomization"
import { isBrokenClaimFragment, isNonFactualResponseClaim } from "./claim-atomization-core"
import { TRACE_VERSION, isFormalRecordType, shouldPromoteRuntimeEvent } from "./trace-semantic-contract"

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
  | "evaluation"
  | "trace"

export type TraceFieldSummary = {
  type: string
  length?: number
  hash?: string
  preview?: string
  value?: string | number | boolean | null
  keys?: string[]
  artifact_id?: string
  payload_ref?: string
  payload_dedupe_group_id?: string
  artifact_status?: "write_failed"
}

function isTraceFieldSummary(input: unknown): input is TraceFieldSummary {
  if (!input || typeof input !== "object" || Array.isArray(input)) return false
  const summary = input as Record<string, unknown>
  const keys = Object.keys(summary)
  const allowedKeys = new Set([
    "type",
    "length",
    "hash",
    "preview",
    "value",
    "keys",
    "artifact_id",
    "payload_ref",
    "payload_dedupe_group_id",
    "artifact_status",
  ])
  if (keys.some((key) => !allowedKeys.has(key))) return false
  if ("artifact_status" in summary && summary.artifact_status !== "write_failed") return false
  const artifactKeys = ["artifact_id", "payload_ref", "payload_dedupe_group_id"]
  const hasValidArtifactFields = artifactKeys.every((key) => !(key in summary) || typeof summary[key] === "string")
  const hasNoArtifactFields = artifactKeys.every((key) => !(key in summary))
  if (!hasValidArtifactFields) return false
  const isSizedSummary =
    typeof summary.length === "number" &&
    Number.isFinite(summary.length) &&
    summary.length >= 0 &&
    typeof summary.hash === "string" &&
    typeof summary.preview === "string"

  switch (summary.type) {
    case "text":
      return isSizedSummary && !("keys" in summary) && !("value" in summary)
    case "array":
      return isSizedSummary && !("value" in summary) && (!("keys" in summary) || summary.keys === undefined)
    case "object":
      return (
        isSizedSummary &&
        !("value" in summary) &&
        (!("keys" in summary) ||
          summary.keys === undefined ||
          (Array.isArray(summary.keys) && summary.keys.every((key) => typeof key === "string")))
      )
    case "null":
      return (
        summary.value === null &&
        hasNoArtifactFields &&
        !("keys" in summary) &&
        !("length" in summary) &&
        !("hash" in summary) &&
        !("preview" in summary)
      )
    case "string":
      return (
        typeof summary.value === "string" &&
        hasNoArtifactFields &&
        !("keys" in summary) &&
        !("length" in summary) &&
        !("hash" in summary) &&
        !("preview" in summary)
      )
    case "number":
      return (
        typeof summary.value === "number" &&
        Number.isFinite(summary.value) &&
        hasNoArtifactFields &&
        !("keys" in summary) &&
        !("length" in summary) &&
        !("hash" in summary) &&
        !("preview" in summary)
      )
    case "boolean":
      return (
        typeof summary.value === "boolean" &&
        hasNoArtifactFields &&
        !("keys" in summary) &&
        !("length" in summary) &&
        !("hash" in summary) &&
        !("preview" in summary)
      )
    case "undefined":
      return keys.length === 1
    default:
      return false
  }
}

function shouldExternalizeCausalContainer(label: string) {
  const field = label.split(".").at(-1)
  if (["input", "output", "messages", "tools", "diff", "stdout", "stderr"].includes(field ?? "")) return true
  return /^context\.compaction\.(previous_summary|serialized_tail|output_summary)(?:\.|$)/.test(label)
}

function isStructuredCausalContainer(label: string) {
  const schemaRoots = [
    "change.data.change_semantics",
    "change.data.diff_semantics",
    "change.data.source_ref_relations",
    "context.pack.data.context_ledger",
    "context.compaction.data.context_ledger",
    "evidence.semantic_fact.data.structured_claim",
    "llm.call.data.message_transforms",
    "verification.data.final_test_result",
  ]
  return (
    schemaRoots.some((root) => label === root || label.startsWith(`${root}.`)) ||
    label.split(".").some((segment) => /_(?:refs|flags|semantics)$/.test(segment))
  )
}

const MAX_STRUCTURED_CAUSAL_COLLECTION_ITEMS = 64

function exceedsStructuredCausalCollectionLimit(input: unknown) {
  if (Array.isArray(input)) return input.length > MAX_STRUCTURED_CAUSAL_COLLECTION_ITEMS
  if (input && typeof input === "object") return Object.keys(input).length > MAX_STRUCTURED_CAUSAL_COLLECTION_ITEMS
  return false
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
  storage_encoding?: "identity" | "json_minified"
  original_length?: number
  stored_length?: number
  availability?: "embedded" | "bundled" | "external" | "missing" | "truncated"
  content_hash?: string
  byte_length?: number
  semantic_slices?: Array<{
    byte_range: [number, number]
    content: string
    hash: string
    truncated: boolean
  }>
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
  algorithm_version?: string
  input_message_count?: number
  compaction_request_message_count?: number
  output_message_count?: number
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
  evidence_tier?: "confirmed" | "content_matched" | "temporal_advisory"
  eligible_for_attribution?: boolean
  derivation_method?: string
  evidence_refs?: string[]
  confidence?: number
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
  repository_revision?: number
  verification_phase?: "baseline" | "post_change" | "post_test_change" | "unknown"
  effective_for_final_state?: boolean
  supersedes_refs?: string[]
  superseded_by_refs?: string[]
  exit_code?: number
  process_exit_code?: number
  parsed_command_outcomes?: Array<{
    source: "reported_exit_marker" | string
    exit_code: number
    status: "passed" | "failed"
  }>
  exit_masked_by_shell?: boolean
  status: "passed" | "failed" | "unknown"
  parsed_failures: TraceParsedFailure[]
  stdout?: TraceFieldSummary
  stderr?: TraceFieldSummary
  quality_flags?: string[]
  changed_test_refs?: string[]
  changed_production_refs?: string[]
  changed_docs_refs?: string[]
  verification_scope_risk_flags?: string[]
  final_test_result?: Record<string, unknown>
  coverage_semantics?: Record<string, unknown>
  verification_attempt?: Record<string, unknown>
  metadata?: Record<string, unknown>
}

export type TraceNumericConstantChange = {
  from: string
  to: string
  before?: string
  after?: string
}

export type TraceChangeSemantics = {
  changed_line_count: number
  added_line_count: number
  removed_line_count: number
  changed_identifiers?: string[]
  numeric_constant_changes?: TraceNumericConstantChange[]
  touched_symbols?: string[]
  operation_kinds?: string[]
  risk_flags?: string[]
  summary?: string
}

export type TraceSourceRefRelation = {
  source_ref: string
  relation: "materialized_by_action" | "executed_in_span" | "motivated_by_evidence" | "explicit_provenance" | string
  inference: "runtime_identity" | "recent_failed_verification" | "explicit" | string
  confidence: number
}

export type TraceChangeRecord = {
  change_id: string
  span_id?: string
  tool_call_id?: string
  files: string[]
  revision_before?: number
  revision_after?: number
  change_target_role?: "production_code" | "test_code" | "docs" | "config" | "mixed" | "unknown" | string
  changed_test_oracle?: boolean
  intent?: string
  diff?: TraceFieldSummary
  change_semantics?: TraceChangeSemantics
  diff_semantics?: Record<string, unknown>
  quality_flags?: string[]
  source_refs?: string[]
  source_ref_relations?: TraceSourceRefRelation[]
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

type ClaimGroundingDecision = {
  candidate_ref: string
  candidate_origin: "confirmed_generation_context" | "explicit_response_source"
  decision:
    | "selected_direct_support"
    | "rejected_no_match"
    | "rejected_lower_ranked_match"
    | "rejected_inapplicable"
  score: number
  reasons: string[]
  rejection_reason?:
    | "semantic_match_below_threshold"
    | "lower_ranked_match"
    | "superseded_verification"
    | "verification_revision_mismatch"
    | "verification_status_mismatch"
    | "unscoped_verification_candidate"
  candidate_verification_refs?: string[]
  candidate_repository_revision?: number
  candidate_verification_phase?: TraceVerificationRecord["verification_phase"]
  candidate_verification_status?: TraceVerificationRecord["status"]
  candidate_effective_for_final_state?: boolean
  candidate_temporal_role?: "current_effective" | "superseded" | "unknown"
  candidate_temporally_eligible?: boolean
  attribution_eligible: boolean
  agent_attention_observed: false
  behavior_impact: "none"
}

type VerificationFactProvenance = {
  verification_refs: string[]
  verification_repository_revision?: number
  verification_phase?: TraceVerificationRecord["verification_phase"]
  verification_status?: TraceVerificationRecord["status"]
  verification_effective_for_final_state?: boolean
  verification_temporal_role: "current_effective" | "superseded" | "unknown"
  verification_supersedes_refs?: string[]
  verification_superseded_by_refs?: string[]
}

export type TraceResponseSegment = {
  segment_id: string
  response_artifact?: string
  text: TraceFieldSummary
  response_role?: "final_answer" | "intermediate_summary" | "subagent_result" | "auto_continue_summary" | string
  visibility?: "user_visible" | "internal_continue" | "compaction_followup" | "debug" | string
  turn_index?: number
  is_final_for_case?: boolean
  finality_source?: "explicit" | "inferred"
  direct_evidence_refs?: string[]
  context_refs?: string[]
  execution_refs?: string[]
  generation_provenance_refs?: string[]
  generation_grounding_candidate_refs?: string[]
  generation_tool_outcome_refs?: string[]
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
  applicability_status?: "active" | "legacy" | "contrast" | "unknown" | string
  applicability_reasons?: string[]
  semantic_role?:
    | "requirement_rule"
    | "observed_pre_change_code"
    | "observed_post_change_code"
    | "observed_intermediate_code"
    | "observed_current_code"
    | "legacy_historical"
    | "test_expectation"
    | "verification_result"
    | "secondary_summary"
    | "unknown"
    | string
  fact_scope?:
    | "requirement"
    | "current_code"
    | "legacy_code"
    | "test_oracle"
    | "verification"
    | "summary"
    | "unknown"
    | string
  conflict_group_id?: string
  conflict_refs?: string[]
  conflict_role?: "active_candidate" | "legacy_candidate" | "conflicting_candidate" | string
  conflict_kind?:
    | "requirement_code_mismatch"
    | "post_change_requirement_mismatch"
    | "stale_test_expectation"
    | "legacy_contrast"
    | "secondary_summary_conflict"
    | "generic_fact_conflict"
    | string
  conflict_severity?: "high" | "medium" | "low" | string
  conflict_issue?: boolean
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
  claim_key?: string
  response_segment_id?: string
  text: TraceFieldSummary
  claim_format?: "factual_claim" | "table_fact" | string
  claim_kind?: "verification" | "change" | "requirement" | "architecture" | "risk" | "fact" | string
  temporal_scope?: "baseline" | "current_revision" | "historical" | "future" | string
  repository_revision?: number
  raw_text?: TraceFieldSummary
  canonical_text?: TraceFieldSummary
  table_cells?: string[]
  table_subject?: string
  table_values?: string[]
  claim_group_id: string
  claim_index: number
  claim_count: number
  source_byte_range: [number, number]
  previous_claim_key?: string
  next_claim_key?: string
  previous_claim_ref?: string
  next_claim_ref?: string
  atomization_status: "atomic" | "group_required" | "invalid_fragment"
  atomization_reason: string
  direct_evidence_refs: string[]
  direct_support_refs?: string[]
  candidate_context_refs?: string[]
  superseded_evidence_refs?: string[]
  context_refs: string[]
  execution_refs: string[]
  generation_provenance_refs?: string[]
  legacy_context_refs?: string[]
  matched_evidence_refs?: string[]
  candidate_evidence_refs?: string[]
  grounding_candidate_refs?: string[]
  grounding_decisions?: ClaimGroundingDecision[]
  grounding_method?: "confirmed_context_semantic_match_v1" | string
  grounding_behavior_impact?: "none" | string
  match_strategy?: string
  match_score?: number
  match_reasons?: string[]
  original_direct_evidence_refs?: string[]
  derived_tool_outcome_refs?: string[]
  candidate_tool_outcome_refs?: string[]
  dependency_tool_outcome_refs?: string[]
  attribution_summary?: Record<string, unknown>
  conflicting_evidence_refs?: string[]
  support_conflict_status?: "conflicted" | "unconflicted" | "unknown" | string
  verification_after_test_change_refs?: string[]
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
  cancelled_after_case_completion_records?: number
  closed_after_case_completion_records?: number
  llm_turns_missing_token_usage: number
  llm_turns_missing_finish_reason: number
  compaction_quality_flags: Record<string, number>
  empty_subagent_results: number
  broad_response_refs: number
  duplicate_evidence_facts: number
  duplicate_semantic_facts?: number
  generic_evidence_facts?: number
  generic_semantic_facts?: number
  unsupported_response_claims?: number
  context_only_response_claims?: number
  payload_duplication_groups?: number
  compaction_check_missing?: number
  broken_claim_fragments?: number
  over_attributed_claims?: number
  generic_mcp_facts?: number
  path_only_evidence_facts?: number
  execution_observations?: number
  task_plan_states?: number
  non_final_response_claims?: number
  weak_evidence_matches?: number
  mcp_json_parse_shadowed?: number
  skill_request_unresolved?: number
  background_llm_turns?: number
  legacy_context_ref_claims?: number
  missing_verification_after_change?: number
  verification_after_test_change?: number
  conflicting_semantic_fact_groups?: number
  actionable_semantic_conflict_groups?: number
  low_value_semantic_conflict_members?: number
  legacy_fact_used_in_final_claim?: number
  task_obligations?: number
  unmet_task_obligations?: number
  raw_stream_delta_events?: number
  issues: TraceHealthIssue[]
}

export type CausalNodeKind =
  | "run.start"
  | "process.signal"
  | "case.completed"
  | "case.failed"
  | "case.observed_defect"
  | "case.missing_semantic"
  | "task.loop"
  | "task.plan_state"
  | "task.obligation"
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
  | "tool.result"
  | "tool.error"
  | "mcp.call"
  | "skill.load"
  | "subagent.call"
  | "loop.decision"
  | "observation"
  | "execution.observation"
  | "evidence.fact"
  | "evidence.semantic_fact"
  | "change"
  | "verification"
  | "response.output"
  | "response.claim"
  | "claim.support_assessment"
  | "design.record"

export type CausalNode = {
  node_id: string
  kind: CausalNodeKind | string
  component?: TraceComponent
  span_id?: string
  parent_span_id?: string
  timestamp: string
  time_ms: number
  title?: string
  status?: TraceStatus | TraceVerificationRecord["status"]
  origin?: CausalIROrigin
  data?: Record<string, unknown>
  input_refs?: string[]
  output_refs?: string[]
  source_refs?: string[]
  source_locations?: TraceSourceLocation[]
  typed_resources?: Record<string, unknown>[]
  artifact_refs?: string[]
  aliases?: string[]
  derivation?: CausalIRDerivation
  metadata?: Record<string, unknown>
}

export type CausalEdge = {
  edge_id: string
  from: TraceRef
  to: TraceRef
  relation: string
  original_relation: string
  normalized_relation: DataflowEdge["relation"]
  evidence_tier: "confirmed" | "content_matched" | "temporal_advisory"
  eligible_for_attribution: boolean
  derivation_method: string
  evidence_refs?: string[]
  confidence?: number
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
  server_status?: TraceStatus
  process_status?: TraceStatus
  server_shutdown_reason?: string
  shutdown_signal?: string
  shutdown_disposition?: string
  case_status?: TraceStatus
  case_completed_at?: string
  collection_mode: "passive_sidecar"
  behavior_impact: "none" | "modified"
  subject_revision?: string
  subject_revision_provenance?: {
    method: "case_trace_config" | "environment_variable"
    source: "CaseTraceConfig.subjectRevision" | "OPENCODE_TRACE_SUBJECT_REVISION"
    bound_at: "case_start"
    case_id: string
    run_id: string
  }
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
    | "motivated_by_evidence"
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
    | "response_to_claim_group"
    | "claim_group_precedes"
    | "external_evaluation_observed"
  eligible_for_attribution?: boolean
  label?: string
  metadata?: Record<string, unknown>
}

export type ProvenanceTraceSummary = {
  trace_version: typeof TRACE_VERSION
  causal_ir_version?: "1.0"
  manifest: TraceManifest
  nodes?: CausalIRNode[]
  edges?: CausalIREdge[]
  records: ProvenanceRecord[]
  dataflow_edges: DataflowEdge[]
  artifacts: TraceArtifact[]
  diagnostics?: Record<string, unknown>[]
  compatibility?: {
    provenance_projection: "provenance-trace.json"
  }
  metrics: {
    spans: number
    events: number
    records: number
    dataflow_edges: number
    artifacts: number
    token_usage: TraceTokenUsage
    stream_summary?: Record<string, number>
    trace_health: TraceHealthMetrics
  }
}

export type ProvenanceTraceView = Omit<
  ProvenanceTraceSummary,
  "causal_ir_version" | "nodes" | "edges" | "diagnostics" | "compatibility"
>

export type CausalIRTraceSummary = ProvenanceTraceView & {
  causal_ir_version: "1.0"
  nodes: CausalIRNode[]
  edges: CausalIREdge[]
  journal: CausalIRJournalSummary
  diagnostics: CausalIRDiagnosticLike[]
  compatibility: {
    provenance_projection: "provenance-trace.json"
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
  subjectRevision?: string
  input?: Record<string, unknown>
  environment?: Record<string, unknown>
}

type GenerationProvenance = {
  refs: string[]
  promptRefs: string[]
  contextRefs: string[]
  llmRefs: string[]
  messageTransforms: Record<string, unknown>[]
  inputMessages?: unknown
  selectedContextRefs: string[]
}

type GenerationGroundingCandidates = {
  candidateRefs: string[]
  toolOutcomeRefs: string[]
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
  evidence_tier?: CausalEdge["evidence_tier"]
  eligible_for_attribution?: boolean
  derivation_method?: string
  evidence_refs?: string[]
  confidence?: number
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
  | "claim_key"
  | "text"
  | "claim_format"
  | "raw_text"
  | "canonical_text"
  | "table_cells"
  | "table_subject"
  | "table_values"
  | "previous_claim_key"
  | "next_claim_key"
  | "previous_claim_ref"
  | "next_claim_ref"
  | "direct_evidence_refs"
  | "context_refs"
  | "execution_refs"
  | "generation_provenance_refs"
  | "grounding_candidate_refs"
  | "grounding_decisions"
  | "grounding_method"
  | "grounding_behavior_impact"
  | "source_refs"
  | "source_locations"
  | "support_level"
  | "quality_flags"
> & {
  claim_id?: string
  claim_key?: string
  text: unknown
  claim_format?: TraceResponseClaimRecord["claim_format"]
  raw_text?: unknown
  canonical_text?: unknown
  table_cells?: string[]
  table_subject?: string
  table_values?: string[]
  previous_claim_key?: string
  next_claim_key?: string
  previous_claim_ref?: string
  next_claim_ref?: string
  source_refs?: string[]
  source_locations?: TraceSourceLocation[]
  evidence_refs?: string[]
  generation_provenance_refs?: string[]
  generation_grounding_candidate_refs?: string[]
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
  temporal_advisory_refs?: string[]
}

type CausalEdgeInput = Omit<
  CausalEdge,
  | "edge_id"
  | "original_relation"
  | "normalized_relation"
  | "evidence_tier"
  | "eligible_for_attribution"
  | "derivation_method"
> & {
  edge_id?: string
  original_relation?: string
  normalized_relation?: DataflowEdge["relation"]
  evidence_tier?: CausalEdge["evidence_tier"]
  eligible_for_attribution?: boolean
  derivation_method?: string
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
  session_id?: string
  message_id?: string
  provider_id?: string
  model_id?: string
  input_tokens?: number
  context_limit?: number
  reserved_output_tokens?: number
  selected_head_messages?: number
  selected_tail_messages?: number
  hidden_compaction_messages?: number
  input_message_count?: number
  compaction_request_message_count?: number
  output_message_count?: number
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
const tokenUsageFields = ["input", "output", "reasoning", "cached_input", "cache_write", "total", "cost"] as const
const secretTextPatterns = [
  /\bsk-[a-zA-Z0-9_-]{8,}\b/g,
  /\bBearer\s+[a-zA-Z0-9._~+/=-]+\b/gi,
  /\b(?:api[_-]?key|password|passwd|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token)\s*(?:=|:)\s*[^\s,;]+/gi,
]
const legacySemanticEdgeProjectionKey = "__case_trace_legacy_semantic_edge_projection"
const legacySemanticEdgeOptionalFields = [
  "evidence_tier",
  "eligible_for_attribution",
  "derivation_method",
  "evidence_refs",
  "confidence",
  "label",
  "metadata",
] as const
type LegacySemanticEdgeOptionalField = (typeof legacySemanticEdgeOptionalFields)[number]
const legacySemanticEdgeOptionalFieldSet = new Set<string>(legacySemanticEdgeOptionalFields)
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

function isTokenUsageContainer(input: string) {
  return normalizeKey(input) === "token_usage"
}

function finiteTokenMetric(input: unknown) {
  return typeof input === "number" && Number.isFinite(input) ? Math.max(0, input) : undefined
}

function sanitizeTokenUsage(input: unknown): TraceTokenUsage | undefined {
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined
  const value = input as Record<string, unknown>
  const output: TraceTokenUsage = {}
  for (const key of tokenUsageFields) {
    const metric = finiteTokenMetric(value[key])
    if (metric !== undefined) output[key] = metric
  }
  return output
}

function sanitizeForJson(
  input: unknown,
  key = "",
  stack = new WeakSet<object>(),
  path: readonly string[] = [],
): unknown {
  if (input === undefined) return undefined
  if (isTokenUsageContainer(key)) return sanitizeTokenUsage(input)
  if (isSensitiveKey(key, path, input)) return "[REDACTED]"
  if (typeof input === "bigint") return String(input)
  if (typeof input === "function") return `[Function ${input.name || "anonymous"}]`
  if (input instanceof Error) return sanitizeForJson(errorInfo(input), key, stack, path)
  if (input instanceof URL) return redactUrlText(input)
  if (typeof input === "string") return redactText(input)
  if (!input || typeof input !== "object") return input
  if (stack.has(input)) return "[Circular]"
  stack.add(input)
  try {
    if (Array.isArray(input))
      return input.map((item, index) => sanitizeForJson(item, String(index), stack, [...path, String(index)]))
    const output: Record<string, unknown> = {}
    for (const [childKey, value] of Object.entries(input as Record<string, unknown>)) {
      const sanitized = sanitizeForJson(value, childKey, stack, [...path, childKey])
      if (sanitized !== undefined) output[childKey] = sanitized
    }
    return output
  } finally {
    stack.delete(input)
  }
}

function json(input: unknown, label?: string) {
  const path = label?.split(".").filter(Boolean) ?? []
  return JSON.stringify(
    sanitizeForJson(normalizeTemporalReferences(input).value, path.at(-1) ?? "", new WeakSet<object>(), path),
    undefined,
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

function isSensitiveKey(input: string, path: readonly string[] = [], value?: unknown) {
  if (!input) return false
  const key = normalizeKey(input)
  if (!key) return false
  const numericMetricKeys = new Set([
    "token_estimate",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
  ])
  const metricContainerKeys = new Set(["token_usage", "input_token_details", "output_token_details"])
  if (key === "token" || key === "tokens") {
    const explicitUsagePath = path.slice(0, -1).some((segment) => {
      const normalized = normalizeKey(segment)
      return (
        normalized === "usage" ||
        normalized === "token_usage" ||
        normalized === "token_metrics" ||
        normalized === "token_details" ||
        normalized.endsWith("_token_usage") ||
        normalized.endsWith("_token_details")
      )
    })
    return !explicitUsagePath || typeof value !== "number" || !Number.isFinite(value)
  }
  if (numericMetricKeys.has(key)) return typeof value !== "number" || !Number.isFinite(value)
  if (metricContainerKeys.has(key)) return !value || typeof value !== "object" || Array.isArray(value)
  if (/^(api_key|authorization|cookie|password|passwd|credential|secret)$/.test(key)) return true
  if (/^(access_token|refresh_token|auth_token|id_token)$/.test(key)) return true
  if (/(^|_)(authorization|cookie|password|passwd|credential|secret)($|_)/.test(key)) return true
  if (/(^|_)api_key($|_)/.test(key)) return true
  if (/(^|_)(access_token|refresh_token|auth_token|id_token)($|_)/.test(key)) return true
  return false
}

class TraceOwnedCausalIRStore {
  private readonly store: CausalIRStore

  constructor(input: { runID: string; caseID: string; append?: (entry: CausalIRJournalEntry) => unknown }) {
    this.store = new CausalIRStore(input)
  }

  get nodes() {
    return this.store.nodes
  }

  get edges() {
    return this.store.edges
  }

  get artifacts() {
    return this.store.artifacts
  }

  get diagnostics() {
    return this.store.diagnostics
  }

  createNode<T extends CausalNodeLike>(node: T): T {
    return this.store.createNode(this.copy(node))
  }

  updateNode<T extends CausalNodeLike>(node: T): T {
    return this.store.updateNode(this.copy(node))
  }

  replaceNodes(nodes: CausalNodeLike[]): void {
    this.store.replaceNodes(this.copy(nodes))
  }

  createEdge<T extends CausalEdgeLike>(edge: T): T {
    return this.store.createEdge(this.copy(edge))
  }

  replaceEdges(edges: CausalEdgeLike[]): void {
    this.store.replaceEdges(this.copy(edges))
  }

  createArtifact<T extends ArtifactLike>(artifact: T): T {
    return this.store.createArtifact(this.copy(artifact))
  }

  reuseArtifact<T extends ArtifactLike>(artifact: T): T {
    return this.store.reuseArtifact(this.copy(artifact))
  }

  createDiagnostic<T extends CausalIRDiagnosticLike>(diagnostic: T): T {
    return this.store.createDiagnostic(this.copy(diagnostic))
  }

  resolveReference(input: string): CausalIRRef {
    return this.store.resolveReference(input)
  }

  checkpoint(data: unknown): void {
    this.store.checkpoint(this.copy(data))
  }

  finalize(data: unknown): CausalIRCommitResult {
    return this.store.finalize(this.copy(data))
  }

  snapshot(): CausalIRStoreSnapshot {
    return this.store.snapshot()
  }

  journalSummary(): CausalIRJournalSummary {
    return this.store.journalSummary()
  }

  synchronize(): CausalIRStoreSnapshot {
    return this.store.synchronize()
  }

  private copy<T>(input: T): T {
    return sanitizeForJson(input) as T
  }
}

function hash(input: string) {
  return crypto.createHash("sha256").update(input).digest("hex").slice(0, 16)
}

function redactText(input: string) {
  let output = input
  output = output.replace(
    /\b([a-z][a-z0-9+.-]*:\/\/)([^/\s:@]+):([^@\s/]+)@/gi,
    (match, scheme: string, username: string, password: string) =>
      username.toUpperCase() === "%5BREDACTED%5D" && password.toUpperCase() === "%5BREDACTED%5D"
        ? match
        : `${scheme}[REDACTED]:[REDACTED]@`,
  )
  output = output.replace(
    /([?&](?:api[_-]?key|authorization|cookie|password|passwd|credential|secret|token|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token)=)[^&#\s"']+/gi,
    "$1[REDACTED]",
  )
  output = output.replace(
    /(["'])(api[_-]?key|authorization|cookie|password|passwd|credential|secret|token|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token)\1(\s*:\s*)(["'])(?:\\.|(?!\4)[\s\S])*?\4/gi,
    (_match, quote: string, key: string, separator: string, valueQuote: string) =>
      `${quote}${key}${quote}${separator}${valueQuote}[REDACTED]${valueQuote}`,
  )
  output = output.replace(
    /(["'])(\s*(?:authorization|proxy-authorization|cookie|set-cookie|x[-_]api[-_]key)\s*:\s*)(?:\\.|(?!\1)[\s\S])*?\1/gi,
    (_match, quote: string, header: string) => `${quote}${header}[REDACTED]${quote}`,
  )
  output = output.replace(
    /(\b(?:cookie|set-cookie)\s*:\s*)(?!\[REDACTED\])(?:(?:"(?:\\.|[^"\\])*")|(?:'(?:\\.|[^'\\])*')|[^\s\r\n"'])+/gi,
    "$1[REDACTED]",
  )
  output = output.replace(
    /(^|[\r\n]|(?:[a-z]*Error:\s+))(\s*(?:authorization|proxy-authorization|cookie|set-cookie|x[-_]api[-_]key)\s*:\s*)[^\r\n]*/gi,
    "$1$2[REDACTED]",
  )
  output = output.replace(
    /(\-\-(?:api[-_]?key|password|passwd|credential|secret|token|access[-_]?token|refresh[-_]?token|auth[-_]?token|id[-_]?token)(?:=|\s+))(?:(?:"[^"]*")|(?:'[^']*')|[^\s,;]+)/gi,
    "$1[REDACTED]",
  )
  output = output.replace(
    /\b((?:api[_-]?key|authorization|proxy[_-]?authorization|cookie|password|passwd|credential|secret|token|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token)\s*=\s*)(?:(?:"[^"]*")|(?:'[^']*')|(?:(?:Basic|Bearer)\s+)?[^\s,]+)/gi,
    "$1[REDACTED]",
  )
  for (const pattern of secretTextPatterns) output = output.replace(pattern, "[REDACTED]")
  return output
}

function redactUrlText(input: URL) {
  const output = new URL(input.toString())
  if (output.username) output.username = "[REDACTED]"
  if (output.password) output.password = "[REDACTED]"
  for (const key of new Set(output.searchParams.keys())) {
    if (normalizeKey(key) === "token" || isSensitiveKey(key, [], "query-value"))
      output.searchParams.set(key, "[REDACTED]")
  }
  return redactText(output.toString())
}

function textForSummary(input: unknown) {
  const sanitized = input instanceof Error || input instanceof URL ? sanitizeForJson(input) : input
  if (input instanceof Error && sanitized && typeof sanitized === "object") {
    const error = sanitized as TraceError
    return redactText(`${error.name ?? "Error"}: ${error.message ?? error.name ?? "Error"}`)
  }
  return redactText(String(sanitized ?? ""))
}

function maxFieldLength() {
  return safeNumber(process.env.OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH || 2048) || 2048
}

function unicodePrefix(input: string, limit: number) {
  const prefix = input.slice(0, limit)
  const last = prefix.charCodeAt(prefix.length - 1)
  if (last >= 0xd800 && last <= 0xdbff) return prefix.slice(0, -1)
  return prefix
}

function validUtf8(input: string) {
  return Buffer.from(input, "utf8").toString("utf8")
}

function summarizeScalar(input: unknown): TraceFieldSummary {
  if (input === null) return { type: "null", value: null }
  if (typeof input === "string") return summarizeText(input)
  if (typeof input === "number" || typeof input === "boolean") return { type: typeof input, value: input }
  if (typeof input === "undefined") return { type: "undefined" }
  return { type: typeof input, value: String(input) }
}

export function summarizeText(input: unknown): TraceFieldSummary {
  const text = textForSummary(input)
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
  return {
    type: Array.isArray(input) ? "array" : "object",
    length: serialized.length,
    hash: hash(serialized),
    preview: serialized.slice(0, maxFieldLength()),
    ...(Array.isArray(input) ? {} : { keys: Object.keys(input as Record<string, unknown>).slice(0, 50) }),
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
  if (location && failures.length) {
    const target = failures[0]
    target.file = location[1]
    target.line = optionalNumber(location[2])
    target.column = optionalNumber(location[3])
  }

  return failures
}

function inferredVerificationStatus(input: {
  explicitStatus?: TraceVerificationRecord["status"]
  exitCode?: number
  parsedFailures?: TraceParsedFailure[]
  command?: string
}) {
  const qualityFlags: string[] = []
  const hasFailures = Boolean(input.parsedFailures?.length)
  if (hasFailures && input.exitCode === 0) qualityFlags.push("failure_output_masked_by_exit_code")
  if (hasFailures && /\|\|\s*true\b/.test(input.command ?? "")) qualityFlags.push("shell_failure_masked")
  const status =
    hasFailures && input.explicitStatus !== "failed"
      ? "failed"
      : (input.explicitStatus ??
        (input.exitCode === undefined ? "unknown" : input.exitCode === 0 ? "passed" : "failed"))
  return { status, quality_flags: qualityFlags }
}

function parsedCommandOutcomes(stdout: unknown, stderr: unknown) {
  const text = [stdout, stderr].filter((item): item is string => typeof item === "string").join("\n")
  return [...text.matchAll(/(?:---\s*)?EXIT(?:\s+CODE)?\s*:\s*(-?\d+)/gi)].map((match) => {
    const exitCode = Number(match[1])
    return {
      source: "reported_exit_marker" as const,
      exit_code: exitCode,
      status: exitCode === 0 ? ("passed" as const) : ("failed" as const),
    }
  })
}

function finalTestResultSemantics(input: {
  command?: string
  purpose?: string
  exitCode?: number
  status: TraceVerificationRecord["status"]
  parsedFailures: TraceParsedFailure[]
}) {
  return omitUndefined({
    command: input.command,
    status: input.status,
    exit_code: input.exitCode,
    parsed_failure_count: input.parsedFailures.length,
    first_failure: input.parsedFailures[0],
    result_kind: isTestLikeCommand(input.command, input.purpose) ? "test_result" : "command_result",
  })
}

function verificationCoverageSemantics(input: {
  command?: string
  changedTestRefs: string[]
  changedProductionRefs: string[]
  changedDocsRefs: string[]
  riskFlags: string[]
}) {
  const command = input.command ?? ""
  const executedScripts = dedupeStrings(
    [
      ...[...command.matchAll(/(?:node|bun)\s+([^\s;&|]+test[^\s;&|]*)/gi)].map((match) => match[1] ?? ""),
      ...[...command.matchAll(/\bnpm\s+run\s+([A-Za-z0-9:_-]+)/g)].map((match) => `npm:${match[1]}`),
      ...(command.includes("npm test") ? ["npm:test"] : []),
      ...(command.includes("test:full") ? ["npm:test:full"] : []),
    ].filter(Boolean),
  )
  const coveredRisks = new Set<string>()
  if (/pricing|renewal|discount|quote/i.test(command)) coveredRisks.add("pricing_behavior")
  if (/owner|ownership/i.test(command)) coveredRisks.add("ownership_metadata")
  if (/quality|generic|hardcode|design/i.test(command)) coveredRisks.add("design_quality")
  if (/\b(full|all|coverage)\b|test:full/i.test(command)) coveredRisks.add("full_regression")
  if (isTestLikeCommand(command)) coveredRisks.add("regression_tests")
  return omitUndefined({
    command,
    executed_scripts: executedScripts,
    covered_risks: [...coveredRisks],
    changed_test_refs: input.changedTestRefs,
    changed_production_refs: input.changedProductionRefs,
    changed_docs_refs: input.changedDocsRefs,
    risk_flags: input.riskFlags,
    scope_summary: [
      executedScripts.length ? `executed ${executedScripts.join(", ")}` : undefined,
      coveredRisks.size ? `covered ${[...coveredRisks].join(", ")}` : undefined,
      input.riskFlags.length ? `risk flags: ${input.riskFlags.join(", ")}` : undefined,
    ]
      .filter(Boolean)
      .join("; "),
  })
}

function verificationAttemptSemantics(command: string | undefined, purpose: string | undefined) {
  const commandText = command ?? ""
  const assertionCount = [...commandText.matchAll(/\bassert\b/g)].length
  const isInlineScript = /\bpython(?:3(?:\.\d+)?)?\s+(?:-[^\s]+\s+)*-[cC]\b/.test(commandText)
  if (!isInlineScript || assertionCount === 0) return undefined
  const scopeText = `${purpose ?? ""} ${commandText}`
  const declaredScope = /\bfull\b.*\bintegration\b|\bintegration\b.*\bfull\b/i.test(scopeText)
    ? "full_integration"
    : /\bintegration\b/i.test(scopeText)
      ? "integration"
      : "handwritten_self_test"
  return {
    attempt_kind: "handwritten_assertion_script",
    detection_method: "passive_command_analysis",
    assertion_count: assertionCount,
    declared_scope: declaredScope,
    oracle_source: "inline_assertions",
    behavior_impact: "none",
  }
}

function isTestLikeCommand(command: string | undefined, purpose?: string) {
  return Boolean(
    (command &&
      /\b(test|pytest|jest|vitest|mocha)\b|bun test|npm test|pnpm test|yarn test|go test|cargo test|node .*test|xcodebuild/i.test(
        command,
      )) ||
      verificationAttemptSemantics(command, purpose),
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
  return omitUndefined({
    input: finiteTokenMetric(value.input ?? value.inputTokens),
    output: finiteTokenMetric(value.output ?? value.outputTokens),
    reasoning: finiteTokenMetric(value.reasoning ?? value.reasoningTokens ?? value.outputTokenDetails?.reasoningTokens),
    cached_input: finiteTokenMetric(
      value.cached_input ?? value.cachedInputTokens ?? value.inputTokenDetails?.cacheReadTokens ?? cache.read,
    ),
    cache_write: finiteTokenMetric(value.cache_write ?? value.inputTokenDetails?.cacheWriteTokens ?? cache.write),
    total: finiteTokenMetric(value.total ?? value.totalTokens),
    cost: finiteTokenMetric(value.cost),
  }) as TraceTokenUsage
}

function mergeUsage(target: TraceTokenUsage, next: TraceTokenUsage) {
  for (const key of tokenUsageFields) {
    const value = finiteTokenMetric(next[key])
    if (value !== undefined) target[key] = (finiteTokenMetric(target[key]) ?? 0) + value
  }
}

function cloneTokenUsage(input: TraceTokenUsage | undefined): TraceTokenUsage | undefined {
  if (!input) return undefined
  return sanitizeTokenUsage(input) as TraceTokenUsage
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

function safeNodeIDPart(input: unknown) {
  const text = stringPreview(input, 120).trim()
  if (!text) return hash(String(input ?? "unknown"))
  const safe = text.replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "")
  return safe.slice(0, 80) || hash(text)
}

function toolCallIDFromPayload(payload: unknown, fallback?: string) {
  return (
    firstStringField(payload, ["callID", "call_id", "toolCallID", "tool_call_id", "id"]) ??
    firstStringField(objectField(payload, "metadata"), ["callID", "call_id", "toolCallID", "tool_call_id"]) ??
    fallback
  )
}

function structuredToolCallIDs(input: unknown, output = new Set<string>(), seen = new WeakSet<object>(), depth = 0) {
  if (input === undefined || input === null || depth > 12) return output
  if (Array.isArray(input)) {
    for (const item of input) structuredToolCallIDs(item, output, seen, depth + 1)
    return output
  }
  if (typeof input !== "object") return output
  if (seen.has(input)) return output
  seen.add(input)
  const record = input as Record<string, unknown>
  for (const key of ["callID", "call_id", "toolCallID", "tool_call_id", "toolCallId"]) {
    const value = record[key]
    if (typeof value === "string" && value.trim()) output.add(value)
  }
  for (const value of Object.values(record)) structuredToolCallIDs(value, output, seen, depth + 1)
  return output
}

function toolNameFromPayload(payload: unknown) {
  return (
    firstStringField(payload, ["tool", "toolName", "tool_name", "name"]) ??
    firstStringField(objectField(payload, "metadata"), ["tool", "toolName", "tool_name", "name"]) ??
    firstStringField(objectField(payload, "input"), ["tool", "toolName", "tool_name", "name"]) ??
    firstStringField(objectField(payload, "args"), ["tool", "toolName", "tool_name", "name"])
  )
}

function toolArgsFromPayload(payload: unknown) {
  return (
    objectField(payload, "args") ??
    objectField(payload, "input") ??
    objectField(objectField(payload, "metadata"), "args")
  )
}

function toolErrorKind(input: unknown) {
  const text = stringPreview(input, 2000)
  if (/ENOENT|not found|no such file|file does not exist|不存在|找不到/i.test(text)) return "file_not_found"
  if (/permission|EACCES|EPERM|denied|forbidden|unauthorized|拒绝|权限/i.test(text)) return "permission_denied"
  if (/timeout|timed out|ETIMEDOUT|超时/i.test(text)) return "timeout"
  if (/invalid|schema|validation|参数|校验/i.test(text)) return "validation_error"
  if (/cancel|abort|aborted|取消/i.test(text)) return "cancelled"
  return "execution_error"
}

function toolSourceRef(kind: "tool.error" | "tool.result", callID: string | undefined) {
  if (!callID) return undefined
  return kind === "tool.error" ? `tool_error:${callID}` : `tool_result:${callID}`
}

function isToolOutcomeRef(ref: string | undefined) {
  return Boolean(ref && (ref.startsWith("tool_error:") || ref.startsWith("tool_result:")))
}

function traceContextLedger(input: {
  input_tokens?: number
  context_limit?: number
  selected_head_messages?: number
  selected_tail_messages?: number
  hidden_compaction_messages?: number
  input_message_count?: number
  compaction_request_message_count?: number
  output_message_count?: number
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
  const algorithm =
    stringField(metadata, ["algorithm", "compaction_algorithm"]) ??
    input.context_ledger?.algorithm ??
    "head-tail-summary"
  return {
    algorithm,
    algorithm_version:
      input.context_ledger?.algorithm_version ??
      stringField(metadata, ["algorithm_version", "compaction_algorithm_version"]) ??
      (algorithm === "head-tail-summary" ? "head-tail-summary/v1" : undefined),
    input_message_count: input.context_ledger?.input_message_count ?? input.input_message_count,
    compaction_request_message_count:
      input.context_ledger?.compaction_request_message_count ?? input.compaction_request_message_count,
    output_message_count: input.context_ledger?.output_message_count ?? input.output_message_count,
    token_estimate_before: input.context_ledger?.token_estimate_before ?? input.input_tokens,
    token_estimate_after:
      input.context_ledger?.token_estimate_after ??
      (input.input_tokens === undefined || input.context_limit === undefined
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
      ...(available ? {} : { child_trace_unavailable_reason: "child_trace_file_not_found" }),
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
    ...(available ? {} : { child_trace_unavailable_reason: "child_trace_file_not_found" }),
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
        subject: stringField(parsed, ["subject", "symbol", "name"]),
        predicate: stringField(parsed, ["predicate", "relation", "property"]),
        value: primitiveClaimValue(firstPresentField(parsed, ["value", "fact", "claim", "owner", "status"])),
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
    if (type === "evidence" || type === "tool_error" || type === "tool_result") {
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

function hasCompleteResponseClaimAtomizationFacts(input: ResponseClaimInput) {
  return (
    typeof input.claim_group_id === "string" &&
    input.claim_group_id.length > 0 &&
    Number.isInteger(input.claim_count) &&
    input.claim_count > 0 &&
    Array.isArray(input.source_byte_range) &&
    input.source_byte_range.length === 2 &&
    Number.isFinite(input.source_byte_range[0]) &&
    Number.isFinite(input.source_byte_range[1]) &&
    input.source_byte_range[0] >= 0 &&
    input.source_byte_range[1] >= input.source_byte_range[0] &&
    ["atomic", "group_required", "invalid_fragment"].includes(input.atomization_status) &&
    typeof input.atomization_reason === "string" &&
    input.atomization_reason.length > 0
  )
}

function normalizeLegacyResponseClaimAtomizationFacts(input: ResponseClaimInput): ResponseClaimInput {
  if (hasCompleteResponseClaimAtomizationFacts(input)) return input
  const originalText = input.raw_text ?? input.text
  const sourceText = typeof originalText === "string" ? originalText : fieldSummaryText(originalText)
  return {
    ...input,
    claim_group_id: `claim_group_${hash(
      ["legacy_response_claim", input.response_segment_id ?? "", input.claim_id ?? input.claim_key ?? input.claim_index, sourceText].join(
        "\0",
      ),
    )}`,
    claim_count: 1,
    source_byte_range: [0, Buffer.byteLength(sourceText)],
    atomization_status: "group_required",
    atomization_reason: "legacy_response_claim_missing_atomization_facts",
  }
}

function responseClaimKind(input: unknown): TraceResponseClaimRecord["claim_kind"] {
  const text = fieldSummaryText(input).toLowerCase()
  if (
    /(?:测试|验证|检查|用例|test|verify|verification|lint|build|构建).*(?:通过|失败|成功|passed|failed)|^(?:全部|所有).*(?:通过|失败)/i.test(
      text,
    )
  )
    return "verification"
  if (/(?:修改|变更|修复|实现|changed?|modified|fixed|implemented)/i.test(text)) return "change"
  if (/(?:需求|requirement|acceptance criteria)/i.test(text)) return "requirement"
  if (/(?:架构|模块边界|调用链|architecture|module boundary|call chain)/i.test(text)) return "architecture"
  if (/(?:风险|限制|隐患|risk|limitation)/i.test(text)) return "risk"
  return "fact"
}

function responseClaimTemporalScope(input: unknown, repositoryRevision: number) {
  const text = fieldSummaryText(input).toLowerCase()
  if (/(?:基线|修改前|变更前|此前|baseline|before (?:the )?change|previously)/i.test(text)) return "historical" as const
  if (/(?:计划|后续|将会|待办|未来|will|todo|future)/i.test(text)) return "future" as const
  return repositoryRevision === 0 ? ("baseline" as const) : ("current_revision" as const)
}

function unchangedPathTargetsFromClaim(input: unknown) {
  const text = fieldSummaryText(input)
  const targets = new Set<string>()
  const afterPattern =
    /(?:未修改|没有修改|未变更|没有变更|不修改|不要修改|did not modify|not modify|left|kept|keep)\s+`?((?:\.{0,2}\/)?[\w@~-]+(?:\/[\w@~.-]+)+\/?)`?/gi
  const beforePattern = /`?((?:\.{0,2}\/)?[\w@~-]+(?:\/[\w@~.-]+)+\/?)`?\s*(?:未修改|未变更|unchanged|untouched)/gi
  for (const pattern of [afterPattern, beforePattern]) {
    for (const match of text.matchAll(pattern)) {
      const target = normalizeScopePath(match[1])
      if (target) targets.add(target)
    }
  }
  return [...targets]
}

function normalizeScopePath(input: unknown) {
  const path = typeof input === "string" ? input.trim() : ""
  if (!path) return undefined
  const cleaned = path.replace(/^`+|`+$/g, "").replace(/[.,，。；;:：)）\]]+$/g, "")
  if (!cleaned.includes("/")) return undefined
  return cleaned.replace(/^\.\//, "")
}

function pathWithinScope(path: string, scope: string) {
  const normalizedPath = normalizeScopePath(path)
  const normalizedScope = normalizeScopePath(scope)
  if (!normalizedPath || !normalizedScope) return false
  const scopePrefix = normalizedScope.endsWith("/") ? normalizedScope : `${normalizedScope}/`
  return normalizedPath === normalizedScope || normalizedPath.startsWith(scopePrefix)
}

type TaskObligationDraft = {
  obligation_type: "verification_required" | "mcp_required" | "subagent_required" | "path_scope_exclusion"
  requirement_text: string
  target_path?: string
  source_refs?: string[]
}

function taskObligationsFromInput(input: unknown, sourceRefs: string[] = []) {
  const text = fieldSummaryText(input)
  const obligations: TaskObligationDraft[] = []
  const source_refs = dedupeStrings(sourceRefs)
  if (
    /npm\s+test|pnpm\s+test|yarn\s+test|bun\s+test|运行[^。.\n]*测试|执行[^。.\n]*测试|run[^.\n]*tests?/i.test(text)
  ) {
    obligations.push({
      obligation_type: "verification_required",
      requirement_text: "Run the requested verification tests.",
      source_refs,
    })
  }
  if (
    /(?:必须|需要|must|should|请).*?(?:调用|call).*?(?:mcp|syntheticfacts|repo_fact)|(?:mcp|syntheticfacts|repo_fact).*?(?:必须|需要|must|should|调用|call)/i.test(
      text,
    )
  ) {
    obligations.push({
      obligation_type: "mcp_required",
      requirement_text: "Call the requested MCP/tool fact source.",
      source_refs,
    })
  }
  if (
    /(?:委派|调用|使用|spawn|delegate).*?(?:subagent|子\s*agent)|(?:subagent|子\s*agent).*?(?:总结|复核|调用|委派|delegate)/i.test(
      text,
    )
  ) {
    obligations.push({
      obligation_type: "subagent_required",
      requirement_text: "Use the requested subagent workflow.",
      source_refs,
    })
  }
  const pathPatterns = [
    /(?:不允许修改|不得修改|不要修改|禁止修改)\s+`?((?:\.{0,2}\/)?[\w@~-]+(?:\/[\w@~.-]+)+\/?)`?/gi,
    /(?:do not modify|must not modify|should not modify)\s+`?((?:\.{0,2}\/)?[\w@~-]+(?:\/[\w@~.-]+)+\/?)`?/gi,
  ]
  for (const pattern of pathPatterns) {
    for (const match of text.matchAll(pattern)) {
      const target = normalizeScopePath(match[1])
      if (!target) continue
      obligations.push({
        obligation_type: "path_scope_exclusion",
        requirement_text: `Do not modify ${target}.`,
        target_path: target,
        source_refs,
      })
    }
  }
  return dedupeTaskObligations(obligations)
}

function dedupeTaskObligations(input: TaskObligationDraft[]) {
  const seen = new Set<string>()
  const output: TaskObligationDraft[] = []
  for (const item of input) {
    const key = `${item.obligation_type}:${item.target_path ?? ""}`
    if (seen.has(key)) {
      const existing = output.find((candidate) => `${candidate.obligation_type}:${candidate.target_path ?? ""}` === key)
      if (existing) existing.source_refs = dedupeStrings([...(existing.source_refs ?? []), ...(item.source_refs ?? [])])
      continue
    }
    seen.add(key)
    output.push(item)
  }
  return output
}

function normalizeMatchText(input: unknown) {
  return stringPreview(input, 6000)
    .toLowerCase()
    .replace(/(\d+(?:\.\d+)?)\s*(?:%|percent\b)/g, (_match, value: string) =>
      String(Number(value) / 100),
    )
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
  for (const key of [
    "canonical_subject",
    "claim",
    "fact_kind",
    "category",
    "source",
    "summary",
    "intent",
    "diff",
    "files",
    "change_semantics",
    "diff_semantics",
  ]) {
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

function sharedCodeIdentifiers(left: string, right: string) {
  const extensions = new Set(["js", "jsx", "ts", "tsx", "mjs", "cjs", "json", "md", "py", "go", "rs", "java"])
  const collect = (value: string) =>
    new Set(
      (value.match(/[a-z_$][\w$]*(?:\.[a-z_$][\w$]*)+/gi) ?? [])
        .map((item) => item.toLowerCase())
        .filter((item) => !extensions.has(item.split(".").at(-1) ?? "")),
    )
  const rightIDs = collect(right)
  return [...collect(left)].filter((item) => rightIDs.has(item))
}

function isVerificationClaimText(input: string) {
  return /test|测试|passed|failed|pass|fail|assert|断言|exit|退出码|验证|genericity|extra check|通用性检查|pricing tests|expected|actual|通过|失败|\b48000\b|\b51000\b|\b850000\b/i.test(
    input,
  )
}

function verificationClaimExpectedStatus(input: string): TraceVerificationRecord["status"] | undefined {
  const passed = /\bpass(?:ed)?\b|全部通过|测试通过|验证通过|成功|exit(?:[_ -]?code)?\s*[:=]?\s*0|退出码\s*[:=]?\s*0/i.test(input)
  const failed = /\bfail(?:ed|ure)?\b|测试失败|验证失败|失败|断言|assert(?:ion)?|error/i.test(input)
  if (passed === failed) return undefined
  return passed ? "passed" : "failed"
}

function isChangeClaimText(input: string) {
  return /修改|变更|修复|实现|改为|调整|更新|changed?|modified|fixed|implemented|updated|adjusted/i.test(input)
}

function isToolFailureClaimText(input: string) {
  return /tool|工具|read|grep|glob|bash|edit|fail|failed|failure|error|not found|no such file|enoent|不存在|失败|错误|找不到|缺失/i.test(
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
  if (sharedCodeIdentifiers(claim, haystack).length) {
    score += 0.4
    reasons.push("exact_code_identifier")
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

function toolOutcomeMatchAnalysis(
  kind: "tool_error" | "tool_result",
  claimText: unknown,
  data: Record<string, unknown> | undefined,
) {
  const reasons: string[] = []
  if (!data) return { score: 0, reasons, weak: false }
  const claim = normalizeMatchText(claimText)
  if (!claim) return { score: 0, reasons, weak: false }
  const dataRecord = recordFromUnknown(data)
  const args = recordFromUnknown(dataRecord?.args)
  const strings = [
    dataRecord?.tool_name,
    dataRecord?.title,
    dataRecord?.error_kind,
    dataRecord?.error_message,
    dataRecord?.output,
    dataRecord?.error,
    args,
    dataRecord?.source_locations,
  ].filter((item) => item !== undefined)
  const haystack = normalizeMatchText(strings.join(" "))
  if (!haystack) return { score: 0, reasons, weak: false }
  let score = 0
  const sourcePaths = collectSourceLocations([dataRecord?.args, dataRecord?.source_locations, dataRecord?.output])
    .map((location) => location.path)
    .filter(Boolean) as string[]
  for (const sourcePath of sourcePaths) {
    const normalizedPath = normalizeMatchText(sourcePath)
    const basename = normalizedPath.split("/").at(-1)
    if (claim.includes(normalizedPath) || (basename && claim.includes(basename))) {
      score += 0.35
      reasons.push("tool_path")
      break
    }
  }
  const claimMentionsFailure =
    /fail|failed|failure|error|not found|no such file|不存在|失败|错误|找不到|缺失|工具失败/i.test(claim)
  const outcomeMentionsFailure =
    /fail|failed|failure|error|not found|no such file|enoent|不存在|失败|错误|找不到/i.test(haystack)
  if (kind === "tool_error") {
    if (claimMentionsFailure) {
      score += 0.35
      reasons.push("claim_mentions_tool_failure")
      if (outcomeMentionsFailure) {
        score += 0.25
        reasons.push("tool_error_text")
      }
      if (/file-not-found|file_not_found|not found|no such file|enoent|不存在|找不到/.test(haystack)) {
        score += 0.25
        reasons.push("tool_error_kind")
      }
    }
  }
  const claimTerms = new Set(matchTerms(claim))
  const outcomeTerms = new Set(matchTerms(haystack))
  const shared = [...claimTerms].filter((term) => outcomeTerms.has(term))
  if (shared.length) {
    score += Math.min(0.4, shared.length * (kind === "tool_error" ? 0.08 : 0.1))
    reasons.push("shared_tool_outcome_terms")
  }
  if (sharedCodeIdentifiers(claim, haystack).length) {
    score += 0.4
    reasons.push("exact_code_identifier")
  }
  if (/0\.15/.test(claim) && /0\.15/.test(haystack)) {
    score += 0.45
    reasons.push("discount_cap_value")
  }
  if (/billing-platform/.test(claim) && /billing-platform/.test(haystack)) {
    score += 0.45
    reasons.push("owner_value")
  }
  const finalScore = Math.min(1, Number(score.toFixed(2)))
  return {
    score: finalScore,
    reasons: dedupeStrings(reasons),
    weak: finalScore > 0 && finalScore < 0.45,
  }
}

function dedupeStrings(input: string[]) {
  return input.filter((item, index, array) => array.indexOf(item) === index)
}

function typedCausalRef(input: string): CausalIRRef {
  const separator = input.indexOf(":")
  const type = separator === -1 ? "external" : input.slice(0, separator)
  const id = separator === -1 ? input : input.slice(separator + 1)
  const refType: CausalIRRef["ref_type"] =
    type === "artifact"
      ? "artifact"
      : type === "raw_event" || type === "event"
        ? "raw_event"
        : ["external", "file", "path", "uri", "url"].includes(type)
          ? "external"
          : "node"
  return { ref_type: refType, ref_id: id, legacy_ref: input }
}

function deterministicDerivation(algorithm: string, inputRefs: string[], derivedAt: string): CausalIRDerivation {
  return {
    algorithm,
    algorithm_version: "1.0.0",
    derived_at: derivedAt,
    input_refs: inputRefs.map(typedCausalRef),
    rule_id: `${algorithm}:1.0.0`,
    reproducible: true,
  }
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

function tokenOverlapScore(left: string, right: string) {
  const leftTokens = new Set(left.toLowerCase().match(/[a-z0-9_./-]{3,}|[\u4e00-\u9fa5]{2,}/g) ?? [])
  const rightTokens = new Set(right.toLowerCase().match(/[a-z0-9_./-]{3,}|[\u4e00-\u9fa5]{2,}/g) ?? [])
  if (!leftTokens.size || !rightTokens.size) return 0
  let shared = 0
  for (const token of leftTokens) if (rightTokens.has(token)) shared++
  return shared / Math.min(leftTokens.size, rightTokens.size)
}

function changeSemanticsFromDiff(diff: unknown): TraceChangeSemantics | undefined {
  const text = fieldSummaryText(diff)
  if (!text.trim()) return undefined
  const lines = parseChangedDiffLines(text)
  const changedLineCount = lines.added.length + lines.removed.length
  if (!changedLineCount) return undefined

  const allChangedLines = [...lines.added, ...lines.removed]
  const changedIdentifiers = identifiersFromLines(allChangedLines)
  const numericChanges = numericConstantChangesFromLines(lines.removed, lines.added)
  const operationKinds = new Set<string>()
  const riskFlags = new Set<string>()

  if (numericChanges.length) {
    operationKinds.add("numeric_constant_update")
    riskFlags.add("numeric_constant_changed")
  }
  if (lines.added.concat(lines.removed).some((line) => /\breturn\b/.test(line)))
    operationKinds.add("return_logic_change")
  if (lines.added.concat(lines.removed).some((line) => /\b(if|switch|case|else)\b/.test(line))) {
    operationKinds.add("conditional_logic_change")
  }
  if (lines.added.some((line) => /\b(import|export|require)\b/.test(line)))
    operationKinds.add("dependency_or_api_change")
  if (lines.added.some((line) => /\b(class|function|const|let|var)\b/.test(line)) || lines.removed.length) {
    operationKinds.add("file_edit")
  }
  if (lines.added.some(isHardcodeCandidateLine)) riskFlags.add("hardcode_candidate")
  if (lines.added.some((line) => /\b(48000|51000|850000|1200)\b/.test(line))) {
    riskFlags.add("test_fixture_value_added")
  }
  if (lines.added.some((line) => /\b(test|spec|fixture|mock)\b/i.test(line))) riskFlags.add("test_coupling_candidate")

  const summaryParts = []
  if (numericChanges.length) {
    summaryParts.push(
      `numeric constants changed: ${numericChanges
        .slice(0, 5)
        .map((change) => `${change.from} -> ${change.to}`)
        .join(", ")}`,
    )
  }
  if (changedIdentifiers.length) summaryParts.push(`touched identifiers: ${changedIdentifiers.slice(0, 8).join(", ")}`)
  if (riskFlags.size) summaryParts.push(`risk flags: ${[...riskFlags].join(", ")}`)

  return {
    changed_line_count: changedLineCount,
    added_line_count: lines.added.length,
    removed_line_count: lines.removed.length,
    ...(changedIdentifiers.length ? { changed_identifiers: changedIdentifiers.slice(0, 40) } : {}),
    ...(numericChanges.length ? { numeric_constant_changes: numericChanges.slice(0, 20) } : {}),
    ...(changedIdentifiers.length ? { touched_symbols: changedIdentifiers.slice(0, 20) } : {}),
    ...(operationKinds.size ? { operation_kinds: [...operationKinds] } : {}),
    ...(riskFlags.size ? { risk_flags: [...riskFlags] } : {}),
    ...(summaryParts.length ? { summary: summaryParts.join("; ") } : {}),
  }
}

function diffSemanticsFromChangeSemantics(semantics: TraceChangeSemantics | undefined) {
  if (!semantics) return undefined
  const operationKinds = semantics.operation_kinds ?? []
  const riskFlags = semantics.risk_flags ?? []
  return omitUndefined({
    changed_line_count: semantics.changed_line_count,
    added_line_count: semantics.added_line_count,
    removed_line_count: semantics.removed_line_count,
    changed_symbols: semantics.touched_symbols ?? semantics.changed_identifiers,
    changed_identifiers: semantics.changed_identifiers,
    numeric_constant_changes: semantics.numeric_constant_changes,
    operation_kinds: operationKinds,
    risk_flags: riskFlags,
    semantic_summary: [
      operationKinds.length ? `operations: ${operationKinds.join(", ")}` : undefined,
      riskFlags.length ? `risks: ${riskFlags.join(", ")}` : undefined,
      semantics.summary,
    ]
      .filter(Boolean)
      .join("; "),
  })
}

function changeTargetRole(files: string[]) {
  const roles = new Set(files.map(changeTargetRoleForFile))
  roles.delete("unknown")
  if (!roles.size) return "unknown"
  if (roles.size === 1) return [...roles][0]
  return "mixed"
}

function changeTargetRoleForFile(file: string) {
  const normalized = file.replace(/\\/g, "/").toLowerCase()
  const segments = normalized.split("/").filter(Boolean)
  const basename = segments.at(-1) ?? normalized
  if (
    segments.some((segment) =>
      ["__tests__", "test", "tests", "spec", "specs", "fixture", "fixtures", "mock", "mocks"].includes(segment),
    )
  )
    return "test_code"
  if (/\.(test|spec)\.[cm]?[jt]sx?$/.test(basename) || /^test[-_.].+\.[cm]?[jt]sx?$/.test(basename)) return "test_code"
  if (
    segments.some((segment) =>
      ["doc", "docs", "requirement", "requirements", "design", "architecture"].includes(segment),
    )
  )
    return "docs"
  if (/(^|\/)(package\.json|tsconfig\.json|vite\.config|rollup\.config|webpack\.config|bunfig\.toml)$/.test(normalized))
    return "config"
  if (segments.some((segment) => ["src", "lib", "packages", "app"].includes(segment))) return "production_code"
  if (/\.[cm]?[jt]sx?$|\.py$|\.go$|\.rs$|\.java$|\.kt$|\.swift$|\.mjs$|\.cjs$/.test(basename)) return "production_code"
  return "unknown"
}

function isTestOracleChange(diff: unknown, role: string | undefined, semantics: TraceChangeSemantics | undefined) {
  if (role !== "test_code" && role !== "mixed") return false
  if (!semantics?.numeric_constant_changes?.length) return false
  const text = fieldSummaryText(diff)
  const lines = parseChangedDiffLines(text)
  const changedLines = [...lines.added, ...lines.removed].join("\n")
  return /\b(assert|expect|toBe|toEqual|equal|strictEqual|should|matcher|断言|期望)\b/i.test(changedLines)
}

function enrichChangeSemanticsForTarget(input: {
  semantics: TraceChangeSemantics | undefined
  diff: unknown
  role: string
  changedTestOracle: boolean
}) {
  if (!input.semantics) return undefined
  const riskFlags = new Set(input.semantics.risk_flags ?? [])
  const operationKinds = new Set(input.semantics.operation_kinds ?? [])
  if (input.role === "test_code" || input.role === "mixed") riskFlags.add("test_code_changed")
  if (input.role === "production_code" || input.role === "mixed") riskFlags.add("production_code_changed")
  if (input.changedTestOracle) {
    riskFlags.add("test_oracle_changed")
    riskFlags.add("test_fixture_value_changed")
    operationKinds.add("test_oracle_update")
  }
  const summaryParts = [input.semantics.summary].filter((item): item is string => Boolean(item))
  if (input.changedTestOracle) summaryParts.push("test oracle changed before verification")
  return {
    ...input.semantics,
    ...(operationKinds.size ? { operation_kinds: [...operationKinds] } : {}),
    ...(riskFlags.size ? { risk_flags: [...riskFlags] } : {}),
    ...(summaryParts.length ? { summary: dedupeStrings(summaryParts).join("; ") } : {}),
  }
}

function semanticFactApplicability(data: Record<string, unknown> | undefined) {
  const structured = recordFromUnknown(data?.structured_claim)
  const span = recordFromUnknown(structured?.source_span)
  const spanPath = typeof span?.path === "string" ? span.path : ""
  const snippet = typeof span?.snippet_preview === "string" ? span.snippet_preview : ""
  const factKind = typeof data?.fact_kind === "string" ? data.fact_kind : ""
  if (factKind === "verification_output") return { status: "unknown", reasons: [] }
  const text = normalizeMatchText(
    [
      snippet,
      spanPath,
      data?.summary,
      data?.claim,
      snippet ? undefined : data?.data,
      snippet ? undefined : data?.source_locations,
      data?.metadata,
    ]
      .map((item) => fieldSummaryText(item))
      .join("\n"),
  )
  const reasons: string[] = []
  const pathLegacy = /(^|\/)(legacy|old|deprecated)(\/|$)/.test(normalizeMatchText(spanPath))
  const legacyStrong =
    pathLegacy ||
    /(^|[\s.:;])legacy\s+(implementation|code|file|migration|note)|deprecated|obsolete|retained\s+(only\s+)?for\s+migration|migration\s+comparison|old\s+implementation|historical|遗留|旧实现|废弃|历史/.test(
      text,
    )
  const activeStrong = /\b(active|current|canonical|source\s+of\s+truth|must|should|now)\b|当前|现行|必须|应当/.test(
    text,
  )
  const activeContrast = /\bnot\b.{0,40}\b(legacy|old|20\s*percent|20\s*%)|old\s+legacy\s+behavior/.test(text)
  if (pathLegacy) reasons.push("legacy_path")
  if (legacyStrong) reasons.push("legacy_context_terms")
  if (activeStrong || activeContrast) reasons.push("active_context_terms")
  if (pathLegacy) return { status: "legacy", reasons }
  if (activeStrong || activeContrast) return { status: "active", reasons }
  if (legacyStrong) return { status: "legacy", reasons }
  return { status: "unknown", reasons }
}

function semanticFactConflictKey(data: Record<string, unknown> | undefined) {
  const structured = recordFromUnknown(data?.structured_claim)
  const subject = normalizeMatchText(structured?.subject ?? data?.canonical_subject)
  const predicate = normalizeMatchText(structured?.predicate)
  const value = structured?.value
  if (!subject || !predicate || value === undefined) return undefined
  const span = recordFromUnknown(structured?.source_span)
  const snippet = normalizeMatchText(span?.snippet_preview)
  if (predicate === "discount-cap" && /\b(loyalty|volume|seat|component)\b/.test(snippet) && !/\bcap\b/.test(snippet)) {
    return undefined
  }
  return {
    key: `${subject}|${predicate}`,
    subject,
    predicate,
    value: semanticFactComparableValue(value),
  }
}

function semanticFactComparableValue(value: unknown) {
  const text = normalizeMatchText(value)
  if (!text) return text
  const percent = text.match(/\b(-?\d+(?:\.\d+)?)\s*(?:percent|%)\b/)
  if (percent) return `${formatComparableNumber(Number(percent[1]))}%`
  const numeric = Number(text)
  if (Number.isFinite(numeric) && Math.abs(numeric) > 0 && Math.abs(numeric) < 1) {
    return `${formatComparableNumber(numeric * 100)}%`
  }
  return text
}

function formatComparableNumber(value: number) {
  return Number(value.toFixed(6)).toString()
}

type SemanticFactChangePoint = {
  time_ms: number
  files: string[]
}

function semanticFactSourcePath(data: Record<string, unknown> | undefined) {
  const structured = recordFromUnknown(data?.structured_claim)
  const span = recordFromUnknown(structured?.source_span)
  const spanPath = typeof span?.path === "string" ? span.path : undefined
  if (spanPath) return spanPath
  const locations = Array.isArray(data?.source_locations) ? data.source_locations : []
  for (const location of locations) {
    const record = recordFromUnknown(location)
    if (typeof record?.path === "string") return record.path
  }
  return undefined
}

function semanticFactText(data: Record<string, unknown> | undefined) {
  const structured = recordFromUnknown(data?.structured_claim)
  const span = recordFromUnknown(structured?.source_span)
  const snippet = span?.snippet_preview
  return normalizeMatchText(
    [
      snippet,
      data?.summary,
      data?.claim,
      snippet ? undefined : data?.data,
      snippet ? undefined : data?.metadata,
      snippet ? undefined : data?.source_locations,
    ]
      .map((item) => fieldSummaryText(item))
      .join("\n"),
  )
}

function sourcePathMatchesChangedFile(sourcePath: string, changedFile: string) {
  const source = sourcePath.replace(/\\/g, "/").toLowerCase()
  const changed = changedFile.replace(/\\/g, "/").toLowerCase()
  return source === changed || source.endsWith(`/${changed}`) || changed.endsWith(`/${source}`)
}

function semanticFactRole(
  data: Record<string, unknown> | undefined,
  node: Pick<CausalNode, "time_ms">,
  changes: SemanticFactChangePoint[],
) {
  const factKind = typeof data?.fact_kind === "string" ? data.fact_kind : ""
  const sourcePath = semanticFactSourcePath(data)
  const text = semanticFactText(data)
  if (factKind === "verification_output") {
    return { semantic_role: "verification_result", fact_scope: "verification" }
  }
  if (factKind === "subagent_result") {
    return { semantic_role: "secondary_summary", fact_scope: "summary" }
  }
  const sourceRole = sourcePath ? changeTargetRoleForFile(sourcePath) : "unknown"
  const pathLegacy = sourcePath
    ? /(^|\/)(legacy|old|deprecated)(\/|$)/.test(sourcePath.replace(/\\/g, "/").toLowerCase())
    : false
  const legacyText =
    /\blegacy\b|deprecated|obsolete|retained\s+(only\s+)?for\s+migration|migration\s+comparison|historical|遗留|旧实现|废弃|历史/.test(
      text,
    )
  if (pathLegacy || (legacyText && sourceRole !== "test_code")) {
    return { semantic_role: "legacy_historical", fact_scope: "legacy_code" }
  }
  if (sourceRole === "test_code") {
    return { semantic_role: "test_expectation", fact_scope: "test_oracle" }
  }
  const requirementText =
    /\b(must|should|required|requirement|source\s+of\s+truth|canonical|current|active)\b|必须|应当|需求|现行|当前/.test(
      text,
    )
  if (sourceRole === "docs" && requirementText) {
    return { semantic_role: "requirement_rule", fact_scope: "requirement" }
  }
  if (sourceRole === "production_code") {
    const relatedChanges = sourcePath
      ? changes.filter((change) => change.files.some((file) => sourcePathMatchesChangedFile(sourcePath, file)))
      : []
    const priorChange = relatedChanges.some((change) => change.time_ms <= node.time_ms)
    const laterChange = relatedChanges.some((change) => change.time_ms > node.time_ms)
    if (priorChange && laterChange) return { semantic_role: "observed_intermediate_code", fact_scope: "current_code" }
    if (priorChange) return { semantic_role: "observed_post_change_code", fact_scope: "current_code" }
    if (laterChange) return { semantic_role: "observed_pre_change_code", fact_scope: "current_code" }
    return { semantic_role: "observed_current_code", fact_scope: "current_code" }
  }
  if (requirementText) {
    return { semantic_role: "requirement_rule", fact_scope: "requirement" }
  }
  return { semantic_role: "unknown", fact_scope: "unknown" }
}

type SemanticFactConflictMember = {
  node: CausalNode
  value: string
  semantic_role: string
}

function semanticFactConflictClassification(members: SemanticFactConflictMember[]) {
  const valuesByRole = new Map<string, Set<string>>()
  for (const member of members) {
    const current = valuesByRole.get(member.semantic_role) ?? new Set<string>()
    current.add(member.value)
    valuesByRole.set(member.semantic_role, current)
  }
  const requirementValues = valuesByRole.get("requirement_rule") ?? new Set<string>()
  const preChangeValues = valuesByRole.get("observed_pre_change_code") ?? new Set<string>()
  const currentCodeValues = new Set<string>([
    ...(valuesByRole.get("observed_current_code") ?? []),
    ...(valuesByRole.get("observed_intermediate_code") ?? []),
  ])
  const postChangeValues = valuesByRole.get("observed_post_change_code") ?? new Set<string>()
  const testValues = valuesByRole.get("test_expectation") ?? new Set<string>()
  const legacyValues = valuesByRole.get("legacy_historical") ?? new Set<string>()
  const secondaryValues = valuesByRole.get("secondary_summary") ?? new Set<string>()

  if (hasValueOutside(requirementValues, postChangeValues)) {
    return { conflict_kind: "post_change_requirement_mismatch", conflict_severity: "high", conflict_issue: true }
  }
  if (hasValueOutside(requirementValues, preChangeValues) || hasValueOutside(requirementValues, currentCodeValues)) {
    return { conflict_kind: "requirement_code_mismatch", conflict_severity: "high", conflict_issue: true }
  }
  if (hasValueOutside(requirementValues, testValues)) {
    return { conflict_kind: "stale_test_expectation", conflict_severity: "medium", conflict_issue: true }
  }
  if (hasValueOutside(requirementValues, legacyValues)) {
    return { conflict_kind: "legacy_contrast", conflict_severity: "low", conflict_issue: false }
  }
  if (secondaryValues.size && new Set(members.map((member) => member.value)).size > 1) {
    return { conflict_kind: "secondary_summary_conflict", conflict_severity: "low", conflict_issue: false }
  }
  return { conflict_kind: "generic_fact_conflict", conflict_severity: "medium", conflict_issue: true }
}

function hasValueOutside(reference: Set<string>, candidate: Set<string>) {
  if (!reference.size || !candidate.size) return false
  for (const value of candidate) {
    if (!reference.has(value)) return true
  }
  return false
}

function semanticConflictMemberIssue(
  role: string,
  classification: ReturnType<typeof semanticFactConflictClassification>,
) {
  if (!classification.conflict_issue) return false
  if (role === "legacy_historical" || role === "secondary_summary") return false
  return true
}

function parseChangedDiffLines(diffText: string): { added: string[]; removed: string[] } {
  const added: string[] = []
  const removed: string[] = []
  for (const rawLine of diffText.split(/\r?\n/)) {
    if (!rawLine) continue
    if (
      rawLine.startsWith("+++") ||
      rawLine.startsWith("---") ||
      rawLine.startsWith("@@") ||
      rawLine.startsWith("Index:") ||
      rawLine.startsWith("diff ") ||
      rawLine.startsWith("new file mode") ||
      rawLine.startsWith("deleted file mode")
    ) {
      continue
    }
    if (rawLine.startsWith("+")) added.push(rawLine.slice(1))
    if (rawLine.startsWith("-")) removed.push(rawLine.slice(1))
  }
  return { added, removed }
}

function identifiersFromLines(lines: string[]) {
  const ignored = new Set([
    "and",
    "as",
    "async",
    "await",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "default",
    "else",
    "export",
    "false",
    "finally",
    "for",
    "from",
    "function",
    "if",
    "import",
    "in",
    "let",
    "new",
    "null",
    "of",
    "return",
    "switch",
    "this",
    "throw",
    "true",
    "try",
    "undefined",
    "var",
    "while",
  ])
  const identifiers: string[] = []
  for (const line of lines) {
    for (const match of line.matchAll(/\b[A-Za-z_$][A-Za-z0-9_$]{1,80}\b/g)) {
      const value = match[0]
      if (ignored.has(value)) continue
      if (/^[A-Z_]+$/.test(value) && value.length <= 2) continue
      identifiers.push(value)
    }
  }
  return dedupeStrings(identifiers)
}

function numericConstantChangesFromLines(removedLines: string[], addedLines: string[]): TraceNumericConstantChange[] {
  const changes: TraceNumericConstantChange[] = []
  const usedAdded = new Set<number>()
  for (const removed of removedLines) {
    const removedNumbers = numericConstantsFromLine(removed)
    if (!removedNumbers.length) continue
    const removedShape = numericShape(removed)
    const addedIndex = addedLines.findIndex((added, index) => {
      if (usedAdded.has(index)) return false
      if (!numericConstantsFromLine(added).length) return false
      return numericShape(added) === removedShape
    })
    if (addedIndex === -1) continue
    usedAdded.add(addedIndex)
    const added = addedLines[addedIndex]
    const addedNumbers = numericConstantsFromLine(added)
    for (let index = 0; index < Math.min(removedNumbers.length, addedNumbers.length); index++) {
      if (removedNumbers[index] === addedNumbers[index]) continue
      changes.push({
        from: removedNumbers[index]!,
        to: addedNumbers[index]!,
        before: removed.trim(),
        after: added.trim(),
      })
    }
  }
  return changes
}

function numericConstantsFromLine(line: string) {
  return [...line.matchAll(/(^|[^A-Za-z0-9_$])(-?\d+(?:\.\d+)?%?)(?=$|[^A-Za-z0-9_$])/g)].map((match) => match[2]!)
}

function numericShape(line: string) {
  return line
    .replace(/(^|[^A-Za-z0-9_$])-?\d+(?:\.\d+)?%?(?=$|[^A-Za-z0-9_$])/g, "$1#")
    .replace(/\s+/g, " ")
    .trim()
}

function isHardcodeCandidateLine(line: string) {
  const trimmed = line.trim()
  if (!trimmed) return false
  if (/\bif\s*\(.+\b(input|req|args|params|case|scenario)\b.+(?:={2,3}|!==|!=).+\)\s*return\b/.test(trimmed))
    return true
  if (/\breturn\s+["'`][^"'`]{1,120}["'`]\s*;?$/.test(trimmed)) return true
  if (/\breturn\s+-?\d+(?:\.\d+)?\s*;?$/.test(trimmed)) return true
  return /\b(if|switch|case)\b/.test(trimmed) && /\b(48000|51000|850000|1200)\b/.test(trimmed)
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

function recordStringField(input: unknown, keys: string[]) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined
  return firstStringField(input, keys)
}

function nearestCompactionCheck(compaction: CausalNode, checks: CausalNode[]) {
  const sessionID = recordStringField(compaction.data, ["session_id", "sessionID"])
  const messageID = recordStringField(compaction.data, ["message_id", "messageID"])
  const before = checks.filter((check) => check.time_ms <= compaction.time_ms)
  const matches = before.filter((check) => {
    const checkSessionID = recordStringField(check.data, ["session_id", "sessionID"])
    const checkMessageID = recordStringField(check.data, ["message_id", "messageID"])
    if (sessionID && checkSessionID && sessionID !== checkSessionID) return false
    if (messageID && checkMessageID && messageID !== checkMessageID) return false
    return true
  })
  return (matches.length ? matches : before).at(-1)
}

function causalNodeSessionID(node: CausalNode) {
  return (
    recordStringField(node.data, ["session_id", "sessionID"]) ??
    recordStringField(objectField(node.data, "input"), ["session_id", "sessionID", "parent_session_id"]) ??
    recordStringField(objectField(node.data, "data"), ["session_id", "sessionID", "parent_session_id"]) ??
    recordStringField(objectField(node.data, "metadata"), ["session_id", "sessionID"]) ??
    recordStringField(node.metadata, ["session_id", "sessionID"])
  )
}

function causalNodeReferencesSession(node: CausalNode, sessionID: string) {
  const directSession = causalNodeSessionID(node)
  if (directSession === sessionID) return true
  return stringPreview(node.data, 20000).includes(sessionID)
}

function childTimelineSummary(records: CausalNode[]) {
  const by_type: Record<string, number> = {}
  for (const record of records) by_type[record.kind] = (by_type[record.kind] ?? 0) + 1
  return {
    record_count: records.length,
    first_event: records[0]
      ? { record_id: records[0].node_id, event_type: records[0].kind, time_ms: records[0].time_ms }
      : undefined,
    last_event: records.at(-1)
      ? { record_id: records.at(-1)?.node_id, event_type: records.at(-1)?.kind, time_ms: records.at(-1)?.time_ms }
      : undefined,
    by_type,
  }
}

function childMetricSummary(records: CausalNode[]) {
  return {
    llm_turn_count: records.filter((record) => record.kind === "llm.turn").length,
    tool_call_count: records.filter((record) => record.kind === "tool.call").length,
    mcp_call_count: records.filter((record) => record.kind === "mcp.call").length,
    semantic_evidence_count: records.filter(
      (record) => record.kind === "evidence.semantic_fact" || record.kind === "evidence.fact",
    ).length,
    response_output_count: records.filter((record) => record.kind === "response.output").length,
  }
}

type SubagentConsumptionEvidence = {
  node: CausalNode
  evidence_tier: "confirmed" | "content_matched"
  derivation_method: string
  matched_text_hash?: string
}

function applySubagentInlineFields(data: Record<string, unknown>, fields: Record<string, unknown>) {
  Object.assign(data, fields)
  delete data.child_trace_unavailable_reason
  const traceRef = objectField(data, "trace_ref")
  if (traceRef && typeof traceRef === "object" && !Array.isArray(traceRef)) {
    Object.assign(traceRef as Record<string, unknown>, fields)
    delete (traceRef as Record<string, unknown>).child_trace_unavailable_reason
  }
  const output = objectField(data, "output")
  const outputTraceRef = objectField(output, "trace_ref")
  if (outputTraceRef && typeof outputTraceRef === "object" && !Array.isArray(outputTraceRef)) {
    Object.assign(outputTraceRef as Record<string, unknown>, fields)
    delete (outputTraceRef as Record<string, unknown>).child_trace_unavailable_reason
  }
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
    "parts",
    "input",
    "body",
    "prompt",
    "messages",
    "message",
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
  if (isStandalonePathText(text)) return true
  if (/returned\s+\d+\s+content\s+item/i.test(text)) return true
  if (/^(result|output|response|summary|file observed|pricing file observed)\b/i.test(text)) return true
  if (text.length < 12 && !/\d/.test(text)) return true
  return false
}

function isStandalonePathText(input: string) {
  const text = input
    .trim()
    .replace(/^["'`]+|["'`]+$/g, "")
    .replace(/[。.!?；;:,，]+$/g, "")
    .trim()
  if (!text || text.length > 500) return false
  if (/\s/.test(text)) return false
  return /^(?:\.{0,2}\/|\/|private\/tmp\/|tmp\/)?[\w@~.-]+(?:\/[\w@~.-]+)+(?:\.[A-Za-z0-9_+-]+)?$/.test(text)
}

function isPathOnlyListingText(input: unknown) {
  const text = stringPreview(input, 4000).trim()
  if (!text) return false
  const stripped = text
    .replace(/<path>\s*([^<]+?)\s*<\/path>/gi, "$1")
    .replace(/<content>[\s\S]*?<\/content>/gi, "")
    .trim()
  if (!stripped) return false
  const lines = stripped
    .split(/\r?\n|,/)
    .map((line) => line.trim())
    .filter(Boolean)
  if (!lines.length || lines.length > 80) return false
  return lines.every((line) => isStandalonePathText(line))
}

function isPathListingSource(source: string, category: string) {
  return /glob|list|ls|find|path|file_search|search_paths/.test(`${source} ${category}`)
}

function isPathOnlyListingEvidence(input: EvidenceFactInput, source: string, category: string) {
  if (!isPathListingSource(source, category)) return false
  if (isPathOnlyListingText(input.summary)) return true
  const record = recordFromUnknown(input.data)
  if (!record) return isPathOnlyListingText(input.data)
  for (const key of ["output", "result", "results", "matches", "paths", "files", "path", "file", "filePath"]) {
    if (record[key] !== undefined && isPathOnlyListingText(record[key])) return true
  }
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

function implementationEntryValueFromText(input: string, sourceSpan?: TraceSourceLocation) {
  const inlineCodePath = input.match(
    /`((?:\.{0,2}\/|\/|~\/|[\w@+.-]+\/)?[\w@+.-]+\.(?:mjs|js|ts|tsx|jsx|json|md|txt|py|go|rs|java|yaml|yml))`/,
  )
  if (inlineCodePath?.[1]) return inlineCodePath[1]
  const explicitPath = sourceLocationsFromText(input).find((location) => location.path)?.path
  if (explicitPath) return explicitPath
  if (/^\s*export\s+function\b/i.test(input) && sourceSpan?.path) return sourceSpan.path
  const functionCall = input.match(/\b([A-Za-z_$][\w$]*)\s*\(([^)]*)\)/)
  if (functionCall?.[1]) return `${functionCall[1]}(${functionCall[2] ?? ""})`
  return sourceSpan?.path ?? "renewalQuote(input)"
}

function structuredClaimFromLineText(
  input: string,
  sourceSpan?: TraceSourceLocation,
): TraceStructuredClaim | undefined {
  const text = input.trim()
  if (!text) return undefined
  const normalized = text.toLowerCase()
  const explicitDiscountCapValue = explicitDiscountCapValueFromText(text)
  if (
    /(discount|折扣)/i.test(text) &&
    /(cap|capped|limit|maximum|max|上限|封顶|math\.min|0\.\d+|\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s+percent)/i.test(text) &&
    explicitDiscountCapValue
  ) {
    return {
      subject: /renewalquote/i.test(text) ? "renewalQuote" : "discount",
      predicate: "discount_cap",
      value: explicitDiscountCapValue,
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
  if (
    /implementation\s+entry|entry point|实现入口|入口|export function renewalQuote|renewalQuote\(input\)/i.test(text)
  ) {
    return {
      subject: "renewalQuote",
      predicate: "implementation_entry",
      value: implementationEntryValueFromText(text, sourceSpan),
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
  if (normalized.includes("math.min") && explicitDiscountCapValue) {
    return {
      subject: "renewalQuote",
      predicate: "discount_cap",
      value: explicitDiscountCapValue,
      source_span: sourceSpan,
      extraction_method: "source_line_pattern",
    }
  }
  return undefined
}

function explicitDiscountCapValueFromText(input: string) {
  const candidates: Array<{ value: string; index: number; end: number; priority: number }> = []
  for (const match of input.matchAll(/\b0\.\d+\b/g)) {
    if (match.index === undefined) continue
    candidates.push({
      value: match[0],
      index: match.index,
      end: match.index + match[0].length,
      priority: 0,
    })
  }
  for (const match of input.matchAll(/\b(\d+(?:\.\d+)?)\s*%/g)) {
    if (match.index === undefined || !match[1]) continue
    candidates.push({
      value: `${match[1]}%`,
      index: match.index,
      end: match.index + match[0].length,
      priority: 1,
    })
  }
  for (const match of input.matchAll(/\b(\d+(?:\.\d+)?)\s+percent\b/gi)) {
    if (match.index === undefined || !match[1]) continue
    candidates.push({
      value: `${match[1]} percent`,
      index: match.index,
      end: match.index + match[0].length,
      priority: 1,
    })
  }
  if (!candidates.length) return undefined

  const anchors = [
    ...input.matchAll(/math\.min|discount\s+cap|renewal\s+discount\s+cap|cap(?:ped)?|limit|maximum|max|上限|封顶/gi),
  ]
    .map((match) =>
      match.index === undefined ? undefined : { start: match.index, end: match.index + match[0].length },
    )
    .filter((anchor): anchor is { start: number; end: number } => anchor !== undefined)
  if (!anchors.length) return candidates.sort((a, b) => a.index - b.index || a.priority - b.priority)[0]?.value

  return candidates
    .map((candidate) => ({
      ...candidate,
      distance: Math.min(
        ...anchors.map((anchor) => {
          if (candidate.end < anchor.start) return anchor.start - candidate.end
          if (candidate.index > anchor.end) return candidate.index - anchor.end
          return 0
        }),
      ),
    }))
    .sort((a, b) => a.distance - b.distance || a.priority - b.priority || a.index - b.index)[0]?.value
}

function structuredLineClaimsFromText(input: string, fallback?: TraceSourceLocation) {
  const claims: TraceStructuredClaim[] = []
  const lines = input.split(/\r?\n/)
  for (const line of lines) {
    const match = line.match(/^\s*(\d+)\s*[:.)、]\s?(.*)$/)
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
  const pathOnlyListing = isPathOnlyListingEvidence(input, source, category)
  if (pathOnlyListing) flags.push("path_only_listing_fact", "path_only_evidence_fact")

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
      const parsedFailures = parseVerificationFailures({
        stdout: firstPresentField(dataRecord, ["stdout", "output", "message"]),
        stderr: firstPresentField(dataRecord, ["stderr"]),
      })
      const rawExit = optionalNumber(firstPresentField(dataRecord, ["exit_code", "exitCode"]))
      const inferred = inferredVerificationStatus({
        explicitStatus: primitiveClaimValue(firstPresentField(dataRecord, ["status", "result"])) as
          | TraceVerificationRecord["status"]
          | undefined,
        exitCode: rawExit,
        parsedFailures,
        command: firstStringField(dataRecord, ["command", "cmd"]),
      })
      const verificationClaim = structuredClaimFromRecord(
        {
          subject:
            firstPresentField(dataRecord, ["command", "cmd", "tool_name", "name"]) ?? input.category ?? input.source,
          predicate: "exit_status",
          value: parsedFailures.length
            ? "failed"
            : (firstPresentField(dataRecord, ["status", "exit_code", "exitCode", "result"]) ?? inferred.status),
          reason: firstPresentField(dataRecord, ["message", "stderr", "stdout"]),
        },
        "verification_output",
        sourceSpan,
      )
      flags.push("weak_verification_claim")
      flags.push(...inferred.quality_flags)
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
  const source = input.source.toLowerCase()
  const category = input.category?.toLowerCase() ?? ""
  const forcedSecondarySummaryFact = Boolean(
    recordFromUnknown(input.metadata)?.derived_from_multifact &&
      (source.includes("subagent") || source.includes("task")),
  )
  const secondarySummaryFact = isSecondarySummaryFact(input, structured.structured_claim) || forcedSecondarySummaryFact
  const qualityFlags = dedupeStrings([
    ...(input.quality_flags ?? []),
    ...evidenceQualityFlags(input),
    ...structured.quality_flags,
    ...(secondarySummaryFact ? ["secondary_source_fact", "summary_derived_fact"] : []),
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
  const claim = factText ?? summary ?? structuredValue
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
    support_level:
      input.support_level ??
      (qualityFlags.includes("empty_subagent_result") ? "weak" : secondarySummaryFact ? "context" : "direct"),
    quality_flags: qualityFlags,
    evidence_origin: secondarySummaryFact ? "secondary_summary" : "primary_observation",
  }
}

function structuredClaimDedupeKey(claim: TraceStructuredClaim) {
  const span = claim.source_span
  return [
    claim.subject ?? "",
    claim.predicate ?? "",
    claim.value === undefined ? "" : String(claim.value),
    claim.qualifier ?? "",
    span?.path ?? span?.uri ?? "",
    span?.line_start === undefined ? "" : String(span.line_start),
  ]
    .join("|")
    .toLowerCase()
}

function isDerivedMultifactInput(input: EvidenceFactInput) {
  return recordFromUnknown(input.metadata)?.derived_from_multifact === true
}

function shouldDeriveMultipleStructuredFacts(input: EvidenceFactInput) {
  if (isDerivedMultifactInput(input)) return false
  const source = input.source.toLowerCase()
  const category = input.category?.toLowerCase() ?? ""
  return /subagent|task|mcp/.test(`${source} ${category}`)
}

function additionalStructuredClaimsFromEvidence(
  input: EvidenceFactInput,
  sourceLocations: TraceSourceLocation[],
  primaryClaim: TraceStructuredClaim,
) {
  if (!shouldDeriveMultipleStructuredFacts(input)) return []
  const fallback = claimSourceSpan(input.data, sourceLocations)
  const seen = new Set<string>([structuredClaimDedupeKey(primaryClaim)])
  const output: TraceStructuredClaim[] = []
  const textCandidates = dedupeStrings([
    stringPreview(input.summary, 6000),
    ...collectTextCandidates(input.data).map((item) => stringPreview(item, 6000)),
  ]).filter((item) => item.trim())
  for (const text of textCandidates) {
    for (const claim of structuredLineClaimsFromText(text, fallback)) {
      const key = structuredClaimDedupeKey(claim)
      if (seen.has(key)) continue
      seen.add(key)
      output.push(claim)
      if (output.length >= 12) return output
    }
  }
  return output
}

function evidenceFactDataFromStructuredClaim(claim: TraceStructuredClaim) {
  return {
    subject: claim.subject,
    predicate: claim.predicate,
    value: claim.value,
    qualifier: claim.qualifier,
    path: claim.source_span?.path,
    uri: claim.source_span?.uri,
    line_start: claim.source_span?.line_start,
    line_end: claim.source_span?.line_end,
    snippet: claim.source_span?.snippet_preview,
    raw_artifact_ref: claim.raw_artifact_ref,
  }
}

function evidenceFactSummaryFromStructuredClaim(claim: TraceStructuredClaim) {
  return [claim.subject, claim.predicate, claim.value === undefined ? undefined : String(claim.value)]
    .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    .join(" ")
}

function isSecondarySummaryFact(input: EvidenceFactInput, claim: TraceStructuredClaim) {
  const source = input.source.toLowerCase()
  if (!source.includes("subagent") && !source.includes("task")) return false
  return (
    claim.extraction_method === "source_line_pattern" ||
    claim.extraction_method === "summary_sentence" ||
    claim.extraction_method === "fallback_summary"
  )
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
  if (isTaskToolOutputObservation(input)) return false
  if (isPlanStateInput(input.source, input.category, input.data ?? input.summary)) return false
  if (/tool|mcp|skill|subagent|task|verification|grep|read|bash|file/i.test(input.source)) return true
  if (input.category && /tool|mcp|skill|subagent|verification|test|file|grep|read/i.test(input.category)) return true
  return false
}

function isTaskToolOutputObservation(input: ObservationInput) {
  if (input.source.toLowerCase() !== "task") return false
  if ((input.category ?? "").toLowerCase() !== "tool_output") return false
  const data = recordFromUnknown(input.data)
  if (!data) return true
  return (
    data.output !== undefined ||
    data.child_session_id !== undefined ||
    data.childMessageId !== undefined ||
    data.subagent_type !== undefined
  )
}

function isPlanStateInput(source: string | undefined, category: string | undefined, payload: unknown) {
  const sourceText = `${source ?? ""} ${category ?? ""}`.toLowerCase()
  if (/todowrite|todo_write|todo-list|todo list|plan_state/.test(sourceText)) return true
  const record = recordFromUnknown(payload)
  const todos = Array.isArray(record?.todos)
    ? record?.todos
    : Array.isArray(objectField(record?.args, "todos"))
      ? (objectField(record?.args, "todos") as unknown[])
      : undefined
  return Boolean(todos?.length)
}

function planStateSummary(payload: unknown) {
  const record = recordFromUnknown(payload)
  const todos = Array.isArray(record?.todos)
    ? record.todos
    : Array.isArray(objectField(record?.args, "todos"))
      ? (objectField(record?.args, "todos") as unknown[])
      : []
  const counts: Record<string, number> = {
    total: todos.length,
    pending: 0,
    in_progress: 0,
    completed: 0,
    cancelled: 0,
  }
  const items = todos
    .map((item) => {
      const todo = recordFromUnknown(item) ?? {}
      const status = String(todo.status ?? "unknown")
      if (status in counts) counts[status] = (counts[status] ?? 0) + 1
      return {
        content: typeof todo.content === "string" ? todo.content : stringPreview(todo.content, 200),
        status,
        priority: typeof todo.priority === "string" ? todo.priority : undefined,
      }
    })
    .filter((item) => item.content)
  return {
    ...counts,
    items: items.slice(0, 20),
    truncated: items.length > 20,
  }
}

function evidenceRecordKind(input: EvidenceFactInput, canonical: ReturnType<typeof canonicalEvidence>) {
  if (isPlanStateInput(input.source, input.category, input.data ?? input.summary)) return "task.plan_state"
  if (canonical.quality_flags.includes("path_only_listing_fact")) return "execution.observation"
  if (
    canonical.quality_flags.includes("path_only_evidence_fact") &&
    canonical.quality_flags.some((flag) => flag === "generic_claim" || flag === "fallback_summary_claim")
  ) {
    return "execution.observation"
  }
  if (
    canonical.fact_kind === "observation" &&
    canonical.quality_flags.some((flag) =>
      ["generic_claim", "fallback_summary_claim", "path_only_evidence_fact"].includes(flag),
    )
  ) {
    return "execution.observation"
  }
  return "evidence.semantic_fact"
}

function evidenceRecordLabel(kind: string) {
  if (kind === "task.plan_state") return "task.plan_state"
  if (kind === "execution.observation") return "execution.observation"
  return "evidence.semantic_fact"
}

function semanticFactDedupeKey(
  input: EvidenceFactInput,
  canonical: ReturnType<typeof canonicalEvidence>,
  sourceLocations: TraceSourceLocation[],
) {
  const path =
    sourceLocations.find((location) => location.path)?.path ??
    canonical.structured_claim.source_span?.path ??
    firstStringField(input.data, ["path", "file", "filePath", "filepath"]) ??
    ""
  return [
    input.source.toLowerCase(),
    input.category?.toLowerCase() ?? "",
    String(canonical.canonical_subject ?? ""),
    String(canonical.claim ?? ""),
    String(path),
    String(canonical.evidence_origin ?? ""),
  ].join("|")
}

function compactionDerivedFields(input: {
  tokenBefore?: number
  tokenAfter?: number
  retainedFactRefs?: unknown[]
  droppedFactRefs?: unknown[]
  outputSummary?: unknown
  autoContinue?: boolean
  afterContextRefs?: string[]
}) {
  const retainedFactCount = input.retainedFactRefs?.length ?? 0
  const droppedFactCount = input.droppedFactRefs?.length ?? 0
  const retentionRatio =
    typeof input.tokenBefore === "number" && input.tokenBefore > 0 && typeof input.tokenAfter === "number"
      ? Number((input.tokenAfter / input.tokenBefore).toFixed(4))
      : undefined
  const risks: string[] = []
  if (input.tokenBefore === undefined) risks.push("missing_token_estimate_before")
  if (typeof input.tokenBefore === "number" && input.tokenBefore > 0 && input.tokenAfter === 0)
    risks.push("zero_token_estimate_after")
  if (droppedFactCount > 0) risks.push("dropped_semantic_facts")
  if (!stringPreview(input.outputSummary, 200).trim()) risks.push("empty_compaction_summary")
  if (input.autoContinue && !(input.afterContextRefs?.length ?? 0)) risks.push("auto_continue_without_after_refs")
  return {
    retention_ratio: retentionRatio,
    retained_fact_count: retainedFactCount,
    dropped_fact_count: droppedFactCount,
    compression_loss_risks: risks,
  }
}

function compactionSummarySemantics(input: unknown) {
  const text = stringPreview(input, 12000)
  if (!text.trim()) {
    return {
      summary_key_facts: [] as string[],
      summary_constraint_facts: [] as string[],
      summary_preserved_paths: [] as string[],
    }
  }
  const lines = text
    .split(/\r?\n/)
    .map((line) =>
      line
        .replace(/^#+\s*/, "")
        .replace(/^[-*]\s*/, "")
        .replace(/^\d+[.)]\s*/, "")
        .replace(/\*\*/g, "")
        .trim(),
    )
    .filter((line) => line && !/^[-=]{3,}$/.test(line) && !isNonFactualResponseClaim(line))
  const constraintPattern =
    /must|do not|don't|only modify|forbidden|constraint|不允许|不能|禁止|只能|必须|不得|out of scope|must stay/i
  const factPattern = /owner|cap|percent|%|discount|renewalQuote|entry|target|负责人|上限|折扣|入口|目标|归属/i
  const pathPattern = /(?:src|docs|test|packages)\/[\w@+./-]+/g
  const summaryConstraintFacts = dedupeStrings(lines.filter((line) => constraintPattern.test(line))).slice(0, 12)
  const summaryKeyFacts = dedupeStrings(
    lines.filter((line) => constraintPattern.test(line) || factPattern.test(line) || pathPattern.test(line)),
  ).slice(0, 20)
  const summaryPreservedPaths = dedupeStrings(
    [...text.matchAll(pathPattern)].map((match) => match[0].replace(/[.,，。；;:：)）\]]+$/, "")),
  ).slice(0, 20)
  return {
    summary_key_facts: summaryKeyFacts,
    summary_constraint_facts: summaryConstraintFacts,
    summary_preserved_paths: summaryPreservedPaths,
  }
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
  private readonly subjectRevision: string | undefined
  private readonly subjectRevisionProvenance: TraceManifest["subject_revision_provenance"]
  private spans = new Map<string, TraceSpan>()
  private spanNodeIDs = new Map<string, string>()
  private events: TraceEvent[] = []
  private readonly causalIR: TraceOwnedCausalIRStore
  private get causalNodes() {
    return this.causalIR.nodes as CausalNode[]
  }
  private get causalEdges() {
    return this.causalIR.edges as CausalEdge[]
  }
  private get artifacts() {
    return this.causalIR.artifacts as TraceArtifact[]
  }
  private artifactByDedupeKey = new Map<string, TraceArtifact>()
  private diagnosticSequence = 0
  private semanticFactNodeIDsByKey = new Map<string, string>()
  private errors: TraceError[] = []
  private contextSnapshots: TraceContextSnapshot[] = []
  private semanticDecisions: TraceSemanticDecision[] = []
  private semanticEdgeSequence = 0
  private verificationRecords: TraceVerificationRecord[] = []
  private changeRecords: TraceChangeRecord[] = []
  private constraintRecords: TraceConstraintRecord[] = []
  private responseSegments: TraceResponseSegment[] = []
  private responseSourceBySegmentID = new Map<string, unknown>()
  private claimedResponseSegmentIDs = new Set<string>()
  private designRecords: TraceDesignRecord[] = []
  private recentFailedVerificationID: string | undefined
  private recentChangeID: string | undefined
  private repositoryRevision = 0
  private recentContextSnapshotIDs: string[] = []
  private recentPromptNodeIDs: string[] = []
  private recentContextNodeIDs: string[] = []
  private recentLLMNodeIDs: string[] = []
  private recentEvidenceNodeIDs: string[] = []
  private recentVerificationIDs: string[] = []
  private recentChangeIDs: string[] = []
  private recentToolSpanIDs: string[] = []
  private recentToolOutcomeRefs: string[] = []
  private recentToolFailureRefs: string[] = []
  private temporalSourceRefsBySelection = new WeakMap<string[], string[]>()
  private temporalAdvisoryEdgeKeys = new Set<string>()
  private contextSetNodeIDsByKey = new Map<string, string>()
  private toolOutcomeRefsByCallID = new Map<
    string,
    Array<{ sourceRef: string; sessionID?: string; messageID?: string }>
  >()
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
    const configuredSubjectRevision = config.subjectRevision?.trim()
    const environmentSubjectRevision = process.env.OPENCODE_TRACE_SUBJECT_REVISION?.trim()
    this.subjectRevision = configuredSubjectRevision || environmentSubjectRevision || undefined
    this.subjectRevisionProvenance = this.subjectRevision
      ? {
          method: configuredSubjectRevision ? "case_trace_config" : "environment_variable",
          source: configuredSubjectRevision ? "CaseTraceConfig.subjectRevision" : "OPENCODE_TRACE_SUBJECT_REVISION",
          bound_at: "case_start",
          case_id: this.caseID,
          run_id: this.runID,
        }
      : undefined
    this.environment = {
      cwd: process.cwd(),
      argv: process.argv.slice(2),
      pid: process.pid,
      ...config.environment,
    }
    this.causalIR = new TraceOwnedCausalIRStore({
      runID: this.runID,
      caseID: this.caseID,
      append: (entry) => this.writeCausalIRRecord(entry),
    })
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
        this.causalIR.updateNode(node)
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
      session_id: stringField(record, ["sessionID", "session_id"]),
      message_id: stringField(record, ["messageID", "message_id"]),
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
        if (traceRef.child_trace_unavailable_reason)
          data.child_trace_unavailable_reason = traceRef.child_trace_unavailable_reason
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
    this.causalIR.updateNode(skillNode)
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
    this.promoteToolRuntimeEvent(input)
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

  private promoteToolRuntimeEvent(input: TraceEventInput) {
    const eventType =
      input.event_type === "execute.error" && input.component === "tool" ? "tool.error" : input.event_type
    if (eventType !== "tool.call" && eventType !== "tool.result" && eventType !== "tool.error") return undefined
    const payload = recordFromUnknown(input.data) ?? {}
    const callID = toolCallIDFromPayload(payload, input.span_id)
    const toolName = toolNameFromPayload(payload)
    const args = toolArgsFromPayload(payload)
    const sessionID = firstStringField(payload, ["sessionID", "session_id"])
    const messageID = firstStringField(payload, ["messageID", "message_id"])
    const scopedCallID = callID
      ? [sessionID ? safeNodeIDPart(sessionID) : undefined, safeNodeIDPart(callID)].filter(Boolean).join("_")
      : undefined
    const sourceRef =
      eventType === "tool.error" || eventType === "tool.result" ? toolSourceRef(eventType, callID) : undefined
    const common = {
      call_id: callID,
      tool_name: toolName,
      session_id: sessionID,
      message_id: messageID,
      part_id: firstStringField(payload, ["partID", "part_id"]),
      args,
      provider_executed: payload.providerExecuted,
      source_ref: sourceRef,
    }

    if (eventType === "tool.call") {
      const nodeID = scopedCallID ? `toolcall_${scopedCallID}` : undefined
      const existing = this.causalNodes.find((node) => {
        if (node.kind !== "tool.call") return false
        if (nodeID && node.node_id === nodeID) return true
        if (input.span_id && node.span_id === input.span_id) return true
        const nodeInput = recordFromUnknown(node.data?.input)
        const nodeSessionID =
          firstStringField(node.data, ["session_id", "sessionID"]) ??
          firstStringField(nodeInput, ["session_id", "sessionID"])
        if (sessionID && nodeSessionID && nodeSessionID !== sessionID) return false
        return Boolean(
          callID &&
            (firstStringField(node.data, ["call_id", "callID"]) === callID ||
              firstStringField(nodeInput, ["callID", "call_id", "toolCallID", "tool_call_id"]) === callID),
        )
      })
      if (existing) {
        existing.data = {
          ...(existing.data ?? {}),
          ...common,
          input: objectField(payload, "input") ?? existing.data?.input,
          provider_metadata:
            objectField(payload, "providerMetadata") ??
            objectField(payload, "provider_metadata") ??
            existing.data?.provider_metadata,
          request_status: "requested",
        }
        existing.source_refs = mergeRefs(existing.source_refs, callID ? [`tool_call:${callID}`] : undefined)
        existing.source_locations = dedupeSourceLocations([
          ...(existing.source_locations ?? []),
          ...collectSourceLocations(args ?? payload),
        ])
        existing.typed_resources = mergeTypedResources(existing.typed_resources, [
          {
            type: "tool_call",
            tool_name: toolName,
            call_id: callID,
            path: firstStringField(args, ["path", "filePath", "filepath"]),
            command: firstStringField(args, ["command"]),
          },
        ])
        existing.artifact_refs = this.collectArtifactRefs(existing.data)
        this.causalIR.updateNode(existing)
        return existing
      }
      const node = this.node({
        node_id: nodeID,
        kind: "tool.call",
        component: "tool",
        span_id: input.span_id,
        title: toolName ?? "tool call",
        status: "success",
        data: {
          ...common,
          input: objectField(payload, "input"),
          provider_metadata: objectField(payload, "providerMetadata") ?? objectField(payload, "provider_metadata"),
          request_status: "requested",
        },
        source_refs: callID ? [`tool_call:${callID}`] : undefined,
        source_locations: collectSourceLocations(args ?? payload),
        typed_resources: [
          {
            type: "tool_call",
            tool_name: toolName,
            call_id: callID,
            path: firstStringField(args, ["path", "filePath", "filepath"]),
            command: firstStringField(args, ["command"]),
          },
        ],
      })
      return node
    }

    const isError = eventType === "tool.error"
    const error = isError ? errorInfo(payload.error ?? input.data) : undefined
    const nodeID = scopedCallID ? `${isError ? "toolerror" : "toolresult"}_${scopedCallID}` : undefined
    const existing = nodeID ? this.causalNodes.find((node) => node.node_id === nodeID) : undefined
    if (existing) return existing
    const node = this.node({
      node_id: nodeID,
      kind: eventType,
      component: "tool",
      span_id: input.span_id,
      title: toolName ?? (isError ? "tool error" : "tool result"),
      status: isError ? "error" : "success",
      data: {
        ...common,
        status: isError ? "error" : "success",
        title: firstStringField(payload, ["title"]),
        metadata: objectField(payload, "metadata"),
        output: isError ? undefined : (objectField(payload, "output") ?? payload.output),
        attachments: payload.attachments,
        error,
        error_kind: isError ? toolErrorKind(payload.error ?? input.data) : undefined,
        error_message: isError ? error?.message : undefined,
        result_kind: isError ? undefined : "tool_output",
        observed_by_model: true,
      },
      source_refs: dedupeStrings([...(callID ? [`tool_call:${callID}`] : []), ...(sourceRef ? [sourceRef] : [])]),
      source_locations: collectSourceLocations(args ?? payload),
      typed_resources: [
        {
          type: isError ? "tool_error" : "tool_result",
          tool_name: toolName,
          call_id: callID,
          error_kind: isError ? toolErrorKind(payload.error ?? input.data) : undefined,
          path: firstStringField(args, ["path", "filePath", "filepath"]),
          command: firstStringField(args, ["command"]),
        },
      ],
    })
    const callNode = this.closeMatchingToolCall(
      callID,
      node,
      isError ? "error" : "success",
      payload.error ?? payload.output,
      sessionID,
    )
    if (sourceRef) {
      this.remember(this.recentToolOutcomeRefs, sourceRef)
      if (isError) this.remember(this.recentToolFailureRefs, sourceRef, 24)
      if (callID) {
        const entries = this.toolOutcomeRefsByCallID.get(callID) ?? []
        entries.push({ sourceRef: `node:${node.node_id}`, sessionID, messageID })
        this.toolOutcomeRefsByCallID.set(callID, entries.slice(-8))
      }
      this.backfillToolOutcomeRef({
        callID,
        sourceRef,
        canonicalOutcomeRef: `node:${node.node_id}`,
        sessionID,
        spanID: input.span_id,
        outcomeKind: eventType,
      })
    }
    if (callID) {
      this.causalEdge({
        from: callNode
          ? { type: "node", id: callNode.node_id, label: toolName ?? "tool.call" }
          : { type: "tool_call", id: callID, label: toolName ?? "tool.call" },
        to: { type: "node", id: node.node_id, label: eventType },
        relation: isError ? "failed_before" : "produced",
        label: isError ? "Tool call failed with an observed error" : "Tool call produced an observed result",
        metadata: {
          compatibility_from_ref: `tool_call:${callID}`,
          compatibility_to_ref: `${isError ? "tool_error" : "tool_result"}:${callID}`,
        },
      })
    }
    return node
  }

  private closeMatchingToolCall(
    callID: string | undefined,
    outcomeNode: CausalNode,
    status: Exclude<TraceStatus, "running">,
    outcome: unknown,
    sessionID?: string,
  ) {
    if (!callID) return
    const node = this.causalNodes.find((item) => {
      if (item.kind !== "tool.call") return false
      if (item.status !== "running" && item.data?.outcome_record_id) return false
      const input = objectField(item.data, "input")
      const nodeSessionID =
        firstStringField(item.data, ["session_id", "sessionID"]) ?? firstStringField(input, ["session_id", "sessionID"])
      if (sessionID && nodeSessionID && nodeSessionID !== sessionID) return false
      return (
        firstStringField(item.data, ["call_id", "callID"]) === callID ||
        firstStringField(input, ["callID", "call_id", "toolCallID", "tool_call_id"]) === callID
      )
    })
    if (!node) return
    node.status = status
    node.data = {
      ...(node.data ?? {}),
      request_status: "completed",
      outcome_record_id: outcomeNode.node_id,
      outcome_ref: `${outcomeNode.kind === "tool.error" ? "tool_error" : "tool_result"}:${callID}`,
      output: status === "success" ? this.summarizeCausalValue(outcome, "tool.call.output") : node.data?.output,
      error: status === "error" ? errorInfo(outcome) : node.data?.error,
    }
    node.artifact_refs = this.collectArtifactRefs(node.data)
    this.causalIR.updateNode(node)
    return node
  }

  private backfillToolOutcomeRef(input: {
    callID: string | undefined
    sourceRef: string
    canonicalOutcomeRef: string
    sessionID?: string
    spanID?: string
    outcomeKind: "tool.result" | "tool.error"
  }) {
    const targetRefs = new Set<string>()
    if (input.callID) targetRefs.add(`tool_call:${input.callID}`)
    if (input.spanID) targetRefs.add(`span:${input.spanID}`)

    const matchingCallNodes = this.causalNodes.filter((node) => {
      if (node.kind !== "tool.call" || !input.callID) return false
      const nodeInput = recordFromUnknown(node.data?.input)
      const matchesCall =
        firstStringField(node.data, ["call_id", "callID"]) === input.callID ||
        firstStringField(nodeInput, ["callID", "call_id", "toolCallID", "tool_call_id"]) === input.callID
      if (!matchesCall) return false
      const nodeSessionID = causalNodeSessionID(node)
      return !input.sessionID || !nodeSessionID || nodeSessionID === input.sessionID
    })
    for (const node of matchingCallNodes) {
      targetRefs.add(`node:${node.node_id}`)
      if (node.span_id) targetRefs.add(`span:${node.span_id}`)
    }

    if (!targetRefs.size) return
    const parsedOutcome = this.parseSourceRef(input.canonicalOutcomeRef)
    let changed = true
    while (changed) {
      changed = false
      for (const node of this.causalNodes) {
        if (!this.canBackfillToolOutcome(node)) continue
        const sourceRefs = node.source_refs ?? []
        if (sourceRefs.includes(input.canonicalOutcomeRef)) continue
        const matchingRefs = sourceRefs.filter((ref) => targetRefs.has(ref))
        if (!matchingRefs.length) continue
        const nodeSessionID = causalNodeSessionID(node)
        if (input.sessionID && nodeSessionID && nodeSessionID !== input.sessionID) continue
        if (input.sessionID && !nodeSessionID && matchingRefs.every((ref) => ref === `tool_call:${input.callID}`))
          continue

        node.source_refs = dedupeStrings([...sourceRefs, input.sourceRef, input.canonicalOutcomeRef])
        if (node.data && typeof node.data === "object") {
          const existing =
            Array.isArray(node.data.tool_outcome_refs) &&
            node.data.tool_outcome_refs.every((item) => typeof item === "string")
              ? (node.data.tool_outcome_refs as string[])
              : []
          node.data = {
            ...node.data,
            tool_outcome_refs: dedupeStrings([...existing, input.sourceRef, input.canonicalOutcomeRef]),
          }
        }
        node.artifact_refs = this.collectArtifactRefs(node.data)
        this.causalIR.updateNode(node)
        if (parsedOutcome && !this.hasCausalEdge(parsedOutcome, node.node_id, "derived_from")) {
          this.causalEdge({
            from: parsedOutcome,
            to: { type: node.kind.startsWith("evidence.") ? "evidence" : "node", id: node.node_id, label: node.kind },
            relation: "derived_from",
            label:
              input.outcomeKind === "tool.error"
                ? "Tool failure provenance was backfilled onto this semantic record"
                : "Tool result provenance was backfilled onto this semantic record",
          })
        }
        targetRefs.add(`node:${node.node_id}`)
        targetRefs.add(`observation:${node.node_id}`)
        targetRefs.add(`evidence:${node.node_id}`)
        changed = true
      }
    }
  }

  private canBackfillToolOutcome(node: CausalNode) {
    return (
      node.kind === "execution.observation" ||
      node.kind === "observation" ||
      node.kind === "evidence.fact" ||
      node.kind === "evidence.semantic_fact"
    )
  }

  private hasCausalEdge(from: TraceRef, toNodeID: string, relation: string) {
    return this.causalEdges.some(
      (edge) =>
        edge.relation === relation &&
        edge.from.type === from.type &&
        edge.from.id === from.id &&
        edge.to.id === toNodeID,
    )
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
    const explicitSourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const generationProvenance = this.currentGenerationProvenance(input.metadata)
    const generationRefs =
      input.component === "processor" || /reasoning|llm|tool/i.test(input.decision_type)
        ? generationProvenance.refs
        : []
    const sourceRefs = dedupeStrings([...explicitSourceRefs, ...generationRefs])
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
    const node = this.node({
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
        generation_provenance_refs: generationRefs,
        message_transforms: generationProvenance.messageTransforms,
        selected_context_refs: generationProvenance.selectedContextRefs,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      source_locations: dedupeSourceLocations([
        ...(input.source_locations ?? []),
        ...collectSourceLocations(input.metadata),
      ]),
      metadata: input.metadata,
    })
    this.linkGenerationProvenanceToDecision(node, generationProvenance)
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
    const explicitSourceRefs = input.source_refs ?? input.evidence_refs ?? []
    const inferredToolOutcomeRefs = this.toolOutcomeRefsSelectedIntoContext(input)
    const inferredToolContextSet = inferredToolOutcomeRefs.length
      ? this.contextSet({
          kind: "confirmed_tool_selection",
          memberRefs: inferredToolOutcomeRefs,
          sessionID: input.session_id,
          messageID: input.message_id,
          selectionMethod: "tool_call_identity_in_message",
        })
      : undefined
    const inferredToolContextSetRef = inferredToolContextSet
      ? `node:${inferredToolContextSet.node_id}`
      : undefined
    const sourceRefs = dedupeStrings([
      ...explicitSourceRefs,
      ...(inferredToolContextSetRef ? [inferredToolContextSetRef] : []),
    ])
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
        inferred_tool_context_refs: inferredToolOutcomeRefs,
        inferred_tool_context_set_ref: inferredToolContextSetRef,
        provenance_inference:
          inferredToolOutcomeRefs.length > 0
            ? { method: "tool_call_identity_in_message", evidence_tier: "confirmed", behavior_impact: "none" }
            : undefined,
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
    for (const ref of explicitSourceRefs) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "node", id: node.node_id, label: "context.transform" },
        relation: "context_transform",
        label: "Context transform consumed source record",
      })
    }
    if (inferredToolContextSet && inferredToolContextSetRef) {
      this.causalEdge({
        from: {
          type: "node",
          id: inferredToolContextSet.node_id,
          label: inferredToolContextSet.kind,
        },
        to: { type: "node", id: node.node_id, label: "context.transform" },
        relation: "used_as_context",
        evidence_tier: "confirmed",
        eligible_for_attribution: true,
        derivation_method: "tool_call_identity_in_message",
        evidence_refs: [inferredToolContextSetRef],
        label: "Confirmed tool-result context set was selected into the model request",
      })
    }
    if (input.stage === "model_messages_built" || input.stage === "llm_request_ready") {
      this.recordRequestedSkillAvailability(input.output, node.node_id)
    }
    return node
  }

  private toolOutcomeRefsSelectedIntoContext(input: ContextTransformInput) {
    const refs: string[] = []
    const callIDs = structuredToolCallIDs([input.input, input.output, input.transforms])
    for (const callID of callIDs) {
      const entries = this.toolOutcomeRefsByCallID.get(callID) ?? []
      for (const entry of entries) {
        if (input.session_id && entry.sessionID !== input.session_id) continue
        refs.push(entry.sourceRef)
      }
    }
    return dedupeStrings(refs)
  }

  private contextSet(input: {
    kind: "confirmed_tool_selection" | "temporal_advisory"
    memberRefs: string[]
    sessionID?: string
    messageID?: string
    selectionMethod: string
  }) {
    const memberRefs = dedupeStrings(input.memberRefs).sort()
    const key = [input.kind, input.sessionID ?? "", input.messageID ?? "", ...memberRefs].join("|")
    const existingID = this.contextSetNodeIDsByKey.get(key)
    if (existingID) {
      const existing = this.causalNodes.find((node) => node.node_id === existingID)
      if (existing) return existing
    }
    const node = this.node(
      {
        node_id: `contextset_${hash(key)}`,
        kind: "context.pack",
        component: "context",
        title:
          input.kind === "confirmed_tool_selection"
            ? "Confirmed tool-result context set"
            : "Temporal advisory context set",
        status: "success",
        data: {
          context_set_kind: input.kind,
          member_refs: memberRefs,
          member_count: memberRefs.length,
          session_id: input.sessionID,
          message_id: input.messageID,
          selection_method: input.selectionMethod,
          membership_storage: "payload_only",
          attribution_eligible: input.kind === "confirmed_tool_selection",
          behavior_impact: "none",
        },
      },
      { trackGeneration: false, writePartial: false },
    )
    this.contextSetNodeIDsByKey.set(key, node.node_id)
    if (input.kind === "confirmed_tool_selection") {
      for (const ref of memberRefs) {
        const parsed = this.parseSourceRef(ref)
        if (!parsed || parsed.id === node.node_id) continue
        this.causalEdge({
          from: parsed,
          to: { type: "node", id: node.node_id, label: node.kind },
          relation: "selected_into_context",
          evidence_tier: "confirmed",
          eligible_for_attribution: true,
          derivation_method: input.selectionMethod,
          evidence_refs: [ref],
          label: "Tool result is a member of the confirmed model-request context set",
        })
      }
    }
    return node
  }

  edge(input: SemanticEdgeInput) {
    const normalized = normalizeTemporalReferences(input).value
    const sequence = ++this.semanticEdgeSequence
    const edge: TraceSemanticEdge = {
      edge_id: normalized.edge_id ?? semanticID("edge", sequence),
      from: normalized.from,
      to: normalized.to,
      relation: normalized.relation,
      evidence_tier: normalized.evidence_tier,
      eligible_for_attribution: normalized.eligible_for_attribution,
      derivation_method: normalized.derivation_method,
      evidence_refs: normalized.evidence_refs,
      confidence: normalized.confidence,
      label: normalized.label,
      metadata: normalized.metadata,
    }
    const projectionFields = legacySemanticEdgeOptionalFields.filter((field) => normalized[field] !== undefined)
    this.causalEdge({
      edge_id: edge.edge_id,
      from: input.from,
      to: input.to,
      relation: input.relation,
      evidence_tier: input.evidence_tier,
      eligible_for_attribution: input.eligible_for_attribution,
      derivation_method: input.derivation_method,
      evidence_refs: input.evidence_refs,
      confidence: input.confidence,
      label: input.label,
      metadata: {
        ...(input.metadata ?? {}),
        [legacySemanticEdgeProjectionKey]: { fields: projectionFields },
      },
    })
    this.write("semantic.edge", edge)
    return edge
  }

  private verificationScopeContext() {
    const changedTestRefs: string[] = []
    const changedProductionRefs: string[] = []
    const changedDocsRefs: string[] = []
    let oracleChanged = false
    for (const change of this.changeRecords) {
      const ref = `change:${change.change_id}`
      const role = change.change_target_role ?? changeTargetRole(change.files)
      const riskFlags = change.change_semantics?.risk_flags ?? []
      if (role === "test_code" || role === "mixed") changedTestRefs.push(ref)
      if (role === "production_code" || role === "mixed") changedProductionRefs.push(ref)
      if (role === "docs") changedDocsRefs.push(ref)
      if (change.changed_test_oracle || riskFlags.includes("test_oracle_changed")) oracleChanged = true
    }
    const riskFlags = dedupeStrings([
      ...(changedTestRefs.length ? ["tests_modified_before_verification"] : []),
      ...(oracleChanged ? ["test_oracle_modified_before_verification"] : []),
      ...(changedTestRefs.length && changedProductionRefs.length
        ? ["production_and_tests_modified_before_verification"]
        : []),
    ])
    return {
      changedTestRefs: dedupeStrings(changedTestRefs),
      changedProductionRefs: dedupeStrings(changedProductionRefs),
      changedDocsRefs: dedupeStrings(changedDocsRefs),
      riskFlags,
    }
  }

  private verificationRecordsForRefs(refs: string[], spanID?: string) {
    const found = new Map<string, TraceVerificationRecord>()
    const seen = new Set<string>()
    const visit = (ref: string, depth: number) => {
      if (!ref || depth > 4 || seen.has(ref)) return
      seen.add(ref)
      if (ref.startsWith("verification:")) {
        const verificationID = ref.slice("verification:".length)
        const verification = this.verificationRecords.find((item) => item.verification_id === verificationID)
        if (verification) found.set(verification.verification_id, verification)
        return
      }
      const node = this.sourceNodeForRef(ref)
      if (!node) return
      for (const verificationRef of stringArrayField(node.data ?? {}, ["verification_refs", "verificationRefs"]) ?? []) {
        visit(verificationRef, depth + 1)
      }
      for (const sourceRef of node.source_refs ?? []) visit(sourceRef, depth + 1)
    }
    for (const ref of refs) visit(ref, 0)
    if (!found.size && spanID) {
      for (const verification of this.verificationRecords) {
        if (verification.span_id === spanID) found.set(verification.verification_id, verification)
      }
    }
    return [...found.values()]
  }

  private verificationFactProvenance(refs: string[], spanID?: string): VerificationFactProvenance | undefined {
    const records = this.verificationRecordsForRefs(refs, spanID)
    if (!records.length) return undefined
    const primary = records.findLast((item) => item.span_id && item.span_id === spanID) ?? records.at(-1)!
    const temporalRole =
      primary.effective_for_final_state === true && primary.repository_revision === this.repositoryRevision
        ? "current_effective"
        : primary.effective_for_final_state === false || Boolean(primary.superseded_by_refs?.length)
          ? "superseded"
          : "unknown"
    return {
      verification_refs: records.map((item) => `verification:${item.verification_id}`),
      verification_repository_revision: primary.repository_revision,
      verification_phase: primary.verification_phase,
      verification_status: primary.status,
      verification_effective_for_final_state: primary.effective_for_final_state,
      verification_temporal_role: temporalRole,
      verification_supersedes_refs: primary.supersedes_refs,
      verification_superseded_by_refs: primary.superseded_by_refs,
    }
  }

  private syncVerificationNode(verification: TraceVerificationRecord) {
    const node = this.causalNodes.find((item) => item.node_id === `vernode_${verification.verification_id}`)
    if (!node) return
    node.data = {
      ...(node.data ?? {}),
      repository_revision: verification.repository_revision,
      verification_phase: verification.verification_phase,
      effective_for_final_state: verification.effective_for_final_state,
      supersedes_refs: verification.supersedes_refs,
      superseded_by_refs: verification.superseded_by_refs,
    }
    node.artifact_refs = this.collectArtifactRefs(node.data)
    this.causalIR.updateNode(node)
    const verificationRef = `verification:${verification.verification_id}`
    for (const derived of this.causalNodes) {
      if (derived.node_id === node.node_id) continue
      const provenance = this.verificationFactProvenance(derived.source_refs ?? [], derived.span_id)
      if (!provenance?.verification_refs.includes(verificationRef)) continue
      derived.data = {
        ...(derived.data ?? {}),
        ...provenance,
      }
      derived.artifact_refs = this.collectArtifactRefs(derived.data)
      this.causalIR.updateNode(derived)
    }
  }

  verification(input: VerificationRecordInput) {
    const parsed = input.parsed_failures ?? parseVerificationFailures({ stdout: input.stdout, stderr: input.stderr })
    const exitCode = optionalNumber(input.exit_code)
    const commandOutcomes = parsedCommandOutcomes(input.stdout, input.stderr)
    const statusInference = inferredVerificationStatus({
      explicitStatus: input.status,
      exitCode,
      parsedFailures: parsed,
      command: input.command,
    })
    const verificationScope = this.verificationScopeContext()
    const verificationPhase =
      this.repositoryRevision === 0
        ? "baseline"
        : verificationScope.changedTestRefs.length
          ? "post_test_change"
          : "post_change"
    const superseded = this.verificationRecords.filter((item) => item.command?.trim() === input.command?.trim())
    const finalTestResult = finalTestResultSemantics({
      command: input.command,
      purpose: input.purpose,
      exitCode,
      status: statusInference.status,
      parsedFailures: parsed,
    })
    const verificationAttempt = verificationAttemptSemantics(input.command, input.purpose)
    const coverageSemantics = verificationCoverageSemantics({
      command: input.command,
      changedTestRefs: verificationScope.changedTestRefs,
      changedProductionRefs: verificationScope.changedProductionRefs,
      changedDocsRefs: verificationScope.changedDocsRefs,
      riskFlags: verificationScope.riskFlags,
    })
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
      repository_revision: this.repositoryRevision,
      verification_phase: verificationPhase,
      effective_for_final_state: true,
      supersedes_refs: superseded.map((item) => `verification:${item.verification_id}`),
      exit_code: exitCode,
      process_exit_code: exitCode,
      parsed_command_outcomes: commandOutcomes,
      exit_masked_by_shell:
        exitCode === 0 && (commandOutcomes.some((outcome) => outcome.status === "failed") || Boolean(parsed.length)),
      status: statusInference.status,
      parsed_failures: parsed,
      stdout: input.stdout === undefined ? undefined : this.summarizeText(input.stdout, "verification.stdout"),
      stderr: input.stderr === undefined ? undefined : this.summarizeText(input.stderr, "verification.stderr"),
      quality_flags: dedupeStrings([
        ...(input.quality_flags ?? []),
        ...statusInference.quality_flags,
        ...verificationScope.riskFlags,
        ...(verificationAttempt ? ["handwritten_self_test"] : []),
      ]),
      changed_test_refs: verificationScope.changedTestRefs,
      changed_production_refs: verificationScope.changedProductionRefs,
      changed_docs_refs: verificationScope.changedDocsRefs,
      verification_scope_risk_flags: verificationScope.riskFlags,
      final_test_result: finalTestResult,
      coverage_semantics: coverageSemantics,
      verification_attempt: verificationAttempt,
      metadata: input.metadata,
    }
    this.verificationRecords.push(verification)
    for (const previous of superseded) {
      previous.effective_for_final_state = false
      previous.superseded_by_refs = dedupeStrings([
        ...(previous.superseded_by_refs ?? []),
        `verification:${verification.verification_id}`,
      ])
      this.syncVerificationNode(previous)
    }
    this.remember(this.recentVerificationIDs, verification.verification_id)
    this.write("semantic.verification", verification)
    const linkedChangeRefs = dedupeStrings([
      ...verificationScope.changedTestRefs,
      ...verificationScope.changedProductionRefs,
      ...verificationScope.changedDocsRefs,
      ...(this.recentChangeID ? [`change:${this.recentChangeID}`] : []),
    ])
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
        repository_revision: verification.repository_revision,
        verification_phase: verification.verification_phase,
        effective_for_final_state: verification.effective_for_final_state,
        supersedes_refs: verification.supersedes_refs,
        superseded_by_refs: verification.superseded_by_refs,
        exit_code: verification.exit_code,
        process_exit_code: verification.process_exit_code,
        parsed_command_outcomes: verification.parsed_command_outcomes,
        exit_masked_by_shell: verification.exit_masked_by_shell,
        quality_flags: verification.quality_flags,
        changed_test_refs: verification.changed_test_refs,
        changed_production_refs: verification.changed_production_refs,
        changed_docs_refs: verification.changed_docs_refs,
        verification_scope_risk_flags: verification.verification_scope_risk_flags,
        final_test_result: verification.final_test_result,
        coverage_semantics: verification.coverage_semantics,
        verification_attempt: verification.verification_attempt,
        parsed_failures: verification.parsed_failures,
        stdout: input.stdout,
        stderr: input.stderr,
      },
      source_locations: sourceLocations,
      source_refs: dedupeStrings([
        ...(input.tool_call_id ? [`tool_call:${input.tool_call_id}`] : []),
        ...linkedChangeRefs,
        ...sourceLocations
          .map((location) => location.uri ?? location.path)
          .filter((item): item is string => Boolean(item)),
      ]),
    })
    if (verification.status === "failed") this.recentFailedVerificationID = verification.verification_id
    for (const ref of linkedChangeRefs) {
      const parsedRef = this.parseSourceRef(ref)
      if (!parsedRef?.id) continue
      this.edge({
        from: { type: "change", id: parsedRef.id },
        to: { type: "verification", id: verification.verification_id },
        relation: "change_to_verification",
        label: "Verification ran after repository change",
      })
    }
    return verification
  }

  change(input: ChangeRecordInput) {
    const revisionBefore = this.repositoryRevision
    const revisionAfter = revisionBefore + 1
    const explicitSourceRefs = input.source_refs ?? input.evidence_refs ?? []
    const actionSourceRefs = dedupeStrings([
      ...(input.tool_call_id ? [`tool_call:${input.tool_call_id}`] : []),
      ...(input.span_id ? [`span:${input.span_id}`] : []),
    ])
    const motivatingEvidenceRefs = this.recentFailedVerificationID
      ? [`verification:${this.recentFailedVerificationID}`]
      : []
    const collectedSourceRefs = dedupeStrings([...actionSourceRefs, ...explicitSourceRefs])
    const sourceRefs = collectedSourceRefs.length ? collectedSourceRefs : undefined
    const collectedSourceRefRelations = [
      ...actionSourceRefs.map(
        (sourceRef): TraceSourceRefRelation => ({
          source_ref: sourceRef,
          relation: sourceRef.startsWith("tool_call:") ? "materialized_by_action" : "executed_in_span",
          inference: "runtime_identity",
          confidence: 1,
        }),
      ),
      ...explicitSourceRefs.map(
        (sourceRef): TraceSourceRefRelation => ({
          source_ref: sourceRef,
          relation: "explicit_provenance",
          inference: "explicit",
          confidence: 1,
        }),
      ),
      ...motivatingEvidenceRefs.map(
        (sourceRef): TraceSourceRefRelation => ({
          source_ref: sourceRef,
          relation: "motivated_by_evidence",
          inference: "recent_failed_verification",
          confidence: 0.8,
        }),
      ),
    ].filter(
      (item, index, items) =>
        items.findIndex(
          (candidate) => candidate.source_ref === item.source_ref && candidate.relation === item.relation,
        ) === index,
    )
    const sourceRefRelations = collectedSourceRefRelations.length ? collectedSourceRefRelations : undefined
    const targetRole = input.change_target_role ?? changeTargetRole(input.files)
    const baseChangeSemantics = input.change_semantics ?? changeSemanticsFromDiff(input.diff)
    const changedTestOracle =
      input.changed_test_oracle ?? isTestOracleChange(input.diff, targetRole, baseChangeSemantics)
    const changeSemantics = enrichChangeSemanticsForTarget({
      semantics: baseChangeSemantics,
      diff: input.diff,
      role: targetRole,
      changedTestOracle,
    })
    const diffSemantics = diffSemanticsFromChangeSemantics(changeSemantics)
    const qualityFlags = dedupeStrings([
      ...(input.quality_flags ?? []),
      ...(changedTestOracle ? ["test_oracle_changed"] : []),
      ...(input.diff === undefined ? ["missing_diff"] : []),
      ...(input.diff !== undefined && !changeSemantics ? ["change_semantics_unavailable"] : []),
    ])
    const change: TraceChangeRecord = {
      change_id: input.change_id ?? semanticID("chg", this.changeRecords.length + 1),
      span_id: input.span_id,
      tool_call_id: input.tool_call_id,
      files: input.files,
      revision_before: revisionBefore,
      revision_after: revisionAfter,
      change_target_role: targetRole,
      changed_test_oracle: changedTestOracle,
      intent: input.intent,
      diff: input.diff === undefined ? undefined : this.summarizeText(input.diff, "change.diff"),
      change_semantics: changeSemantics,
      diff_semantics: diffSemantics,
      quality_flags: qualityFlags,
      source_refs: sourceRefs,
      source_ref_relations: sourceRefRelations,
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
        revision_before: revisionBefore,
        revision_after: revisionAfter,
        change_target_role: targetRole,
        changed_test_oracle: changedTestOracle,
        intent: input.intent,
        diff: input.diff,
        change_semantics: changeSemantics,
        diff_semantics: diffSemantics,
        quality_flags: qualityFlags,
        source_refs: sourceRefs,
        source_ref_relations: sourceRefRelations,
        verification_refs: input.verification_refs,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      source_locations: collectSourceLocations(input.files),
      temporal_advisory_refs: motivatingEvidenceRefs,
    })
    this.repositoryRevision = revisionAfter
    for (const verification of this.verificationRecords) {
      if (verification.effective_for_final_state === false) continue
      verification.effective_for_final_state = false
      this.syncVerificationNode(verification)
    }
    this.recentChangeID = change.change_id
    if (this.recentFailedVerificationID) {
      this.edge({
        from: { type: "verification", id: this.recentFailedVerificationID },
        to: { type: "change", id: change.change_id },
        relation: "motivated_by_evidence",
        label: "Failed verification may have motivated the change; it does not carry the change defect",
        metadata: {
          causal_semantics: "motivation_not_defect_propagation",
          inference: "recent_failed_verification",
          confidence: 0.8,
        },
        evidence_tier: "temporal_advisory",
        eligible_for_attribution: false,
        derivation_method: "recent_source_fallback",
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

  private narrowResponseRecordSourceRefs(sourceRefs: string[], classifiedRefs: ReturnType<typeof classifySourceRefs>) {
    if (!sourceRefs.length) return sourceRefs
    const directRefs = classifiedRefs.direct_evidence_refs.slice(-6)
    const strongExecutionRefs = classifiedRefs.execution_refs
      .filter((ref) => /^(verification|change|tool_span|tool_call):/.test(ref))
      .slice(-3)
    const fallbackRefs = dedupeStrings([
      ...classifiedRefs.execution_refs.slice(-4),
      ...classifiedRefs.context_refs.slice(-3),
      ...sourceRefs.filter(isToolOutcomeRef),
    ])
    const narrowed = directRefs.length ? dedupeStrings([...directRefs, ...strongExecutionRefs]) : fallbackRefs
    return narrowed.length ? narrowed.slice(0, 8) : sourceRefs.slice(-8)
  }

  private currentGenerationProvenance(scope?: Record<string, unknown>): GenerationProvenance {
    const sessionID = firstStringField(scope ?? {}, ["session_id", "sessionID"])
    const messageID = firstStringField(scope ?? {}, ["message_id", "messageID"])
    const nodeScopeValue = (node: CausalNode, fields: string[]) =>
      firstStringField(node.data ?? {}, fields) ??
      firstStringField(recordFromUnknown(node.data?.input) ?? {}, fields) ??
      firstStringField(node.metadata ?? {}, fields)
    const nodePosition = (node: CausalNode) =>
      this.causalNodes.findIndex((candidate) => candidate.node_id === node.node_id)
    const exactContextAnchor = [...this.recentContextNodeIDs]
      .reverse()
      .map((id) => this.causalNodes.find((candidate) => candidate.node_id === id))
      .find((node) => {
        if (!node || !messageID) return false
        if (sessionID && nodeScopeValue(node, ["session_id", "sessionID"]) !== sessionID) return false
        return nodeScopeValue(node, ["message_id", "messageID"]) === messageID
      })
    const anchorPosition = exactContextAnchor ? nodePosition(exactContextAnchor) : undefined
    const scopedNodeIDs = (nodeIDs: string[], limit: number, matchMessage: boolean) =>
      nodeIDs
        .filter((id) => {
          const node = this.causalNodes.find((candidate) => candidate.node_id === id)
          if (!node) return false
          if (sessionID && nodeScopeValue(node, ["session_id", "sessionID"]) !== sessionID) return false
          if (matchMessage && messageID) {
            const nodeMessageID = nodeScopeValue(node, ["message_id", "messageID"])
            if (nodeMessageID) return nodeMessageID === messageID
            return anchorPosition !== undefined && nodePosition(node) >= anchorPosition
          }
          return true
        })
        .slice(-limit)
    const promptNodeIDs = scopedNodeIDs(this.recentPromptNodeIDs, 2, false)
    const contextNodeIDs = scopedNodeIDs(this.recentContextNodeIDs, 4, true)
    const llmNodeIDs = scopedNodeIDs(this.recentLLMNodeIDs, 2, true)
    const nodeIDs = dedupeStrings([...promptNodeIDs, ...contextNodeIDs, ...llmNodeIDs])
    const refs = nodeIDs.map((id) => `node:${id}`)
    const promptRefs = promptNodeIDs.map((id) => `node:${id}`)
    const contextRefs = contextNodeIDs.map((id) => `node:${id}`)
    const llmRefs = llmNodeIDs.map((id) => `node:${id}`)
    const selectedLLMSpanIDs = new Set(
      llmNodeIDs
        .map((id) => this.causalNodes.find((node) => node.node_id === id)?.span_id)
        .filter((id): id is string => Boolean(id)),
    )
    const contextSnapshotRefs = this.recentContextSnapshotIDs
      .map((snapshotID) => ({
        snapshotID,
        node: this.causalNodes.find((node) => node.node_id === `ctxnode_${snapshotID}`),
      }))
      .filter(({ node }) => {
        if (!node) return false
        if (node.span_id && selectedLLMSpanIDs.has(node.span_id)) return true
        if (sessionID && nodeScopeValue(node, ["session_id", "sessionID"]) !== sessionID) return false
        if (messageID && nodeScopeValue(node, ["message_id", "messageID"]) !== messageID) return false
        return Boolean(sessionID || messageID)
      })
      .slice(-2)
      .map(({ snapshotID }) => `context_snapshot:${snapshotID}`)
    const contextNodes = contextNodeIDs
      .map((id) => this.causalNodes.find((node) => node.node_id === id))
      .filter((node): node is CausalNode => Boolean(node))
    const messageTransforms = contextNodes.map((node) => ({
      node_ref: `node:${node.node_id}`,
      event_type: node.kind,
      stage: stringField(node.data ?? {}, ["stage"]) ?? node.title,
      transforms: node.data?.transforms,
    }))
    const inputMessages = contextNodes
      .map((node) => {
        const output = recordFromUnknown(node.data?.output)
        return output?.model_messages ?? output?.messages ?? node.data?.messages
      })
      .find((value) => value !== undefined)
    const selectedContextRefs = dedupeStrings([...contextRefs, ...contextSnapshotRefs])
    return {
      refs,
      promptRefs,
      contextRefs,
      llmRefs,
      messageTransforms,
      inputMessages,
      selectedContextRefs,
    }
  }

  private generationGroundingCandidates(
    provenance: GenerationProvenance,
  ): GenerationGroundingCandidates {
    const contextNodes = provenance.contextRefs
      .map((ref) => this.sourceNodeForRef(ref))
      .filter((node): node is CausalNode => Boolean(node))
    const selectedToolNodeRefs = dedupeStrings(
      contextNodes.flatMap((node) => {
        const inferredRefs = stringArrayField(node.data ?? {}, [
          "inferred_tool_context_refs",
          "inferredToolContextRefs",
        ])
        const contextSetRef = firstStringField(node.data ?? {}, [
          "inferred_tool_context_set_ref",
          "inferredToolContextSetRef",
        ])
        const contextSet = contextSetRef ? this.sourceNodeForRef(contextSetRef) : undefined
        const memberRefs = stringArrayField(contextSet?.data ?? {}, ["member_refs", "memberRefs"])
        return [...(inferredRefs ?? []), ...(memberRefs ?? [])]
      }),
    )
    const canonicalToolOutcomeRef = (ref: string) => {
      const node = this.sourceNodeForRef(ref)
      const callID = firstStringField(node?.data ?? {}, ["call_id", "callID"])
      if (!node || !callID) return undefined
      if (node.kind === "tool.error") return `tool_error:${callID}`
      if (node.kind === "tool.result") return `tool_result:${callID}`
      return undefined
    }
    const toolOutcomeRefs = dedupeStrings([
      ...selectedToolNodeRefs
        .flatMap((ref) => this.toolOutcomeRefsFromSourceRefs([ref]))
        .flatMap((ref) => {
          if (isToolOutcomeRef(ref)) return [ref]
          const canonical = canonicalToolOutcomeRef(ref)
          return canonical ? [canonical] : []
        }),
      ...selectedToolNodeRefs.flatMap((ref) => {
        const canonical = canonicalToolOutcomeRef(ref)
        return canonical ? [canonical] : []
      }),
    ])
    const selectedIdentities = new Set([...selectedToolNodeRefs, ...toolOutcomeRefs])
    const isSelectedGenerationEvidence = (node: CausalNode) => {
      const nodeRef = `node:${node.node_id}`
      const outcomeRefs = dedupeStrings([
        ...(node.source_refs ?? []).filter(isToolOutcomeRef),
        ...this.toolOutcomeRefsFromSourceRefs([nodeRef]),
      ])
      return outcomeRefs.some((ref) => selectedIdentities.has(ref))
    }
    const candidateRefs = this.causalNodes.flatMap((node) => {
      if (!isSelectedGenerationEvidence(node)) return []
      if (node.kind === "evidence.semantic_fact") return [`evidence:${node.node_id}`]
      if (node.kind === "verification") {
        const verificationID = firstStringField(node.data ?? {}, ["verification_id", "verificationID"])
        return verificationID ? [`verification:${verificationID}`] : []
      }
      if (node.kind === "change") {
        const changeID = firstStringField(node.data ?? {}, ["change_id", "changeID"])
        return changeID ? [`change:${changeID}`] : []
      }
      return []
    })
    return {
      candidateRefs: dedupeStrings([...candidateRefs, ...toolOutcomeRefs]),
      toolOutcomeRefs,
    }
  }

  private linkGenerationProvenanceToDecision(decisionNode: CausalNode, provenance: GenerationProvenance) {
    if (!provenance.refs.length) return
    const target = { type: "node", id: decisionNode.node_id, label: "decision" }
    const link = (ref: string, relation: string, label: string) => {
      const parsed = this.parseSourceRef(ref)
      if (!parsed || this.hasCausalEdge(parsed, decisionNode.node_id, relation)) return
      this.causalEdge({
        from: parsed,
        to: target,
        relation,
        evidence_tier: "confirmed",
        eligible_for_attribution: true,
        derivation_method: "same_generation_lifecycle",
        evidence_refs: [ref],
        label,
      })
    }
    for (const ref of provenance.promptRefs) link(ref, "prompted", "Prompt assembly contributed to model decision")
    for (const ref of provenance.contextRefs)
      link(ref, "used_as_context", "Context/message transform contributed to model decision")
    for (const ref of provenance.llmRefs) link(ref, "produced", "LLM generation produced semantic decision")
  }

  private linkGenerationProvenanceToResponse(input: {
    responseNode: CausalNode
    responseSegmentID: string
    responseText: unknown
    provenance: GenerationProvenance
  }) {
    if (!input.provenance.refs.length) return
    const responseRef = { type: "node", id: input.responseNode.node_id, label: "response.output" }
    for (const ref of input.provenance.promptRefs) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed || this.hasCausalEdge(parsed, input.responseNode.node_id, "prompted")) continue
      this.causalEdge({
        from: { ...parsed, label: "prompt.assembly" },
        to: responseRef,
        relation: "prompted",
        label: "Prompt assembly contributed to generated response output",
      })
    }
    for (const ref of input.provenance.contextRefs) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed || this.hasCausalEdge(parsed, input.responseNode.node_id, "used_as_context")) continue
      this.causalEdge({
        from: { ...parsed, label: "context" },
        to: responseRef,
        relation: "used_as_context",
        label: "Context/message transform was selected for generated response output",
      })
    }
    for (const ref of input.provenance.llmRefs) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      if (!this.hasCausalEdge(parsed, input.responseNode.node_id, "produced")) {
        this.causalEdge({
          from: { ...parsed, label: "llm.call" },
          to: responseRef,
          relation: "produced",
          label: "LLM generation produced response output",
        })
      }
      const llmNode = this.causalNodes.find((node) => node.node_id === parsed.id)
      if (!llmNode) continue
      const currentData = llmNode.data ?? {}
      llmNode.data = {
        ...currentData,
        generation_provenance_version: "v1",
        generated_response_refs: dedupeStrings([
          ...(stringArrayField(currentData, ["generated_response_refs", "generatedResponseRefs"]) ?? []),
          `response_segment:${input.responseSegmentID}`,
        ]),
        message_transforms:
          input.provenance.messageTransforms.length > 0
            ? input.provenance.messageTransforms
            : currentData.message_transforms,
        input_messages: currentData.input_messages ?? input.provenance.inputMessages,
        selected_context_refs:
          input.provenance.selectedContextRefs.length > 0
            ? input.provenance.selectedContextRefs
            : currentData.selected_context_refs,
        compaction_provenance: currentData.compaction_provenance ?? { status: "not_observed_for_response" },
        output_text: currentData.output_text ?? this.summarizeText(input.responseText, "llm.generated_output"),
      }
      llmNode.artifact_refs = this.collectArtifactRefs(llmNode.data)
      this.causalIR.updateNode(llmNode)
    }
  }

  responseOutput(input: ResponseOutputInput) {
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const classifiedRefs = classifySourceRefs(sourceRefs)
    const responseRecordSourceRefs = this.narrowResponseRecordSourceRefs(sourceRefs, classifiedRefs)
    const generationProvenance = this.currentGenerationProvenance(input.metadata)
    const generationGrounding = this.generationGroundingCandidates(generationProvenance)
    const visibility = input.visibility ?? (input.metadata?.visibility as string | undefined) ?? "user_visible"
    const turnIndex = input.turn_index ?? optionalNumber(input.metadata?.turn_index) ?? this.responseSegments.length + 1
    const metadataResponseRole = input.metadata?.response_role as TraceResponseSegment["response_role"] | undefined
    const responseRoleExplicit = input.response_role !== undefined || metadataResponseRole !== undefined
    const metadataFinality =
      typeof input.metadata?.is_final_for_case === "boolean" ? input.metadata.is_final_for_case : undefined
    const finalityExplicit = input.is_final_for_case !== undefined || metadataFinality !== undefined
    const responseRole = input.response_role ?? metadataResponseRole ?? this.inferResponseRole(visibility)
    const isFinalForCase = input.is_final_for_case ?? metadataFinality ?? responseRole === "final_answer"
    const finalitySource = finalityExplicit || responseRoleExplicit ? "explicit" : "inferred"
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
      finality_source: finalitySource,
      direct_evidence_refs: classifiedRefs.direct_evidence_refs,
      context_refs: classifiedRefs.context_refs,
      execution_refs: classifiedRefs.execution_refs,
      generation_provenance_refs: generationProvenance.refs,
      generation_grounding_candidate_refs: generationGrounding.candidateRefs,
      generation_tool_outcome_refs: generationGrounding.toolOutcomeRefs,
      generation_context_refs: generationProvenance.contextRefs,
      generation_llm_refs: generationProvenance.llmRefs,
      message_transform_refs: generationProvenance.contextRefs,
      candidate_source_refs: sourceRefs,
    }
    const segment: TraceResponseSegment = {
      segment_id: input.segment_id ?? semanticID("segment", this.responseSegments.length + 1),
      response_artifact: input.response_artifact,
      text: this.summarizeText(input.text, "result.response.output"),
      response_role: responseRole,
      visibility,
      turn_index: turnIndex,
      is_final_for_case: isFinalForCase,
      finality_source: finalitySource,
      direct_evidence_refs: classifiedRefs.direct_evidence_refs,
      context_refs: classifiedRefs.context_refs,
      execution_refs: classifiedRefs.execution_refs,
      generation_provenance_refs: generationProvenance.refs,
      generation_grounding_candidate_refs: generationGrounding.candidateRefs,
      generation_tool_outcome_refs: generationGrounding.toolOutcomeRefs,
      source_refs: sourceRefs,
      source_locations: sourceLocations,
      metadata,
    }
    this.responseSegments.push(segment)
    this.responseSourceBySegmentID.set(segment.segment_id, input.text)
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
        finality_source: finalitySource,
        direct_evidence_refs: classifiedRefs.direct_evidence_refs,
        context_refs: classifiedRefs.context_refs,
        execution_refs: classifiedRefs.execution_refs,
        generation_provenance_refs: generationProvenance.refs,
        generation_grounding_candidate_refs: generationGrounding.candidateRefs,
        generation_tool_outcome_refs: generationGrounding.toolOutcomeRefs,
        generation_context_refs: generationProvenance.contextRefs,
        generation_llm_refs: generationProvenance.llmRefs,
        message_transform_refs: generationProvenance.contextRefs,
        candidate_source_refs: sourceRefs,
        source_locations: sourceLocations,
        metadata,
      },
      source_refs: responseRecordSourceRefs,
      source_locations: sourceLocations,
      temporal_advisory_refs: this.temporalSourceRefs(sourceRefs),
    })
    this.linkGenerationProvenanceToResponse({
      responseNode: record,
      responseSegmentID: segment.segment_id,
      responseText: input.text,
      provenance: generationProvenance,
    })
    for (const ref of classifiedRefs.direct_evidence_refs) {
      this.linkSourceToResponse(ref, record.node_id)
    }
    return segment
  }

  private evidenceConflictInfo(refs: string[]) {
    const directEvidenceRefs = refs.filter((ref) => ref.startsWith("evidence:"))
    const conflictRefs: string[] = []
    const legacyEvidenceRefs: string[] = []
    for (const ref of directEvidenceRefs) {
      const node = this.evidenceNodeForRef(ref)
      const data = node?.data
      if (!data) continue
      if (data.applicability_status === "legacy") legacyEvidenceRefs.push(ref)
      conflictRefs.push(...(stringArrayField(data, ["conflict_refs", "conflictRefs"]) ?? []))
    }
    const conflictingEvidenceRefs = dedupeStrings(conflictRefs.filter((ref) => !directEvidenceRefs.includes(ref)))
    return {
      conflictingEvidenceRefs,
      legacyEvidenceRefs: dedupeStrings(legacyEvidenceRefs),
      supportConflictStatus: conflictingEvidenceRefs.length ? "conflicted" : "unconflicted",
    }
  }

  private verificationAfterTestChangeRefsForRefs(refs: string[]) {
    const output: string[] = []
    for (const ref of refs) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed || parsed.type !== "verification") continue
      const node = this.causalNodes.find(
        (item) => item.kind === "verification" && item.data?.verification_id === parsed.id,
      )
      const riskFlags = stringArrayField(node?.data ?? {}, [
        "verification_scope_risk_flags",
        "verificationScopeRiskFlags",
      ])
      if (riskFlags?.includes("test_oracle_modified_before_verification")) output.push(ref)
      else if (riskFlags?.includes("tests_modified_before_verification")) output.push(ref)
    }
    return dedupeStrings(output)
  }

  private changeScopeExclusionEvidenceRefsForClaim(claimText: unknown, sourceRefs: string[]) {
    const targets = unchangedPathTargetsFromClaim(claimText)
    if (!targets.length) return []
    const changeRefs = dedupeStrings(sourceRefs.filter((ref) => ref.startsWith("change:")))
    if (!changeRefs.length) return []
    const changeRefSet = new Set(changeRefs)
    const changes = this.changeRecords.filter((change) => changeRefSet.has(`change:${change.change_id}`))
    if (!changes.length) return []
    const changedFiles = dedupeStrings(changes.flatMap((change) => change.files ?? []))
    const evidenceRefs: string[] = []
    for (const target of targets) {
      if (changedFiles.some((file) => pathWithinScope(file, target))) continue
      const fact = this.evidenceFact({
        source: "trace",
        category: "change_scope_exclusion",
        summary: `No recorded changes under ${target}`,
        data: {
          subject: "repository_change_scope",
          predicate: "change_scope_excludes",
          value: target,
          path: target,
          target_path: target,
          changed_files: changedFiles,
        },
        source_refs: changeRefs,
        confidence: "derived",
        support_level: "inferred",
        quality_flags: ["absence_evidence", "derived_from_change_scope"],
      })
      if (fact) evidenceRefs.push(`evidence:${fact.node_id}`)
    }
    return dedupeStrings(evidenceRefs)
  }

  responseClaim(legacyInput: ResponseClaimInput) {
    const input = normalizeLegacyResponseClaimAtomizationFacts(legacyInput)
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const classifiedRefs = classifySourceRefs(sourceRefs)
    const generationProvenanceRefs = dedupeStrings([
      ...(input.generation_provenance_refs ?? []),
      ...sourceRefs.filter((ref) => ref.startsWith("node:")),
    ])
    const claimText = input.canonical_text ?? input.text
    const changeScopeExclusionRefs = this.changeScopeExclusionEvidenceRefsForClaim(claimText, sourceRefs)
    const candidateToolOutcomeRefs = dedupeStrings([
      ...this.toolOutcomeRefsFromSourceRefs(sourceRefs),
      ...this.recentToolFailureRefs,
    ])
    const recentToolFailureRefs = this.recentToolFailureRefs.filter(
      (ref) => !this.toolOutcomeRefsFromSourceRefs(sourceRefs).includes(ref),
    )
    const candidateClassifiedRefs = {
      ...classifiedRefs,
      direct_evidence_refs: dedupeStrings([
        ...classifiedRefs.direct_evidence_refs,
        ...this.toolOutcomeRefsFromSourceRefs(sourceRefs),
        ...changeScopeExclusionRefs,
        ...classifySourceRefs(input.generation_grounding_candidate_refs).direct_evidence_refs,
      ]),
    }
    const generationGroundingCandidateRefs = dedupeStrings(input.generation_grounding_candidate_refs ?? [])
    const groundingCandidateRefs = dedupeStrings([
      ...candidateClassifiedRefs.direct_evidence_refs,
      ...classifiedRefs.execution_refs.filter(
        (ref) => ref.startsWith("change:") || ref.startsWith("verification:"),
      ),
      ...generationGroundingCandidateRefs,
    ])
    const evidenceMatch = this.matchEvidenceForClaim(
      claimText,
      groundingCandidateRefs,
      generationGroundingCandidateRefs,
    )
    const matchedClassifiedRefs = classifySourceRefs(evidenceMatch.refs)
    const effectiveDirectEvidenceRefs = matchedClassifiedRefs.direct_evidence_refs.length
      ? matchedClassifiedRefs.direct_evidence_refs
      : generationGroundingCandidateRefs.length === 0 && candidateClassifiedRefs.direct_evidence_refs.length <= 1
        ? candidateClassifiedRefs.direct_evidence_refs
        : []
    const claimKind = responseClaimKind(claimText)
    const temporalScope = responseClaimTemporalScope(claimText, this.repositoryRevision)
    const verificationSourceRefs = dedupeStrings([
      ...classifiedRefs.execution_refs,
      ...generationGroundingCandidateRefs,
    ]).filter((ref) => ref.startsWith("verification:"))
    const effectiveVerificationRefs = verificationSourceRefs.filter((ref) => {
      const verificationID = ref.slice("verification:".length)
      const verification = this.verificationRecords.find((item) => item.verification_id === verificationID)
      return (
        verification?.effective_for_final_state === true && verification.repository_revision === this.repositoryRevision
      )
    })
    const temporallyInapplicableEvidenceRefs = evidenceMatch.decisions
      .filter(
        (item) =>
          item.rejection_reason === "superseded_verification" ||
          item.rejection_reason === "verification_revision_mismatch",
      )
      .map((item) => item.candidate_ref)
    const supersededEvidenceRefs = dedupeStrings([
      ...verificationSourceRefs.filter((ref) => !effectiveVerificationRefs.includes(ref)),
      ...temporallyInapplicableEvidenceRefs,
    ])
    const matchedExecutionSupportRefs = evidenceMatch.refs.filter((ref) => {
      if (ref.startsWith("verification:")) return effectiveVerificationRefs.includes(ref)
      if (!ref.startsWith("change:")) return false
      const changeID = ref.slice("change:".length)
      const change = this.changeRecords.find((item) => item.change_id === changeID)
      return change?.revision_after === this.repositoryRevision
    })
    const directSupportRefs = dedupeStrings([
      ...effectiveDirectEvidenceRefs,
      ...matchedExecutionSupportRefs,
      ...(claimKind === "verification" && temporalScope !== "historical" && temporalScope !== "future"
        ? effectiveVerificationRefs
        : []),
    ])
    const weakEvidenceMatch =
      evidenceMatch.weak && !(claimKind === "verification" && effectiveVerificationRefs.length > 0)
    const groundingDecisions: ClaimGroundingDecision[] = evidenceMatch.decisions.map((item) =>
      directSupportRefs.includes(item.candidate_ref)
          ? {
            ...item,
            decision: "selected_direct_support",
            rejection_reason: undefined,
            attribution_eligible: true,
          }
        : {
            ...item,
            attribution_eligible: false,
          },
    )
    const legacyContextRefs = sourceRefs.filter((ref) => !effectiveDirectEvidenceRefs.includes(ref))
    const effectiveClassifiedRefs = {
      ...candidateClassifiedRefs,
      direct_evidence_refs: effectiveDirectEvidenceRefs,
    }
    const conflictInfo = this.evidenceConflictInfo(effectiveDirectEvidenceRefs)
    const verificationAfterTestChangeRefs = this.verificationAfterTestChangeRefsForRefs([
      ...effectiveDirectEvidenceRefs,
      ...classifiedRefs.execution_refs,
    ])
    const sourceLocations = dedupeSourceLocations([
      ...(input.source_locations ?? []),
      ...collectSourceLocations(input.text),
      ...collectSourceLocations(input.metadata),
    ])
    const supportLevel =
      input.support_level ?? (directSupportRefs.length ? "direct" : responseClaimSupportLevel(effectiveClassifiedRefs))
    const attributionSourceRefs = directSupportRefs.length
      ? directSupportRefs
      : dedupeStrings([...classifiedRefs.execution_refs.slice(0, 3), ...classifiedRefs.context_refs.slice(0, 3)])
    const dependencyToolOutcomeRefs = dedupeStrings([
      ...effectiveDirectEvidenceRefs.filter(isToolOutcomeRef),
      ...this.toolOutcomeRefsFromSourceRefs(effectiveDirectEvidenceRefs),
    ])
    const attributionSummary = {
      direct_evidence_count: effectiveDirectEvidenceRefs.length,
      matched_evidence_count: evidenceMatch.refs.length,
      candidate_evidence_count: evidenceMatch.candidateRefs.length,
      grounding_candidate_count: groundingCandidateRefs.length,
      grounding_selected_count: directSupportRefs.length,
      grounding_rejected_count: groundingDecisions.filter((item) => item.decision !== "selected_direct_support")
        .length,
      context_ref_count: classifiedRefs.context_refs.length,
      execution_ref_count: classifiedRefs.execution_refs.length,
      legacy_context_count: legacyContextRefs.length,
      candidate_tool_outcome_count: candidateToolOutcomeRefs.length,
      dependency_tool_outcome_count: dependencyToolOutcomeRefs.length,
      derived_tool_outcome_count: dependencyToolOutcomeRefs.length,
      conflicting_evidence_count: conflictInfo.conflictingEvidenceRefs.length,
      verification_after_test_change_count: verificationAfterTestChangeRefs.length,
      support_level: supportLevel,
      match_strategy: evidenceMatch.strategy,
      match_score: evidenceMatch.score,
    }
    const qualityFlags = dedupeStrings([
      ...(input.quality_flags ?? []),
      ...(directSupportRefs.length ? [] : responseClaimQualityFlags(effectiveClassifiedRefs)),
      ...(isBrokenClaimFragment(input.text) ? ["broken_claim_fragment"] : []),
      ...(weakEvidenceMatch ? ["weak_evidence_match"] : []),
      ...(groundingCandidateRefs.length && !evidenceMatch.refs.length
        ? ["unmatched_direct_evidence_refs"]
        : []),
      ...(conflictInfo.conflictingEvidenceRefs.length ? ["conflicting_evidence"] : []),
      ...(conflictInfo.legacyEvidenceRefs.length ? ["legacy_evidence_used"] : []),
      ...(verificationAfterTestChangeRefs.length ? ["verification_after_test_change"] : []),
    ])
    const claim: TraceResponseClaimRecord = {
      claim_id: input.claim_id ?? semanticID("claim", this.causalNodes.length + 1),
      claim_key: input.claim_key,
      response_segment_id: input.response_segment_id,
      text: this.summarizeText(claimText, "result.response.claim"),
      claim_format: input.claim_format ?? "factual_claim",
      claim_kind: claimKind,
      temporal_scope: temporalScope,
      repository_revision: this.repositoryRevision,
      raw_text:
        input.raw_text === undefined ? undefined : this.summarizeText(input.raw_text, "result.response.claim.raw"),
      canonical_text:
        input.canonical_text === undefined
          ? undefined
          : this.summarizeText(input.canonical_text, "result.response.claim"),
      table_cells: input.table_cells,
      table_subject: input.table_subject,
      table_values: input.table_values,
      claim_group_id: input.claim_group_id,
      claim_index: input.claim_index,
      claim_count: input.claim_count,
      source_byte_range: input.source_byte_range,
      previous_claim_key: input.previous_claim_key,
      next_claim_key: input.next_claim_key,
      previous_claim_ref: input.previous_claim_ref,
      next_claim_ref: input.next_claim_ref,
      atomization_status: input.atomization_status,
      atomization_reason: input.atomization_reason,
      direct_evidence_refs: effectiveDirectEvidenceRefs,
      direct_support_refs: directSupportRefs,
      candidate_context_refs: classifiedRefs.context_refs,
      superseded_evidence_refs: supersededEvidenceRefs,
      context_refs: classifiedRefs.context_refs,
      execution_refs: classifiedRefs.execution_refs,
      generation_provenance_refs: generationProvenanceRefs,
      legacy_context_refs: legacyContextRefs,
      matched_evidence_refs: evidenceMatch.refs,
      candidate_evidence_refs: evidenceMatch.candidateRefs,
      grounding_candidate_refs: groundingCandidateRefs,
      grounding_decisions: groundingDecisions,
      grounding_method: "confirmed_context_semantic_match_v1",
      grounding_behavior_impact: "none",
      match_strategy: evidenceMatch.strategy,
      match_score: evidenceMatch.score,
      match_reasons: evidenceMatch.reasons,
      original_direct_evidence_refs: classifiedRefs.direct_evidence_refs,
      derived_tool_outcome_refs: dependencyToolOutcomeRefs,
      candidate_tool_outcome_refs: candidateToolOutcomeRefs,
      dependency_tool_outcome_refs: dependencyToolOutcomeRefs,
      attribution_summary: attributionSummary,
      conflicting_evidence_refs: conflictInfo.conflictingEvidenceRefs,
      support_conflict_status: conflictInfo.supportConflictStatus,
      verification_after_test_change_refs: verificationAfterTestChangeRefs,
      source_refs: attributionSourceRefs,
      source_locations: sourceLocations,
      support_level: supportLevel,
      quality_flags: qualityFlags,
      metadata: omitUndefined({
        ...(input.metadata ?? {}),
        claim_group_id: input.claim_group_id,
        claim_count: input.claim_count,
        source_byte_range: input.source_byte_range,
        previous_claim_ref: input.previous_claim_ref,
        next_claim_ref: input.next_claim_ref,
        atomization_status: input.atomization_status,
        atomization_reason: input.atomization_reason,
        original_direct_evidence_refs: classifiedRefs.direct_evidence_refs,
        derived_tool_outcome_refs: dependencyToolOutcomeRefs,
        candidate_tool_outcome_refs: candidateToolOutcomeRefs,
        dependency_tool_outcome_refs: dependencyToolOutcomeRefs,
        generation_provenance_refs: generationProvenanceRefs,
      }),
    }
    this.write("semantic.response_claim", claim)
    const responseNodeID =
      input.metadata && typeof input.metadata.response_node_id === "string"
        ? input.metadata.response_node_id
        : undefined
    const derivationInputRefs = this.canonicalDerivationInputRefs([
      ...(responseNodeID ? [`node:${responseNodeID}`] : []),
      ...(claim.response_segment_id ? [`response_segment:${claim.response_segment_id}`] : []),
      ...attributionSourceRefs,
    ])
    if (!derivationInputRefs.length) derivationInputRefs.push(`external:response_claim_input:${claim.claim_id}`)
    const derivedAt = nowIso()
    const node = this.node({
      node_id: `responseclaim_${claim.claim_id}`,
      kind: "response.claim",
      component: "result",
      title: `Response claim ${claim.claim_index}`,
      status: "success",
      origin: "deterministic_derived",
      input_refs: derivationInputRefs,
      derivation: deterministicDerivation("response_claim_extraction", derivationInputRefs, derivedAt),
      data: {
        claim_id: claim.claim_id,
        response_segment_id: claim.response_segment_id,
        text: claimText,
        claim_format: claim.claim_format,
        claim_kind: claim.claim_kind,
        temporal_scope: claim.temporal_scope,
        repository_revision: claim.repository_revision,
        raw_text: input.raw_text,
        canonical_text: input.canonical_text,
        table_cells: claim.table_cells,
        table_subject: claim.table_subject,
        table_values: claim.table_values,
        claim_group_id: claim.claim_group_id,
        claim_index: claim.claim_index,
        claim_count: claim.claim_count,
        source_byte_range: claim.source_byte_range,
        previous_claim_key: claim.previous_claim_key,
        next_claim_key: claim.next_claim_key,
        previous_claim_ref: claim.previous_claim_ref,
        next_claim_ref: claim.next_claim_ref,
        atomization_status: claim.atomization_status,
        atomization_reason: claim.atomization_reason,
        direct_evidence_refs: claim.direct_evidence_refs,
        direct_support_refs: claim.direct_support_refs,
        candidate_context_refs: claim.candidate_context_refs,
        superseded_evidence_refs: claim.superseded_evidence_refs,
        context_refs: claim.context_refs,
        execution_refs: claim.execution_refs,
        generation_provenance_refs: claim.generation_provenance_refs,
        legacy_context_refs: claim.legacy_context_refs,
        matched_evidence_refs: claim.matched_evidence_refs,
        candidate_evidence_refs: claim.candidate_evidence_refs,
        grounding_candidate_refs: claim.grounding_candidate_refs,
        grounding_decisions: claim.grounding_decisions,
        grounding_method: claim.grounding_method,
        grounding_behavior_impact: claim.grounding_behavior_impact,
        match_strategy: claim.match_strategy,
        match_score: claim.match_score,
        match_reasons: claim.match_reasons,
        original_direct_evidence_refs: claim.original_direct_evidence_refs,
        derived_tool_outcome_refs: claim.derived_tool_outcome_refs,
        candidate_tool_outcome_refs: claim.candidate_tool_outcome_refs,
        dependency_tool_outcome_refs: claim.dependency_tool_outcome_refs,
        attribution_summary: claim.attribution_summary,
        conflicting_evidence_refs: claim.conflicting_evidence_refs,
        support_conflict_status: claim.support_conflict_status,
        verification_after_test_change_refs: claim.verification_after_test_change_refs,
        support_level: claim.support_level,
        quality_flags: claim.quality_flags,
        source_locations: sourceLocations,
        metadata: claim.metadata,
      },
      source_refs: attributionSourceRefs,
      source_locations: sourceLocations,
      metadata: claim.metadata,
      temporal_advisory_refs: dedupeStrings([...this.temporalSourceRefs(sourceRefs), ...recentToolFailureRefs]),
    })
    if (responseNodeID) {
      this.causalEdge({
        from: { type: "node", id: responseNodeID, label: "response.output" },
        to: { type: "response_claim", id: node.node_id, label: "response.claim" },
        relation: "response_to_claim",
        label: "Response output was split into a claim",
      })
      if (claim.claim_group_id) {
        this.causalEdge({
          from: { type: "node", id: responseNodeID, label: "response.output" },
          to: { type: "response_claim", id: node.node_id, label: "response.claim" },
          relation: "response_to_claim_group",
          label: "Response output contains a claim in this claim group",
          metadata: { claim_group_id: claim.claim_group_id },
        })
      }
    }
    for (const ref of claim.direct_evidence_refs) this.linkSourceToClaim(ref, node.node_id, "evidence_to_claim")
    for (const ref of claim.direct_support_refs ?? []) {
      if (claim.direct_evidence_refs.includes(ref) || claim.execution_refs.includes(ref)) continue
      this.linkSourceToClaim(ref, node.node_id, "execution_to_claim")
    }
    for (const ref of claim.superseded_evidence_refs ?? []) {
      if (claim.direct_support_refs?.includes(ref)) continue
      this.linkSupersededSourceToClaim(ref, node.node_id)
    }
    for (const ref of claim.context_refs) this.linkSourceToClaim(ref, node.node_id, "context_to_claim")
    for (const ref of claim.execution_refs) this.linkSourceToClaim(ref, node.node_id, "execution_to_claim")
    this.claimSupportAssessment(claim, node.node_id)
    return claim
  }

  private claimSupportAssessment(claim: TraceResponseClaimRecord, claimNodeID: string) {
    const attributionRefs = dedupeStrings([...claim.direct_evidence_refs, ...(claim.matched_evidence_refs ?? [])])
    const transitiveToolOutcomeRefs = this.toolOutcomeRefsFromSourceRefs(attributionRefs)
    const toolFailureContextRefs = this.toolFailureContextRefsForClaim(claim, attributionRefs)
    const allToolOutcomeRefs = dedupeStrings([
      ...attributionRefs.filter(isToolOutcomeRef),
      ...(claim.dependency_tool_outcome_refs ?? []),
      ...(claim.derived_tool_outcome_refs ?? []),
      ...transitiveToolOutcomeRefs,
    ])
    const toolFailureRefs = allToolOutcomeRefs.filter((ref) => ref.startsWith("tool_error:"))
    const toolResultRefs = allToolOutcomeRefs.filter((ref) => ref.startsWith("tool_result:"))
    const relevantToolFailureRefs = dedupeStrings([...toolFailureRefs, ...toolFailureContextRefs])
    const replacementEvidenceRefs = claim.direct_evidence_refs.filter(
      (ref) => ref.startsWith("evidence:") || ref.startsWith("observation:"),
    )
    const toolFailureHandledStatus = relevantToolFailureRefs.length
      ? replacementEvidenceRefs.length
        ? "recovered_with_replacement_evidence"
        : "observed_as_direct_failure_evidence"
      : "not_applicable"
    const missingEvidenceTypes = dedupeStrings([
      ...(claim.direct_evidence_refs.length ? [] : ["direct_evidence"]),
      ...(claim.support_level === "unsupported" && !claim.context_refs.length ? ["context"] : []),
      ...(claim.support_level === "unsupported" && !claim.execution_refs.length ? ["execution"] : []),
    ])
    const weakMatchReasons = dedupeStrings([
      ...(claim.quality_flags.includes("weak_evidence_match") ? ["weak_evidence_match"] : []),
      ...(claim.quality_flags.includes("unmatched_direct_evidence_refs") ? ["unmatched_direct_evidence_refs"] : []),
      ...(claim.match_score !== undefined && claim.match_score > 0 && claim.match_score < 0.5
        ? [`low_match_score:${claim.match_score}`]
        : []),
    ])
    const assessmentID = `claimsupport_${safeNodeIDPart(claim.claim_id)}`
    const derivationInputRefs = this.canonicalDerivationInputRefs([
      `response_claim:${claimNodeID}`,
      ...claim.direct_evidence_refs,
      ...(claim.conflicting_evidence_refs ?? []),
      ...(claim.verification_after_test_change_refs ?? []),
      ...allToolOutcomeRefs,
      ...claim.context_refs,
      ...claim.execution_refs,
    ])
    const derivedAt = nowIso()
    const node = this.node({
      node_id: assessmentID,
      kind: "claim.support_assessment",
      component: "result",
      title: `Claim support assessment ${claim.claim_index}`,
      status: "success",
      origin: "deterministic_derived",
      input_refs: derivationInputRefs,
      derivation: deterministicDerivation("claim_support_assessment", derivationInputRefs, derivedAt),
      data: {
        assessment_id: assessmentID,
        claim_id: claim.claim_id,
        response_segment_id: claim.response_segment_id,
        claim_node_id: claimNodeID,
        support_level: claim.support_level,
        quality_flags: claim.quality_flags,
        missing_evidence_types: missingEvidenceTypes,
        direct_evidence_refs: claim.direct_evidence_refs,
        derived_tool_outcome_refs: claim.derived_tool_outcome_refs,
        candidate_tool_outcome_refs: claim.candidate_tool_outcome_refs,
        dependency_tool_outcome_refs: claim.dependency_tool_outcome_refs,
        matched_evidence_refs: claim.matched_evidence_refs,
        candidate_evidence_refs: claim.candidate_evidence_refs,
        grounding_candidate_refs: claim.grounding_candidate_refs,
        grounding_decisions: claim.grounding_decisions,
        grounding_method: claim.grounding_method,
        grounding_behavior_impact: claim.grounding_behavior_impact,
        context_refs: claim.context_refs,
        execution_refs: claim.execution_refs,
        tool_failure_context_refs: toolFailureContextRefs,
        tool_failure_dependency_refs: toolFailureRefs,
        tool_failure_handled_status: toolFailureHandledStatus,
        replacement_evidence_refs: replacementEvidenceRefs,
        tool_result_dependency_refs: toolResultRefs,
        conflicting_evidence_refs: claim.conflicting_evidence_refs,
        support_conflict_status: claim.support_conflict_status,
        verification_after_test_change_refs: claim.verification_after_test_change_refs,
        weak_match_reasons: weakMatchReasons,
        match_strategy: claim.match_strategy,
        match_score: claim.match_score,
        match_reasons: claim.match_reasons,
        attribution_summary: claim.attribution_summary,
      },
      source_refs: derivationInputRefs,
      source_locations: claim.source_locations,
      temporal_advisory_refs: toolFailureContextRefs,
    })
    for (const ref of toolFailureRefs) this.linkSourceToClaim(ref, claimNodeID, "context_to_claim")
    this.updateToolFailureHandling(relevantToolFailureRefs, {
      claimNodeID,
      replacementEvidenceRefs,
      handledStatus: toolFailureHandledStatus,
    })
    this.causalEdge({
      from: { type: "response_claim", id: claimNodeID, label: "response.claim" },
      to: { type: "claim_support", id: node.node_id, label: "claim.support_assessment" },
      relation: "derived_from",
      label: "Claim support assessment derived from response claim attribution",
    })
    return node
  }

  private canonicalDerivationInputRefs(input: string[]) {
    return dedupeStrings(
      input.flatMap((ref) => {
        const declared = typedCausalRef(ref)
        if (!declared.ref_id) return []
        const resolved = this.causalIR.resolveReference(ref)
        if (declared.ref_type === "node" && resolved.ref_type === "external") return [`external:${ref}`]
        return [ref]
      }),
    )
  }

  private toolFailureContextRefsForClaim(claim: TraceResponseClaimRecord, attributionRefs: string[]) {
    const candidateRefs = dedupeStrings([
      ...(claim.candidate_tool_outcome_refs ?? []),
      ...(claim.dependency_tool_outcome_refs ?? []),
      ...(claim.derived_tool_outcome_refs ?? []),
    ]).filter((ref) => ref.startsWith("tool_error:"))
    if (!candidateRefs.length) return []
    const claimText = [
      fieldSummaryText(claim.text),
      fieldSummaryText(claim.raw_text),
      fieldSummaryText(claim.canonical_text),
    ].join("\n")
    const attributionText = attributionRefs.map((ref) => fieldSummaryText(this.sourceNodeForRef(ref)?.data)).join("\n")
    return candidateRefs.filter((ref) => this.toolFailureRelevantToText(ref, `${claimText}\n${attributionText}`))
  }

  private toolFailureRelevantToText(ref: string, text: string) {
    const node = this.sourceNodeForRef(ref)
    if (!node) return false
    const haystack = text.toLowerCase()
    const data = recordFromUnknown(node.data) ?? {}
    const args = recordFromUnknown(data.args)
    const pathValue =
      firstStringField(args, ["filePath", "filepath", "path"]) ??
      firstStringField(data, ["path", "filePath", "filepath"])
    if (pathValue) {
      const normalizedPath = pathValue.toLowerCase()
      const basename = path.basename(pathValue).toLowerCase()
      if (haystack.includes(normalizedPath) || (basename && haystack.includes(basename))) return true
    }
    const message = [
      stringField(data, ["error_message"]),
      stringPreview(data.error, 1000),
      stringPreview(data.output, 1000),
    ]
      .join("\n")
      .toLowerCase()
    if (!message.trim()) return false
    const mentionsFailure = /not found|no such file|enoent|missing|不存在|失败|找不到/.test(haystack)
    if (mentionsFailure && /not found|no such file|enoent|missing|不存在/.test(message)) return true
    return tokenOverlapScore(haystack, message) >= 0.35
  }

  private updateToolFailureHandling(
    refs: string[],
    input: { claimNodeID: string; replacementEvidenceRefs: string[]; handledStatus: string },
  ) {
    for (const ref of refs) {
      const node = this.sourceNodeForRef(ref)
      if (!node || node.kind !== "tool.error") continue
      const data = node.data ?? {}
      const downstreamClaimRefs = dedupeStrings([
        ...(stringArrayField(data, ["downstream_claim_refs", "downstreamClaimRefs"]) ?? []),
        `response_claim:${input.claimNodeID}`,
      ])
      const replacementEvidenceRefs = dedupeStrings([
        ...(stringArrayField(data, ["replacement_evidence_refs", "replacementEvidenceRefs"]) ?? []),
        ...input.replacementEvidenceRefs,
      ])
      node.data = {
        ...data,
        downstream_claim_refs: downstreamClaimRefs,
        replacement_evidence_refs: replacementEvidenceRefs,
        handled_status: input.handledStatus,
      }
      node.artifact_refs = this.collectArtifactRefs(node.data)
      this.causalIR.updateNode(node)
    }
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
        finality_source: segment.finality_source,
      }
      const node = this.causalNodes.find((item) => item.node_id === `responsenode_${segment.segment_id}`)
      if (!node?.data) continue
      node.data.response_role = segment.response_role
      node.data.is_final_for_case = segment.is_final_for_case
      node.data.finality_source = segment.finality_source
      const metadata =
        node.data.metadata && typeof node.data.metadata === "object"
          ? (node.data.metadata as Record<string, unknown>)
          : {}
      node.data.metadata = {
        ...metadata,
        response_role: segment.response_role,
        is_final_for_case: segment.is_final_for_case,
        finality_source: segment.finality_source,
      }
      this.causalIR.updateNode(node)
    }
  }

  private responseSegmentIDForDesignRecord(record: TraceDesignRecord) {
    const metadata =
      record.metadata && typeof record.metadata === "object" ? (record.metadata as Record<string, unknown>) : {}
    const metadataSegmentID = stringField(metadata, ["source_segment_id", "response_segment_id"])
    if (metadataSegmentID) return metadataSegmentID
    const responseSegmentRef = record.source_refs?.find((ref) => ref.startsWith("response_segment:"))
    return responseSegmentRef?.slice("response_segment:".length)
  }

  private pruneDesignRecordsForFinalResponses() {
    const finalSegmentIDs = new Set(
      this.responseSegments
        .filter(
          (segment) =>
            segment.response_role === "final_answer" &&
            segment.visibility === "user_visible" &&
            segment.is_final_for_case === true,
        )
        .map((segment) => segment.segment_id),
    )
    const staleDesignIDs = new Set<string>()
    const retainedDesignRecords: TraceDesignRecord[] = []
    for (const record of this.designRecords) {
      if (record.source !== "final_response") {
        retainedDesignRecords.push(record)
        continue
      }
      const segmentID = this.responseSegmentIDForDesignRecord(record)
      if (!segmentID || finalSegmentIDs.has(segmentID)) {
        retainedDesignRecords.push(record)
        continue
      }
      staleDesignIDs.add(record.design_id)
    }
    if (!staleDesignIDs.size) return

    this.designRecords = retainedDesignRecords
    this.causalIR.replaceNodes(this.causalNodes.filter((node) => !staleDesignIDs.has(node.node_id)))
    const referencesStaleDesign = (edge: { from: TraceRef; to: TraceRef }) =>
      (edge.from.type === "design_record" && staleDesignIDs.has(edge.from.id)) ||
      (edge.to.type === "design_record" && staleDesignIDs.has(edge.to.id))
    this.causalIR.replaceEdges(this.causalEdges.filter((edge) => !referencesStaleDesign(edge)))
    this.write("semantic.design_record.pruned", {
      design_ids: [...staleDesignIDs],
      reason: "source response segment is no longer the final user-visible answer",
    })
  }

  private promoteFinalResponseFromExitGate(input: ExitGateInput): TraceResponseSegment | undefined {
    if (input.has_final_answer !== true) return undefined
    if (input.decision !== "exit") return undefined
    const candidates = this.responseSegments.filter((segment) => {
      if (segment.visibility !== "user_visible") return false
      const metadata = segment.metadata ?? {}
      if (!input.message_id) return true
      return metadata.messageID === input.message_id || metadata.message_id === input.message_id
    })
    const segment =
      candidates.at(-1) ?? this.responseSegments.filter((item) => item.visibility === "user_visible").at(-1)
    if (!segment) return undefined
    segment.response_role = "final_answer"
    segment.is_final_for_case = true
    segment.finality_source = "explicit"
    segment.metadata = {
      ...(segment.metadata ?? {}),
      response_role: "final_answer",
      is_final_for_case: true,
      finality_source: "explicit",
      finality_reason: "exit_gate_has_final_answer",
      finality_gate_message_id: input.message_id,
    }
    const node = this.causalNodes.find((item) => item.node_id === `responsenode_${segment.segment_id}`)
    if (node?.data) {
      node.data.response_role = "final_answer"
      node.data.is_final_for_case = true
      node.data.finality_source = "explicit"
      const metadata =
        node.data.metadata && typeof node.data.metadata === "object"
          ? (node.data.metadata as Record<string, unknown>)
          : {}
      node.data.metadata = {
        ...metadata,
        response_role: "final_answer",
        is_final_for_case: true,
        finality_source: "explicit",
        finality_reason: "exit_gate_has_final_answer",
        finality_gate_message_id: input.message_id,
      }
      this.causalIR.updateNode(node)
    }
    return segment
  }

  private emitFinalResponseClaims(caseStatus: TraceStatus) {
    for (const segment of this.responseSegments) {
      if (this.claimedResponseSegmentIDs.has(segment.segment_id)) continue
      if (segment.response_role !== "final_answer") continue
      if (segment.visibility !== "user_visible") continue
      if (segment.is_final_for_case !== true) continue
      if (caseStatus !== "success" && segment.finality_source !== "explicit") continue
      const responseNodeID = `responsenode_${segment.segment_id}`
      const responseNode = this.causalNodes.find((item) => item.node_id === responseNodeID)
      const responseText = this.responseSourceBySegmentID.get(segment.segment_id) ?? responseNode?.data?.text ?? fieldSummaryText(segment.text)
      const claims = atomizeResponseClaims(responseText)
      const plannedClaims = claims.map((claim, index) => {
        const claimID = semanticID("claim", this.causalNodes.length + index + 1)
        return {
          claim,
          claim_id: claimID,
          record_ref: `record:responseclaim_${claimID}`,
        }
      })
      plannedClaims.forEach(({ claim, claim_id }, index) => {
        this.responseClaim({
          claim_id,
          response_segment_id: segment.segment_id,
          claim_key: claim.key,
          text: claim.text,
          claim_format: claim.claim_format,
          raw_text: claim.raw_text,
          canonical_text: claim.canonical_text,
          table_cells: claim.table_cells,
          table_subject: claim.table_subject,
          table_values: claim.table_values,
          claim_group_id: claim.claim_group_id,
          claim_index: claim.claim_index,
          claim_count: claim.claim_count,
          source_byte_range: claim.source_byte_range,
          previous_claim_key: claim.previous_claim_key,
          next_claim_key: claim.next_claim_key,
          previous_claim_ref: plannedClaims[index - 1]?.record_ref,
          next_claim_ref: plannedClaims[index + 1]?.record_ref,
          atomization_status: claim.atomization_status,
          atomization_reason: claim.atomization_reason,
          source_refs: segment.source_refs,
          source_locations: segment.source_locations,
          generation_provenance_refs: segment.generation_provenance_refs,
          generation_grounding_candidate_refs: segment.generation_grounding_candidate_refs,
          metadata: {
            response_node_id: responseNodeID,
            response_role: segment.response_role,
            turn_index: segment.turn_index,
            is_final_for_case: segment.is_final_for_case,
            finality_source: segment.finality_source,
            generation_provenance_refs: segment.generation_provenance_refs,
          },
        })
      })
      for (let index = 0; index + 1 < plannedClaims.length; index++) {
        const current = plannedClaims[index]!
        const next = plannedClaims[index + 1]!
        this.causalEdge({
          from: { type: "response_claim", id: `responseclaim_${current.claim_id}`, label: "response.claim" },
          to: { type: "response_claim", id: `responseclaim_${next.claim_id}`, label: "response.claim" },
          relation: "claim_group_precedes",
          eligible_for_attribution: false,
          label: "Adjacent claim/group order within a response segment",
          metadata: {
            causal_semantics: "claim_group_order_only",
            eligible_for_attribution: false,
            behavior_impact: "none",
          },
        })
      }
      this.claimedResponseSegmentIDs.add(segment.segment_id)
      this.responseSourceBySegmentID.delete(segment.segment_id)
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
    this.node({
      node_id: design.design_id,
      kind: "design.record",
      component: "processor",
      span_id: design.span_id,
      title: "Design record",
      status: "success",
      data: {
        design_id: design.design_id,
        source: design.source,
        requirement_summary: design.requirement_summary,
        existing_boundaries: design.existing_boundaries,
        design_constraints: design.design_constraints,
        candidate_solutions: design.candidate_solutions,
        selected_solution: design.selected_solution,
        tradeoffs: design.tradeoffs,
        risks: design.risks,
        test_strategy: design.test_strategy,
        metadata: design.metadata,
      },
      source_refs: design.source_refs,
      metadata: design.metadata,
    })
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
      this.causalIR.updateNode(existing)
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
    const finalSegment = this.promoteFinalResponseFromExitGate(input)
    const normalizedSourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const sourceRefs = mergeRefs(
      normalizedSourceRefs,
      finalSegment ? [`response_segment:${finalSegment.segment_id}`] : [],
    )
    const node = this.node({
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
      temporal_advisory_refs: this.temporalSourceRefs(normalizedSourceRefs),
    })
    if (finalSegment) {
      this.edge({
        from: { type: "response_segment", id: finalSegment.segment_id },
        to: { type: "exit_gate", id: gateID },
        relation: "selected_by",
        label: "Exit gate marked this response as the final user-visible answer",
      })
    }
    return node
  }

  evidenceFact(input: EvidenceFactInput) {
    const factID = input.fact_id ?? semanticID("fact", this.causalNodes.length + 1)
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
    const verificationProvenance = this.verificationFactProvenance(sourceRefs, input.span_id)
    const sourceLocations = dedupeSourceLocations([
      ...(input.source_locations ?? []),
      ...collectSourceLocations(input.data),
      ...collectSourceLocations(input.metadata),
    ])
    const canonical = canonicalEvidence(input, sourceLocations)
    const recordKind = evidenceRecordKind(input, canonical)
    const planItems = recordKind === "task.plan_state" ? planStateSummary(input.data ?? input.summary) : undefined
    const dedupeKey =
      recordKind === "evidence.semantic_fact" ? semanticFactDedupeKey(input, canonical, sourceLocations) : undefined
    const existingID = dedupeKey ? this.semanticFactNodeIDsByKey.get(dedupeKey) : undefined
    const existing = existingID ? this.causalNodes.find((node) => node.node_id === existingID) : undefined
    if (existing) {
      const existingData = existing.data ?? {}
      const occurrenceCount = optionalNumber(existingData.occurrence_count) ?? 1
      existing.source_refs = mergeRefs(existing.source_refs, sourceRefs)
      existing.data = {
        ...existingData,
        ...verificationProvenance,
        occurrence_count: occurrenceCount + 1,
        duplicate_source_refs: dedupeStrings([
          ...(stringArrayField(existingData, ["duplicate_source_refs", "duplicateSourceRefs"]) ?? []),
          ...sourceRefs,
        ]),
      }
      existing.artifact_refs = this.collectArtifactRefs(existing.data)
      for (const ref of sourceRefs ?? []) {
        const parsed = this.parseSourceRef(ref)
        if (!parsed) continue
        this.causalEdge({
          from: parsed,
          to: { type: "evidence", id: existing.node_id, label: "evidence.semantic_fact" },
          relation: "derived_from",
          label: "Duplicate semantic evidence occurrence merged into existing fact",
          metadata: { duplicate_suppressed: true },
        })
      }
      const diagnosticData = {
        duplicate_key: dedupeKey,
        duplicate_of: existing.node_id,
        source_refs: sourceRefs,
      }
      this.causalIR.createDiagnostic({
        diagnostic_id: `suppression_${++this.diagnosticSequence}_${hash(json(diagnosticData))}`,
        kind: "evidence_duplicate_suppressed",
        level: "info",
        status: "suppressed",
        ...diagnosticData,
      })
      const stored = this.causalIR.updateNode(existing) as CausalNode
      this.writePartial()
      return stored
    }
    const node = this.node({
      node_id: `evidence_${factID}`,
      kind: recordKind,
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
        evidence_origin: canonical.evidence_origin,
        ...verificationProvenance,
        source_locations: sourceLocations,
        evidence_class:
          recordKind === "evidence.semantic_fact"
            ? "semantic_fact"
            : recordKind === "task.plan_state"
              ? "plan_state"
              : "execution_observation",
        plan_items: planItems,
        metadata: input.metadata,
      },
      source_refs: sourceRefs,
      source_locations: sourceLocations,
      metadata: input.metadata,
    })
    if (dedupeKey) this.semanticFactNodeIDsByKey.set(dedupeKey, node.node_id)
    if (recordKind === "evidence.semantic_fact") this.remember(this.recentEvidenceNodeIDs, node.node_id)
    for (const ref of sourceRefs ?? []) {
      const parsed = this.parseSourceRef(ref)
      if (!parsed) continue
      this.causalEdge({
        from: parsed,
        to: { type: "evidence", id: node.node_id, label: evidenceRecordLabel(recordKind) },
        relation: "derived_from",
        label:
          recordKind === "task.plan_state"
            ? "Plan state derived from source record"
            : recordKind === "execution.observation"
              ? "Execution observation derived from source record"
              : "Semantic evidence derived from source record",
      })
    }
    if (recordKind === "evidence.semantic_fact" && !isDerivedMultifactInput(input)) {
      const additionalClaims = additionalStructuredClaimsFromEvidence(
        input,
        sourceLocations,
        canonical.structured_claim,
      )
      for (const claim of additionalClaims) {
        this.evidenceFact({
          source: input.source,
          category: input.category,
          summary: evidenceFactSummaryFromStructuredClaim(claim) || input.summary,
          data: evidenceFactDataFromStructuredClaim(claim),
          span_id: input.span_id,
          source_refs: dedupeStrings([...sourceRefs, `evidence:${node.node_id}`]),
          source_locations: claim.source_span ? [claim.source_span] : sourceLocations,
          confidence: input.confidence ?? "observed",
          support_level: input.support_level,
          quality_flags: dedupeStrings([...(input.quality_flags ?? []), "multi_fact_extracted"]),
          metadata: {
            ...(input.metadata ?? {}),
            derived_from_multifact: true,
            parent_fact_ref: `evidence:${node.node_id}`,
          },
        })
      }
    }
    return node
  }

  node(
    input: CausalNodeInput,
    options: { trackGeneration?: boolean; writePartial?: boolean } = {},
  ) {
    const temporal = normalizeTemporalReferences(input)
    const normalized = temporal.value
    const requestedSourceRefs = input.source_refs ?? input.evidence_refs
    const sourceRefs = normalized.source_refs ?? normalized.evidence_refs
    const temporalAdvisoryRefs = dedupeStrings([
      ...(normalized.temporal_advisory_refs ?? []),
      ...this.temporalSourceRefs(requestedSourceRefs),
      ...(temporal.selectors.length ? this.currentSourceRefs() : []),
    ])
    const timestamp = nowIso()
    const node: CausalNode = {
      node_id: normalized.node_id ?? semanticID("node", this.causalNodes.length + 1),
      kind: normalized.kind,
      component: normalized.component,
      span_id: normalized.span_id,
      parent_span_id: normalized.parent_span_id,
      timestamp,
      time_ms: Math.max(0, Date.now() - this.startedAt),
      title: normalized.title,
      status: normalized.status,
      origin: normalized.origin,
      input_refs: normalized.input_refs,
      output_refs: normalized.output_refs,
      data:
        normalized.data === undefined
          ? undefined
          : this.summarizeCausalObject(normalized.data, `${normalized.kind}.data`),
      source_refs: sourceRefs,
      source_locations: normalized.source_locations,
      typed_resources: normalized.typed_resources,
      artifact_refs: [],
      aliases: normalized.aliases,
      derivation: normalized.derivation,
      metadata: normalized.metadata,
    }
    node.artifact_refs = this.collectArtifactRefs(node.data)
    const stored = this.causalIR.createNode(node) as CausalNode
    if (options.trackGeneration !== false) {
      if (stored.kind === "prompt.assembly") this.remember(this.recentPromptNodeIDs, stored.node_id)
      if (stored.kind === "context.pack" || stored.kind === "context.transform")
        this.remember(this.recentContextNodeIDs, stored.node_id)
      if (stored.kind === "llm.call") this.remember(this.recentLLMNodeIDs, stored.node_id)
    }
    this.createTemporalAdvisoryEdges(stored, temporalAdvisoryRefs)
    if (options.writePartial !== false) this.writePartial()
    return stored
  }

  private createTemporalAdvisoryEdges(node: Pick<CausalNode, "node_id" | "kind">, refs: string[]) {
    const temporalRefs = dedupeStrings(refs)
    if (!temporalRefs.length) return
    const contextSet = this.contextSet({
      kind: "temporal_advisory",
      memberRefs: temporalRefs,
      selectionMethod: "recent_source_fallback",
    })
    if (contextSet.node_id === node.node_id) return
    const key = `${contextSet.node_id}->${node.node_id}`
    if (this.temporalAdvisoryEdgeKeys.has(key)) return
    this.temporalAdvisoryEdgeKeys.add(key)
    this.causalIR.createEdge({
      edge_id: semanticID("cedge", this.causalEdges.length + 1),
      from: { type: "node", id: contextSet.node_id, label: contextSet.kind },
      to: { type: "node", id: node.node_id, label: node.kind },
      relation: "derived_from",
      label: "Temporal proximity advisory context set; not attribution-bearing",
      evidence_tier: "temporal_advisory",
      eligible_for_attribution: false,
      derivation_method: "recent_source_fallback",
      evidence_refs: [`node:${contextSet.node_id}`],
      metadata: {
        temporal_advisory: true,
        eligible_for_attribution: false,
        membership_expansion: "context_set_payload",
      },
    })
  }

  causalEdge(input: CausalEdgeInput) {
    if (input.from.type.startsWith("recent_") || input.to.type.startsWith("recent_")) return undefined
    const temporal = normalizeTemporalReferences(input)
    const normalized = temporal.value
    const edge: CausalEdgeInput & { edge_id: string } = {
      edge_id: normalized.edge_id ?? semanticID("cedge", this.causalEdges.length + 1),
      from: normalized.from,
      to: normalized.to,
      relation: normalized.relation,
      original_relation: normalized.original_relation,
      normalized_relation: normalized.normalized_relation,
      evidence_tier: normalized.evidence_tier,
      eligible_for_attribution: normalized.eligible_for_attribution,
      derivation_method: normalized.derivation_method,
      evidence_refs: normalized.evidence_refs,
      confidence: normalized.confidence,
      label: normalized.label,
      metadata: normalized.metadata,
    }
    const stored = this.causalIR.createEdge(edge) as CausalEdge
    if (temporal.selectors.length) {
      const target = this.causalNodes.find((node) => node.node_id === normalized.to.id)
      if (target) this.createTemporalAdvisoryEdges(target, this.currentSourceRefs())
    }
    this.writePartial()
    return stored
  }

  observation(input: ObservationInput) {
    if (isWeakObservation(input)) {
      const diagnosticData = {
        source: input.source,
        category: input.category,
        reason: "path-only tool_output observation has no independent attribution value",
      }
      this.causalIR.createDiagnostic({
        diagnostic_id: `suppression_${++this.diagnosticSequence}_${hash(json(diagnosticData))}`,
        kind: "weak_observation_suppressed",
        level: "info",
        status: "suppressed",
        ...diagnosticData,
      })
      this.writePartial()
      return undefined
    }
    const sourceRefs = this.observationSourceRefs(input)
    const verificationProvenance = this.verificationFactProvenance(sourceRefs, input.span_id)
    const semanticExtras = observationSemanticExtras(input.source, input.data)
    const sourceLocations = dedupeSourceLocations([
      ...(input.source_locations ?? []),
      ...collectSourceLocations(input.data),
      ...collectSourceLocations(input.metadata),
      ...(semanticExtras.source_locations ?? []),
    ])
    const observationKind = isPlanStateInput(input.source, input.category, input.data ?? input.summary)
      ? "task.plan_state"
      : "execution.observation"
    const node = this.node({
      kind: observationKind,
      component: this.componentForObservationSource(input.source),
      span_id: input.span_id,
      title: input.category ?? input.source,
      status: "success",
      data: {
        source: input.source,
        category: input.category,
        summary: input.summary,
        data: input.data,
        evidence_class: observationKind === "task.plan_state" ? "plan_state" : "execution_observation",
        plan_items: observationKind === "task.plan_state" ? planStateSummary(input.data ?? input.summary) : undefined,
        ...verificationProvenance,
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
        to: { type: "node", id: node.node_id, label: observationKind },
        relation:
          parsed.type === "compaction" || parsed.id.startsWith("compaction")
            ? "compaction_to_observation"
            : "source_to_observation",
        label:
          observationKind === "task.plan_state"
            ? "Plan state produced from source record"
            : "Execution observation produced from source record",
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
    const sourceRefs = this.normalizeSourceRefs(input.source_refs ?? input.evidence_refs)
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
    const forcedSummaryArtifactID =
      input.output_summary === undefined
        ? undefined
        : (this.collectArtifactRefs(outputSummary)[0] ?? this.writeCompactionSummaryArtifact(input.output_summary))
    contextLedger.summary_artifact_id = contextLedger.summary_artifact_id ?? forcedSummaryArtifactID
    if (contextLedger.summary_artifact_id && contextLedger.quality_flags?.includes("summary_artifact_pending")) {
      contextLedger.quality_flags = contextLedger.quality_flags.filter((flag) => flag !== "summary_artifact_pending")
    }
    const serializedTailArtifactRef = this.collectArtifactRefs(serializedTail)[0]
    const summaryArtifactRef = contextLedger.summary_artifact_id ?? forcedSummaryArtifactID
    const metadata = input.metadata ?? {}
    const explicitAfterContextRefs = dedupeStrings([
      ...(input.after_context_refs ?? []),
      ...(stringArrayField(metadata, ["after_context_refs", "afterContextRefs"]) ?? []),
    ])
    const afterContextRefs =
      explicitAfterContextRefs.length || !contextLedger.auto_continue_prompt_ref
        ? explicitAfterContextRefs
        : [contextLedger.auto_continue_prompt_ref]
    const summarySemantics = compactionSummarySemantics(input.output_summary)
    const inferredRetainedFactRefs = this.inferredRetainedFactRefsFromSummary(summarySemantics)
    if (inferredRetainedFactRefs.length) {
      contextLedger.retained_fact_refs = dedupeStrings([
        ...(contextLedger.retained_fact_refs ?? []),
        ...inferredRetainedFactRefs,
      ])
      contextLedger.quality_flags = dedupeStrings([
        ...(contextLedger.quality_flags ?? []),
        "retained_fact_refs_inferred_from_summary",
      ])
      if (contextLedger.ledger_id_quality === "unknown") contextLedger.ledger_id_quality = "estimated"
    }
    const compactionDerived = compactionDerivedFields({
      tokenBefore: contextLedger.token_estimate_before,
      tokenAfter: contextLedger.token_estimate_after,
      retainedFactRefs: contextLedger.retained_fact_refs,
      droppedFactRefs: contextLedger.dropped_fact_refs,
      outputSummary: input.output_summary,
      autoContinue: input.auto_continue,
      afterContextRefs,
    })
    const node = this.node({
      kind: "context.compaction",
      component: "context",
      span_id: input.span_id,
      title: `${input.trigger} compaction`,
      status: input.result === "error" ? "error" : "success",
      data: {
        trigger: input.trigger,
        session_id: input.session_id,
        message_id: input.message_id,
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
        algorithm_version: contextLedger.algorithm_version,
        input_message_count: contextLedger.input_message_count,
        compaction_request_message_count: contextLedger.compaction_request_message_count,
        output_message_count: contextLedger.output_message_count,
        before_context_refs: sourceRefs ?? [],
        after_context_refs: afterContextRefs,
        serialized_tail_artifact_ref: serializedTailArtifactRef,
        summary_artifact_ref: summaryArtifactRef,
        retained_message_ids: contextLedger.retained_message_ids,
        dropped_message_ids: contextLedger.dropped_message_ids,
        retained_fact_refs: contextLedger.retained_fact_refs,
        dropped_fact_refs: contextLedger.dropped_fact_refs,
        retained_fact_count: compactionDerived.retained_fact_count,
        dropped_fact_count: compactionDerived.dropped_fact_count,
        summary_key_facts: summarySemantics.summary_key_facts,
        summary_constraint_facts: summarySemantics.summary_constraint_facts,
        summary_preserved_paths: summarySemantics.summary_preserved_paths,
        token_estimate_before: contextLedger.token_estimate_before,
        token_estimate_after: contextLedger.token_estimate_after,
        retention_ratio: compactionDerived.retention_ratio,
        compression_loss_risks: compactionDerived.compression_loss_risks,
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

  private observationSourceRefs(input: ObservationInput) {
    const provided = input.source_refs ?? input.evidence_refs
    const refs = provided ? this.normalizeSourceRefs(provided) : []
    if (!this.isToolOutcomeObservation(input)) return refs
    if (refs.some(isToolOutcomeRef)) return refs
    this.setTemporalSourceRefs(refs, [...this.temporalSourceRefs(refs), ...this.recentToolOutcomeRefs.slice(-2)])
    return refs
  }

  private isToolOutcomeObservation(input: ObservationInput) {
    const text = `${input.source ?? ""} ${input.category ?? ""}`.toLowerCase()
    return /tool|mcp|skill|subagent|task|verification|grep|read|glob|bash|file|edit|write/.test(text)
  }

  private inferredRetainedFactRefsFromSummary(summarySemantics: ReturnType<typeof compactionSummarySemantics>) {
    const summaryText = normalizeMatchText(
      [
        ...summarySemantics.summary_key_facts,
        ...summarySemantics.summary_constraint_facts,
        ...summarySemantics.summary_preserved_paths,
      ].join("\n"),
    )
    if (!summaryText) return []
    const summaryPaths = summarySemantics.summary_preserved_paths
      .map((item) => normalizeSourcePath(item))
      .filter((item): item is string => Boolean(item))
    const refs: string[] = []
    for (const node of this.causalNodes) {
      if (node.kind !== "evidence.semantic_fact" && node.kind !== "evidence.fact") continue
      const data = node.data ?? {}
      const structured = recordFromUnknown(data.structured_claim)
      const span = recordFromUnknown(structured?.source_span)
      const sourcePaths = dedupeStrings([
        typeof span?.path === "string" ? span.path : "",
        ...((node.source_locations ?? []).map((location) => location.path).filter(Boolean) as string[]),
      ]).filter(Boolean)
      const pathMatch = sourcePaths.some((path) => {
        const normalizedPath = normalizeSourcePath(path)
        if (!normalizedPath) return false
        const normalizedPathText = normalizeMatchText(normalizedPath)
        return (
          summaryText.includes(normalizedPathText) ||
          summaryPaths.some(
            (summaryPath) => normalizedPath.startsWith(summaryPath) || summaryPath.startsWith(normalizedPath),
          )
        )
      })
      const value =
        structured?.value === undefined || structured?.value === null ? "" : normalizeMatchText(structured.value)
      const predicate = typeof structured?.predicate === "string" ? structured.predicate : undefined
      const subject = typeof structured?.subject === "string" ? normalizeMatchText(structured.subject) : ""
      const predicateMatch = predicate ? predicateMatchesClaim(predicate, summaryText) : false
      const subjectMatch = Boolean(subject && summaryText.includes(subject))
      const highSignalValue = Boolean(value && value.length >= 3 && summaryText.includes(value))
      const claimText = normalizeMatchText(data.claim)
      const claimMatch = Boolean(claimText && claimText.length >= 16 && summaryText.includes(claimText))
      if (pathMatch || claimMatch || (highSignalValue && (predicateMatch || subjectMatch || pathMatch))) {
        refs.push(`evidence:${node.node_id}`)
      }
    }
    return dedupeStrings(refs).slice(0, 50)
  }

  currentSourceRefs() {
    return [
      ...this.recentPromptNodeIDs.slice(-2).map((id) => `prompt:${id}`),
      ...this.recentContextNodeIDs.slice(-3).map((id) => `context:${id}`),
      ...this.recentContextSnapshotIDs.slice(-2).map((id) => `context_snapshot:${id}`),
      ...this.recentLLMNodeIDs.slice(-2).map((id) => `llm:${id}`),
      ...this.recentEvidenceNodeIDs.slice(-6).map((id) => `evidence:${id}`),
      ...this.recentToolOutcomeRefs.slice(-6),
      ...this.recentToolSpanIDs.slice(-3).map((id) => `tool_span:${id}`),
      ...this.recentVerificationIDs.slice(-3).map((id) => `verification:${id}`),
      ...this.recentChangeIDs.slice(-3).map((id) => `change:${id}`),
    ].filter((item, index, array) => array.indexOf(item) === index)
  }

  private enrichSemanticFactApplicabilityAndConflicts() {
    const facts = this.causalNodes.filter((node) => node.kind === "evidence.semantic_fact")
    const changePoints: SemanticFactChangePoint[] = this.causalNodes
      .filter((node) => node.kind === "change")
      .map((node) => ({
        time_ms: node.time_ms,
        files: stringArrayField(node.data ?? {}, ["files"]) ?? [],
      }))
    const updated = new Set<string>()
    for (const fact of facts) {
      if (!fact.data) continue
      const applicability = semanticFactApplicability(fact.data)
      const role = semanticFactRole(fact.data, fact, changePoints)
      fact.data = {
        ...fact.data,
        applicability_status: fact.data.applicability_status ?? applicability.status,
        applicability_reasons: dedupeStrings([
          ...(stringArrayField(fact.data, ["applicability_reasons", "applicabilityReasons"]) ?? []),
          ...applicability.reasons,
        ]),
        semantic_role: fact.data.semantic_role ?? role.semantic_role,
        fact_scope: fact.data.fact_scope ?? role.fact_scope,
      }
      updated.add(fact.node_id)
    }

    const groups = new Map<string, SemanticFactConflictMember[]>()
    for (const fact of facts) {
      const conflictKey = semanticFactConflictKey(fact.data)
      if (!conflictKey?.value) continue
      const current = groups.get(conflictKey.key) ?? []
      current.push({
        node: fact,
        value: conflictKey.value,
        semantic_role: typeof fact.data?.semantic_role === "string" ? fact.data.semantic_role : "unknown",
      })
      groups.set(conflictKey.key, current)
    }

    let groupIndex = 0
    for (const [key, members] of groups) {
      const values = new Set(members.map((member) => member.value))
      if (values.size <= 1) continue
      groupIndex += 1
      const groupID = `fact_conflict_${groupIndex}_${safeNodeIDPart(key)}`
      const classification = semanticFactConflictClassification(members)
      for (const member of members) {
        const otherRefs = members
          .filter((candidate) => candidate.node.node_id !== member.node.node_id)
          .map((candidate) => `evidence:${candidate.node.node_id}`)
        const status =
          typeof member.node.data?.applicability_status === "string" ? member.node.data.applicability_status : "unknown"
        const role = typeof member.node.data?.semantic_role === "string" ? member.node.data.semantic_role : "unknown"
        member.node.data = {
          ...(member.node.data ?? {}),
          conflict_group_id: member.node.data?.conflict_group_id ?? groupID,
          conflict_refs: dedupeStrings([
            ...(stringArrayField(member.node.data ?? {}, ["conflict_refs", "conflictRefs"]) ?? []),
            ...otherRefs,
          ]),
          conflict_role:
            member.node.data?.conflict_role ??
            (status === "active"
              ? "active_candidate"
              : status === "legacy"
                ? "legacy_candidate"
                : "conflicting_candidate"),
          conflict_kind: member.node.data?.conflict_kind ?? classification.conflict_kind,
          conflict_severity: member.node.data?.conflict_severity ?? classification.conflict_severity,
          conflict_issue:
            typeof member.node.data?.conflict_issue === "boolean"
              ? member.node.data.conflict_issue
              : semanticConflictMemberIssue(role, classification),
        }
        updated.add(member.node.node_id)
      }
    }

    for (const fact of facts) {
      if (!updated.has(fact.node_id)) continue
      fact.artifact_refs = this.collectArtifactRefs(fact.data)
      this.causalIR.updateNode(fact)
    }
  }

  finish(input?: FinishTraceInput) {
    try {
      if (this.finished) return
      this.evaluateConstraints()
      this.normalizeFinalResponseSegments()
      this.pruneDesignRecordsForFinalResponses()
      const error = input?.error ? errorInfo(input.error) : undefined
      if (error) this.errors.push(error)
      this.result = input?.result ?? this.result
      const status = input?.status ?? (error ? "error" : "success")
      const caseStatus = this.inferCaseStatus(status, error)
      this.enrichSemanticFactApplicabilityAndConflicts()
      this.emitFinalResponseClaims(caseStatus)
      this.emitTaskObligations()
      this.finalizeOpenRecords(status, caseStatus)
      this.backfillCompactionEstimates()
      this.enrichInlineSubagentRefs()
      this.enrichMcpConsumptionRefs()
      this.emitCaseLifecycleRecord(status, caseStatus)
      this.finished = true
      const checkpointSummary = this.causalIRSummary(status, caseStatus, true)
      const summary = this.summary(status, checkpointSummary)
      this.write("trace.finish", summary)
      const causalIR: CausalIRTraceSummary = {
        ...checkpointSummary,
        journal: this.causalIR.journalSummary(),
      }
      const finalization = this.causalIR.finalize(causalIR)
      const emittedCausalIR: CausalIRTraceSummary = finalization.committed
        ? causalIR
        : {
            ...causalIR,
            journal: this.causalIR.journalSummary(),
          }
      const provenance = this.projectProvenanceSummary(emittedCausalIR)
      const canonical = jsonPretty(emittedCausalIR)
      this.safeWrite(this.traceFile, canonical)
      this.safeWrite(this.manifestFile, jsonPretty(emittedCausalIR.manifest))
      this.safeLinkOrWrite(this.traceFile, this.partialFile, canonical)
      this.safeWrite(this.provenanceTraceFile, jsonPretty(provenance))
      this.safeWrite(this.legacyTraceFile, jsonPretty(summary))
      this.safeWrite(this.htmlFile, renderProvenanceTraceHtml(provenance))
    } finally {
      this.responseSourceBySegmentID.clear()
    }
  }

  flushForSignal(signal: NodeJS.Signals) {
    const result = {
      ...(recordFromUnknown(this.result) ?? {}),
      reason: signal,
      signal,
      trace_html_flush: "process_signal",
    }
    if (!this.finished) {
      this.finish({ status: "cancelled", result })
      return
    }
    this.result = result
    const caseStatus = this.observedCaseStatus() ?? this.inferCaseStatus("cancelled", undefined)
    const causalIR = this.causalIRSummary("cancelled", caseStatus)
    const summary = this.summary("cancelled", causalIR)
    const provenance = this.projectProvenanceSummary(causalIR)
    const canonical = jsonPretty(causalIR)
    this.safeWrite(this.traceFile, canonical)
    this.safeWrite(this.manifestFile, jsonPretty(causalIR.manifest))
    this.safeLinkOrWrite(this.traceFile, this.partialFile, canonical)
    this.safeWrite(this.provenanceTraceFile, jsonPretty(provenance))
    this.safeWrite(this.legacyTraceFile, jsonPretty(summary))
    this.safeWrite(this.htmlFile, renderProvenanceTraceHtml(provenance))
  }

  private summary(
    status: TraceStatus,
    canonical: Pick<CausalIRTraceSummary, "edges" | "dataflow_edges">,
  ): TraceSummary {
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
      dataflow_edges: this.projectLegacySemanticEdges(canonical.edges, canonical.dataflow_edges),
      verification_records: this.verificationRecords,
      change_records: this.changeRecords,
      constraint_records: this.constraintRecords,
      response_segments: this.responseSegments,
      design_records: this.designRecords,
    }
  }

  private manifest(status: TraceStatus, caseStatus?: TraceStatus): TraceManifest {
    const ended = Date.now()
    const shutdownReason = this.serverShutdownReason()
    const manifestStatus = caseStatus ?? status
    const shutdown = this.shutdownLifecycle(manifestStatus)
    return {
      trace_version: TRACE_VERSION,
      case_id: this.caseID,
      run_id: this.runID,
      session_id: this.sessionID,
      started_at: this.startedIso,
      ended_at: new Date(ended).toISOString(),
      duration_ms: Math.max(0, ended - this.startedAt),
      status: manifestStatus,
      server_status: status,
      process_status: status,
      server_shutdown_reason: shutdownReason,
      shutdown_signal: shutdown.signal,
      shutdown_disposition: shutdown.disposition,
      case_status: caseStatus ?? status,
      case_completed_at: caseStatus === "success" ? new Date(ended).toISOString() : undefined,
      collection_mode: "passive_sidecar",
      behavior_impact: "none",
      subject_revision: this.subjectRevision,
      subject_revision_provenance: this.subjectRevisionProvenance,
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

  private causalIRSummary(
    status: TraceStatus,
    caseStatus?: TraceStatus,
    synchronize = false,
  ): CausalIRTraceSummary {
    const manifest = this.manifest(status, caseStatus)
    const records = this.provenanceRecords().filter((record) => !this.isCaseDiagnosticKind(record.event_type))
    const traceHealth = this.traceHealth(records)
    this.syncCaseDiagnosticNodes(
      traceHealth,
      manifest.shutdown_disposition !== "interrupted_before_case_completion",
    )
    const causalIR = synchronize ? this.causalIR.synchronize() : this.causalIR.snapshot()
    const streamSummary = this.streamSummary()
    const provenance = this.projectCausalIRSnapshot(causalIR, manifest, {
      spans: this.spans.size,
      events: this.events.length,
      token_usage: cloneTokenUsage(this.tokenUsage) ?? {},
      stream_summary: streamSummary,
      trace_health: traceHealth,
    })
    return {
      trace_version: TRACE_VERSION,
      causal_ir_version: causalIR.version,
      manifest,
      nodes: causalIR.nodes,
      edges: causalIR.edges,
      records: provenance.records,
      dataflow_edges: provenance.dataflow_edges,
      artifacts: causalIR.artifacts as TraceArtifact[],
      journal: this.causalIR.journalSummary(),
      diagnostics: causalIR.diagnostics,
      compatibility: {
        provenance_projection: "provenance-trace.json",
      },
      metrics: {
        spans: this.spans.size,
        events: this.events.length,
        records: causalIR.nodes.length,
        dataflow_edges: causalIR.edges.length,
        artifacts: causalIR.artifacts.length,
        token_usage: cloneTokenUsage(this.tokenUsage) ?? {},
        stream_summary: streamSummary,
        trace_health: traceHealth,
      },
    }
  }

  private projectProvenanceSummary(summary: CausalIRTraceSummary): ProvenanceTraceView {
    return this.projectCausalIRSnapshot(
      {
        version: summary.causal_ir_version,
        runID: summary.manifest.run_id,
        caseID: summary.manifest.case_id,
        nodes: summary.nodes,
        edges: summary.edges,
        artifacts: summary.artifacts,
        diagnostics: summary.diagnostics,
      },
      summary.manifest,
      summary.metrics,
    )
  }

  private projectLegacySemanticEdges(
    canonicalEdges: CausalIREdge[],
    compatibilityEdges: DataflowEdge[],
  ): TraceSemanticEdge[] {
    const compatibilityByID = new Map(compatibilityEdges.map((edge) => [edge.edge_id, edge]))
    const output: TraceSemanticEdge[] = []
    for (const edge of canonicalEdges) {
      const marker = edge.metadata?.[legacySemanticEdgeProjectionKey]
      if (!marker || typeof marker !== "object" || Array.isArray(marker)) continue
      const rawFields = (marker as Record<string, unknown>).fields
      if (!Array.isArray(rawFields)) continue
      const fields = new Set<LegacySemanticEdgeOptionalField>(
        rawFields.filter(
          (field): field is LegacySemanticEdgeOptionalField =>
            typeof field === "string" && legacySemanticEdgeOptionalFieldSet.has(field),
        ),
      )
      const compatibility = compatibilityByID.get(edge.edge_id)
      if (!compatibility) continue
      const projected: TraceSemanticEdge = {
        edge_id: edge.edge_id,
        from: compatibility.from,
        to: compatibility.to,
        relation: edge.original_relation,
      }
      if (fields.has("evidence_tier")) projected.evidence_tier = edge.evidence_tier
      if (fields.has("eligible_for_attribution")) projected.eligible_for_attribution = edge.eligible_for_attribution
      if (fields.has("derivation_method")) projected.derivation_method = edge.derivation_method
      if (fields.has("evidence_refs"))
        projected.evidence_refs = edge.evidence_refs.map((ref) => ref.legacy_ref ?? `${ref.ref_type}:${ref.ref_id}`)
      if (fields.has("confidence")) projected.confidence = edge.confidence
      if (fields.has("label")) projected.label = edge.label
      if (fields.has("metadata")) {
        const metadata = { ...(edge.metadata ?? {}) }
        delete metadata[legacySemanticEdgeProjectionKey]
        projected.metadata = metadata
      }
      output.push(projected)
    }
    return output
  }

  private projectCausalIRSnapshot(
    snapshot: CausalIRStoreSnapshot,
    manifest: TraceManifest,
    metrics: Pick<
      ProvenanceTraceSummary["metrics"],
      "spans" | "events" | "token_usage" | "stream_summary" | "trace_health"
    >,
  ): ProvenanceTraceView {
    const projection = projectProvenanceTrace(snapshot, {
      traceVersion: TRACE_VERSION,
      manifest,
      metrics,
    })
    const dataflowEdges = projection.dataflow_edges.map((edge) => {
      const metadata = { ...edge.metadata }
      delete metadata[legacySemanticEdgeProjectionKey]
      return { ...edge, metadata }
    })
    return {
      trace_version: TRACE_VERSION,
      manifest,
      records: projection.records,
      dataflow_edges: dataflowEdges,
      artifacts: projection.artifacts as TraceArtifact[],
      metrics: {
        spans: projection.metrics.spans,
        events: projection.metrics.events,
        records: projection.metrics.records,
        dataflow_edges: dataflowEdges.length,
        artifacts: projection.metrics.artifacts,
        token_usage: metrics.token_usage,
        stream_summary: metrics.stream_summary,
        trace_health: metrics.trace_health,
      },
    }
  }

  private syncCaseDiagnosticNodes(traceHealth: TraceHealthMetrics, completionDiagnosticsEligible: boolean) {
    const current = this.causalNodes.filter((node) => this.isCaseDiagnosticKind(node.kind))
    const currentByID = new Map(current.map((node) => [node.node_id, node]))
    const desired = this.caseDiagnosticNodes(traceHealth, currentByID, completionDiagnosticsEligible)
    const desiredIDs = new Set(desired.map((node) => node.node_id))

    for (const node of desired) {
      const existing = currentByID.get(node.node_id)
      if (!existing) {
        this.causalIR.createNode(node)
        continue
      }
      if (JSON.stringify(existing) !== JSON.stringify(node)) this.causalIR.updateNode(node)
    }

    if (current.some((node) => !desiredIDs.has(node.node_id))) {
      this.causalIR.replaceNodes(
        this.causalNodes.filter((node) => !this.isCaseDiagnosticKind(node.kind) || desiredIDs.has(node.node_id)),
      )
    }
  }

  private caseDiagnosticNodes(
    traceHealth: TraceHealthMetrics,
    currentByID: Map<string, CausalNode>,
    completionDiagnosticsEligible: boolean,
  ): CausalNode[] {
    const nodes: CausalNode[] = []
    const timestamp = nowIso()
    const timeMs = Date.now() - this.startedAt
    const diagnosticNode = (node: CausalNode) => {
      const current = currentByID.get(node.node_id)
      nodes.push({
        ...node,
        timestamp: current?.timestamp ?? node.timestamp,
        time_ms: current?.time_ms ?? node.time_ms,
      })
    }
    const missingVerification = traceHealth.issues.find((issue) => issue.kind === "missing_verification_after_change")
    if (missingVerification && completionDiagnosticsEligible) {
      const missingID = "missing_semantic_final_test_result"
      const sourceRefs = dedupeStrings(missingVerification.refs ?? [])
      diagnosticNode({
        node_id: missingID,
        kind: "case.missing_semantic",
        component: "trace",
        timestamp,
        time_ms: timeMs,
        title: "Missing final test result",
        status: "error",
        source_refs: sourceRefs,
        data: {
          semantic_name: "final_test_result",
          gap_kind: "required_trace_semantic_missing",
          reason: "No test-like verification command was recorded after repository changes.",
          issue_kind: missingVerification.kind,
          issue_refs: sourceRefs,
        },
      })
      diagnosticNode({
        node_id: "observed_defect_missing_verification_after_change",
        kind: "case.observed_defect",
        component: "trace",
        timestamp,
        time_ms: timeMs,
        title: "Observed defect: missing verification after change",
        status: "error",
        source_refs: dedupeStrings([`record:${missingID}`, ...sourceRefs]),
        data: {
          defect_type: "missing_verification_after_change",
          component: "verification",
          failure_type: "final_test_result_missing",
          reason: missingVerification.message,
          issue_kind: missingVerification.kind,
          issue_refs: sourceRefs,
        },
      })
    }
    for (const issue of traceHealth.issues.filter((item) => item.kind === "verification_after_test_change")) {
      const id = `observed_defect_${safeNodeIDPart(`${issue.kind}_${issue.record_id ?? nodes.length}`)}`
      diagnosticNode({
        node_id: id,
        kind: "case.observed_defect",
        component: "trace",
        timestamp,
        time_ms: timeMs,
        title: "Observed defect: verification after test change",
        status: "error",
        source_refs: dedupeStrings(issue.refs ?? []),
        data: {
          defect_type: "verification_after_test_change",
          component: "verification",
          failure_type: "verification_scope_risk",
          reason: issue.message,
          issue_kind: issue.kind,
          issue_refs: issue.refs ?? [],
        },
      })
    }
    return nodes
  }

  private isCaseDiagnosticKind(kind: string) {
    return kind === "case.missing_semantic" || kind === "case.observed_defect"
  }

  private serverShutdownReason() {
    const result = recordFromUnknown(this.result) ?? {}
    const flush = stringField(result, ["trace_html_flush"])
    if (flush === "process_signal") return "process_signal"
    const signal = stringField(result, ["signal"])
    if (signal) return "process_signal"
    return stringField(result, ["reason"])
  }

  private shutdownLifecycle(caseStatus: TraceStatus) {
    const result = recordFromUnknown(this.result) ?? {}
    const explicitSignal = stringField(result, ["signal"])
    const reason = stringField(result, ["reason"])
    const signal = explicitSignal ?? (reason && /^SIG[A-Z0-9]+$/.test(reason) ? reason : undefined)
    if (signal && caseStatus === "success") {
      return { signal, disposition: "graceful_after_case_completion" }
    }
    if (signal) {
      return { signal, disposition: "interrupted_before_case_completion" }
    }
    return {
      signal: undefined,
      disposition: caseStatus === "success" ? "normal_case_completion" : "case_ended_without_signal",
    }
  }

  private streamSummary() {
    const counts: Record<string, number> = {}
    for (const event of this.events) {
      const key = `${event.event_type.replaceAll(".", "_").replaceAll("-", "_")}_events`
      counts[key] = (counts[key] ?? 0) + 1
    }
    return counts
  }

  private inferCaseStatus(serverStatus: TraceStatus, error: TraceError | undefined): TraceStatus {
    const resultStatus = stringField(recordFromUnknown(this.result) ?? {}, ["case_status", "caseStatus", "status"])
    if (resultStatus === "success" || resultStatus === "error" || resultStatus === "cancelled") return resultStatus
    if (error || this.errors.length) return "error"
    const finalAnswer = this.responseSegments.some(
      (segment) =>
        segment.response_role === "final_answer" &&
        segment.visibility === "user_visible" &&
        segment.is_final_for_case === true,
    )
    if (finalAnswer && serverStatus === "success") return "success"
    const explicitFinalAnswer = this.responseSegments.some(
      (segment) =>
        segment.response_role === "final_answer" &&
        segment.visibility === "user_visible" &&
        segment.is_final_for_case === true &&
        segment.finality_source === "explicit",
    )
    if (explicitFinalAnswer) return "success"
    return serverStatus
  }

  private observedCaseStatus(): TraceStatus | undefined {
    const caseRecord = [...this.causalNodes]
      .reverse()
      .find((node) => node.kind === "case.completed" || node.kind === "case.failed")
    const status = caseRecord?.data?.case_status ?? caseRecord?.status
    return status === "success" || status === "error" || status === "cancelled" ? status : undefined
  }

  private emitCaseLifecycleRecord(serverStatus: TraceStatus, caseStatus: TraceStatus) {
    const shutdownReason = this.serverShutdownReason()
    const shutdown = this.shutdownLifecycle(caseStatus)
    const finalSegment = this.responseSegments
      .filter(
        (segment) =>
          segment.response_role === "final_answer" &&
          segment.visibility === "user_visible" &&
          segment.is_final_for_case === true &&
          (caseStatus === "success" || segment.finality_source === "explicit"),
      )
      .at(-1)
    const failedOpenRecordRefs = this.finalizedOpenRecordRefsForCase(caseStatus)
    const signalNode =
      shutdown.signal && shutdown.disposition === "interrupted_before_case_completion"
        ? this.node({
            node_id: `process_signal_${hash(`${this.runID}:${shutdown.signal}`).slice(0, 8)}`,
            kind: "process.signal",
            component: "runtime",
            title: `process received ${shutdown.signal}`,
            status: "cancelled",
            data: {
              signal: shutdown.signal,
              shutdown_disposition: shutdown.disposition,
              server_shutdown_reason: shutdownReason,
              observation_source:
                stringField(recordFromUnknown(this.result) ?? {}, ["trace_html_flush"]) === "process_signal"
                  ? "process_signal_handler"
                  : "finish_result",
              sender_identity_available: false,
              recording_mode: "passive_posthoc",
              agent_feedback: "none",
            },
          })
        : undefined
    const sourceRefs = dedupeStrings([
      ...(finalSegment ? [`response_segment:${finalSegment.segment_id}`] : failedOpenRecordRefs),
      ...(signalNode ? [`node:${signalNode.node_id}`] : []),
    ])
    const node = this.node({
      node_id: `case_${caseStatus === "success" ? "completed" : "failed"}_${hash(this.runID).slice(0, 8)}`,
      kind: caseStatus === "success" ? "case.completed" : "case.failed",
      component: "run",
      title: caseStatus === "success" ? "case completed" : "case failed",
      status: caseStatus,
      data: {
        run_id: this.runID,
        case_id: this.caseID,
        server_status: serverStatus,
        process_status: serverStatus,
        server_shutdown_reason: shutdownReason,
        shutdown_signal: shutdown.signal,
        shutdown_disposition: shutdown.disposition,
        case_status: caseStatus,
        final_response_segment_id: finalSegment?.segment_id,
        result: this.result,
        token_usage: cloneTokenUsage(this.tokenUsage) ?? {},
        finalized_open_record_refs: failedOpenRecordRefs,
        finalized_open_record_count: failedOpenRecordRefs.length,
      },
      source_refs: sourceRefs,
    })
    if (signalNode) {
      this.causalEdge({
        from: { type: "node", id: signalNode.node_id },
        to: { type: "node", id: node.node_id },
        relation: "failed_before",
        label: "External process signal interrupted the case before completion",
        metadata: {
          causal_role: "external_interruption",
          sender_identity_available: false,
        },
      })
    }
    if (caseStatus !== "success") {
      for (const ref of failedOpenRecordRefs) {
        const source = this.traceRefFromSourceRef(ref)
        if (!source) continue
        this.causalEdge({
          from: source,
          to: { type: "node", id: node.node_id },
          relation: "failed_before",
          label: "Running semantic record was finalized before case failure",
        })
      }
    }
  }

  private finalizedOpenRecordRefsForCase(caseStatus: TraceStatus) {
    if (caseStatus === "success") return []
    return dedupeStrings(
      this.causalNodes
        .filter((node) => node.data?.finalized_status === "finalized_without_close")
        .slice(-16)
        .map((node) => `node:${node.node_id}`),
    )
  }

  private emitTaskObligations() {
    const obligations = this.taskObligations()
    for (const obligation of obligations) {
      const evaluation = this.evaluateTaskObligation(obligation)
      const obligationID = `obl_${hash(`${obligation.obligation_type}:${obligation.target_path ?? ""}:${this.caseID}`).slice(0, 10)}`
      const requirementSourceRefs = dedupeStrings(obligation.source_refs ?? [])
      const sourceRefs = dedupeStrings([...requirementSourceRefs, ...evaluation.refs])
      this.node({
        node_id: `obligation_${obligationID}`,
        kind: "task.obligation",
        component: "processor",
        title: obligation.requirement_text,
        status: evaluation.status === "fulfilled" ? "success" : "error",
        data: {
          obligation_id: obligationID,
          obligation_type: obligation.obligation_type,
          requirement_text: obligation.requirement_text,
          target_path: obligation.target_path,
          requirement_source_refs: requirementSourceRefs,
          status: evaluation.status,
          evaluation_refs: evaluation.refs,
          missing_action: evaluation.missing_action,
          quality_flags: evaluation.status === "unmet" ? ["task_obligation_unmet"] : [],
        },
        source_refs: sourceRefs,
      })
    }
  }

  private taskObligations() {
    const obligations = [...taskObligationsFromInput(this.input)]
    for (const node of this.causalNodes) {
      if (node.kind !== "prompt.assembly") continue
      const stage = typeof node.data?.stage === "string" ? node.data.stage : ""
      if (!["initial_user_request", "user_message_created"].includes(stage)) continue
      obligations.push(
        ...taskObligationsFromInput(
          {
            input: node.data?.input,
            parts: node.data?.parts,
          },
          [this.recordRefForNode(node)],
        ),
      )
    }
    return dedupeTaskObligations(obligations)
  }

  private evaluateTaskObligation(obligation: TaskObligationDraft) {
    if (obligation.obligation_type === "verification_required") {
      const refs = this.verificationRecords
        .filter((record) => isTestLikeCommand(record.command))
        .map((record) => `verification:${record.verification_id}`)
      return {
        status: refs.length ? "fulfilled" : "unmet",
        refs,
        missing_action: refs.length ? undefined : "No test-like verification command was recorded.",
      }
    }
    if (obligation.obligation_type === "mcp_required") {
      const refs = this.causalNodes
        .filter((node) => node.kind === "mcp.call")
        .map((node) => this.recordRefForNode(node))
      return {
        status: refs.length ? "fulfilled" : "unmet",
        refs,
        missing_action: refs.length ? undefined : "No mcp.call record was observed.",
      }
    }
    if (obligation.obligation_type === "subagent_required") {
      const refs = this.causalNodes
        .filter((node) => node.kind === "subagent.call")
        .map((node) => this.recordRefForNode(node))
      return {
        status: refs.length ? "fulfilled" : "unmet",
        refs,
        missing_action: refs.length ? undefined : "No subagent.call record was observed.",
      }
    }
    const target = obligation.target_path
    const changeRefs = this.changeRecords.map((record) => `change:${record.change_id}`)
    const violatingChanges = target
      ? this.changeRecords.filter((record) => record.files.some((file) => pathWithinScope(file, target)))
      : []
    return {
      status: violatingChanges.length ? "unmet" : "fulfilled",
      refs: violatingChanges.length ? violatingChanges.map((record) => `change:${record.change_id}`) : changeRefs,
      missing_action: violatingChanges.length ? `Recorded change touched forbidden path ${target}.` : undefined,
    }
  }

  private traceRefFromSourceRef(ref: string): TraceRef | undefined {
    const parsed = this.parseSourceRef(ref)
    if (!parsed) return undefined
    return { type: parsed.type, id: parsed.id }
  }

  private finalizeOpenRecords(status: TraceStatus, caseStatus?: TraceStatus) {
    const serviceShutdownAfterCompletion = status === "cancelled" && caseStatus === "success"
    const finalStatus: TraceStatus = serviceShutdownAfterCompletion
      ? "success"
      : status === "running"
        ? "cancelled"
        : status
    const finalizedStatus = serviceShutdownAfterCompletion ? "closed_after_case_completion" : "finalized_without_close"
    const finalizedReason = serviceShutdownAfterCompletion
      ? "service_shutdown_after_completion"
      : finalStatus === "cancelled"
        ? "trace_cancelled"
        : finalStatus === "error"
          ? "trace_error"
          : "trace_finished"
    const finalizedAt = new Date().toISOString()
    for (const span of this.spans.values()) {
      if (span.status !== "running") continue
      span.status = finalStatus
      span.end_time = finalizedAt
      span.end_ms = Date.now() - this.startedAt
      span.duration_ms = Math.max(0, span.end_ms - span.start_ms)
      span.metadata = {
        ...(span.metadata ?? {}),
        finalized_status: finalizedStatus,
        finalized_reason: finalizedReason,
      }
    }
    for (const node of this.causalNodes) {
      if (node.status !== "running") continue
      node.status = finalStatus
      node.data = {
        ...(node.data ?? {}),
        finalized_status: finalizedStatus,
        finalized_reason: finalizedReason,
        finalized_at: finalizedAt,
        original_status: "running",
      }
      node.artifact_refs = this.collectArtifactRefs(node.data)
      this.causalIR.updateNode(node)
    }
  }

  private backfillCompactionEstimates() {
    const checks = this.causalNodes.filter((node) => node.kind === "context.compaction_check")
    for (const compaction of this.causalNodes.filter((node) => node.kind === "context.compaction")) {
      if (!compaction.data) continue
      if (optionalNumber(compaction.data.token_estimate_before) !== undefined) continue
      const match = nearestCompactionCheck(compaction, checks)
      const estimate =
        optionalNumber(match?.data?.token_estimate) ?? optionalNumber(objectField(match?.data?.token_usage, "total"))
      if (estimate === undefined) continue
      compaction.data.token_estimate_before = estimate
      compaction.data.estimate_source = "nearest_compaction_check"
      const contextLedger =
        compaction.data.context_ledger && typeof compaction.data.context_ledger === "object"
          ? (compaction.data.context_ledger as Record<string, unknown>)
          : {}
      contextLedger.token_estimate_before = contextLedger.token_estimate_before ?? estimate
      const flags = Array.isArray(contextLedger.quality_flags)
        ? contextLedger.quality_flags.filter((flag) => flag !== "missing_token_estimate")
        : []
      contextLedger.quality_flags = flags
      compaction.data.context_ledger = contextLedger
      const derived = compactionDerivedFields({
        tokenBefore: optionalNumber(compaction.data.token_estimate_before),
        tokenAfter: optionalNumber(compaction.data.token_estimate_after),
        retainedFactRefs: Array.isArray(compaction.data.retained_fact_refs) ? compaction.data.retained_fact_refs : [],
        droppedFactRefs: Array.isArray(compaction.data.dropped_fact_refs) ? compaction.data.dropped_fact_refs : [],
        outputSummary: compaction.data.output_summary,
        autoContinue: compaction.data.auto_continue === true,
        afterContextRefs: Array.isArray(compaction.data.after_context_refs)
          ? (compaction.data.after_context_refs.filter((item) => typeof item === "string") as string[])
          : [],
      })
      compaction.data.retention_ratio = derived.retention_ratio
      compaction.data.retained_fact_count = derived.retained_fact_count
      compaction.data.dropped_fact_count = derived.dropped_fact_count
      compaction.data.compression_loss_risks = derived.compression_loss_risks
      compaction.artifact_refs = this.collectArtifactRefs(compaction.data)
      this.causalIR.updateNode(compaction)
    }
  }

  private enrichInlineSubagentRefs() {
    const subagentNodes = this.causalNodes.filter((node) => node.kind === "subagent.call")
    for (const subagent of subagentNodes) {
      if (!subagent.data) continue
      const childSessionID = firstStringField(subagent.data, ["child_session_id", "childSessionID"])
      if (!childSessionID) continue
      const childRecords = this.causalNodes.filter(
        (node) => node.node_id !== subagent.node_id && causalNodeReferencesSession(node, childSessionID),
      )
      if (!childRecords.length) continue
      const childRecordRefs = childRecords.map((record) => `node:${record.node_id}`)
      const childPromptRefs = childRecords
        .filter((record) => record.kind === "prompt.assembly")
        .map((record) => `prompt:${record.node_id}`)
      const childResultRefs = childRecords
        .filter((record) => record.kind === "response.output" || record.kind === "agent.lifecycle")
        .map((record) =>
          record.kind === "response.output" && typeof record.data?.segment_id === "string"
            ? `response_segment:${record.data.segment_id}`
            : `node:${record.node_id}`,
        )
      const childKeyEvidenceRefs = childRecords
        .filter((record) => record.kind === "evidence.semantic_fact" || record.kind === "evidence.fact")
        .map((record) => `evidence:${record.node_id}`)
        .slice(0, 20)
      const parentConsumptionEvidence = this.parentConsumptionEvidenceForSubagent(
        subagent,
        dedupeStrings([...childRecordRefs, ...childPromptRefs, ...childResultRefs, ...childKeyEvidenceRefs]),
      )
      const parentConsumers = parentConsumptionEvidence.map((item) => item.node)
      const parentConsumptionRefs = parentConsumers.map((record) => this.recordRefForNode(record))
      const fullTraceRefArtifact = this.writeArtifact(
        "json",
        "subagent.child_trace_refs",
        prettyJsonString(
          json({
            child_session_id: childSessionID,
            child_record_refs: childRecordRefs,
            child_prompt_refs: childPromptRefs,
            child_result_refs: childResultRefs,
            child_key_evidence_refs: childKeyEvidenceRefs,
          }),
        ),
      )
      const inlineFields = {
        child_trace_available: true,
        child_trace_mode: "inline_same_trace",
        child_record_count: childRecords.length,
        child_record_refs: childRecordRefs.slice(0, 20),
        child_record_refs_truncated: childRecordRefs.length > 20,
        child_prompt_refs: childPromptRefs.slice(0, 20),
        child_result_refs: childResultRefs.slice(0, 20),
        child_key_evidence_refs: childKeyEvidenceRefs,
        child_key_fact_refs: childKeyEvidenceRefs,
        child_output_preview: fieldSummaryText(subagent.data.output).slice(0, 2000),
        parent_consumption_refs: parentConsumptionRefs,
        parent_consumption_evidence: parentConsumptionEvidence.map((item) => ({
          consumer_ref: this.recordRefForNode(item.node),
          evidence_tier: item.evidence_tier,
          derivation_method: item.derivation_method,
          matched_text_hash: item.matched_text_hash,
          behavior_impact: "none",
        })),
        child_timeline_summary: childTimelineSummary(childRecords),
        child_metric_summary: childMetricSummary(childRecords),
        child_trace_artifact_ref: fullTraceRefArtifact?.artifact_id,
        child_trace_artifact_status: fullTraceRefArtifact ? undefined : "write_failed",
      }
      applySubagentInlineFields(subagent.data, inlineFields)
      subagent.artifact_refs = this.collectArtifactRefs(subagent.data)
      this.causalIR.updateNode(subagent)
      for (const evidence of parentConsumptionEvidence) {
        const consumer = evidence.node
        if (
          this.hasCausalEdge(
            { type: "node", id: subagent.node_id, label: "subagent.call" },
            consumer.node_id,
            "reported_to",
          )
        )
          continue
        this.causalEdge({
          from: { type: "node", id: subagent.node_id, label: "subagent.call" },
          to: {
            type: this.provenanceTypeForNode(consumer),
            id:
              consumer.kind === "response.output" && typeof consumer.data?.segment_id === "string"
                ? consumer.data.segment_id
                : consumer.node_id,
            label: consumer.kind,
          },
          relation: "reported_to",
          evidence_tier: evidence.evidence_tier,
          derivation_method: evidence.derivation_method,
          confidence: evidence.evidence_tier === "confirmed" ? 1 : 0.9,
          metadata: {
            behavior_impact: "none",
            matched_text_hash: evidence.matched_text_hash,
          },
          label: "Subagent result was consumed by the parent agent record",
        })
      }
    }
  }

  private parentConsumptionEvidenceForSubagent(
    subagent: CausalNode,
    childRefs: string[] = [],
  ): SubagentConsumptionEvidence[] {
    const refs = dedupeStrings([
      `node:${subagent.node_id}`,
      ...(subagent.span_id ? [`span:${subagent.span_id}`, `tool_span:${subagent.span_id}`] : []),
      ...(subagent.source_refs ?? []),
      ...childRefs,
    ])
    const childSessionID = firstStringField(subagent.data, ["child_session_id", "childSessionID"])
    const subagentInput = recordFromUnknown(subagent.data?.input)
    const parentSessionID =
      firstStringField(subagent.data, ["parent_session_id", "parentSessionID"]) ??
      firstStringField(subagentInput, ["parent_session_id", "parentSessionID", "session_id", "sessionID"])
    const subagentPosition = this.causalNodes.findIndex((node) => node.node_id === subagent.node_id)
    const childCompletionPositions = this.causalNodes.flatMap((node, index) => {
      if (!childSessionID || !causalNodeReferencesSession(node, childSessionID)) return []
      if (node.kind === "response.output") return [index]
      if (
        node.kind === "agent.lifecycle" &&
        /response\.completed|turn\.completed|completed/.test(firstStringField(node.data, ["phase"]) ?? "")
      )
        return [index]
      return []
    })
    const completionPosition = Math.max(subagentPosition, ...childCompletionPositions)
    const contentConsumerKinds = new Set([
      "context.transform",
      "context.pack",
      "decision",
      "response.output",
      "response.claim",
      "agent.lifecycle",
    ])
    const childPhrases = dedupeStrings(
      [
        ...collectTextCandidates(subagent.data?.output),
        ...this.causalNodes
          .filter(
            (node) =>
              childSessionID && node.kind === "response.output" && causalNodeReferencesSession(node, childSessionID),
          )
          .flatMap((node) => collectTextCandidates(node.data)),
      ]
        .map((item) => normalizeMatchText(item))
        .filter((item) => item.length >= 16 && item.length <= 2000),
    )
    return this.causalNodes.flatMap<SubagentConsumptionEvidence>((node, position) => {
      if (node.node_id === subagent.node_id) return []
      if (position <= completionPosition) return []
      if (childSessionID && causalNodeReferencesSession(node, childSessionID)) return []
      const nodeSessionID = causalNodeSessionID(node)
      if (parentSessionID && nodeSessionID && nodeSessionID !== parentSessionID) return []
      if ((node.source_refs ?? []).some((ref) => refs.includes(ref))) {
        return [
          {
            node,
            evidence_tier: "confirmed",
            derivation_method: "explicit_source_ref",
            matched_text_hash: undefined,
          },
        ]
      }
      if (!contentConsumerKinds.has(node.kind)) return []
      if (parentSessionID && nodeSessionID !== parentSessionID) return []
      const consumerText = normalizeMatchText(collectTextCandidates(node.data).join("\n"))
      const matched = childPhrases.find((phrase) => consumerText.includes(phrase))
      if (!matched) return []
      return [
        {
          node,
          evidence_tier: "content_matched",
          derivation_method: "exact_normalized_child_output_match",
          matched_text_hash: hash(matched),
        },
      ]
    })
  }

  private enrichMcpConsumptionRefs() {
    const mcpNodes = this.causalNodes.filter((node) => node.kind === "mcp.call")
    for (const mcp of mcpNodes) {
      if (!mcp.data) continue
      const sourceRefs = dedupeStrings([
        `node:${mcp.node_id}`,
        ...(mcp.span_id ? [`span:${mcp.span_id}`] : []),
        ...(mcp.source_refs ?? []),
      ])
      const consumers = this.causalNodes.filter((node) => {
        if (node.node_id === mcp.node_id) return false
        const refs = node.source_refs ?? []
        return refs.some((ref) => sourceRefs.includes(ref))
      })
      if (!consumers.length) {
        const outputPreview = fieldSummaryText(mcp.data.output)
        if (outputPreview && !mcp.data.output_preview) {
          mcp.data = { ...mcp.data, output_preview: outputPreview.slice(0, 2000) }
          mcp.artifact_refs = this.collectArtifactRefs(mcp.data)
          this.causalIR.updateNode(mcp)
        }
        continue
      }
      const consumedByRefs = dedupeStrings([
        ...(stringArrayField(mcp.data, ["consumed_by_refs", "consumedByRefs"]) ?? []),
        ...consumers.map((node) => this.recordRefForNode(node)),
      ])
      const outputPreview = fieldSummaryText(mcp.data.output)
      mcp.data = {
        ...mcp.data,
        consumed_by_refs: consumedByRefs,
        output_preview: outputPreview ? outputPreview.slice(0, 2000) : mcp.data.output_preview,
      }
      mcp.artifact_refs = this.collectArtifactRefs(mcp.data)
      this.causalIR.updateNode(mcp)
      for (const consumer of consumers) {
        if (this.hasCausalEdge({ type: "node", id: mcp.node_id, label: "mcp.call" }, consumer.node_id, "returned_by"))
          continue
        this.causalEdge({
          from: { type: "node", id: mcp.node_id, label: "mcp.call" },
          to: { type: this.provenanceTypeForNode(consumer), id: consumer.node_id, label: consumer.kind },
          relation: "returned_by",
          label: "MCP call returned data consumed by this semantic record",
        })
      }
    }
  }

  private recordRefForNode(node: CausalNode) {
    if (node.kind === "response.output" && typeof node.data?.segment_id === "string")
      return `response_segment:${node.data.segment_id}`
    if (node.kind === "prompt.assembly") return `prompt:${node.node_id}`
    if (node.kind === "context.transform" || node.kind === "context.pack") return `context:${node.node_id}`
    if (node.kind === "response.claim") return `response_claim:${node.node_id}`
    if (node.kind === "evidence.semantic_fact" || node.kind === "evidence.fact") return `evidence:${node.node_id}`
    if (node.kind === "execution.observation" || node.kind === "observation") return `observation:${node.node_id}`
    return `node:${node.node_id}`
  }

  private provenanceTypeForNode(node: CausalNode) {
    if (node.kind === "response.output") return "response_segment"
    if (node.kind === "response.claim") return "response_claim"
    if (node.kind === "evidence.semantic_fact" || node.kind === "evidence.fact") return "evidence"
    if (node.kind === "execution.observation" || node.kind === "observation") return "observation"
    return "node"
  }

  private traceHealth(records: ProvenanceRecord[]): TraceHealthMetrics {
    const issues: TraceHealthIssue[] = []
    const circularReferenceMarkers = countCircularMarkers(records)
    const openRecords = records.filter((record) => record.status === "running")
    const finalizedOpenRecords = records.filter((record) => record.data?.finalized_status === "finalized_without_close")
    const caseCompletedSuccessfully = records.some(
      (record) => record.event_type === "case.completed" && record.status === "success",
    )
    const cancelledAfterCaseCompletionRecords = caseCompletedSuccessfully
      ? finalizedOpenRecords.filter(
          (record) => record.status === "cancelled" && record.data?.finalized_reason === "trace_cancelled",
        )
      : []
    const closedAfterCaseCompletionRecords = records.filter(
      (record) => record.data?.finalized_status === "closed_after_case_completion",
    )
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
      (record) =>
        (record.status === "success" || record.status === "cancelled") && expectedFinalizedTypes.has(record.event_type),
    )
    const unexpectedClosedAfterCaseCompletionRecords = closedAfterCaseCompletionRecords.filter(
      (record) => !expectedFinalizedTypes.has(record.event_type),
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
    if (cancelledAfterCaseCompletionRecords.length) {
      issues.push({
        kind: "cancelled_after_case_completion",
        severity: "warning",
        message: "Records were marked cancelled even though the case had already completed successfully.",
        count: cancelledAfterCaseCompletionRecords.length,
        refs: cancelledAfterCaseCompletionRecords.slice(0, 10).map((record) => `node:${record.record_id}`),
      })
    }
    if (unexpectedClosedAfterCaseCompletionRecords.length) {
      issues.push({
        kind: "closed_after_case_completion",
        severity: "warning",
        message:
          "Unexpected record types were still running after the final case response and were closed during service shutdown.",
        count: unexpectedClosedAfterCaseCompletionRecords.length,
        refs: unexpectedClosedAfterCaseCompletionRecords.slice(0, 10).map((record) => `node:${record.record_id}`),
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
    const isExpectedCancelledLlmTurn = (record: ProvenanceRecord) =>
      record.status === "cancelled" && record.data?.finalized_reason === "trace_cancelled"
    const isClosedAfterCaseCompletionLlmTurn = (record: ProvenanceRecord) =>
      record.data?.finalized_status === "closed_after_case_completion"
    const llmTurnsRequiringCompletionMetadata = llmTurns
      .filter((record) => !isExpectedCancelledLlmTurn(record))
      .filter((record) => !isClosedAfterCaseCompletionLlmTurn(record))
    const llmTurnsMissingTokenUsage = llmTurnsRequiringCompletionMetadata.filter(
      (record) => record.data?.agent_role !== "title" && !record.token_usage?.total,
    )
    const llmTurnsMissingFinishReason = llmTurnsRequiringCompletionMetadata.filter(
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
      if (
        record.event_type !== "subagent.call" &&
        record.event_type !== "evidence.fact" &&
        record.event_type !== "evidence.semantic_fact"
      )
        return false
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
    const semanticEvidenceRecords = records.filter(
      (item) => item.event_type === "evidence.semantic_fact" || item.event_type === "evidence.fact",
    )
    const evidenceKeys = new Map<string, number>()
    for (const record of semanticEvidenceRecords) {
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
    const evidenceFacts = semanticEvidenceRecords
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
      const directRefs = record.data?.direct_evidence_refs
      return Array.isArray(legacyRefs) && legacyRefs.length > 0 && (!Array.isArray(directRefs) || !directRefs.length)
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
    const taskObligations = records.filter((record) => record.event_type === "task.obligation")
    const unmetTaskObligations = taskObligations.filter((record) => record.data?.status === "unmet")
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
    for (const record of unmetTaskObligations.slice(0, 20)) {
      issues.push({
        kind: "task_obligation_unmet",
        severity: "warning",
        message: "A user-requested task obligation was not fulfilled by the observed trace.",
        record_id: record.record_id,
        event_type: record.event_type,
        refs: stringArrayField(record.data ?? {}, ["evaluation_refs", "evaluationRefs"]) ?? [],
        metadata: {
          obligation_type: record.data?.obligation_type,
          target_path: record.data?.target_path,
          missing_action: record.data?.missing_action,
        },
      })
    }
    const payloadDuplicationGroups = this.artifacts.filter((artifact) => (artifact.occurrences ?? 1) > 1).length
    const rawStreamDeltaEvents = this.events.filter((event) => /delta/i.test(event.event_type)).length
    const compactionRecords = records.filter((record) => record.event_type === "context.compaction")
    const compactionCheckRecords = records.filter((record) => record.event_type === "context.compaction_check")
    const compactionCheckMissing =
      compactionRecords.length && !compactionCheckRecords.length ? compactionRecords.length : 0
    const changeRecords = records.filter((record) => record.event_type === "change")
    const verificationRecords = records.filter((record) => record.event_type === "verification")
    const missingVerificationAfterChange = changeRecords.length && !verificationRecords.length ? changeRecords : []
    const verificationAfterTestChange = verificationRecords.filter((record) =>
      stringArrayField(record.data ?? {}, ["verification_scope_risk_flags", "verificationScopeRiskFlags"])?.some(
        (flag) => flag === "tests_modified_before_verification" || flag === "test_oracle_modified_before_verification",
      ),
    )
    const semanticFactConflictGroups = new Map<string, ProvenanceRecord[]>()
    for (const record of records) {
      if (record.event_type !== "evidence.semantic_fact") continue
      const groupID = typeof record.data?.conflict_group_id === "string" ? record.data.conflict_group_id : undefined
      if (!groupID) continue
      const current = semanticFactConflictGroups.get(groupID) ?? []
      current.push(record)
      semanticFactConflictGroups.set(groupID, current)
    }
    const actionableSemanticConflictGroups = [...semanticFactConflictGroups.values()].filter((group) =>
      group.some((record) => record.data?.conflict_issue === true),
    )
    const lowValueSemanticConflictMembers = [...semanticFactConflictGroups.values()]
      .flat()
      .filter((record) => record.data?.conflict_issue === false)
    const legacyFactUsedInFinalClaim = records.filter(
      (record) =>
        record.event_type === "response.claim" &&
        stringArrayField(record.data ?? {}, ["quality_flags", "qualityFlags"])?.includes("legacy_evidence_used"),
    )
    if (compactionCheckMissing) {
      issues.push({
        kind: "compaction_check_missing",
        severity: "warning",
        message: "Compaction record was observed without a preceding compaction check record.",
        count: compactionCheckMissing,
      })
    }
    if (missingVerificationAfterChange.length) {
      issues.push({
        kind: "missing_verification_after_change",
        severity: "warning",
        message: "Repository changes were recorded but no verification command was observed before finalization.",
        count: missingVerificationAfterChange.length,
        refs: missingVerificationAfterChange
          .slice(0, 10)
          .map((record) => `change:${record.data?.change_id ?? record.record_id}`),
      })
    }
    for (const record of verificationAfterTestChange.slice(0, 20)) {
      issues.push({
        kind: "verification_after_test_change",
        severity: "warning",
        message: "Verification ran after test files or test oracle values were modified.",
        record_id: record.record_id,
        event_type: record.event_type,
        refs: [
          ...(stringArrayField(record.data ?? {}, ["changed_test_refs", "changedTestRefs"]) ?? []),
          ...(stringArrayField(record.data ?? {}, ["changed_production_refs", "changedProductionRefs"]) ?? []),
        ].slice(0, 20),
      })
    }
    for (const [groupID, group] of [...semanticFactConflictGroups.entries()].slice(0, 20)) {
      const conflictKind =
        group.map((record) => record.data?.conflict_kind).find((kind): kind is string => typeof kind === "string") ??
        "conflicting_semantic_fact_group"
      const conflictSeverity =
        group
          .map((record) => record.data?.conflict_severity)
          .find((severity): severity is string => typeof severity === "string") ?? "medium"
      const conflictIssue = group.some((record) => record.data?.conflict_issue === true)
      issues.push({
        kind: "conflicting_semantic_fact_group",
        severity: conflictIssue ? "warning" : "info",
        message: "Semantic facts with the same subject and predicate report conflicting values.",
        count: group.length,
        refs: group.slice(0, 20).map((record) => `evidence:${record.record_id}`),
        metadata: { conflict_group_id: groupID, conflict_kind: conflictKind, conflict_severity: conflictSeverity },
      })
      if (conflictIssue) {
        issues.push({
          kind: conflictKind,
          severity: conflictSeverity === "high" ? "warning" : "info",
          message: `Semantic fact conflict classified as ${conflictKind}.`,
          count: group.length,
          refs: group
            .filter((record) => record.data?.conflict_issue === true)
            .slice(0, 20)
            .map((record) => `evidence:${record.record_id}`),
          metadata: { conflict_group_id: groupID, conflict_kind: conflictKind, conflict_severity: conflictSeverity },
        })
      }
    }
    for (const record of legacyFactUsedInFinalClaim.slice(0, 20)) {
      issues.push({
        kind: "legacy_fact_used_in_final_claim",
        severity: "warning",
        message: "A final response claim used evidence marked as legacy while conflicting evidence was available.",
        record_id: record.record_id,
        event_type: record.event_type,
        refs: [
          ...(stringArrayField(record.data ?? {}, ["direct_evidence_refs", "directEvidenceRefs"]) ?? []),
          ...(stringArrayField(record.data ?? {}, ["conflicting_evidence_refs", "conflictingEvidenceRefs"]) ?? []),
        ].slice(0, 20),
      })
    }
    return {
      circular_reference_markers: circularReferenceMarkers,
      open_records: openRecords.length,
      finalized_open_records: finalizedOpenRecords.length,
      expected_lifecycle_finalized_records: expectedLifecycleFinalizedRecords.length,
      unexpected_missing_close_records: unexpectedMissingCloseRecords.length,
      cancelled_after_case_completion_records: cancelledAfterCaseCompletionRecords.length,
      closed_after_case_completion_records: closedAfterCaseCompletionRecords.length,
      llm_turns_missing_token_usage: llmTurnsMissingTokenUsage.length,
      llm_turns_missing_finish_reason: llmTurnsMissingFinishReason.length,
      compaction_quality_flags: compactionQualityFlags,
      empty_subagent_results: emptySubagentRecords.length,
      broad_response_refs: broadResponses.length,
      duplicate_evidence_facts: duplicateEvidenceFacts,
      duplicate_semantic_facts: duplicateEvidenceFacts,
      generic_evidence_facts: genericEvidenceFacts.length,
      generic_semantic_facts: genericEvidenceFacts.length,
      unsupported_response_claims: unsupportedResponseClaims.length,
      context_only_response_claims: contextOnlyResponseClaims.length,
      payload_duplication_groups: payloadDuplicationGroups,
      compaction_check_missing: compactionCheckMissing,
      missing_verification_after_change: missingVerificationAfterChange.length,
      verification_after_test_change: verificationAfterTestChange.length,
      conflicting_semantic_fact_groups: semanticFactConflictGroups.size,
      actionable_semantic_conflict_groups: actionableSemanticConflictGroups.length,
      low_value_semantic_conflict_members: lowValueSemanticConflictMembers.length,
      legacy_fact_used_in_final_claim: legacyFactUsedInFinalClaim.length,
      task_obligations: taskObligations.length,
      unmet_task_obligations: unmetTaskObligations.length,
      broken_claim_fragments: brokenClaimFragments.length,
      over_attributed_claims: overAttributedClaims.length,
      generic_mcp_facts: genericMcpFacts.length,
      path_only_evidence_facts: pathOnlyEvidenceFacts.length,
      execution_observations: records.filter((record) => record.event_type === "execution.observation").length,
      task_plan_states: records.filter((record) => record.event_type === "task.plan_state").length,
      non_final_response_claims: nonFinalResponseClaims.length,
      weak_evidence_matches: weakEvidenceMatches.length,
      mcp_json_parse_shadowed: mcpJsonParseShadowed.length,
      skill_request_unresolved: unresolvedSkillRequests.length,
      background_llm_turns: backgroundLlmTurns.length,
      legacy_context_ref_claims: legacyContextRefClaims.length,
      raw_stream_delta_events: rawStreamDeltaEvents,
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

  summarizeText(input: unknown, label = "text"): TraceFieldSummary {
    const text = textForSummary(input)
    const summary: TraceFieldSummary = {
      type: "text",
      length: text.length,
      hash: hash(text),
      preview: text.slice(0, maxFieldLength()),
    }
    if (text.length <= maxFieldLength()) return summary
    const artifact = this.writeArtifact("text", label, text)
    if (!artifact) return { ...summary, artifact_status: "write_failed" }
    return {
      ...summary,
      artifact_id: artifact.artifact_id,
      payload_ref: artifact.artifact_id,
      payload_dedupe_group_id: artifact.dedupe_key,
    }
  }

  summarizeJson(input: unknown, label = "json", forceArtifact = false): TraceFieldSummary {
    if (input === null || typeof input !== "object") return summarizeScalar(input)
    const serialized = json(input, label)
    const summary: TraceFieldSummary = {
      type: Array.isArray(input) ? "array" : "object",
      length: serialized.length,
      hash: hash(serialized),
      preview: serialized.slice(0, maxFieldLength()),
      ...(Array.isArray(input) ? {} : { keys: Object.keys(input as Record<string, unknown>).slice(0, 50) }),
    }
    if (!forceArtifact && serialized.length <= maxFieldLength()) return summary
    const artifact = this.writeArtifact("json", label, prettyJsonString(serialized))
    if (!artifact) return { ...summary, artifact_status: "write_failed" }
    return {
      ...summary,
      artifact_id: artifact.artifact_id,
      payload_ref: artifact.artifact_id,
      payload_dedupe_group_id: artifact.dedupe_key,
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
    if (isTokenUsageContainer(label.split(".").at(-1) ?? "")) return sanitizeTokenUsage(input)
    if (input === null) return null
    if (typeof input === "string") {
      if (input.length > maxFieldLength() && shouldExternalizeCausalContainer(label)) {
        return this.summarizeText(input, this.causalArtifactLabel(label))
      }
      return input.length > maxFieldLength() ? this.summarizeText(input, this.causalArtifactLabel(label)) : input
    }
    if (typeof input === "number" || typeof input === "boolean") return input
    if (Array.isArray(input)) {
      const serialized = json(input, label)
      if (serialized.length > maxFieldLength() && shouldExternalizeCausalContainer(label)) {
        return this.summarizeJson(input, this.causalArtifactLabel(label))
      }
      if (isTraceFieldSummary(input)) return input
      if (isStructuredCausalContainer(label) && exceedsStructuredCausalCollectionLimit(input)) {
        return this.summarizeJson(input, this.causalArtifactLabel(label), true)
      }
      if (serialized.length > maxFieldLength() && !isStructuredCausalContainer(label)) {
        return this.summarizeJson(input, this.causalArtifactLabel(label))
      }
      return input.map((item, index) => this.summarizeCausalValue(item, `${label}.${index}`))
    }
    if (typeof input === "object") {
      const serialized = json(input, label)
      if (serialized.length > maxFieldLength() && shouldExternalizeCausalContainer(label)) {
        return this.summarizeJson(input, this.causalArtifactLabel(label))
      }
      if (isTraceFieldSummary(input)) return input
      if (isStructuredCausalContainer(label) && exceedsStructuredCausalCollectionLimit(input)) {
        return this.summarizeJson(input, this.causalArtifactLabel(label), true)
      }
      if (serialized.length > maxFieldLength() && !isStructuredCausalContainer(label)) {
        return this.summarizeJson(input, this.causalArtifactLabel(label))
      }
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

  private writeCompactionSummaryArtifact(input: unknown) {
    if (typeof input === "string") return this.writeArtifact("text", "compaction.output_summary", input)?.artifact_id
    return this.writeArtifact("json", "compaction.output_summary", prettyJsonString(json(input)))?.artifact_id
  }

  private normalizeSourceRefs(input: string[] | undefined) {
    const provided = input ?? []
    const carriedTemporalRefs = this.temporalSourceRefs(input)
    const concreteProvided = dedupeStrings(provided.filter((item) => !item.startsWith("recent_")))
    let temporalRefs = carriedTemporalRefs
    if (!provided.length || provided.some((item) => item.startsWith("recent_"))) {
      temporalRefs = dedupeStrings([
        ...carriedTemporalRefs,
        ...this.currentSourceRefs().filter((ref) => !concreteProvided.includes(ref)),
      ])
    }
    if (temporalRefs.length) this.setTemporalSourceRefs(concreteProvided, temporalRefs)
    return concreteProvided
  }

  private temporalSourceRefs(selection: string[] | undefined) {
    return selection ? (this.temporalSourceRefsBySelection.get(selection) ?? []) : []
  }

  private setTemporalSourceRefs(selection: string[], refs: string[]) {
    this.temporalSourceRefsBySelection.set(selection, dedupeStrings(refs))
    return selection
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
    const temporalAdvisoryRefs = this.temporalSourceRefs(sourceRefs)
    const checkID = input.check_id ?? semanticID("compactioncheck", this.causalNodes.length + 1)
    const usage = input.token_usage ? normalizeTokenUsage(input.token_usage) : undefined
    const previousContextRecord = [...this.causalNodes]
      .reverse()
      .find((node) => node.kind === "context.compaction_check" || node.kind === "context.compaction")
    if (
      input.overflow === false &&
      previousContextRecord?.kind === "context.compaction_check" &&
      previousContextRecord.data?.overflow === false &&
      firstStringField(previousContextRecord.data, ["session_id", "sessionID"]) === input.session_id &&
      firstStringField(previousContextRecord.data, ["model_id", "modelID"]) === input.model_id &&
      firstStringField(previousContextRecord.data, ["selected_algorithm", "selectedAlgorithm"]) ===
        input.selected_algorithm
    ) {
      const previousData = previousContextRecord.data ?? {}
      previousContextRecord.data = omitUndefined({
        ...previousData,
        check_count: (optionalNumber(previousData.check_count) ?? 1) + 1,
        aggregated_noop_checks: true,
        first_token_estimate:
          optionalNumber(previousData.first_token_estimate) ?? optionalNumber(previousData.token_estimate),
        last_check_id: checkID,
        message_id: input.message_id ?? previousData.message_id,
        token_usage: usage ?? previousData.token_usage,
        token_estimate: input.token_estimate,
        context_limit: input.context_limit,
        reserved_tokens: input.reserved_tokens,
        trigger_reason: input.trigger_reason ?? previousData.trigger_reason,
        metadata: input.metadata ?? previousData.metadata,
      })
      previousContextRecord.source_refs = mergeRefs(previousContextRecord.source_refs, sourceRefs)
      previousContextRecord.artifact_refs = this.collectArtifactRefs(previousContextRecord.data)
      this.causalIR.updateNode(previousContextRecord)
      this.createTemporalAdvisoryEdges(previousContextRecord, temporalAdvisoryRefs)
      this.writePartial()
      return previousContextRecord
    }
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
      check_count: 1,
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

  private sourceNodeForRef(ref: string) {
    const parsed = this.parseSourceRef(ref)
    if (!parsed) return undefined
    if (parsed.type === "evidence") return this.evidenceNodeForRef(ref)
    if (parsed.type === "observation" || parsed.type === "node") {
      return this.causalNodes.find((node) => node.node_id === parsed.id)
    }
    if (parsed.type === "tool_error" || parsed.type === "tool_result") {
      const expectedKind = parsed.type === "tool_error" ? "tool.error" : "tool.result"
      return this.causalNodes.find(
        (node) =>
          node.kind === expectedKind &&
          (node.source_refs?.includes(ref) ||
            firstStringField(node.data, ["call_id", "callID"]) === parsed.id ||
            node.node_id.endsWith(`_${safeNodeIDPart(parsed.id)}`)),
      )
    }
    if (parsed.type === "change" || parsed.type === "verification") {
      const expectedKind = parsed.type
      const identityKeys = parsed.type === "change" ? ["change_id", "changeID"] : ["verification_id", "verificationID"]
      return this.causalNodes.find(
        (node) =>
          node.kind === expectedKind &&
          (node.node_id === parsed.id || firstStringField(node.data, identityKeys) === parsed.id),
      )
    }
    return this.causalNodes.find((node) => node.source_refs?.includes(ref) || node.node_id === parsed.id)
  }

  private toolOutcomeRefsFromSourceRefs(refs: string[]) {
    const output = new Set<string>()
    const seen = new Set<string>()
    const visit = (ref: string, depth: number) => {
      if (depth > 4 || seen.has(ref)) return
      seen.add(ref)
      if (isToolOutcomeRef(ref)) {
        output.add(ref)
        return
      }
      const node = this.sourceNodeForRef(ref)
      if (!node) return
      for (const sourceRef of node.source_refs ?? []) visit(sourceRef, depth + 1)
      const dataRefs =
        Array.isArray(node.data?.tool_outcome_refs) &&
        node.data.tool_outcome_refs.every((item) => typeof item === "string")
          ? (node.data.tool_outcome_refs as string[])
          : []
      for (const sourceRef of dataRefs) visit(sourceRef, depth + 1)
      const callID = firstStringField(node.data, ["call_id", "callID"])
      if (callID) {
        const sessionID = firstStringField(node.data, ["session_id", "sessionID"])
        const outcomes = this.toolOutcomeRefsByCallID.get(callID) ?? []
        const outcomeRef = outcomes.findLast((item) => !sessionID || item.sessionID === sessionID)?.sourceRef
        if (outcomeRef) output.add(outcomeRef)
      }
    }
    for (const ref of refs) visit(ref, 0)
    return [...output]
  }

  private verificationCandidateSemantics(
    ref: string,
    node: CausalNode | undefined,
    claimText: string,
  ): {
    verificationRefs?: string[]
    repositoryRevision?: number
    verificationPhase?: TraceVerificationRecord["verification_phase"]
    verificationStatus?: TraceVerificationRecord["status"]
    effectiveForFinalState?: boolean
    temporalRole?: VerificationFactProvenance["verification_temporal_role"]
    temporallyEligible: boolean
    rejectionReason?: ClaimGroundingDecision["rejection_reason"]
  } {
    const data = node?.data ?? {}
    const factKind = typeof data.fact_kind === "string" ? data.fact_kind : undefined
    const embeddedRefs = stringArrayField(data, ["verification_refs", "verificationRefs"]) ?? []
    const isVerificationCandidate =
      factKind === "verification_output" || ref.startsWith("verification:") || embeddedRefs.length > 0
    if (!isVerificationCandidate) return { temporallyEligible: true }

    const verificationRefs = dedupeStrings([
      ...(ref.startsWith("verification:") ? [ref] : []),
      ...embeddedRefs,
    ])
    const repositoryRevision = optionalNumber(
      data.verification_repository_revision ?? data.repository_revision,
    )
    const phaseValue = firstStringField(data, ["verification_phase", "verificationPhase"])
    const verificationPhase = ["baseline", "post_change", "post_test_change", "unknown"].includes(
      phaseValue ?? "",
    )
      ? (phaseValue as TraceVerificationRecord["verification_phase"])
      : undefined
    const statusValue =
      firstStringField(data, ["verification_status", "verificationStatus", "status"]) ?? node?.status
    const verificationStatus = ["passed", "failed", "unknown"].includes(statusValue ?? "")
      ? (statusValue as TraceVerificationRecord["status"])
      : undefined
    const effectiveValue = data.verification_effective_for_final_state ?? data.effective_for_final_state
    const effectiveForFinalState =
      typeof effectiveValue === "boolean" ? effectiveValue : undefined
    const roleValue = firstStringField(data, ["verification_temporal_role", "verificationTemporalRole"])
    const temporalRole = ["current_effective", "superseded", "unknown"].includes(roleValue ?? "")
      ? (roleValue as VerificationFactProvenance["verification_temporal_role"])
      : effectiveForFinalState === false
        ? "superseded"
        : effectiveForFinalState === true && repositoryRevision === this.repositoryRevision
          ? "current_effective"
          : undefined
    const expectedStatus = verificationClaimExpectedStatus(claimText)
    const temporalScope = responseClaimTemporalScope(claimText, this.repositoryRevision)
    let rejectionReason: ClaimGroundingDecision["rejection_reason"]
    if (temporalScope === "historical") {
      if (effectiveForFinalState === true && repositoryRevision === this.repositoryRevision) {
        rejectionReason = "verification_revision_mismatch"
      } else if (expectedStatus && verificationStatus && verificationStatus !== expectedStatus) {
        rejectionReason = "verification_status_mismatch"
      }
    } else if (effectiveForFinalState === false || temporalRole === "superseded") {
      rejectionReason = "superseded_verification"
    } else if (repositoryRevision !== undefined && repositoryRevision !== this.repositoryRevision) {
      rejectionReason = "verification_revision_mismatch"
    } else if (expectedStatus && verificationStatus && verificationStatus !== expectedStatus) {
      rejectionReason = "verification_status_mismatch"
    }
    return {
      verificationRefs: verificationRefs.length ? verificationRefs : undefined,
      repositoryRevision,
      verificationPhase,
      verificationStatus,
      effectiveForFinalState,
      temporalRole,
      temporallyEligible: rejectionReason === undefined,
      rejectionReason,
    }
  }

  private matchEvidenceForClaim(
    claimText: unknown,
    evidenceRefs: string[],
    generationGroundingCandidateRefs: string[] = [],
  ) {
    const candidateRefs = dedupeStrings(evidenceRefs)
    const generationCandidateSet = new Set(generationGroundingCandidateRefs)
    const claimPreview = stringPreview(claimText, 2000)
    const isVerificationClaim = isVerificationClaimText(claimPreview)
    const isChangeClaim = isChangeClaimText(claimPreview)
    const mentionsToolFailure = isToolFailureClaimText(claimPreview)
    const analyzed = candidateRefs
      .map((ref) => {
        const node = this.sourceNodeForRef(ref)
        const analysis = this.sourceRefMatchAnalysis(ref, claimText, node)
        const verification = this.verificationCandidateSemantics(ref, node, stringPreview(claimText, 2000))
        return {
          ref,
          score: analysis.score,
          reasons: analysis.reasons,
          weak: analysis.weak,
          factKind: typeof node?.data?.fact_kind === "string" ? node.data.fact_kind : undefined,
          verification,
        }
      })
    const hasScopedVerificationCandidate = analyzed.some(
      (item) =>
        item.verification.temporallyEligible &&
        Boolean(item.verification.verificationRefs?.length) &&
        (item.verification.repositoryRevision !== undefined ||
          item.verification.effectiveForFinalState !== undefined ||
          item.verification.verificationStatus !== undefined),
    )
    if (isVerificationClaim && hasScopedVerificationCandidate) {
      for (const item of analyzed) {
        if (item.factKind !== "verification_output" || item.verification.verificationRefs?.length) continue
        item.verification.temporallyEligible = false
        item.verification.rejectionReason = "unscoped_verification_candidate"
      }
    }
    const scored = analyzed
      .filter((item) => item.verification.temporallyEligible && item.score >= 0.35)
      .sort((a, b) => b.score - a.score)
    let preferred =
      isVerificationClaim && !mentionsToolFailure && scored.some((item) => item.factKind === "verification_output")
        ? scored.filter((item) => item.factKind === "verification_output")
        : scored
    let selectionLimit = 3
    if (isChangeClaim && scored.some((item) => item.ref.startsWith("change:"))) {
      const changeCandidates = scored.filter((item) => item.ref.startsWith("change:"))
      if (isVerificationClaim) {
        const verificationCandidates = scored.filter(
          (item) => item.factKind === "verification_output" || item.ref.startsWith("verification:"),
        )
        const remainingCandidates = scored.filter(
          (item) =>
            !item.ref.startsWith("change:") &&
            item.factKind !== "verification_output" &&
            !item.ref.startsWith("verification:"),
        )
        const seen = new Set<string>()
        preferred = [
          ...changeCandidates.slice(0, 1),
          ...verificationCandidates.slice(0, 1),
          ...remainingCandidates,
          ...changeCandidates.slice(1),
          ...verificationCandidates.slice(1),
        ].filter((item) => {
          if (seen.has(item.ref)) return false
          seen.add(item.ref)
          return true
        })
        selectionLimit = 4
      } else {
        preferred = [
          ...changeCandidates,
          ...scored.filter((item) => !item.ref.startsWith("change:")),
        ]
      }
    }
    const refs = preferred.slice(0, selectionLimit).map((item) => item.ref)
    const score = preferred[0]?.score ?? 0
    const reasons = dedupeStrings(preferred.flatMap((item) => item.reasons))
    const weak = preferred.slice(0, selectionLimit).some((item) => item.weak)
    const selectedRefSet = new Set(refs)
    const decisions: ClaimGroundingDecision[] = analyzed.map((item) => {
      const selected = selectedRefSet.has(item.ref)
      const aboveThreshold = item.score >= 0.35
      return {
        candidate_ref: item.ref,
        candidate_origin: generationCandidateSet.has(item.ref)
          ? "confirmed_generation_context"
          : "explicit_response_source",
        decision: selected
          ? "selected_direct_support"
          : item.verification.rejectionReason
            ? "rejected_inapplicable"
            : aboveThreshold
              ? "rejected_lower_ranked_match"
              : "rejected_no_match",
        score: item.score,
        reasons: item.reasons,
        rejection_reason: selected
          ? undefined
          : item.verification.rejectionReason
            ? item.verification.rejectionReason
            : aboveThreshold
              ? "lower_ranked_match"
              : "semantic_match_below_threshold",
        candidate_verification_refs: item.verification.verificationRefs,
        candidate_repository_revision: item.verification.repositoryRevision,
        candidate_verification_phase: item.verification.verificationPhase,
        candidate_verification_status: item.verification.verificationStatus,
        candidate_effective_for_final_state: item.verification.effectiveForFinalState,
        candidate_temporal_role: item.verification.temporalRole,
        candidate_temporally_eligible: item.verification.temporallyEligible,
        attribution_eligible: selected,
        agent_attention_observed: false,
        behavior_impact: "none",
      }
    })
    return {
      refs,
      score,
      reasons,
      weak,
      decisions,
      candidateRefs,
      strategy: refs.length ? "structured_text_overlap" : candidateRefs.length ? "no_direct_match" : "no_evidence_refs",
    }
  }

  private sourceRefMatchAnalysis(ref: string, claimText: unknown, node: CausalNode | undefined) {
    if (ref.startsWith("tool_error:")) return toolOutcomeMatchAnalysis("tool_error", claimText, node?.data)
    if (ref.startsWith("tool_result:")) return toolOutcomeMatchAnalysis("tool_result", claimText, node?.data)
    return evidenceMatchAnalysis(claimText, node?.data)
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
          ? "Semantic evidence supported response output"
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
          ? parsed.type === "tool_error"
            ? "Tool failure observation supports response claim"
            : parsed.type === "tool_result"
              ? "Tool result observation supports response claim"
              : "Semantic evidence supports response claim"
          : relation === "context_to_claim"
            ? "Context record contextualizes response claim"
            : "Execution record was used for response claim",
    })
  }

  private linkSupersededSourceToClaim(ref: string, claimNodeID: string) {
    const parsed = this.parseSourceRef(ref)
    if (!parsed) return
    this.causalEdge({
      from: parsed,
      to: { type: "response_claim", id: claimNodeID, label: "response.claim" },
      relation: "context_to_claim",
      evidence_tier: "temporal_advisory",
      eligible_for_attribution: false,
      derivation_method: "verification_temporal_applicability_v1",
      label: "Superseded verification evidence contextualizes but does not support the current claim",
      metadata: {
        causal_semantics: "superseded_verification_context",
        behavior_impact: "none",
      },
    })
  }

  private writeArtifact(kind: TraceArtifact["kind"], label: string, content: string): TraceArtifact | undefined {
    const redacted = redactText(content)
    let storedContent = redacted
    let storageEncoding: TraceArtifact["storage_encoding"] = "identity"
    if (kind === "json") {
      try {
        storedContent = JSON.stringify(JSON.parse(redacted))
        storageEncoding = "json_minified"
      } catch {}
    }
    storedContent = validUtf8(storedContent)
    const contentHash = hash(storedContent)
    const semanticContent = unicodePrefix(storedContent, maxFieldLength())
    const dedupeKey = `${kind}:${contentHash}`
    const existing = this.artifactByDedupeKey.get(dedupeKey)
    if (existing) {
      existing.occurrences = (existing.occurrences ?? 1) + 1
      this.causalIR.reuseArtifact(existing)
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
      length: storedContent.length,
      hash: contentHash,
      preview: storedContent.slice(0, maxFieldLength()),
      created_at: nowIso(),
      dedupe_key: dedupeKey,
      occurrences: 1,
      storage_encoding: storageEncoding,
      original_length: Buffer.byteLength(redacted),
      stored_length: Buffer.byteLength(storedContent),
      availability: "bundled",
      content_hash: contentHash,
      byte_length: Buffer.byteLength(storedContent),
      semantic_slices: [
        {
          byte_range: [0, Buffer.byteLength(semanticContent)],
          content: semanticContent,
          hash: hash(semanticContent),
          truncated: semanticContent.length < storedContent.length,
        },
      ],
    }
    const target = path.join(this.caseDir, relativePath)
    const temp = `${target}.tmp-${process.pid}-${crypto.randomUUID()}`
    try {
      fs.mkdirSync(path.dirname(target), { recursive: true })
      fs.writeFileSync(temp, storedContent)
      fs.renameSync(temp, target)
      this.causalIR.createArtifact(artifact)
      this.artifactByDedupeKey.set(dedupeKey, artifact)
      this.write("artifact.write", artifact)
      return artifact
    } catch {
      try {
        fs.unlinkSync(temp)
      } catch {}
      this.causalIR.createDiagnostic({
        diagnostic_id: `artifact_write_failed_${++this.diagnosticSequence}_${contentHash}`,
        kind: "artifact_write_failed",
        level: "warning",
        status: "write_failed",
        artifact_id: artifactID,
        expected_path: relativePath,
        message: "Artifact content could not be persisted; no artifact link was registered.",
      })
      this.writePartial()
      return undefined
    }
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

  private writeCausalIRRecord(entry: CausalIRJournalEntry) {
    if (!this.writable) return false
    try {
      fs.appendFileSync(this.recordsFile, JSON.stringify(entry) + "\n")
      return true
    } catch {
      return false
    }
  }

  private writePartial(force = false, summary?: CausalIRTraceSummary, checkpoint = force) {
    if (!this.writable) return
    const now = Date.now()
    const interval = safeNumber(process.env.OPENCODE_CASE_TRACE_PARTIAL_INTERVAL_MS || 5000) || 5000
    if (!force && now < this.nextPartialWrite) return
    this.nextPartialWrite = now + interval
    const causalIR = summary ?? this.causalIRSummary("running")
    this.safeWrite(this.partialFile, jsonPretty(causalIR))
    this.safeWrite(this.htmlFile, renderProvenanceTraceHtml(this.projectProvenanceSummary(causalIR)))
    if (checkpoint) this.causalIR.checkpoint(causalIR)
  }

  private safeWrite(target: string, content: string) {
    const temporary = path.join(
      path.dirname(target),
      `.${path.basename(target)}.${process.pid}.${crypto.randomUUID()}.tmp`,
    )
    try {
      fs.mkdirSync(path.dirname(target), { recursive: true })
      fs.writeFileSync(temporary, content)
      fs.renameSync(temporary, target)
    } catch {
      try {
        fs.unlinkSync(temporary)
      } catch {}
    }
  }

  private safeLinkOrWrite(source: string, target: string, fallbackContent: string) {
    const temporary = path.join(
      path.dirname(target),
      `.${path.basename(target)}.${process.pid}.${crypto.randomUUID()}.link`,
    )
    try {
      fs.mkdirSync(path.dirname(target), { recursive: true })
      fs.linkSync(source, temporary)
      fs.renameSync(temporary, target)
      return
    } catch {
      try {
        fs.unlinkSync(temporary)
      } catch {}
    }
    this.safeWrite(target, fallbackContent)
  }
}

function jsonPretty(input: unknown) {
  return JSON.stringify(sanitizeForJson(normalizeTemporalReferences(input).value), undefined, 2)
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
  current.flushForSignal(signal)
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
    process.prependOnceListener(signal, () => {
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
    installProcessFinalizer()
    active = new ActiveCaseTrace({ ...input, caseID })
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
