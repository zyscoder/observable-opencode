import type { TraceComponent } from "./case-trace"

export const TRACE_VERSION = "6.0" as const

export const FORMAL_RECORD_TYPES = [
  "run.start",
  "process.signal",
  "case.completed",
  "case.failed",
  "case.observed_defect",
  "case.missing_semantic",
  "task.loop",
  "task.plan_state",
  "task.obligation",
  "prompt.assembly",
  "context.transform",
  "context.pack",
  "context.compaction_check",
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
  "observation",
  "execution.observation",
  "evidence.fact",
  "evidence.semantic_fact",
  "change",
  "verification",
  "response.output",
  "response.claim",
  "claim.support_assessment",
  "design.record",
] as const

export type FormalRecordType = (typeof FORMAL_RECORD_TYPES)[number]

export const FORMAL_DATAFLOW_RELATIONS = [
  "selected_into_context",
  "prompted",
  "produced",
  "consumed",
  "compressed_from",
  "compressed_to",
  "spawned",
  "continued_from",
  "derived_from",
  "verified_by",
  "modified_by",
  "failed_before",
  "motivated_by_evidence",
  "read_from",
  "returned_by",
  "submitted",
  "assembled",
  "transformed_to",
  "resolved_to",
  "used_as_context",
  "selected_by",
  "called",
  "returned_to",
  "delegated_to",
  "reported_to",
  "supported_response",
  "claimed_by",
  "supports_claim",
  "contextualizes_claim",
  "executed_for_claim",
] as const

export type FormalDataflowRelation = (typeof FORMAL_DATAFLOW_RELATIONS)[number]

const FORMAL_RECORD_TYPE_SET = new Set<string>(FORMAL_RECORD_TYPES)
const FORMAL_RELATION_SET = new Set<string>(FORMAL_DATAFLOW_RELATIONS)

export const RELATION_MIGRATIONS: Readonly<Record<string, FormalDataflowRelation>> = Object.freeze({
  tool_to_change: "modified_by",
  tool_to_observation: "produced",
  failure_to_change: "motivated_by_evidence",
  change_to_verification: "verified_by",
  context_to_llm: "prompted",
  compaction_to_context: "compressed_to",
  prompt_to_context: "used_as_context",
  prompt_to_message: "assembled",
  message_to_context: "used_as_context",
  context_transform: "transformed_to",
  context_to_context: "transformed_to",
  context_to_provider: "transformed_to",
  decision_to_tool: "selected_by",
  decision_to_task: "selected_by",
  tool_call_to_span: "called",
  subagent_to_parent: "reported_to",
  parent_to_subagent: "delegated_to",
  source_to_response: "consumed",
  evidence_to_response: "supported_response",
  response_to_claim: "claimed_by",
  evidence_to_claim: "supports_claim",
  context_to_claim: "contextualizes_claim",
  execution_to_claim: "executed_for_claim",
  source_to_observation: "derived_from",
  source_to_compaction: "compressed_from",
  compaction_to_observation: "derived_from",
  evidence_to_compaction: "compressed_from",
  context_to_compaction_summary: "compressed_to",
  llm_to_tool: "prompted",
  processor_to_final_response: "produced",
  final_response_to_design_record: "produced",
  evidence_to_observation: "derived_from",
})

const ACTION_AFFECTING_RUNTIME_EVENTS = new Set([
  "permission.request",
  "permission.result",
  "permission.denied",
  "approval.request",
  "approval.result",
  "tool.blocked",
  "tool.skipped",
  "turn.error",
])

export function isFormalRecordType(type: string): type is FormalRecordType {
  return FORMAL_RECORD_TYPE_SET.has(type)
}

export function isFormalDataflowRelation(relation: string): relation is FormalDataflowRelation {
  return FORMAL_RELATION_SET.has(relation)
}

export function normalizeRelationDetails(relation: string) {
  const migrated = RELATION_MIGRATIONS[relation]
  if (migrated) return { original: relation, normalized: migrated, known: true }
  if (isFormalDataflowRelation(relation)) return { original: relation, normalized: relation, known: true }
  return { original: relation, normalized: "derived_from" as const, known: false }
}

export function normalizeRelation(relation: string): FormalDataflowRelation {
  return normalizeRelationDetails(relation).normalized
}

export function shouldPromoteRuntimeEvent(component: TraceComponent, eventType: string, data?: unknown): boolean {
  if (component !== "runtime" && component !== "prompt" && component !== "processor") return false
  if (ACTION_AFFECTING_RUNTIME_EVENTS.has(eventType)) return true
  if (eventType === "tool-input-delta") return false
  if (/delta$/i.test(eventType) || /text-delta/i.test(eventType) || /token/i.test(eventType)) return false
  if (component === "prompt" && /^prompt\.parts\./.test(eventType)) return false
  if (component === "prompt" && eventType === "user.message.created") return false
  if (component === "processor" && eventType === "tool.call") return false
  if (isEmptyToolOverrideOnly(data)) return false
  return false
}

function isEmptyToolOverrideOnly(data: unknown) {
  if (!data || typeof data !== "object") return false
  const value = data as Record<string, unknown>
  const keys = Object.keys(value)
  if (!keys.length) return false
  return keys.every((key) => {
    if (key === "tool_overrides") return isEmptyObject(value[key])
    if (key === "part_count") return true
    if (key === "part_types") return true
    return false
  })
}

function isEmptyObject(input: unknown) {
  return Boolean(input && typeof input === "object" && !Array.isArray(input) && Object.keys(input).length === 0)
}
