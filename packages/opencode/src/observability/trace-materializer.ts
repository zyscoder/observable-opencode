import { Database } from "bun:sqlite"
import crypto from "node:crypto"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import {
  CAUSAL_IR_VERSION,
  CausalIRJournalValidationError,
  canonicalCausalIREdge,
  canonicalCausalIRNode,
  causalIRLegacyRef,
  causalIRPayloadHash,
  projectProvenanceTrace,
  typedCausalIRReference,
  validateCausalIRJournal,
  type ArtifactLike,
  type CausalIREdge,
  type CausalIRJournalEntry,
  type CausalIRNode,
  type CausalNodeLike,
  type CausalIRRef,
  type CausalIRRuntimeCloseData,
  type CausalIRStoreSnapshot,
  type CausalIRDiagnosticLike,
} from "./causal-ir"
import { atomizeResponseClaims } from "./claim-atomization"
import {
  streamingJsonArray,
  streamingJsonRawChunks,
  streamingJsonRawItem,
  writeStreamingJsonObjectAtomic,
  type StreamingJsonObjectMember,
} from "./streaming-json-writer"
import { isFormalRecordType, TRACE_VERSION } from "./trace-semantic-contract"
import {
  readTraceSessionManifest,
  resolveTraceManifestPath,
  traceSessionLockRootForCase,
  TRACE_MANIFEST_LOCK_KEY,
  withTraceSessionLock,
  type TraceSegmentDescriptor,
  type TraceSessionManifest,
} from "./trace-segment"

export type TraceMaterializationResult = {
  caseDir: string
  traceFile: string
  manifestFile: string
  partialFile: string
  completeness: "complete" | "incomplete"
  recoveredLines: number
}

type JsonRow = { json: string }
type OrderedJsonRow = JsonRow & { ordinal: number }

function legacyFieldSummary(value: unknown) {
  if (value === undefined) return undefined
  if (
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    typeof (value as Record<string, unknown>).type === "string" &&
    typeof (value as Record<string, unknown>).hash === "string" &&
    typeof (value as Record<string, unknown>).preview === "string"
  )
    return value
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean")
    return { type: value === null ? "null" : typeof value, value }
  const serialized = JSON.stringify(value) ?? "null"
  const previewLimit = Math.max(1, Number(process.env.OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH) || 2_000)
  return {
    type: Array.isArray(value) ? "array" : "object",
    length: serialized.length,
    hash: crypto.createHash("sha256").update(serialized).digest("hex").slice(0, 16),
    preview: serialized.slice(0, previewLimit),
    ...(Array.isArray(value) ? {} : { keys: Object.keys(value as Record<string, unknown>).slice(0, 50) }),
  }
}
type KindRow = { kind: string }
type CountRow = { count: number }
type HashRow = { payload_hash: string }
type OwnerRow = { node_id: string }
type LegacyRow = { legacy_ref: string }
type OccurrenceRow = { owner_type: "node" | "edge"; owner_id: string; field: string }
type DiagnosticIDRow = { diagnostic_id: string }
type EntityIDRow = { entity_id: string }

type SegmentReplayScope = {
  key: string
  segmentID: string
  runID: string
  caseID: string
  pathPrefix: string
  namespace: boolean
  idMaps: Record<"node" | "edge" | "artifact" | "diagnostic", Map<string, string>>
  nodeAliases: Map<string, string | null>
}

type TerminalEnvelope = {
  manifest: Record<string, unknown>
  metrics?: Record<string, unknown>
}

type ScopedCausalIREdge = CausalIREdge & {
  scope?: {
    run_id: string
    case_id: string
  }
}

const MATERIALIZER_GC_BYTES = 4 * 1024 * 1024
const MEMORY_WATERMARK_BYTES = 128 * 1024 * 1024

const OPERATIONS = new Set<CausalIRJournalEntry["operation"]>([
  "node.created",
  "node.updated",
  "edge.created",
  "artifact.created",
  "artifact.reused",
  "diagnostic.created",
  "case.checkpointed",
  "case.finalized",
  "case.runtime_closed",
])

function record(input: unknown): Record<string, unknown> | undefined {
  return input && typeof input === "object" && !Array.isArray(input) ? (input as Record<string, unknown>) : undefined
}

function nonemptyString(input: unknown): input is string {
  return typeof input === "string" && input.length > 0
}

function scopedEntityID(scope: SegmentReplayScope, type: "node" | "edge" | "artifact" | "diagnostic", id: string) {
  if (!scope.namespace) return id
  const existing = scope.idMaps[type].get(id)
  if (existing) return existing
  const scoped = `${scope.segmentID}::${type}::${id}`
  scope.idMaps[type].set(id, scoped)
  return scoped
}

function resolvedScopedEntityID(
  scope: SegmentReplayScope,
  type: "node" | "edge" | "artifact" | "diagnostic",
  id: string,
) {
  if (!scope.namespace) return id
  return scope.idMaps[type].get(id)
}

const NODE_ID_KEY_SCHEMES = new Map<string, readonly string[]>([
  ["node_id", ["node", "record"]],
  ["parent_node_id", ["node", "record"]],
  ["record_id", ["record", "node"]],
  ["parent_record_id", ["record", "node"]],
  ["outcome_record_id", ["record", "node"]],
  ["response_node_id", ["node", "response", "response_segment"]],
  ["result_node_id", ["node", "record"]],
  ["design_id", ["design"]],
  ["claim_id", ["claim", "response_claim"]],
  ["claim_node_id", ["claim", "response_claim", "node"]],
  ["segment_id", ["response_segment", "final_response_evidence"]],
  ["source_segment_id", ["response_segment", "final_response_evidence"]],
  ["response_segment_id", ["response_segment", "final_response_evidence"]],
  ["final_response_segment_id", ["response_segment", "final_response_evidence"]],
  ["evidence_id", ["evidence", "repo_fact"]],
  ["fact_id", ["evidence", "repo_fact"]],
  ["verification_id", ["verification"]],
  ["change_id", ["change"]],
  ["observation_id", ["observation"]],
  ["context_id", ["context", "context_snapshot"]],
  ["context_snapshot_id", ["context_snapshot", "context"]],
  ["input_context_snapshot_id", ["context_snapshot", "context"]],
  ["snapshot_id", ["context_snapshot", "context"]],
  ["response_id", ["response", "response_segment", "final_response_evidence"]],
  ["request_id", ["llm_request", "request"]],
  ["message_id", ["message"]],
  ["finality_gate_message_id", ["message"]],
  ["tool_call_id", ["tool_call", "tool_result", "tool_error"]],
  ["tool_result_id", ["tool_result"]],
  ["tool_id", ["tool", "tool_call"]],
  ["skill_id", ["skill"]],
  ["mcp_id", ["mcp", "mcp_call"]],
  ["mcp_call_id", ["mcp_call"]],
  ["span_id", ["span", "tool_span"]],
  ["turn_id", ["llm_turn", "llm"]],
  ["assessment_id", ["claim_support"]],
  ["check_id", ["compaction_check"]],
  ["last_check_id", ["compaction_check"]],
  ["compaction_id", ["compaction"]],
  ["decision_id", ["decision"]],
  ["evaluation_id", ["external_evaluation"]],
  ["gate_id", ["exit_gate"]],
  ["task_id", ["subagent_task"]],
  ["constraint_id", ["constraint"]],
  ["ledger_id", ["ledger"]],
  ["lifecycle_id", ["lifecycle"]],
  ["obligation_id", ["obligation"]],
  ["runtime_id", ["runtime"]],
])
const EDGE_ID_KEYS = new Set(["edge_id"])
const ARTIFACT_ID_KEYS = new Set(["artifact_id", "output_artifact_id", "summary_artifact_id"])
const DIAGNOSTIC_ID_KEYS = new Set(["diagnostic_id"])
const REFERENCE_KEYS = new Set([
  "reference",
  "references",
  "typed_ref",
  "from",
  "to",
  "input_ref",
  "input_refs",
  "output_ref",
  "output_refs",
  "source_ref",
  "source_refs",
  "artifact_ref",
  "artifact_refs",
  "evidence_ref",
  "evidence_refs",
  "legacy_ref",
  "legacy_refs",
  "node_ref",
  "node_refs",
  "edge_ref",
  "edge_refs",
  "diagnostic_ref",
  "diagnostic_refs",
  "record_ref",
  "record_refs",
  "result_ref",
  "result_refs",
  "context_ref",
  "context_refs",
  "trace_ref",
  "trace_refs",
  "after_context_ref",
  "auto_continue_prompt_ref",
  "auto_continue_without_after_ref",
  "before_context_ref",
  "broad_response_ref",
  "candidate_context_ref",
  "candidate_evidence_ref",
  "candidate_ref",
  "candidate_source_ref",
  "candidate_tool_outcome_ref",
  "candidate_verification_ref",
  "changed_docs_ref",
  "changed_production_ref",
  "changed_test_ref",
  "child_key_evidence_ref",
  "child_key_fact_ref",
  "child_prompt_ref",
  "child_record_ref",
  "child_result_ref",
  "child_trace_artifact_ref",
  "child_trace_ref",
  "code_ref",
  "compatibility_from_ref",
  "compatibility_to_ref",
  "conflict_ref",
  "conflicting_evidence_ref",
  "consumed_by_ref",
  "consumer_ref",
  "dependency_tool_outcome_ref",
  "derived_tool_outcome_ref",
  "direct_evidence_ref",
  "direct_support_ref",
  "downstream_claim_ref",
  "dropped_fact_ref",
  "evaluation_ref",
  "execution_ref",
  "explicit_source_ref",
  "finalized_open_record_ref",
  "generated_response_ref",
  "generation_context_ref",
  "generation_grounding_candidate_ref",
  "generation_llm_ref",
  "generation_provenance_ref",
  "generation_tool_outcome_ref",
  "grounding_candidate_ref",
  "inferred_tool_context_ref",
  "inferred_tool_context_set_ref",
  "input_context_ref",
  "input_context_snapshot_ref",
  "issue_ref",
  "legacy_context_ref",
  "legacy_input_ref",
  "legacy_output_ref",
  "legacy_source_ref",
  "matched_evidence_ref",
  "member_ref",
  "message_transform_ref",
  "next_claim_ref",
  "no_evidence_ref",
  "original_direct_evidence_ref",
  "outcome_ref",
  "parent_consumption_ref",
  "parent_fact_ref",
  "payload_ref",
  "previous_claim_ref",
  "prompt_transform_ref",
  "raw_artifact_ref",
  "replacement_evidence_ref",
  "requirement_source_ref",
  "retained_fact_ref",
  "runtime_ref",
  "selected_context_ref",
  "serialized_tail_artifact_ref",
  "summary_artifact_ref",
  "superseded_by_ref",
  "superseded_evidence_ref",
  "supersedes_ref",
  "target_ref",
  "temporal_advisory_ref",
  "tool_failure_context_ref",
  "tool_failure_dependency_ref",
  "tool_outcome_ref",
  "tool_result_dependency_ref",
  "tool_schema_ref",
  "unmatched_direct_evidence_ref",
  "unresolved_ref",
  "verification_after_test_change_ref",
  "verification_ref",
  "verification_superseded_by_ref",
  "verification_supersedes_ref",
])

function scopedLegacyReference(value: string | undefined, scope: SegmentReplayScope) {
  if (!value || !scope.namespace) return value
  const separator = value.indexOf(":")
  if (separator < 1 || separator === value.length - 1) return value
  const prefix = value.slice(0, separator)
  const type =
    prefix === "node" || prefix === "record"
      ? "node"
      : prefix === "artifact"
        ? "artifact"
        : prefix === "edge"
          ? "edge"
          : prefix === "diagnostic"
            ? "diagnostic"
            : undefined
  const id = value.slice(separator + 1)
  if (type) return `${prefix}:${resolvedScopedEntityID(scope, type, id) ?? `${scope.segmentID}::${type}::${id}`}`
  const alias = resolvedNodeAlias(scope, value) ?? resolvedNodeAlias(scope, id)
  return alias && CAUSAL_NODE_ALIAS_SCHEMES.has(prefix) ? `${prefix}:${alias}` : value
}

function referenceEntityType(key: string): "node" | "edge" | "artifact" | "diagnostic" | undefined {
  const normalized = key.toLowerCase()
  if (NODE_ID_KEY_SCHEMES.has(normalized)) return "node"
  if (EDGE_ID_KEYS.has(normalized)) return "edge"
  if (ARTIFACT_ID_KEYS.has(normalized)) return "artifact"
  if (DIAGNOSTIC_ID_KEYS.has(normalized)) return "diagnostic"
  return undefined
}

function isReferenceKey(key: string) {
  const normalized = key.toLowerCase()
  return REFERENCE_KEYS.has(normalized) || normalized.endsWith("_ref") || normalized.endsWith("_refs")
}

const CAUSAL_NODE_ALIAS_SCHEMES = new Set([
  "design",
  "claim",
  "record",
  "node",
  "prompt",
  "context",
  "context_snapshot",
  "compaction_check",
  "compaction",
  "llm_request",
  "span",
  "llm_turn",
  "llm",
  "exit_gate",
  "decision",
  "tool",
  "tool_call",
  "tool_span",
  "tool_result",
  "tool_error",
  "mcp",
  "mcp_call",
  "skill",
  "subagent_task",
  "observation",
  "evidence",
  "repo_fact",
  "external_evaluation",
  "change",
  "verification",
  "response",
  "response_segment",
  "final_response_evidence",
  "response_claim",
  "claim_support",
])

