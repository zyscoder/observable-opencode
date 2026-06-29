import type { TraceComponent } from "./case-trace"

export const TRACE_VERSION = "4.0" as const

export const FORMAL_RECORD_TYPES = [
  "run.start",
  "task.loop",
  "context.pack",
  "context.compaction",
  "llm.call",
  "tool.call",
  "mcp.call",
  "skill.load",
  "subagent.call",
  "observation",
  "change",
  "verification",
  "response.output",
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
  "read_from",
  "returned_by",
] as const

export type FormalDataflowRelation = (typeof FORMAL_DATAFLOW_RELATIONS)[number]

const FORMAL_RECORD_TYPE_SET = new Set<string>(FORMAL_RECORD_TYPES)
const FORMAL_RELATION_SET = new Set<string>(FORMAL_DATAFLOW_RELATIONS)

const RELATION_MIGRATIONS: Record<string, FormalDataflowRelation> = {
  tool_to_change: "modified_by",
  tool_to_observation: "produced",
  failure_to_change: "failed_before",
  change_to_verification: "verified_by",
  context_to_llm: "prompted",
  compaction_to_context: "compressed_to",
  source_to_response: "consumed",
  source_to_observation: "derived_from",
  source_to_compaction: "compressed_from",
  compaction_to_observation: "derived_from",
  evidence_to_compaction: "compressed_from",
  context_to_compaction_summary: "compressed_to",
  llm_to_tool: "prompted",
  processor_to_final_response: "produced",
  final_response_to_design_record: "produced",
  evidence_to_observation: "derived_from",
}

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

export function normalizeRelation(relation: string): FormalDataflowRelation {
  const migrated = RELATION_MIGRATIONS[relation]
  if (migrated) return migrated
  if (FORMAL_RELATION_SET.has(relation)) return relation as FormalDataflowRelation
  return "derived_from"
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