function resolvedNodeAlias(scope: SegmentReplayScope, alias: string) {
  const resolved = scope.nodeAliases.get(alias)
  return resolved === null ? undefined : resolved
}

function resolvedNodeID(scope: SegmentReplayScope, id: string, key = "") {
  const fieldMatches = new Set<string>()
  let fieldAliasFound = false
  for (const scheme of NODE_ID_KEY_SCHEMES.get(key.toLowerCase()) ?? []) {
    const alias = `${scheme}:${id}`
    if (!scope.nodeAliases.has(alias)) continue
    fieldAliasFound = true
    const resolved = scope.nodeAliases.get(alias)
    if (!resolved) return undefined
    fieldMatches.add(resolved)
  }
  if (fieldAliasFound) return fieldMatches.size === 1 ? fieldMatches.values().next().value : undefined
  const direct = resolvedScopedEntityID(scope, "node", id)
  if (direct) return direct
  const alias = resolvedNodeAlias(scope, id)
  if (alias) return alias
  return undefined
}

function canonicalReference(value: unknown): CausalIRRef | undefined {
  const candidate = record(value)
  if (
    !candidate ||
    !nonemptyString(candidate.ref_id) ||
    (candidate.ref_type !== "node" &&
      candidate.ref_type !== "artifact" &&
      candidate.ref_type !== "raw_event" &&
      candidate.ref_type !== "external")
  )
    return undefined
  return candidate as CausalIRRef
}

function scopedSchemaReferences(value: unknown, scope: SegmentReplayScope, key = ""): unknown {
  if (!scope.namespace) return value
  if (typeof value === "string") {
    const entityType = referenceEntityType(key)
    if (entityType === "node") return resolvedNodeID(scope, value, key) ?? value
    if (entityType) return resolvedScopedEntityID(scope, entityType, value) ?? value
    if (!isReferenceKey(key)) return value
    const legacy = scopedLegacyReference(value, scope)
    if (legacy !== value) return legacy
    const node = resolvedNodeID(scope, value, key)
    if (node) return node
    const matches = (["edge", "artifact", "diagnostic"] as const)
      .map((type) => resolvedScopedEntityID(scope, type, value))
      .filter(nonemptyString)
    return matches.length === 1 ? matches[0] : value
  }
  if (Array.isArray(value)) return value.map((item) => scopedSchemaReferences(item, scope, key))
  const canonical = canonicalReference(value)
  if (canonical) return isReferenceKey(key) ? scopedReference(canonical, scope, key) : value
  const object = record(value)
  if (!object) return value
  return Object.fromEntries(
    Object.entries(object).map(([childKey, item]) => [childKey, scopedSchemaReferences(item, scope, childKey)]),
  )
}

function scopedReference(ref: CausalIRRef, scope: SegmentReplayScope, key = ""): CausalIRRef {
  if (ref.ref_type === "node") {
    const refID = resolvedNodeID(scope, ref.ref_id, key)
    if (!refID) return ref
    return {
      ...ref,
      ref_id: refID,
      ...(ref.legacy_ref ? { legacy_ref: scopedLegacyReference(ref.legacy_ref, scope) } : {}),
    }
  }
  if (ref.ref_type === "artifact") {
    const refID = resolvedScopedEntityID(scope, "artifact", ref.ref_id)
    if (!refID) return ref
    return {
      ...ref,
      ref_id: refID,
      ...(ref.legacy_ref ? { legacy_ref: scopedLegacyReference(ref.legacy_ref, scope) } : {}),
    }
  }
  if (ref.ref_type === "raw_event" && scope.namespace)
    return { ...ref, ref_id: `${scope.runID}::raw_event::${ref.ref_id}` }
  return ref
}

function scopedNode(node: CausalIRNode, scope: SegmentReplayScope): CausalIRNode {
  if (!scope.namespace) return node
  const nodeID = scopedEntityID(scope, "node", node.node_id)
  const transformed = scopedSchemaReferences(node, scope) as CausalIRNode
  const payload = scopedSchemaReferences(node.payload, scope) as Record<string, unknown>
  return {
    ...transformed,
    node_id: nodeID,
    scope: { ...node.scope, run_id: scope.runID, case_id: scope.caseID },
    payload,
    data: payload,
    input_refs: node.input_refs.map((ref) => scopedReference(ref, scope)),
    output_refs: node.output_refs.map((ref) => scopedReference(ref, scope)),
    source_refs: node.source_refs.map((ref) => scopedReference(ref, scope)),
    artifact_refs: node.artifact_refs.map((id) => resolvedScopedEntityID(scope, "artifact", id) ?? id),
    aliases: [
      ...node.aliases.map((alias) => scopedLegacyReference(alias, scope) ?? alias),
      `run:${scope.runID}:node:${node.node_id}`,
    ],
    integrity: { ...node.integrity, payload_hash: causalIRPayloadHash(payload) },
    metadata: {
      ...(node.metadata ?? {}),
      original_node_id: node.node_id,
      segment_id: scope.segmentID,
      run_id: scope.runID,
    },
    derivation: node.derivation
      ? { ...node.derivation, input_refs: node.derivation.input_refs.map((ref) => scopedReference(ref, scope)) }
      : null,
  }
}

function scopedEdge(edge: CausalIREdge, scope: SegmentReplayScope): ScopedCausalIREdge {
  if (!scope.namespace) return edge
  const metadata = scopedSchemaReferences(edge.metadata, scope) as Record<string, unknown> | undefined
  return {
    ...edge,
    edge_id: scopedEntityID(scope, "edge", edge.edge_id),
    from: scopedReference(edge.from, scope),
    to: scopedReference(edge.to, scope),
    evidence_refs: edge.evidence_refs.map((ref) => scopedReference(ref, scope)),
    scope: { run_id: scope.runID, case_id: scope.caseID },
    metadata: {
      ...(metadata ?? {}),
      original_edge_id: edge.edge_id,
      segment_id: scope.segmentID,
      run_id: scope.runID,
    },
  }
}

function scopedArtifact(artifact: ArtifactLike, scope: SegmentReplayScope): ArtifactLike {
  const relativePath = artifact.path.replaceAll("\\", "/").replace(/^\/+/, "")
  if (relativePath.split("/").includes("..")) throw new Error(`unsafe artifact path: ${artifact.path}`)
  const scopedPath =
    scope.pathPrefix && scope.pathPrefix !== "."
      ? path.posix.join(scope.pathPrefix.replaceAll("\\", "/"), relativePath)
      : relativePath
  if (!scope.namespace) return { ...artifact, path: scopedPath }
  const transformed = scopedSchemaReferences(artifact, scope) as ArtifactLike
  return {
    ...transformed,
    artifact_id: scopedEntityID(scope, "artifact", artifact.artifact_id),
    path: scopedPath,
    original_artifact_id: artifact.artifact_id,
    scope: { segment_id: scope.segmentID, run_id: scope.runID, case_id: scope.caseID },
  }
}

function scopedDiagnostic(diagnostic: CausalIRDiagnosticLike, scope: SegmentReplayScope): CausalIRDiagnosticLike {
  if (!scope.namespace) return diagnostic
  const transformed = scopedSchemaReferences(diagnostic, scope) as CausalIRDiagnosticLike
  return {
    ...transformed,
    diagnostic_id: scopedEntityID(scope, "diagnostic", diagnostic.diagnostic_id),
    original_diagnostic_id: diagnostic.diagnostic_id,
    scope: { segment_id: scope.segmentID, run_id: scope.runID, case_id: scope.caseID },
  }
}

function scopedSnapshot(snapshot: CausalIRStoreSnapshot, scope: SegmentReplayScope): CausalIRStoreSnapshot {
  return {
    ...snapshot,
    runID: scope.runID,
    caseID: scope.caseID,
    nodes: snapshot.nodes.map((node) => scopedNode(node, scope)),
    edges: snapshot.edges.map((edge) => scopedEdge(canonicalCausalIREdge(edge), scope)),
    artifacts: snapshot.artifacts.map((artifact) => scopedArtifact(artifact, scope)),
    diagnostics: snapshot.diagnostics.map((diagnostic) => scopedDiagnostic(diagnostic, scope)),
  }
}

function hashKey(operation: CausalIRJournalEntry["operation"], entityID: string) {
  switch (operation) {
    case "node.created":
    case "node.updated":
      return `node:${entityID}`
    case "edge.created":
      return `edge:${entityID}`
    case "artifact.created":
    case "artifact.reused":
      return `artifact:${entityID}`
    case "diagnostic.created":
      return `diagnostic:${entityID}`
    case "case.checkpointed":
    case "case.finalized":
    case "case.runtime_closed":
      return `case:${entityID}`
  }
}

function lifecycleSnapshot(input: unknown): CausalIRStoreSnapshot | undefined {
  const data = record(input)
  const snapshot = record(data?.snapshot)
  if (
    !snapshot ||
    !Array.isArray(snapshot.nodes) ||
    !Array.isArray(snapshot.edges) ||
    !Array.isArray(snapshot.artifacts) ||
    !Array.isArray(snapshot.diagnostics)
  )
    return undefined
  return snapshot as CausalIRStoreSnapshot
}

function normalizedValidationEntry(entry: CausalIRJournalEntry) {
  const normalized: CausalIRJournalEntry = { ...entry, sequence: 1 }
  delete normalized.previous_payload_hash
  if (entry.operation !== "case.finalized") return normalized

  const data = record(entry.data)
  if (data?.format === "compact_causal_ir_finalization") return normalized
  const trace = record(data?.trace)
  const journal = record(trace?.journal)
  if (!trace || !journal) return normalized
  const normalizedJournal: Record<string, unknown> = { ...journal, entry_count: 0, last_sequence: 0 }
  delete normalizedJournal.last_payload_hash
  normalized.data = { ...data, trace: { ...trace, journal: normalizedJournal } }
  normalized.payload_hash = causalIRPayloadHash(normalized.data)
  return normalized
}

function isLegacyLifecycleFinalization(entry: CausalIRJournalEntry) {
  if (entry.operation !== "case.finalized") return false
  const data = record(entry.data)
  return data?.format !== "compact_causal_ir_finalization" && record(data?.trace) !== undefined
}

function legacyLifecycleSummaryMatchesPrefix(
  entry: CausalIRJournalEntry,
  prefixLength: number,
  precedingPayloadHash: string | undefined,
) {
  const data = record(entry.data)
  const trace = record(data?.trace)
  const summary = record(trace?.journal)
  if (!summary) return false
  return (
    summary.entry_count === prefixLength &&
    summary.last_sequence === prefixLength &&
    (prefixLength === 0 ? summary.last_payload_hash === undefined : summary.last_payload_hash === precedingPayloadHash)
  )
}

function validateEntryShape(entry: CausalIRJournalEntry, requireInitialRunNode: boolean) {
  validateCausalIRJournal([normalizedValidationEntry(entry)], { requireInitialRunNode })
}

function finalizedClose(entry: CausalIRJournalEntry): CausalIRRuntimeCloseData | undefined {
  if (entry.operation !== "case.finalized") return undefined
  const data = record(entry.data)
  const canonical = record(data?.canonical)
  const legacyTrace = record(data?.trace)
  const manifest = record(canonical?.manifest) ?? record(legacyTrace?.manifest) ?? record(data?.data)
  if (!manifest) return undefined
  const status = manifest.status
  if (status !== "success" && status !== "error" && status !== "cancelled") return undefined
  return {
    format: "runtime_close",
    status,
    closed_at: typeof manifest.ended_at === "string" ? manifest.ended_at : entry.time,
    ...(manifest.result === undefined ? {} : { result: manifest.result }),
    ...(manifest.error === undefined ? {} : { error: manifest.error }),
    manifest: {
      case_id: entry.case_id,
      run_id: entry.run_id,
      ...(typeof manifest.session_id === "string" ? { session_id: manifest.session_id } : {}),
    },
  }
}

function terminalEnvelope(entry: CausalIRJournalEntry): TerminalEnvelope | undefined {
  if (entry.operation === "case.runtime_closed") {
    const close = record(entry.data)
    const manifest = record(close?.manifest)
    if (!close || !manifest) return undefined
    return {
      manifest: {
        ...manifest,
        status: close.status,
        ended_at: close.closed_at,
        ...(close.result === undefined ? {} : { result: close.result }),
        ...(close.error === undefined ? {} : { error: close.error }),
      },
    }
  }
  if (entry.operation !== "case.finalized") return undefined
  const data = record(entry.data)
  const canonical = record(data?.canonical)
  const legacyTrace = record(data?.trace)
  const manifest = record(canonical?.manifest) ?? record(legacyTrace?.manifest) ?? record(data?.data)
  if (!manifest) return undefined
  return {
    manifest,
    metrics: record(canonical?.metrics) ?? record(legacyTrace?.metrics),
  }
}

class ReplayIndex {
  readonly db: Database
  private scope: SegmentReplayScope | undefined
  private runID = ""
  private caseID = ""
  private runtimeClosed = false
  private terminalClose: CausalIRRuntimeCloseData | undefined
  private terminal: TerminalEnvelope | undefined
  private lastOperation: CausalIRJournalEntry["operation"] | undefined
  private lastPayloadHash: string | undefined
  private readonly nextOrdinal = { nodes: 0, edges: 0, artifacts: 0, diagnostics: 0 }
  recoveredLines = 0
  private segmentRecoveredLines = 0

  constructor(readonly indexPath: string) {
    this.db = new Database(indexPath, { create: true, strict: true })
    this.db.exec("PRAGMA journal_mode = OFF")
    this.db.exec("PRAGMA synchronous = OFF")
    this.db.exec("PRAGMA temp_store = FILE")
    this.db.exec("PRAGMA cache_size = -8192")
    this.db.exec(`
      CREATE TABLE nodes (
        entity_id TEXT PRIMARY KEY,
        segment_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        kind TEXT NOT NULL,
        wrapped INTEGER NOT NULL,
        json TEXT NOT NULL
      );
      CREATE TABLE edges (
        entity_id TEXT PRIMARY KEY,
        segment_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        wrapped INTEGER NOT NULL,
        json TEXT NOT NULL
      );
      CREATE TABLE artifacts (
        entity_id TEXT PRIMARY KEY,
        segment_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        wrapped INTEGER NOT NULL,
        json TEXT NOT NULL
      );
      CREATE TABLE diagnostics (
        entity_id TEXT PRIMARY KEY,
        segment_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        kind TEXT,
        wrapped INTEGER NOT NULL,
        json TEXT NOT NULL
      );
      CREATE TABLE payload_hashes (
        hash_key TEXT PRIMARY KEY,
        payload_hash TEXT NOT NULL
      );
      CREATE TABLE aliases (
        alias TEXT NOT NULL,
        node_id TEXT NOT NULL,
        PRIMARY KEY (alias, node_id)
      );
      CREATE TABLE unresolved_occurrences (
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
        legacy_ref TEXT NOT NULL,
        owner_type TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        field TEXT NOT NULL
      );
      CREATE TABLE generated_diagnostic_fragments (
        diagnostic_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        json TEXT NOT NULL,
        PRIMARY KEY (diagnostic_id, ordinal)
      );
    `)
  }

  get identities() {
    return { runID: this.runID, caseID: this.caseID }
  }

  get currentRecoveredLines() {
    return this.segmentRecoveredLines
  }

  beginSegment(scope: SegmentReplayScope) {
    this.scope = scope
    this.runID = ""
    this.caseID = ""
    this.runtimeClosed = false
    this.terminalClose = undefined
    this.terminal = undefined
    this.lastOperation = undefined
    this.lastPayloadHash = undefined
    this.segmentRecoveredLines = 0
    this.db.exec("DELETE FROM payload_hashes")
  }

  get close() {
    return this.lastOperation === "case.runtime_closed" || this.lastOperation === "case.finalized"
      ? this.terminalClose
      : undefined
  }

  get finalPayloadHash() {
    return this.lastPayloadHash
  }

  get envelope() {
    return this.lastOperation === "case.runtime_closed" || this.lastOperation === "case.finalized"
      ? this.terminal
      : undefined
  }

  get terminalOperation() {
    return this.lastOperation
  }

  private validationError(message: string): never {
    throw new CausalIRJournalValidationError(this.segmentRecoveredLines + 1, message)
  }

  private validate(entry: CausalIRJournalEntry) {
    const line = this.segmentRecoveredLines + 1
    if (this.runtimeClosed) this.validationError("entry follows runtime close terminal")
    if (!Number.isSafeInteger(entry.sequence) || entry.sequence !== line)
      this.validationError(`expected contiguous sequence ${line}`)
    if (!nonemptyString(entry.time)) this.validationError("missing journal time")
    if (!nonemptyString(entry.run_id) || !nonemptyString(entry.case_id))
      this.validationError("missing run or case identity")
    if (this.runID && (entry.run_id !== this.runID || entry.case_id !== this.caseID))
      this.validationError("journal run or case identity changed")
    if (!OPERATIONS.has(entry.operation)) this.validationError(`unknown operation ${String(entry.operation)}`)
    if (!nonemptyString(entry.record_type) || !nonemptyString(entry.entity_id) || !nonemptyString(entry.payload_hash))
      this.validationError("missing record type, entity identity, or payload hash")
    if (isLegacyLifecycleFinalization(entry) && entry.payload_hash !== causalIRPayloadHash(entry.data))
      this.validationError("journal payload hash does not match data")
    if (
      isLegacyLifecycleFinalization(entry) &&
      !legacyLifecycleSummaryMatchesPrefix(entry, line - 1, this.lastPayloadHash)
    )
      this.validationError("malformed lifecycle finalization entry")
    try {
      validateEntryShape(entry, line === 1)
    } catch (error) {
      if (error instanceof CausalIRJournalValidationError) this.validationError(error.message)
      throw error
    }
    const previous = this.db
      .query<HashRow, [string]>("SELECT payload_hash FROM payload_hashes WHERE hash_key = ?")
      .get(hashKey(entry.operation, entry.entity_id))?.payload_hash
    if (entry.previous_payload_hash !== previous)
      this.validationError("previous payload hash does not match entity history")
    return previous
  }

  apply(input: unknown, rawLine: string) {
    if (!this.scope) throw new Error("replay segment scope is not initialized")
    if (!record(input)) this.validationError("expected a journal entry object")
    const entry = input as CausalIRJournalEntry
    const previous = this.validate(entry)
    const unchanged = previous === entry.payload_hash
    const originalSnapshot = lifecycleSnapshot(entry.data)
    const snapshot = originalSnapshot ? scopedSnapshot(originalSnapshot, this.scope) : undefined

    this.db.transaction(() => {
      if (
        !unchanged &&
        (entry.operation === "node.created" || entry.operation === "node.updated") &&
        record(entry.data)
      ) {
        const node = scopedNode(entry.data as CausalIRNode, this.scope!)
        this.putNode(node, this.scope!.namespace ? JSON.stringify(node) : rawLine, !this.scope!.namespace)
      } else if (!unchanged && entry.operation === "edge.created" && record(entry.data)) {
        this.putEdge(scopedEdge(canonicalCausalIREdge(entry.data as CausalIREdge), this.scope!))
      } else if (
        !unchanged &&
        (entry.operation === "artifact.created" || entry.operation === "artifact.reused") &&
        record(entry.data)
      ) {
        const original = entry.data as ArtifactLike
        const artifact = scopedArtifact(original, this.scope!)
        const transformed = this.scope!.namespace || artifact.path !== original.path
        this.putArtifact(artifact, transformed ? JSON.stringify(artifact) : rawLine, !transformed)
      } else if (!unchanged && entry.operation === "diagnostic.created" && record(entry.data)) {
        const diagnostic = scopedDiagnostic(entry.data as CausalIRDiagnosticLike, this.scope!)
        this.putDiagnostic(
          diagnostic,
          this.scope!.namespace ? JSON.stringify(diagnostic) : rawLine,
          !this.scope!.namespace,
        )
      } else if ((entry.operation === "case.checkpointed" || entry.operation === "case.finalized") && snapshot) {
        this.replaceSnapshot(snapshot)
      }

      const data = record(entry.data)
      if (entry.operation === "case.checkpointed" && data?.hash_state_replacement === "nodes_and_edges") {
        this.replacePayloadHashes("node", originalSnapshot?.nodes ?? [], "node_id")
        this.replacePayloadHashes("edge", originalSnapshot?.edges ?? [], "edge_id")
      } else if (entry.operation === "case.checkpointed" && data?.hash_state_replacement === "edges") {
        this.replacePayloadHashes("edge", originalSnapshot?.edges ?? [], "edge_id")
      }
      this.db
        .query("INSERT OR REPLACE INTO payload_hashes(hash_key, payload_hash) VALUES (?, ?)")
        .run(hashKey(entry.operation, entry.entity_id!), entry.payload_hash!)
    })()

    if (!this.runID) {
      this.runID = entry.run_id
      this.caseID = entry.case_id
    }
    this.segmentRecoveredLines = entry.sequence
    this.recoveredLines += 1
    this.lastOperation = entry.operation
    this.lastPayloadHash = entry.payload_hash
    this.runtimeClosed = entry.operation === "case.runtime_closed"
    const terminalClose = this.runtimeClosed ? (entry.data as CausalIRRuntimeCloseData) : finalizedClose(entry)
    this.terminalClose = terminalClose
      ? (scopedSchemaReferences(terminalClose, this.scope) as CausalIRRuntimeCloseData)
      : undefined
    const terminal = terminalEnvelope(entry)
    this.terminal = terminal
      ? {
          manifest: scopedSchemaReferences(terminal.manifest, this.scope) as Record<string, unknown>,
          ...(terminal.metrics
            ? { metrics: scopedSchemaReferences(terminal.metrics, this.scope) as Record<string, unknown> }
            : {}),
        }
      : undefined
  }

  private putNode(node: CausalIRNode, json = JSON.stringify(node), wrapped = false) {
    if (!this.scope) throw new Error("replay segment scope is not initialized")
    this.db
      .query(
        `
        INSERT INTO nodes(entity_id, segment_key, ordinal, kind, wrapped, json) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET segment_key = excluded.segment_key, kind = excluded.kind, wrapped = excluded.wrapped, json = excluded.json
      `,
      )
      .run(node.node_id, this.scope.key, ++this.nextOrdinal.nodes, node.kind, wrapped ? 1 : 0, json)
  }

  private putEdge(edge: CausalIREdge) {
    if (!this.scope) throw new Error("replay segment scope is not initialized")
    this.db
      .query(
        `
        INSERT INTO edges(entity_id, segment_key, ordinal, wrapped, json) VALUES (?, ?, ?, 0, ?)
        ON CONFLICT(entity_id) DO UPDATE SET segment_key = excluded.segment_key, wrapped = excluded.wrapped, json = excluded.json
      `,
      )
      .run(edge.edge_id, this.scope.key, ++this.nextOrdinal.edges, JSON.stringify(edge))
  }

  private putArtifact(artifact: ArtifactLike, json = JSON.stringify(artifact), wrapped = false) {
    if (!this.scope) throw new Error("replay segment scope is not initialized")
    this.db
      .query(
        `
        INSERT INTO artifacts(entity_id, segment_key, ordinal, wrapped, json) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET segment_key = excluded.segment_key, wrapped = excluded.wrapped, json = excluded.json
      `,
      )
      .run(artifact.artifact_id, this.scope.key, ++this.nextOrdinal.artifacts, wrapped ? 1 : 0, json)
  }

  private putDiagnostic(diagnostic: CausalIRDiagnosticLike, json = JSON.stringify(diagnostic), wrapped = false) {
    if (!this.scope) throw new Error("replay segment scope is not initialized")
    this.db
      .query(
        `
        INSERT INTO diagnostics(entity_id, segment_key, ordinal, kind, wrapped, json) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET segment_key = excluded.segment_key, kind = excluded.kind, wrapped = excluded.wrapped, json = excluded.json
      `,
      )
      .run(
        diagnostic.diagnostic_id,
        this.scope.key,
        ++this.nextOrdinal.diagnostics,
        typeof diagnostic.kind === "string" ? diagnostic.kind : null,
        wrapped ? 1 : 0,
        json,
      )
  }

  private replaceSnapshot(snapshot: CausalIRStoreSnapshot) {
    if (!this.scope) throw new Error("replay segment scope is not initialized")
    this.db.query("DELETE FROM nodes WHERE segment_key = ?").run(this.scope.key)
    this.db.query("DELETE FROM edges WHERE segment_key = ?").run(this.scope.key)
    this.db.query("DELETE FROM artifacts WHERE segment_key = ?").run(this.scope.key)
    this.db.query("DELETE FROM diagnostics WHERE segment_key = ?").run(this.scope.key)
    for (const node of snapshot.nodes) this.putNode(node)
    for (const edge of snapshot.edges) this.putEdge(canonicalCausalIREdge(edge))
    for (const artifact of snapshot.artifacts) this.putArtifact(artifact)
    for (const diagnostic of snapshot.diagnostics) this.putDiagnostic(diagnostic)
  }

  private replacePayloadHashes(
    type: "node" | "edge",
    items: Array<CausalIRNode | CausalIREdge>,
    idKey: "node_id" | "edge_id",
  ) {
    this.db.query("DELETE FROM payload_hashes WHERE hash_key LIKE ?").run(`${type}:%`)
    for (const item of items) {
      const entityID = item[idKey as keyof typeof item]
      if (typeof entityID !== "string") continue
      this.db
        .query("INSERT OR REPLACE INTO payload_hashes(hash_key, payload_hash) VALUES (?, ?)")
        .run(`${type}:${entityID}`, causalIRPayloadHash(item))
    }
  }

  private inspectReference(ownerType: "node" | "edge", ownerID: string, field: string, ref: CausalIRRef) {
    if (ref.ref_type !== "external" || !ref.legacy_ref) return
    const legacy = typedCausalIRReference(ref.legacy_ref)
    if (legacy.ref_type !== "node") return
    this.db
      .query("INSERT INTO unresolved_occurrences(legacy_ref, owner_type, owner_id, field) VALUES (?, ?, ?, ?)")
      .run(causalIRLegacyRef(legacy), ownerType, ownerID, field)
  }

  reconcileDiagnostics() {
    this.db.exec("DELETE FROM aliases; DELETE FROM unresolved_occurrences; DELETE FROM generated_diagnostic_fragments")
    for (const row of this.entityRows("nodes")) {
      const node = JSON.parse(row.json) as CausalIRNode
      for (const alias of node.aliases)
        this.db.query("INSERT OR IGNORE INTO aliases(alias, node_id) VALUES (?, ?)").run(alias, node.node_id)
      for (const [index, ref] of node.input_refs.entries())
        this.inspectReference("node", node.node_id, `input_refs[${index}]`, ref)
      for (const [index, ref] of node.output_refs.entries())
        this.inspectReference("node", node.node_id, `output_refs[${index}]`, ref)
      for (const [index, ref] of node.source_refs.entries())
        this.inspectReference("node", node.node_id, `source_refs[${index}]`, ref)
      for (const [index, ref] of (node.derivation?.input_refs ?? []).entries())
        this.inspectReference("node", node.node_id, `derivation.input_refs[${index}]`, ref)
    }
    for (const row of this.entityRows("edges")) {
      const edge = JSON.parse(row.json) as CausalIREdge
      if (edge.normalized_relation === "derived_from" && edge.derivation_method === "unknown_relation_fallback") {
        this.putGeneratedDiagnostic({
          diagnostic_id: `unknown_relation:${edge.edge_id}`,
          kind: "unknown_relation",
          level: "warning",
          message: `Unknown causal relation: ${edge.original_relation}`,
          edge_id: edge.edge_id,
          relation: edge.original_relation,
        })
      }
      this.inspectReference("edge", edge.edge_id, "from", edge.from)
      this.inspectReference("edge", edge.edge_id, "to", edge.to)
      for (const [index, ref] of edge.evidence_refs.entries())
        this.inspectReference("edge", edge.edge_id, `evidence_refs[${index}]`, ref)
    }
    for (const row of this.db
      .query<{ alias: string }, []>("SELECT alias FROM aliases GROUP BY alias HAVING COUNT(*) > 1")
      .iterate()) {
      const diagnosticID = `alias_collision:${causalIRPayloadHash(row.alias).slice(0, 16)}`
      const head = {
        diagnostic_id: diagnosticID,
        kind: "alias_collision",
        level: "warning",
        message: `Ambiguous causal alias: ${row.alias}`,
        alias: row.alias,
      }
      this.spoolGeneratedDiagnostic(
        diagnosticID,
        `${JSON.stringify(head).slice(0, -1)},"owner_ids":[`,
        this.db
          .query<OwnerRow, [string]>("SELECT node_id FROM aliases WHERE alias = ? ORDER BY node_id")
          .iterate(row.alias),
        (owner) => JSON.stringify(owner.node_id),
        "]}",
      )
    }
    for (const row of this.db
      .query<LegacyRow, []>("SELECT legacy_ref FROM unresolved_occurrences GROUP BY legacy_ref")
      .iterate()) {
      const first = this.db
        .query<
          OccurrenceRow,
          [string]
        >("SELECT owner_type, owner_id, field FROM unresolved_occurrences WHERE legacy_ref = ? ORDER BY ordinal LIMIT 1")
        .get(row.legacy_ref)
      const count = this.db
        .query<CountRow, [string]>("SELECT COUNT(*) AS count FROM unresolved_occurrences WHERE legacy_ref = ?")
        .get(row.legacy_ref)!.count
      const diagnosticID = `unresolved_ref:${causalIRPayloadHash(row.legacy_ref).slice(0, 16)}`
      const head = {
        diagnostic_id: diagnosticID,
        kind: "unresolved_ref",
        level: "warning",
        message: `Unresolved causal reference: ${row.legacy_ref}`,
        legacy_ref: row.legacy_ref,
        occurrence_count: count,
      }
      const tail = {
        owner_type: first?.owner_type,
        field: first?.field,
        ...(first?.owner_type === "node" ? { node_id: first.owner_id } : first ? { edge_id: first.owner_id } : {}),
      }
      this.spoolGeneratedDiagnostic(
        diagnosticID,
        `${JSON.stringify(head).slice(0, -1)},"affected_owners":[`,
        this.db
          .query<
            OccurrenceRow,
            [string]
          >("SELECT owner_type, owner_id, field FROM unresolved_occurrences WHERE legacy_ref = ? ORDER BY ordinal")
          .iterate(row.legacy_ref),
        (owner) => JSON.stringify(owner),
        `],${JSON.stringify(tail).slice(1)}`,
      )
    }
  }

  private putGeneratedDiagnostic(diagnostic: CausalIRDiagnosticLike) {
    this.db.transaction(() => {
      this.db.query("DELETE FROM generated_diagnostic_fragments WHERE diagnostic_id = ?").run(diagnostic.diagnostic_id)
      this.db
        .query("INSERT INTO generated_diagnostic_fragments(diagnostic_id, ordinal, json) VALUES (?, 0, ?)")
        .run(diagnostic.diagnostic_id, JSON.stringify(diagnostic))
    })()
  }

  private spoolGeneratedDiagnostic<T>(
    diagnosticID: string,
    prefix: string,
    rows: Iterable<T>,
    serialize: (row: T) => string,
    suffix: string,
  ) {
    this.db.transaction(() => {
      const insert = this.db.query(
        "INSERT INTO generated_diagnostic_fragments(diagnostic_id, ordinal, json) VALUES (?, ?, ?)",
      )
      this.db.query("DELETE FROM generated_diagnostic_fragments WHERE diagnostic_id = ?").run(diagnosticID)
      let ordinal = 0
      insert.run(diagnosticID, ordinal++, prefix)
      let first = true
      for (const row of rows) {
        insert.run(diagnosticID, ordinal++, `${first ? "" : ","}${serialize(row)}`)
        first = false
      }
      insert.run(diagnosticID, ordinal, suffix)
    })()
  }

  count(table: "nodes" | "edges" | "artifacts") {
    return this.db.query<CountRow, []>(`SELECT COUNT(*) AS count FROM ${table}`).get()!.count
  }

  segmentCount(table: "nodes" | "edges" | "artifacts", segmentKey: string) {
    return this.db
      .query<CountRow, [string]>(`SELECT COUNT(*) AS count FROM ${table} WHERE segment_key = ?`)
      .get(segmentKey)!.count
  }

  private *segmentNodeRows(segmentKey: string, kind?: string) {
    let ordinal = -1
    while (true) {
      const row = kind
        ? this.db
            .query<OrderedJsonRow, [string, string, number]>(
              `SELECT ordinal, CASE WHEN wrapped = 1 THEN json_extract(json, '$.data') ELSE json END AS json
               FROM nodes WHERE segment_key = ? AND kind = ? AND ordinal > ? ORDER BY ordinal LIMIT 1`,
            )
            .get(segmentKey, kind, ordinal)
        : this.db
            .query<OrderedJsonRow, [string, number]>(
              `SELECT ordinal, CASE WHEN wrapped = 1 THEN json_extract(json, '$.data') ELSE json END AS json
               FROM nodes WHERE segment_key = ? AND ordinal > ? ORDER BY ordinal LIMIT 1`,
            )
            .get(segmentKey, ordinal)
      if (!row) return
      ordinal = row.ordinal
      yield row
    }
  }

  runStart(segmentKey: string) {
    const row = this.segmentNodeRows(segmentKey, "run.start")[Symbol.iterator]().next().value as JsonRow | undefined
    return row ? (JSON.parse(row.json) as CausalIRNode) : undefined
  }

  private replaceNode(node: CausalIRNode) {
    const canonical = canonicalCausalIRNode(
      {
        node_id: node.node_id,
        kind: node.kind,
        schema_version: node.schema_version,
        origin: node.origin,
        component: node.component,
        span_id: node.span_id,
        parent_span_id: node.parent_span_id,
        timestamp: node.timestamp,
        time_ms: node.time_ms,
        title: node.title,
        status: node.status,
        input_refs: node.legacy_input_refs ?? node.input_refs.map(causalIRLegacyRef),
        output_refs: node.legacy_output_refs ?? node.output_refs.map(causalIRLegacyRef),
        source_refs: node.legacy_source_refs ?? node.source_refs.map(causalIRLegacyRef),
        source_locations: node.source_locations,
        typed_resources: node.typed_resources,
        artifact_refs: node.artifact_refs,
        aliases: node.aliases,
        derivation: node.derivation ?? undefined,
        data: node.data,
        metadata: node.metadata,
      },
      { runID: node.scope.run_id, caseID: node.scope.case_id, sequence: node.order.sequence },
    )
    this.db
      .query("UPDATE nodes SET kind = ?, wrapped = 0, json = ? WHERE entity_id = ?")
      .run(canonical.kind, JSON.stringify(canonical), canonical.node_id)
  }

  private putDerivedNode(
    input: Omit<CausalNodeLike, "timestamp" | "time_ms" | "origin" | "derivation"> & {
      timestamp: string
      time_ms: number
    },
  ) {
    const inputRefs = input.input_refs ?? []
    const node = canonicalCausalIRNode(
      {
        ...input,
        origin: "deterministic_derived",
        derivation: {
          algorithm: "trace_materializer_terminal_enrichment",
          algorithm_version: "1",
          derived_at: input.timestamp,
          input_refs: inputRefs.map(typedCausalIRReference),
          reproducible: true,
        },
      },
      { runID: this.runID, caseID: this.caseID, sequence: this.nextOrdinal.nodes + 1 },
    )
    this.putNode(node)
    return node
  }

  private putDerivedEdge(input: { edgeID: string; from: string; to: string; relation: string; label: string }) {
    this.putEdge(
      canonicalCausalIREdge({
        edge_id: input.edgeID,
        from: { type: "node", id: input.from },
        to: { type: "node", id: input.to },
        relation: input.relation,
        eligible_for_attribution: false,
        derivation_method: "trace_materializer_terminal_enrichment_v1",
        label: input.label,
        metadata: { behavior_impact: "none", projection_stage: "terminal_materialization" },
      }),
    )
  }

  enrichRuntimeTerminal(input: {
    segmentKey: string
    status: "success" | "error" | "cancelled"
    closedAt: string
    result?: Record<string, unknown>
    error?: unknown
  }) {
    if (
      this.db
        .query<
          CountRow,
          [string]
        >("SELECT COUNT(*) AS count FROM nodes WHERE segment_key = ? AND kind IN ('case.completed', 'case.failed')")
        .get(input.segmentKey)!.count > 0
    )
      return

    const finalizedReason =
      input.status === "cancelled" ? "trace_cancelled" : input.status === "error" ? "trace_error" : "trace_finished"
    const failedOpenRefs: string[] = []
    for (const row of this.segmentNodeRows(input.segmentKey)) {
      const node = JSON.parse(row.json) as CausalIRNode
      if (node.status !== "running") continue
      if (input.status !== "success" && failedOpenRefs.length < 16) failedOpenRefs.push(`node:${node.node_id}`)
      node.status = input.status
      node.data = {
        ...(node.data ?? {}),
        finalized_status: "finalized_without_close",
        finalized_reason: finalizedReason,
        finalized_at: input.closedAt,
        original_status: "running",
      }
      this.replaceNode(node)
    }

    let timeMS = Date.parse(input.closedAt)
    const runStart = this.runStart(input.segmentKey)
    if (runStart && Number.isFinite(timeMS)) timeMS = Math.max(0, timeMS - Date.parse(runStart.timestamp))
    if (!Number.isFinite(timeMS)) timeMS = this.nextOrdinal.nodes

    let finalResponseSegmentID: string | undefined
    for (const row of this.segmentNodeRows(input.segmentKey, "response.output")) {
      const response = JSON.parse(row.json) as CausalIRNode
      const data = response.data ?? {}
      if (
        data.response_role !== "final_answer" ||
        data.visibility !== "user_visible" ||
        data.is_final_for_case !== true ||
        (input.status !== "success" && data.finality_source !== "explicit")
      )
        continue
      const segmentID = typeof data.segment_id === "string" ? data.segment_id : response.node_id
      finalResponseSegmentID = segmentID
      const claims = atomizeResponseClaims(data.text)
      claims.forEach((claim, index) => {
        const suffix = causalIRPayloadHash(`${segmentID}:${index}:${claim.key}`).slice(0, 12)
        const claimNodeID = `materialized_responseclaim_${suffix}`
        const sourceRefs = [
          `node:${response.node_id}`,
          ...(Array.isArray(data.source_refs)
            ? data.source_refs.filter((ref): ref is string => typeof ref === "string")
            : []),
        ]
        const supportLevel = sourceRefs.length > 1 ? "supported" : "unsupported"
        this.putDerivedNode({
          node_id: claimNodeID,
          kind: "response.claim",
          component: "result",
          title: claim.text,
          status: "success",
          timestamp: input.closedAt,
          time_ms: timeMS,
          input_refs: [`node:${response.node_id}`],
          source_refs: sourceRefs,
          data: {
            claim_id: `claim_${suffix}`,
            response_segment_id: segmentID,
            text: claim.text,
            claim_key: claim.key,
            claim_format: claim.claim_format,
            raw_text: claim.raw_text,
            canonical_text: claim.canonical_text,
            claim_group_id: claim.claim_group_id,
            claim_index: claim.claim_index,
            claim_count: claim.claim_count,
            source_byte_range: claim.source_byte_range,
            atomization_status: claim.atomization_status,
            atomization_reason: claim.atomization_reason,
            source_refs: sourceRefs.slice(1),
            support_level: supportLevel,
            quality_flags: supportLevel === "unsupported" ? ["unsupported_response_claim"] : [],
            metadata: {
              response_node_id: response.node_id,
              response_role: data.response_role,
              is_final_for_case: true,
              finality_source: data.finality_source,
            },
          },
        })
        const assessmentNodeID = `materialized_claimsupport_${suffix}`
        this.putDerivedNode({
          node_id: assessmentNodeID,
          kind: "claim.support_assessment",
          component: "evaluation",
          title: `support assessment for ${claim.text}`,
          status: "success",
          timestamp: input.closedAt,
          time_ms: timeMS,
          input_refs: [`node:${claimNodeID}`],
          source_refs: [`node:${claimNodeID}`, ...sourceRefs.slice(1)],
          data: {
            assessment_id: `claimsupport_${suffix}`,
            claim_id: `claim_${suffix}`,
            response_segment_id: segmentID,
            claim_node_id: claimNodeID,
            support_level: supportLevel,
            quality_flags: supportLevel === "unsupported" ? ["unsupported_response_claim"] : [],
          },
        })
        this.putDerivedEdge({
          edgeID: `materialized_response_to_claim_${suffix}`,
          from: response.node_id,
          to: claimNodeID,
          relation: "response_to_claim",
          label: "Final response was deterministically atomized into a claim",
        })
      })
    }

    const signal = typeof input.result?.signal === "string" ? input.result.signal : undefined
    let signalNode: CausalIRNode | undefined
    if (signal) {
      signalNode = this.putDerivedNode({
        node_id: `materialized_process_signal_${causalIRPayloadHash(`${this.runID}:${signal}`).slice(0, 12)}`,
        kind: "process.signal",
        component: "runtime",
        title: `process received ${signal}`,
        status: "cancelled",
        timestamp: input.closedAt,
        time_ms: timeMS,
        data: {
          signal,
          shutdown_disposition: "interrupted_before_case_completion",
          server_shutdown_reason: "process_signal",
          observation_source: "process_signal_handler",
          sender_identity_available: false,
          recording_mode: "passive_posthoc",
          agent_feedback: "none",
        },
      })
    }

    const caseKind = input.status === "success" ? "case.completed" : "case.failed"
    const caseNode = this.putDerivedNode({
      node_id: `materialized_${caseKind.replace(".", "_")}_${causalIRPayloadHash(this.runID).slice(0, 12)}`,
      kind: caseKind,
      component: "run",
      title: input.status === "success" ? "case completed" : "case failed",
      status: input.status,
      timestamp: input.closedAt,
      time_ms: timeMS,
      input_refs: signalNode ? [`node:${signalNode.node_id}`] : finalResponseSegmentID ? [] : failedOpenRefs,
      source_refs: signalNode
        ? [`node:${signalNode.node_id}`]
        : finalResponseSegmentID
          ? [`response_segment:${finalResponseSegmentID}`]
          : failedOpenRefs,
      data: {
        run_id: this.runID,
        case_id: this.caseID,
        server_status: input.status,
        process_status: input.status,
        server_shutdown_reason: signal ? "process_signal" : undefined,
        shutdown_signal: signal,
        shutdown_disposition: signal ? "interrupted_before_case_completion" : "normal_case_completion",
        case_status: input.status,
        final_response_segment_id: finalResponseSegmentID,
        result: input.result,
        error: input.error,
        finalized_open_record_refs: input.status === "success" ? [] : failedOpenRefs,
        finalized_open_record_count: input.status === "success" ? 0 : failedOpenRefs.length,
      },
    })
    if (signalNode)
      this.putDerivedEdge({
        edgeID: `materialized_signal_to_case_${causalIRPayloadHash(this.runID).slice(0, 12)}`,
        from: signalNode.node_id,
        to: caseNode.node_id,
        relation: "failed_before",
        label: "External process signal interrupted the case",
      })
  }

  recordCount() {
    let count = 0
    for (const row of this.db.query<KindRow, []>("SELECT kind FROM nodes").iterate())
      if (isFormalRecordType(row.kind === "final.claim" ? "response.output" : row.kind)) count += 1
    return count
  }

  addContinuation(input: {
    segmentKey: string
    runID: string
    caseID: string
    previousRunID: string
    fromSegmentKey: string
  }) {
    const current = this.db
      .query<
        EntityIDRow,
        [string]
      >("SELECT entity_id FROM nodes WHERE segment_key = ? AND kind = 'run.start' ORDER BY ordinal LIMIT 1")
      .get(input.segmentKey)?.entity_id
    const previous = this.db
      .query<
        EntityIDRow,
        [string]
      >("SELECT entity_id FROM nodes WHERE segment_key = ? AND kind = 'run.start' ORDER BY ordinal LIMIT 1")
      .get(input.fromSegmentKey)?.entity_id
    if (!current || !previous) return
    const priorScope = this.scope
    this.scope = {
      key: input.segmentKey,
      segmentID: input.segmentKey,
      runID: input.runID,
      caseID: input.caseID,
      pathPrefix: "",
      namespace: true,
      idMaps: {
        node: new Map(),
        edge: new Map(),
        artifact: new Map(),
        diagnostic: new Map(),
      },
      nodeAliases: new Map(),
    }
    this.putEdge({
      edge_id: `${input.segmentKey}::edge::run.continuation::${input.previousRunID}`,
      from: { ref_type: "node", ref_id: current },
      to: { ref_type: "node", ref_id: previous },
      original_relation: "continued_from",
      normalized_relation: "continued_from",
      evidence_tier: "confirmed",
      eligible_for_attribution: false,
      derivation_method: "session_manifest_continuation_v1",
      evidence_refs: [],
      label: "Run continued from the preceding immutable session segment",
      scope: { run_id: input.runID, case_id: input.caseID },
      metadata: {
        provenance_type: "run.continuation",
        continuation_of: input.previousRunID,
        segment_id: input.segmentKey,
        run_id: input.runID,
      },
    } as ScopedCausalIREdge)
    this.scope = priorScope
  }

  private entityRows(table: "nodes" | "edges" | "artifacts") {
    return this.db
      .query<
        JsonRow,
        []
      >(`SELECT CASE WHEN wrapped = 1 THEN json_extract(json, '$.data') ELSE json END AS json FROM ${table} ORDER BY ordinal`)
      .iterate()
  }

  *rawEntities(table: "nodes" | "edges" | "artifacts") {
    for (const row of this.entityRows(table)) yield streamingJsonRawItem(row.json)
  }

  *diagnosticItems() {
    for (const row of this.db
      .query<
        JsonRow,
        []
      >("SELECT CASE WHEN wrapped = 1 THEN json_extract(json, '$.data') ELSE json END AS json FROM diagnostics WHERE kind IS NULL OR kind NOT IN ('unknown_relation', 'alias_collision', 'unresolved_ref') ORDER BY ordinal")
      .iterate())
      yield streamingJsonRawItem(row.json)
    for (const row of this.db
      .query<
        DiagnosticIDRow,
        []
      >("SELECT DISTINCT diagnostic_id FROM generated_diagnostic_fragments ORDER BY diagnostic_id")
      .iterate())
      yield streamingJsonRawChunks(this.generatedDiagnosticChunks(row.diagnostic_id))
  }

  private *generatedDiagnosticChunks(diagnosticID: string) {
    for (const row of this.db
      .query<
        JsonRow,
        [string]
      >("SELECT json FROM generated_diagnostic_fragments WHERE diagnostic_id = ? ORDER BY ordinal")
      .iterate(diagnosticID))
      yield row.json
  }

  *compatibilityRecords(manifest: Record<string, unknown>, metrics: Record<string, unknown>) {
    for (const row of this.entityRows("nodes")) {
      const node = JSON.parse(row.json) as CausalIRNode
      const projection = projectProvenanceTrace(
        {
          version: CAUSAL_IR_VERSION,
          runID: this.runID,
          caseID: this.caseID,
          nodes: [node],
          edges: [],
          artifacts: [],
          diagnostics: [],
        },
        {
          traceVersion: TRACE_VERSION,
          manifest: manifest as { case_id: string; run_id: string },
          metrics: metrics as never,
        },
      )
      if (projection.records[0]) yield projection.records[0]
    }
  }

  *compatibilityEdges(manifest: Record<string, unknown>, metrics: Record<string, unknown>) {
    for (const row of this.entityRows("edges")) {
      const edge = JSON.parse(row.json) as CausalIREdge
      const projection = projectProvenanceTrace(
        {
          version: CAUSAL_IR_VERSION,
          runID: this.runID,
          caseID: this.caseID,
          nodes: [],
          edges: [edge],
          artifacts: [],
          diagnostics: [],
        },
        {
          traceVersion: TRACE_VERSION,
          manifest: manifest as { case_id: string; run_id: string },
          metrics: metrics as never,
        },
      )
      yield projection.dataflow_edges[0]!
    }
  }

  *legacyNodeData(kinds: string[]) {
    const placeholders = kinds.map(() => "?").join(", ")
    for (const row of this.db
      .query<
        JsonRow,
        string[]
      >(`SELECT CASE WHEN wrapped = 1 THEN json_extract(json, '$.data') ELSE json END AS json FROM nodes WHERE kind IN (${placeholders}) ORDER BY ordinal`)
      .iterate(...kinds)) {
      const node = JSON.parse(row.json) as CausalIRNode
      yield node.data ?? {}
    }
  }

  *legacySpans() {
    for (const row of this.db
      .query<JsonRow, []>(
        `SELECT CASE WHEN wrapped = 1 THEN json_extract(json, '$.data') ELSE json END AS json
         FROM nodes
         WHERE kind IN ('llm.call', 'tool.call', 'mcp.call', 'skill.load', 'subagent.call', 'task.loop', 'execution.observation')
         ORDER BY ordinal`,
      )
      .iterate()) {
      const node = JSON.parse(row.json) as CausalIRNode
      if (!node.span_id) continue
      const data = node.data ?? {}
      yield {
        span_id: node.span_id,
        parent_span_id: node.parent_span_id,
        component: node.component,
        operation: data.operation,
        name: node.title,
        status: node.status,
        start_time: node.timestamp,
        start_ms: node.time_ms,
        end_time: data.finalized_at,
        duration_ms: data.duration_ms,
        input_summary: legacyFieldSummary(data.input),
        output_summary: legacyFieldSummary(data.output),
        token_usage: data.token_usage,
        error: data.error,
        metadata: {
          ...(node.metadata ?? {}),
          ...(data.finalized_status === undefined ? {} : { finalized_status: data.finalized_status }),
          ...(data.finalized_reason === undefined ? {} : { finalized_reason: data.finalized_reason }),
        },
      }
    }
  }

  closeIndex() {
    this.db.close(false)
  }

  releaseTransientBindings() {
    const database = this.db as unknown as { clearQueryCache(): void }
    database.clearQueryCache()
  }
}

function memoryPhase(phase: string, journalBytes = 0) {
  if (process.env.OPENCODE_TRACE_MATERIALIZER_MEMORY_PHASES !== "1") return
  process.stdout.write(
    `\nTRACE_MATERIALIZER_PHASE ${JSON.stringify({ phase, journalBytes, rss: process.memoryUsage().rss, maxRSS: process.resourceUsage().maxRSS })}\n`,
  )
}

function readPhysicalLines(
  file: string,
  visit: (line: string, lineNumber: number, recoverableTail: boolean) => void,
  afterLine: (sourceBytes: number) => void,
) {
  const fd = fs.openSync(file, "r")
  const chunk = Buffer.allocUnsafe(64 * 1024)
  let lineBuffer = Buffer.allocUnsafe(64 * 1024)
  let lineLength = 0
  let lineNumber = 0

  const append = (start: number, end: number) => {
    const sourceLength = end - start
    const required = lineLength + sourceLength
    if (required > lineBuffer.length) {
      let capacity = lineBuffer.length
      while (capacity < required) capacity += Math.max(64 * 1024, Math.ceil(capacity / 2 / (64 * 1024)) * 64 * 1024)
      const grown = Buffer.allocUnsafe(capacity)
      lineBuffer.copy(grown, 0, 0, lineLength)
      lineBuffer = grown
    }
    chunk.copy(lineBuffer, lineLength, start, end)
    lineLength = required
  }

  const emit = (recoverableTail: boolean, newlineBytes: number) => {
    const decodedLength = lineLength > 0 && lineBuffer[lineLength - 1] === 0x0d ? lineLength - 1 : lineLength
    let line: string | undefined = lineBuffer.toString("utf8", 0, decodedLength)
    visit(line, ++lineNumber, recoverableTail)
    line = undefined
    afterLine(lineLength + newlineBytes)
    lineLength = 0
  }

  try {
    while (true) {
      const bytes = fs.readSync(fd, chunk, 0, chunk.length, null)
      if (!bytes) break
      let start = 0
      for (let index = chunk.indexOf(0x0a, start); index !== -1 && index < bytes; index = chunk.indexOf(0x0a, start)) {
        append(start, index)
        emit(false, 1)
        start = index + 1
      }
      append(start, bytes)
    }
    if (lineLength > 0) emit(true, 0)
  } finally {
    fs.closeSync(fd)
  }
  return lineNumber
}

class JournalJsonError extends Error {}

function parseLine(line: string) {
  try {
    if (!line.trim()) throw new Error("expected a JSON object")
    const entry = JSON.parse(line) as unknown
    if (!record(entry)) throw new Error("expected a JSON object")
    return entry
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new JournalJsonError(detail)
  }
}

function discoverSegmentEntityIDs(recordsFile: string, scope: SegmentReplayScope) {
  if (!scope.namespace) return
  const registerAlias = (alias: string, nodeID: string) => {
    const existing = scope.nodeAliases.get(alias)
    if (existing === undefined) scope.nodeAliases.set(alias, nodeID)
    else if (existing !== nodeID) scope.nodeAliases.set(alias, null)
  }
  const registerNode = (value: unknown) => {
    const node = record(value)
    if (!node || !nonemptyString(node.node_id)) return
    const scoped = scopedEntityID(scope, "node", node.node_id)
    registerAlias(node.node_id, scoped)
    for (const alias of Array.isArray(node.aliases) ? node.aliases : []) {
      if (!nonemptyString(alias)) continue
      const separator = alias.indexOf(":")
      const scheme = separator > 0 ? alias.slice(0, separator) : undefined
      if (!scheme || !CAUSAL_NODE_ALIAS_SCHEMES.has(scheme)) continue
      registerAlias(alias, scoped)
      const local = alias.slice(separator + 1)
      if (local) registerAlias(local, scoped)
    }
  }
  const register = (
    type: "node" | "edge" | "artifact" | "diagnostic",
    value: unknown,
    key: "node_id" | "edge_id" | "artifact_id" | "diagnostic_id",
  ) => {
    const entity = record(value)
    if (entity && nonemptyString(entity[key])) scopedEntityID(scope, type, entity[key] as string)
  }
  readPhysicalLines(
    recordsFile,
    (line) => {
      let entry: Record<string, unknown>
      try {
        entry = parseLine(line) as Record<string, unknown>
      } catch (error) {
        if (error instanceof JournalJsonError) return
        throw error
      }
      const operation = entry.operation
      if (operation === "node.created" || operation === "node.updated") registerNode(entry.data)
      if (operation === "edge.created") register("edge", entry.data, "edge_id")
      if (operation === "artifact.created" || operation === "artifact.reused")
        register("artifact", entry.data, "artifact_id")
      if (operation === "diagnostic.created") register("diagnostic", entry.data, "diagnostic_id")
      const snapshot = lifecycleSnapshot(entry.data)
      if (!snapshot) return
      for (const node of snapshot.nodes) registerNode(node)
      for (const edge of snapshot.edges) register("edge", edge, "edge_id")
      for (const artifact of snapshot.artifacts) register("artifact", artifact, "artifact_id")
      for (const diagnostic of snapshot.diagnostics) register("diagnostic", diagnostic, "diagnostic_id")
    },
    () => {},
  )
}

function recoverJournal(recordsFile: string, index: ReplayIndex) {
  let droppedLines = 0
  let journalBytes = 0
  let bytesSinceGC = 0
  let nextWatermark = MEMORY_WATERMARK_BYTES

  const processLine = (line: string, lineNumber: number, recoverableTail: boolean) => {
    try {
      index.apply(parseLine(line), line)
    } catch (error) {
      if (recoverableTail && error instanceof JournalJsonError && index.currentRecoveredLines > 0) {
        droppedLines = 1
        return
      }
      if (recoverableTail && index.currentRecoveredLines === 0) throw new Error(`${recordsFile}:1: journal is empty`)
      if (error instanceof JournalJsonError)
        throw new Error(`${recordsFile}:${lineNumber}: invalid JSONL entry (${error.message})`)
      const detail = error instanceof Error ? error.message : String(error)
      throw new Error(`${recordsFile}:${lineNumber}: ${detail}`)
    }
  }

  const lineCount = readPhysicalLines(recordsFile, processLine, (sourceBytes) => {
    journalBytes += sourceBytes
    bytesSinceGC += sourceBytes
    if (bytesSinceGC >= MATERIALIZER_GC_BYTES) {
      index.releaseTransientBindings()
      Bun.gc(true)
      bytesSinceGC = 0
    }
    while (journalBytes >= nextWatermark) {
      memoryPhase("replay_watermark", journalBytes)
      nextWatermark += MEMORY_WATERMARK_BYTES
    }
  })
  if (!lineCount) throw new Error(`${recordsFile}:1: journal is empty`)
  if (bytesSinceGC > 0) {
    index.releaseTransientBindings()
    Bun.gc(true)
  }
  memoryPhase("replay_complete", journalBytes)
  return { droppedLines, journalBytes }
}

function traceMembers(
  index: ReplayIndex,
  manifest: Record<string, unknown>,
  metrics: Record<string, unknown>,
  journal: Record<string, unknown>,
): StreamingJsonObjectMember[] {
  return [
    ["trace_version", TRACE_VERSION],
    ["causal_ir_version", CAUSAL_IR_VERSION],
    ["manifest", manifest],
    ["nodes", streamingJsonArray(index.rawEntities("nodes"))],
    ["edges", streamingJsonArray(index.rawEntities("edges"))],
    ["artifacts", streamingJsonArray(index.rawEntities("artifacts"))],
    ["journal", journal],
    ["metrics", metrics],
    ["diagnostics", streamingJsonArray(index.diagnosticItems())],
    ["compatibility", { provenance_projection: "provenance-trace.json" }],
    ["records", streamingJsonArray(index.compatibilityRecords(manifest, metrics))],
    ["dataflow_edges", streamingJsonArray(index.compatibilityEdges(manifest, metrics))],
  ]
}

function provenanceMembers(
  index: ReplayIndex,
  manifest: Record<string, unknown>,
  metrics: Record<string, unknown>,
): StreamingJsonObjectMember[] {
  return [
    ["trace_version", TRACE_VERSION],
    ["manifest", manifest],
    ["records", streamingJsonArray(index.compatibilityRecords(manifest, metrics))],
    ["dataflow_edges", streamingJsonArray(index.compatibilityEdges(manifest, metrics))],
    ["artifacts", streamingJsonArray(index.rawEntities("artifacts"))],
    ["metrics", metrics],
  ]
}

function legacyMembers(
  index: ReplayIndex,
  manifest: Record<string, unknown>,
  metrics: Record<string, unknown>,
): StreamingJsonObjectMember[] {
  return [
    ["trace_version", "1.3"],
    ["case_id", manifest.case_id],
    ["run_id", manifest.run_id],
    ["session_id", manifest.session_id],
    ["started_at", manifest.started_at],
    ["ended_at", manifest.ended_at],
    ["duration_ms", manifest.duration_ms],
    ["status", manifest.status],
    ["input", manifest.input],
    ["environment", manifest.environment],
    ["token_usage", manifest.token_usage ?? {}],
    ["spans", streamingJsonArray(index.legacySpans())],
    ["events", streamingJsonArray(index.compatibilityRecords(manifest, metrics))],
    ["artifacts", streamingJsonArray(index.rawEntities("artifacts"))],
    ["errors", streamingJsonArray(manifest.error === undefined ? [] : [manifest.error])],
    ["result", manifest.result],
    ["context_snapshots", streamingJsonArray(index.legacyNodeData(["context.pack"]))],
    ["semantic_decisions", streamingJsonArray(index.legacyNodeData(["decision"]))],
    ["dataflow_edges", streamingJsonArray(index.compatibilityEdges(manifest, metrics))],
    ["verification_records", streamingJsonArray(index.legacyNodeData(["verification"]))],
    ["change_records", streamingJsonArray(index.legacyNodeData(["change"]))],
    ["constraint_records", streamingJsonArray(index.legacyNodeData(["semantic.constraint"]))],
    ["response_segments", streamingJsonArray(index.legacyNodeData(["response.output"]))],
    ["design_records", streamingJsonArray(index.legacyNodeData(["design.record"]))],
  ]
}

function mergeAdditive(target: unknown, source: unknown): unknown {
  if (typeof target === "number" && typeof source === "number") return target + source
  if (Array.isArray(target) && Array.isArray(source)) return [...target, ...source]
  const targetRecord = record(target)
  const sourceRecord = record(source)
  if (targetRecord && sourceRecord) {
    const merged: Record<string, unknown> = { ...targetRecord }
    for (const [key, value] of Object.entries(sourceRecord))
      merged[key] = key in merged ? mergeAdditive(merged[key], value) : value
    return merged
  }
  return source
}

function aggregateMetrics(
  replays: Array<{
    envelope?: TerminalEnvelope
    counts: { nodes: number; edges: number; artifacts: number }
  }>,
  index: ReplayIndex,
  terminalManifestRunID: string,
  segmented: boolean,
) {
  const aggregate = replays.reduce<Record<string, unknown>>((metrics, replay) => {
    const fallback = {
      spans: 0,
      events: replay.counts.nodes,
      token_usage: {},
      stream_summary: {},
      trace_health: { issues: [] },
    }
    return mergeAdditive(metrics, replay.envelope?.metrics ?? fallback) as Record<string, unknown>
  }, {})
  return {
    ...aggregate,
    spans: typeof aggregate.spans === "number" ? aggregate.spans : 0,
    events: typeof aggregate.events === "number" ? aggregate.events : index.count("nodes"),
    token_usage: record(aggregate.token_usage) ?? {},
    trace_health: { issues: [], ...(record(aggregate.trace_health) ?? {}) },
    records: index.count("nodes"),
    dataflow_edges: index.count("edges"),
    artifacts: index.count("artifacts"),
    ...(segmented
      ? {
          aggregation: {
            mode: "session_segments_v1",
            terminal_manifest_run_id: terminalManifestRunID,
            numeric_metrics: "sum",
            graph_counts: "materialized_unique_entities",
          },
        }
      : {}),
  }
}

function copyFileAtomic(source: string, destination: string) {
  if (path.resolve(source) === path.resolve(destination)) return
  fs.mkdirSync(path.dirname(destination), { recursive: true })
  const temporary = path.join(
    path.dirname(destination),
    `.${path.basename(destination)}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`,
  )
  let handle: number | undefined
  try {
    fs.copyFileSync(source, temporary)
    handle = fs.openSync(temporary, "r")
    fs.fsyncSync(handle)
    fs.closeSync(handle)
    handle = undefined
    fs.renameSync(temporary, destination)
  } catch (error) {
    if (handle !== undefined) fs.closeSync(handle)
    try {
      fs.unlinkSync(temporary)
    } catch {}
    throw error
  }
}

function linkFileAtomic(source: string, destination: string) {
  fs.mkdirSync(path.dirname(destination), { recursive: true })
  const temporary = path.join(
    path.dirname(destination),
    `.${path.basename(destination)}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`,
  )
  try {
    fs.linkSync(source, temporary)
    fs.renameSync(temporary, destination)
  } catch (error) {
    try {
      fs.unlinkSync(temporary)
    } catch {}
    throw error
  }
}

function linkCompatibilityArtifacts(sourceDirectory: string, destinationDirectory: string, copyOnly = false) {
  if (!fs.existsSync(sourceDirectory)) return
  if (!fs.statSync(sourceDirectory).isDirectory()) return
  for (const entry of fs.readdirSync(sourceDirectory, { withFileTypes: true })) {
    const source = path.join(sourceDirectory, entry.name)
    const destination = path.join(destinationDirectory, entry.name)
    if (entry.isDirectory()) {
      fs.mkdirSync(destination, { recursive: true })
      linkCompatibilityArtifacts(source, destination, copyOnly)
      continue
    }
    if (!entry.isFile() || fs.existsSync(destination)) continue
    fs.mkdirSync(path.dirname(destination), { recursive: true })
    if (copyOnly) {
      copyFileAtomic(source, destination)
      continue
    }
    try {
      fs.linkSync(source, destination)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") continue
      const temporary = `${destination}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`
      try {
        fs.copyFileSync(source, temporary, fs.constants.COPYFILE_EXCL)
        fs.linkSync(temporary, destination)
      } catch (copyError) {
        if ((copyError as NodeJS.ErrnoException).code !== "EEXIST") throw copyError
      } finally {
        try {
          fs.unlinkSync(temporary)
        } catch {}
      }
    }
  }
}

function fileContentHash(file: string) {
  const hash = crypto.createHash("sha256")
  const handle = fs.openSync(file, "r")
  const buffer = Buffer.allocUnsafe(64 * 1024)
  try {
    while (true) {
      const bytes = fs.readSync(handle, buffer, 0, buffer.length, null)
      if (!bytes) break
      hash.update(buffer.subarray(0, bytes))
    }
  } finally {
    fs.closeSync(handle)
  }
  return hash.digest("hex")
}

function publishCompatibilityArtifacts(sourceDirectory: string, caseDir: string) {
  const rewrites = new Map<string, string>()
  if (!fs.existsSync(sourceDirectory)) return rewrites
  const visit = (directory: string) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const source = path.join(directory, entry.name)
      if (entry.isDirectory()) {
        visit(source)
        continue
      }
      if (!entry.isFile()) continue
      const sourceRelative = path.posix.join(
        "artifacts",
        path.relative(sourceDirectory, source).replaceAll(path.sep, "/"),
      )
      const digest = fileContentHash(source)
      const requestedDestination = path.join(caseDir, sourceRelative)
      if (!fs.existsSync(requestedDestination)) {
        copyFileAtomic(source, requestedDestination)
        rewrites.set(sourceRelative, sourceRelative)
        continue
      }
      if (fileContentHash(requestedDestination) === digest) {
        rewrites.set(sourceRelative, sourceRelative)
        continue
      }
      const contentRelative = path.posix.join("artifacts", "sha256", digest)
      const contentDestination = path.join(caseDir, contentRelative)
      if (fs.existsSync(contentDestination)) {
        if (fileContentHash(contentDestination) !== digest)
          throw new Error(`${contentDestination}: content-addressed artifact hash collision`)
      } else copyFileAtomic(source, contentDestination)
      rewrites.set(sourceRelative, contentRelative)
    }
  }
  visit(sourceDirectory)
  return rewrites
}

function rewriteCompatibilityArtifactProjection(
  file: string,
  caseDir: string,
  rewrites: Map<string, string>,
  publicPrefix = "",
) {
  if (!fs.existsSync(file) || !rewrites.size) return
  const document = record(JSON.parse(fs.readFileSync(file, "utf8")) as unknown)
  if (!document || !Array.isArray(document.artifacts)) return
  document.artifacts = document.artifacts.map((input) => {
    const artifact = record(input)
    if (!artifact || !nonemptyString(artifact.path)) return input
    const sourcePath = artifact.path.replaceAll("\\", "/").replace(/^\/+/, "")
    const rewritten = rewrites.get(sourcePath)
    if (!rewritten) return input
    const digest = fileContentHash(path.join(caseDir, rewritten))
    const declared = typeof artifact.hash === "string" ? artifact.hash.replace(/^sha256:/, "") : undefined
    if (declared && /^[a-f0-9]{64}$/i.test(declared) && declared.toLowerCase() !== digest)
      throw new Error(`${file}: compatibility artifact ${sourcePath} hash does not match its bytes`)
    const publicPath = publicPrefix ? path.posix.join(publicPrefix, rewritten) : rewritten
    if (publicPath === sourcePath) return input
    return { ...artifact, path: publicPath, content_sha256: digest }
  })
  fs.writeFileSync(file, JSON.stringify(document))
}

function lstatExists(file: string) {
  try {
    fs.lstatSync(file)
    return true
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false
    throw error
  }
}

function fsyncDirectory(directory: string) {
  const handle = fs.openSync(directory, "r")
  try {
    fs.fsyncSync(handle)
  } finally {
    fs.closeSync(handle)
  }
}

function swapSymlink(destination: string, target: string) {
  fs.mkdirSync(path.dirname(destination), { recursive: true })
  const temporary = path.join(path.dirname(destination), `.${path.basename(destination)}.${crypto.randomUUID()}.tmp`)
  try {
    fs.symlinkSync(target, temporary)
    fs.renameSync(temporary, destination)
  } catch (error) {
    try {
      fs.unlinkSync(temporary)
    } catch {}
    throw error
  }
}

function derivedPublicationFailure(step: string) {
  if (process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP === step)
    throw new Error(`injected derived publication failure: ${step}`)
}

function copyVisibleFile(source: string, destination: string) {
  if (!fs.existsSync(source) || !fs.statSync(source).isFile()) return
  copyFileAtomic(source, destination)
}

function ensurePreexistingGeneration(
  caseDir: string,
  derivedDir: string,
  currentLink: string,
  includeArtifacts: boolean,
) {
  if (lstatExists(currentLink)) return undefined
  const relativeFiles = [
    "trace.json",
    "manifest.json",
    "legacy-trace.json",
    "provenance-trace.json",
    "partial/latest.json",
  ]
  const hasVisibleSet = relativeFiles.some((relative) => fs.existsSync(path.join(caseDir, relative)))
  if (!hasVisibleSet) return undefined
  const name = `preexisting-${crypto.randomUUID()}`
  const generationDir = path.join(derivedDir, "generations", name)
  try {
    fs.mkdirSync(generationDir, { recursive: true })
    for (const relative of relativeFiles)
      copyVisibleFile(path.join(caseDir, relative), path.join(generationDir, relative))
    if (includeArtifacts)
      linkCompatibilityArtifacts(path.join(caseDir, "artifacts"), path.join(generationDir, "artifacts"))
    swapSymlink(currentLink, path.posix.join("generations", name))
  } catch (error) {
    try {
      if (lstatExists(currentLink) && fs.readlinkSync(currentLink) === path.posix.join("generations", name))
        fs.unlinkSync(currentLink)
    } catch {}
    fs.rmSync(generationDir, { recursive: true, force: true })
    throw error
  }
  return path.posix.join("generations", name)
}

type RootLinkTransaction = {
  backupDir: string
  moved: Array<{ destination: string; backup: string }>
  created: string[]
  createdDirectories: string[]
}

function installRootCompatibilityLinks(caseDir: string, relativeFiles: string[]): RootLinkTransaction {
  const transaction: RootLinkTransaction = {
    backupDir: path.join(caseDir, ".derived", `link-backup-${crypto.randomUUID()}`),
    moved: [],
    created: [],
    createdDirectories: [],
  }
  try {
    for (const relative of relativeFiles) {
      const destination = path.join(caseDir, relative)
      const target = path.relative(path.dirname(destination), path.join(caseDir, ".derived", "current", relative))
      if (
        lstatExists(destination) &&
        fs.lstatSync(destination).isSymbolicLink() &&
        fs.readlinkSync(destination) === target
      )
        continue
      const parent = path.dirname(destination)
      if (!fs.existsSync(parent)) {
        fs.mkdirSync(parent, { recursive: true })
        transaction.createdDirectories.push(parent)
      }
      if (lstatExists(destination)) {
        const backup = path.join(transaction.backupDir, relative)
        fs.mkdirSync(path.dirname(backup), { recursive: true })
        fs.renameSync(destination, backup)
        transaction.moved.push({ destination, backup })
      } else transaction.created.push(destination)
      const temporary = path.join(parent, `.${path.basename(destination)}.${crypto.randomUUID()}.link`)
      try {
        fs.symlinkSync(target, temporary)
        fs.renameSync(temporary, destination)
        derivedPublicationFailure("root_link_installing")
      } catch (error) {
        try {
          fs.unlinkSync(temporary)
        } catch {}
        throw error
      }
    }
  } catch (error) {
    rollbackRootCompatibilityLinks(transaction)
    throw error
  }
  return transaction
}

function rollbackRootCompatibilityLinks(transaction: RootLinkTransaction) {
  for (const destination of [...transaction.created].reverse()) {
    try {
      fs.unlinkSync(destination)
    } catch {}
  }
  for (const item of [...transaction.moved].reverse()) {
    try {
      fs.rmSync(item.destination, { recursive: true, force: true })
      fs.renameSync(item.backup, item.destination)
    } catch {}
  }
  fs.rmSync(transaction.backupDir, { recursive: true, force: true })
  for (const directory of [...transaction.createdDirectories].reverse()) {
    try {
      fs.rmdirSync(directory)
    } catch {}
  }
}

function publishStagedTrace(stageDir: string, caseDir: string, generation: number) {
  const derivedDir = path.join(caseDir, ".derived")
  const generationsDir = path.join(derivedDir, "generations")
  const currentLink = path.join(derivedDir, "current")
  const legacyAuthority = fs.existsSync(path.join(caseDir, "records.jsonl"))
  const derivedExisted = lstatExists(derivedDir)
  const generationsExisted = lstatExists(generationsDir)
  let preexistingGeneration: string | undefined
  const generationName = String(generation)
  const generationDir = path.join(generationsDir, generationName)
  const temporaryGeneration = path.join(generationsDir, `.${generationName}.${crypto.randomUUID()}.tmp`)
  let oldCurrent: string | undefined
  let links: RootLinkTransaction | undefined
  let currentSwapped = false
  let generationInstalled = false
  try {
    fs.mkdirSync(generationsDir, { recursive: true })
    preexistingGeneration = ensurePreexistingGeneration(caseDir, derivedDir, currentLink, !legacyAuthority)
    oldCurrent = lstatExists(currentLink) ? fs.readlinkSync(currentLink) : undefined
    if (!fs.existsSync(generationDir)) {
      fs.mkdirSync(temporaryGeneration, { recursive: true })
      derivedPublicationFailure("generation_copying")
      if (oldCurrent)
        linkCompatibilityArtifacts(
          path.join(derivedDir, "current", "artifacts"),
          path.join(temporaryGeneration, "artifacts"),
        )
      for (const relative of ["trace.json", "manifest.json", "provenance-trace.json", "legacy-trace.json"])
        copyVisibleFile(path.join(stageDir, relative), path.join(temporaryGeneration, relative))
      linkFileAtomic(
        path.join(temporaryGeneration, "trace.json"),
        path.join(temporaryGeneration, "partial", "latest.json"),
      )
      const artifactRewrites = publishCompatibilityArtifacts(path.join(stageDir, "artifacts"), temporaryGeneration)
      rewriteCompatibilityArtifactProjection(
        path.join(temporaryGeneration, "legacy-trace.json"),
        temporaryGeneration,
        artifactRewrites,
        legacyAuthority ? ".derived/current" : "",
      )
      fs.renameSync(temporaryGeneration, generationDir)
      generationInstalled = true
      try {
        fsyncDirectory(generationsDir)
      } catch {}
    }
    derivedPublicationFailure("generation_ready")
    const rootFiles = [
      "trace.json",
      "manifest.json",
      "legacy-trace.json",
      "provenance-trace.json",
      "partial/latest.json",
      ...(!legacyAuthority ? ["artifacts"] : []),
    ]
    links = installRootCompatibilityLinks(caseDir, rootFiles)
    derivedPublicationFailure("root_links_ready")
    derivedPublicationFailure("current_swap_ready")
    swapSymlink(currentLink, path.posix.join("generations", generationName))
    currentSwapped = true
    derivedPublicationFailure("current_swapped")
    fs.rmSync(links.backupDir, { recursive: true, force: true })
  } catch (error) {
    if (currentSwapped) {
      if (oldCurrent) swapSymlink(currentLink, oldCurrent)
      else {
        try {
          fs.unlinkSync(currentLink)
        } catch {}
      }
    }
    if (links) rollbackRootCompatibilityLinks(links)
    if (generationInstalled) fs.rmSync(generationDir, { recursive: true, force: true })
    if (preexistingGeneration) {
      try {
        fs.unlinkSync(currentLink)
      } catch {}
      fs.rmSync(path.join(derivedDir, preexistingGeneration), { recursive: true, force: true })
    }
    fs.rmSync(temporaryGeneration, { recursive: true, force: true })
    for (const [directory, existed] of [
      [generationsDir, generationsExisted],
      [derivedDir, derivedExisted],
    ] as const) {
      if (existed) continue
      try {
        fs.rmdirSync(directory)
      } catch {}
    }
    throw error
  } finally {
    fs.rmSync(temporaryGeneration, { recursive: true, force: true })
  }
}

type MaterializationSource = {
  key: string
  runID?: string
  caseID?: string
  pathPrefix: string
  recordsFile: string
  descriptor?: TraceSegmentDescriptor
}

function readSession(caseDir: string) {
  const file = path.join(caseDir, "session.json")
  const session = readTraceSessionManifest(file)
  if (!session) return undefined
  if (!session.segments.length) throw new Error(`${file}: trace session has no segments`)
  return session
}

function materializationSources(caseDir: string, session: TraceSessionManifest | undefined): MaterializationSource[] {
  if (!session)
    return [
      {
        key: "legacy-flat",
        pathPrefix: "",
        recordsFile: path.join(caseDir, "records.jsonl"),
      },
    ]
  return session.segments.map((descriptor) => ({
    key: descriptor.segment_id,
    runID: descriptor.run_id,
    caseID: descriptor.case_id,
    pathPrefix: descriptor.path,
    recordsFile: resolveTraceManifestPath(caseDir, descriptor.records),
    descriptor,
  }))
}

function materializeTraceSnapshot(input: {
  caseDir: string
  outputDir?: string
  sessionSnapshot: TraceSessionManifest | null
  copyArtifacts?: boolean
}): TraceMaterializationResult {
  const caseDir = path.resolve(input.caseDir)
  if (!fs.statSync(caseDir).isDirectory()) throw new Error(`${caseDir}: expected a case directory`)
  const outputDir = path.resolve(input.outputDir ?? caseDir)
  const session = input.sessionSnapshot ?? undefined
  const sources = materializationSources(caseDir, session)
  fs.mkdirSync(outputDir, { recursive: true })
  const indexPath = path.join(
    outputDir,
    `.trace-materializer.${process.pid}.${Math.random().toString(16).slice(2)}.sqlite`,
  )
  const index = new ReplayIndex(indexPath)
  try {
    memoryPhase("materialize_start")
    const namespace = session !== undefined && sources.length > 1
    const replays = sources.map((source) => {
      const scope: SegmentReplayScope = {
        key: source.key,
        segmentID: source.key,
        runID: source.runID ?? "",
        caseID: source.caseID ?? "",
        pathPrefix: source.pathPrefix,
        namespace,
        idMaps: {
          node: new Map(),
          edge: new Map(),
          artifact: new Map(),
          diagnostic: new Map(),
        },
        nodeAliases: new Map(),
      }
      discoverSegmentEntityIDs(source.recordsFile, scope)
      index.beginSegment(scope)
      const recovered = recoverJournal(source.recordsFile, index)
      const identities = index.identities
      if (source.runID && identities.runID !== source.runID)
        throw new Error(`${source.recordsFile}: descriptor run identity does not match journal`)
      if (source.caseID && identities.caseID !== source.caseID)
        throw new Error(`${source.recordsFile}: descriptor case identity does not match journal`)
      return {
        source,
        identities,
        close: index.close,
        envelope: index.envelope,
        terminalOperation: index.terminalOperation,
        counts: {
          nodes: index.segmentCount("nodes", source.key),
          edges: index.segmentCount("edges", source.key),
          artifacts: index.segmentCount("artifacts", source.key),
        },
        recoveredLines: index.currentRecoveredLines,
        lastPayloadHash: index.finalPayloadHash,
        ...recovered,
      }
    })
    const byRunID = new Map(replays.map((replay) => [replay.identities.runID, replay]))
    for (const replay of replays) {
      const previousRunID = replay.source.descriptor?.continuation_of
      if (!previousRunID) continue
      const previous = byRunID.get(previousRunID)
      if (!previous) throw new Error(`${replay.source.recordsFile}: continuation run ${previousRunID} is missing`)
      index.addContinuation({
        segmentKey: replay.source.key,
        runID: replay.identities.runID,
        caseID: replay.identities.caseID,
        previousRunID,
        fromSegmentKey: previous.source.key,
      })
    }
    const droppedLines = replays.reduce((total, replay) => total + replay.droppedLines, 0)
    const journalBytes = replays.reduce((total, replay) => total + replay.journalBytes, 0)
    const latest = replays.at(-1)!
    const validTerminal = replays.findLast((replay) => replay.close && replay.envelope?.manifest)
    const terminal = latest
    const previousTerminal = terminal.close ? undefined : validTerminal
    const complete = droppedLines === 0 && replays.every((replay) => replay.close !== undefined)
    const completeness = complete ? "complete" : "incomplete"
    const terminalManifest = terminal.envelope?.manifest ?? {}
    const runStart = index.runStart(terminal.source.key)
    const runData = runStart?.data ?? {}
    const closeResult = record(terminalManifest.result)
    const publicResult = closeResult
      ? Object.fromEntries(Object.entries(closeResult).filter(([key]) => key !== "open_lifecycle"))
      : terminalManifest.result
    const terminalStatus =
      terminalManifest.status === "success" ||
      terminalManifest.status === "error" ||
      terminalManifest.status === "cancelled"
        ? terminalManifest.status
        : (terminal.close?.status ?? "error")
    const historicalInterruptions = Boolean(
      session && replays.some((replay) => replay.droppedLines > 0 || !replay.close),
    )
    const status = terminal.close ? terminalStatus : "error"
    const runID = terminal.identities.runID
    const caseID = session?.logical_case_id ?? latest.identities.caseID
    const effectiveSegments = session?.segments.map((descriptor) => {
      const replay = replays.find((candidate) => candidate.source.descriptor?.segment_id === descriptor.segment_id)
      if (!replay) return descriptor
      if (replay.close) {
        const reconciled =
          replay.close.status === "success" ? "completed" : replay.close.status === "error" ? "failed" : "cancelled"
        return descriptor.status === reconciled ? descriptor : { ...descriptor, status: reconciled }
      }
      if (descriptor.status !== "running" && descriptor.status !== "interrupted_unfinalized")
        return { ...descriptor, status: "interrupted_unfinalized" as const }
      return descriptor
    })
    const sourceFiles = record(terminalManifest.files) ?? {}
    const activeRecords = terminal.source.descriptor?.records ?? "records.jsonl"
    const activeRawEvents = terminal.source.descriptor
      ? path.join(terminal.source.descriptor.path, "raw-events.jsonl")
      : "raw-events.jsonl"
    const manifest: Record<string, unknown> = {
      ...terminalManifest,
      trace_version: TRACE_VERSION,
      case_id: caseID,
      run_id: runID,
      started_at: terminalManifest.started_at ?? runStart?.timestamp,
      ended_at: terminalManifest.ended_at ?? terminal.close?.closed_at,
      ...((terminalManifest.duration_ms ??
        (runStart && terminal.close?.closed_at
          ? Math.max(0, Date.parse(terminal.close.closed_at) - Date.parse(runStart.timestamp))
          : undefined)) === undefined
        ? {}
        : {
            duration_ms:
              terminalManifest.duration_ms ??
              Math.max(0, Date.parse(terminal.close!.closed_at) - Date.parse(runStart!.timestamp)),
          }),
      status,
      server_status: terminalManifest.server_status ?? status,
      process_status: terminalManifest.process_status ?? status,
      case_status: terminalManifest.case_status ?? status,
      ...(status === "success"
        ? { case_completed_at: terminalManifest.case_completed_at ?? terminal.close?.closed_at }
        : {}),
      collection_mode: terminalManifest.collection_mode ?? "passive_sidecar",
      behavior_impact: terminalManifest.behavior_impact ?? "none",
      subject_revision: terminalManifest.subject_revision ?? runData.subject_revision,
      input: terminalManifest.input ?? runData.input,
      environment: terminalManifest.environment ?? runData.environment,
      token_usage: terminalManifest.token_usage ?? {},
      result: publicResult,
      ...(terminalManifest.error === undefined ? {} : { error: terminalManifest.error }),
      ...(typeof closeResult?.signal === "string"
        ? {
            server_shutdown_reason: "process_signal",
            shutdown_signal: closeResult.signal,
            shutdown_disposition: "interrupted_before_case_completion",
          }
        : { shutdown_disposition: terminalManifest.shutdown_disposition ?? "normal_case_completion" }),
      ...(previousTerminal?.envelope?.manifest
        ? {
            previous_terminal: {
              ...previousTerminal.envelope.manifest,
              run_id: previousTerminal.identities.runID,
            },
          }
        : {}),
      ...((session?.session_id ?? terminal.close?.manifest.session_id)
        ? { session_id: session?.session_id ?? terminal.close?.manifest.session_id }
        : {}),
      ...(session
        ? {
            session_generation: session.generation,
            segments: effectiveSegments,
            segment_summary: {
              count: effectiveSegments!.length,
              completed: effectiveSegments!.filter((segment) => segment.status === "completed").length,
              failed: effectiveSegments!.filter((segment) => segment.status === "failed").length,
              cancelled: effectiveSegments!.filter((segment) => segment.status === "cancelled").length,
              interrupted_unfinalized: effectiveSegments!.filter(
                (segment) => segment.status === "interrupted_unfinalized",
              ).length,
              running: effectiveSegments!.filter((segment) => segment.status === "running").length,
            },
          }
        : {}),
      files: {
        ...sourceFiles,
        trace: "trace.json",
        legacy_trace: "legacy-trace.json",
        provenance_trace: "provenance-trace.json",
        session: session ? "session.json" : undefined,
        records: activeRecords,
        raw_events: activeRawEvents,
        partial_latest: "partial/latest.json",
      },
      ...(completeness === "complete"
        ? {}
        : {
            recovery_status: "incomplete_journal_replay",
            ...(terminalManifest.recovery_status === undefined
              ? {}
              : { source_recovery_status: terminalManifest.recovery_status }),
            ...(terminalManifest.recovery === undefined ? {} : { source_recovery: terminalManifest.recovery }),
            recovery: {
              dropped_lines: droppedLines,
              segments: replays
                .filter((replay) => replay.droppedLines > 0 || !replay.close)
                .map((replay) => ({
                  run_id: replay.identities.runID,
                  path: path.relative(caseDir, replay.source.recordsFile),
                  dropped_lines: replay.droppedLines,
                  status: replay.close ? "recovered" : "interrupted_unfinalized",
                })),
            },
            ...(historicalInterruptions
              ? {
                  historical_interruptions: true,
                  session_recovery: {
                    status: "incomplete_segment_history",
                    dropped_lines: droppedLines,
                    segments: replays
                      .filter((replay) => replay.droppedLines > 0 || !replay.close)
                      .map((replay) => ({
                        run_id: replay.identities.runID,
                        path: path.relative(caseDir, replay.source.recordsFile),
                        dropped_lines: replay.droppedLines,
                        status: replay.close ? "recovered" : "interrupted_unfinalized",
                      })),
                  },
                }
              : {}),
          }),
    }
    if (terminal.terminalOperation === "case.runtime_closed" && terminal.close) {
      index.enrichRuntimeTerminal({
        segmentKey: terminal.source.key,
        status,
        closedAt: terminal.close.closed_at,
        ...(record(terminal.close.result) ? { result: record(terminal.close.result)! } : {}),
        ...(terminal.close.error === undefined ? {} : { error: terminal.close.error }),
      })
    }
    index.reconcileDiagnostics()
    memoryPhase("diagnostics_complete", journalBytes)
    const metrics = aggregateMetrics(replays, index, terminal.identities.runID, namespace)
    const poisoned = replays.some(
      (replay) =>
        replay.source.descriptor !== undefined && replay.source.descriptor.status !== "running" && !replay.close,
    )
    const journal = session
      ? {
          schema_version: CAUSAL_IR_VERSION,
          format: "causal-ir-segmented-jsonl",
          path: "session.json",
          summary_scope: "entries_before_lifecycle_entry",
          entry_count: index.recoveredLines,
          last_sequence: index.recoveredLines,
          last_payload_hash: latest.lastPayloadHash,
          poisoned,
          segments: replays.map((replay) => ({
            run_id: replay.identities.runID,
            path: path.relative(caseDir, replay.source.recordsFile),
            entry_count: replay.recoveredLines,
            last_sequence: replay.recoveredLines,
            last_payload_hash: replay.lastPayloadHash,
            dropped_lines: replay.droppedLines,
          })),
        }
      : {
          schema_version: CAUSAL_IR_VERSION,
          format: "causal-ir-jsonl",
          path: "records.jsonl",
          summary_scope: "entries_before_lifecycle_entry",
          entry_count: index.recoveredLines,
          last_sequence: index.recoveredLines,
          last_payload_hash: index.finalPayloadHash,
          poisoned: false,
        }
    const traceFile = path.join(outputDir, "trace.json")
    const manifestFile = path.join(outputDir, "manifest.json")
    const partialFile = path.join(outputDir, "partial", "latest.json")
    const provenanceFile = path.join(outputDir, "provenance-trace.json")
    const legacyFile = path.join(outputDir, "legacy-trace.json")
    writeStreamingJsonObjectAtomic(traceFile, traceMembers(index, manifest, metrics, journal))
    memoryPhase("trace_complete", journalBytes)
    writeStreamingJsonObjectAtomic(manifestFile, Object.entries(manifest))
    memoryPhase("manifest_complete", journalBytes)
    linkFileAtomic(traceFile, partialFile)
    memoryPhase("partial_complete", journalBytes)
    writeStreamingJsonObjectAtomic(provenanceFile, provenanceMembers(index, manifest, metrics))
    memoryPhase("provenance_complete", journalBytes)
    const legacySource = fs.existsSync(path.join(path.dirname(terminal.source.recordsFile), "legacy-trace.json"))
      ? terminal
      : undefined
    if (legacySource) {
      copyFileAtomic(path.join(path.dirname(legacySource.source.recordsFile), "legacy-trace.json"), legacyFile)
      linkCompatibilityArtifacts(
        path.join(path.dirname(legacySource.source.recordsFile), "artifacts"),
        path.join(outputDir, "artifacts"),
        input.copyArtifacts === true,
      )
    } else {
      writeStreamingJsonObjectAtomic(legacyFile, legacyMembers(index, manifest, metrics))
    }
    return { caseDir, traceFile, manifestFile, partialFile, completeness, recoveredLines: index.recoveredLines }
  } finally {
    index.closeIndex()
    try {
      fs.unlinkSync(indexPath)
    } catch {}
  }
}

export function materializeTrace(input: {
  caseDir: string
  outputDir?: string
  _lockHeld?: boolean
}): TraceMaterializationResult {
  const caseDir = path.resolve(input.caseDir)
  const locked = <T>(operation: () => T) =>
    input._lockHeld
      ? operation()
      : withTraceSessionLock(traceSessionLockRootForCase(caseDir), TRACE_MANIFEST_LOCK_KEY, operation)
  if (input.outputDir)
    return locked(() =>
      materializeTraceSnapshot({
        caseDir,
        outputDir: input.outputDir,
        sessionSnapshot: readSession(caseDir) ?? null,
        copyArtifacts: true,
      }),
    )

  const initial = locked(() => {
    const session = readSession(caseDir)
    if (session) return { session }
    return { result: materializeTraceSnapshot({ caseDir, sessionSnapshot: null }) }
  })
  if (initial.result) return initial.result
  let session = initial.session

  for (let attempt = 0; attempt < 4; attempt += 1) {
    const stageDir = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-trace-publication-"))
    try {
      const staged = materializeTraceSnapshot({ caseDir, outputDir: stageDir, sessionSnapshot: session })
      const readyFile = process.env.OPENCODE_TRACE_MATERIALIZER_STAGE_READY_FILE
      if (readyFile) fs.writeFileSync(readyFile, `${session.generation}\n`)
      const publication = locked(() => {
        const latest = readSession(caseDir)
        if (!latest || latest.generation !== session.generation) return { latest, published: false }
        publishStagedTrace(stageDir, caseDir, session.generation)
        return { latest, published: true }
      })
      if (publication.published)
        return {
          ...staged,
          caseDir,
          traceFile: path.join(caseDir, "trace.json"),
          manifestFile: path.join(caseDir, "manifest.json"),
          partialFile: path.join(caseDir, "partial", "latest.json"),
        }
      if (!publication.latest) throw new Error(`${caseDir}: trace session disappeared during materialization`)
      session = publication.latest
    } finally {
      fs.rmSync(stageDir, { recursive: true, force: true })
    }
  }
  throw new Error(`${caseDir}: trace session changed repeatedly during materialization`)
}
