import { createHash } from "node:crypto"
import {
  isFormalRecordType,
  normalizeRelationDetails,
  type FormalDataflowRelation,
} from "./trace-semantic-contract"
import type { DataflowEdge, ProvenanceRecord } from "./case-trace"

export const CAUSAL_IR_VERSION = "1.0" as const

export type CausalIRRef = {
  ref_type: "node" | "artifact" | "raw_event" | "external"
  ref_id: string
  legacy_ref?: string
  label?: string
}

export type CausalIROrigin = "observed" | "deterministic_derived" | "offline_derived"

export type CausalIRDerivation = {
  algorithm: string
  algorithm_version: string
  derived_at: string
  input_refs: CausalIRRef[]
  rule_id?: string
  reproducible: boolean
}

export type CausalNodeLike = {
  node_id: string
  kind: string
  schema_version?: string
  origin?: CausalIROrigin
  component?: ProvenanceRecord["component"]
  span_id?: string
  parent_span_id?: string
  timestamp: string
  time_ms: number
  title?: string
  status?: ProvenanceRecord["status"]
  input_refs?: string[]
  output_refs?: string[]
  data?: Record<string, unknown>
  source_refs?: string[]
  source_locations?: ProvenanceRecord["source_locations"]
  typed_resources?: ProvenanceRecord["typed_resources"]
  artifact_refs?: string[]
  aliases?: string[]
  derivation?: CausalIRDerivation
  metadata?: Record<string, unknown>
}

export type CausalIRNode = {
  node_id: string
  kind: string
  schema_version: string
  origin: CausalIROrigin
  component: NonNullable<ProvenanceRecord["component"]>
  status?: ProvenanceRecord["status"]
  title?: string
  order: {
    sequence: number
    timestamp: string
    time_ms: number
  }
  scope: {
    run_id: string
    case_id: string
    session_id?: string
    message_id?: string
    part_id?: string
    call_id?: string
    span_id?: string
    parent_span_id?: string
    agent_id?: string
    parent_agent_id?: string
  }
  payload: Record<string, unknown>
  input_refs: CausalIRRef[]
  output_refs: CausalIRRef[]
  source_refs: CausalIRRef[]
  source_locations: NonNullable<ProvenanceRecord["source_locations"]>
  artifact_refs: string[]
  aliases: string[]
  derivation: CausalIRDerivation | null
  integrity: {
    payload_hash: string
    source_hash?: string
  }
  metadata?: Record<string, unknown>
  // Compatibility aliases remain a superset; projectors do not treat them as the canonical envelope.
  timestamp: string
  time_ms: number
  span_id?: string
  parent_span_id?: string
  data: Record<string, unknown>
  typed_resources?: ProvenanceRecord["typed_resources"]
  legacy_input_refs?: string[]
  legacy_output_refs?: string[]
  legacy_source_refs?: string[]
}

export type CausalEvidenceTier = "confirmed" | "content_matched" | "temporal_advisory"

export type CausalEdgeLike = {
  edge_id: string
  from: DataflowEdge["from"]
  to: DataflowEdge["to"]
  relation: string
  original_relation?: string
  normalized_relation?: FormalDataflowRelation
  evidence_tier?: CausalEvidenceTier
  eligible_for_attribution?: boolean
  derivation_method?: string
  evidence_refs?: string[]
  confidence?: number
  label?: string
  metadata?: Record<string, unknown>
}

export type CausalIREdge = {
  edge_id: string
  from: CausalIRRef
  to: CausalIRRef
  original_relation: string
  normalized_relation: FormalDataflowRelation
  evidence_tier: CausalEvidenceTier
  eligible_for_attribution: boolean
  derivation_method: string
  evidence_refs: CausalIRRef[]
  confidence?: number
  label?: string
  metadata?: Record<string, unknown>
}

export type ArtifactLike = {
  artifact_id: string
  hash: string
  path: string
  [key: string]: unknown
}

export type CausalIRDiagnosticLike = {
  diagnostic_id: string
  [key: string]: unknown
}

export type CausalIRNodeInput = CausalNodeLike
export type CausalIREdgeInput = CausalEdgeLike

export type CausalIRJournalEntry = {
  sequence: number
  time: string
  run_id: string
  case_id: string
  operation:
    | "node.created"
    | "node.updated"
    | "edge.created"
    | "artifact.created"
    | "artifact.reused"
    | "diagnostic.created"
    | "case.checkpointed"
    | "case.finalized"
  record_type: string
  entity_id?: string
  previous_payload_hash?: string
  payload_hash?: string
  data: unknown
}

export type CausalIRJournalSummary = {
  schema_version: typeof CAUSAL_IR_VERSION
  format: "causal-ir-jsonl"
  path: "records.jsonl"
  summary_scope: "entries_before_lifecycle_entry"
  entry_count: number
  last_sequence: number
  last_payload_hash?: string
  poisoned: boolean
}

export type CausalIRCommitResult = {
  committed: boolean
  operation: CausalIRJournalEntry["operation"]
  sequence: number
  payload_hash: string | undefined
  poisoned: boolean
}

export type CausalIRStoreSnapshot = {
  version: typeof CAUSAL_IR_VERSION
  runID: string
  caseID: string
  nodes: CausalIRNode[]
  edges: CausalIREdge[]
  artifacts: ArtifactLike[]
  diagnostics: CausalIRDiagnosticLike[]
}

export type CausalIRTraceDocument = {
  trace_version: string
  causal_ir_version: typeof CAUSAL_IR_VERSION
  manifest: Record<string, unknown>
  nodes: CausalIRNode[]
  edges: CausalIREdge[]
  artifacts: ArtifactLike[]
  journal: CausalIRJournalSummary
  metrics: Record<string, unknown>
  diagnostics: CausalIRDiagnosticLike[]
  compatibility: Record<string, unknown>
  records: unknown[]
  dataflow_edges: unknown[]
}

export type ProvenanceProjectionInput = {
  traceVersion: string
  manifest: {
    case_id: string
    run_id: string
    [key: string]: unknown
  }
  metrics: {
    spans?: number
    events?: number
    records?: number
    dataflow_edges?: number
    artifacts?: number
    token_usage: Record<string, unknown>
    trace_health: {
      issues: unknown[]
      [key: string]: unknown
    }
    [key: string]: unknown
  }
}

export type ProvenanceCompatibilityRecord = ProvenanceRecord

export type ProvenanceCompatibilityDataflowEdge = Omit<DataflowEdge, "metadata"> & {
  metadata: Record<string, unknown> & {
    original_relation: string
    normalized_relation: FormalDataflowRelation
    evidence_tier: CausalEvidenceTier
    eligible_for_attribution: boolean
    derivation_method: string
  }
}

export type ProvenanceTraceProjection = {
  trace_version: string
  manifest: ProvenanceProjectionInput["manifest"]
  records: ProvenanceCompatibilityRecord[]
  dataflow_edges: ProvenanceCompatibilityDataflowEdge[]
  artifacts: ArtifactLike[]
  metrics: ProvenanceProjectionInput["metrics"] & {
    spans: number
    events: number
    records: number
    dataflow_edges: number
    artifacts: number
  }
}

type CausalIRLifecycleJournalData = {
  snapshot: CausalIRStoreSnapshot
  data: unknown
  trace?: CausalIRTraceDocument
}

type CausalIRStoreInput = {
  runID: string
  caseID: string
  append?: (entry: CausalIRJournalEntry) => unknown
}

type CanonicalRelationAttributes = {
  original_relation: string
  normalized_relation: FormalDataflowRelation
  evidence_tier: CausalEvidenceTier
  eligible_for_attribution: boolean
  derivation_method: string
}

const EVIDENCE_TIERS = new Set<CausalEvidenceTier>(["confirmed", "content_matched", "temporal_advisory"])

function evidenceTier(input: unknown): CausalEvidenceTier | undefined {
  return typeof input === "string" && EVIDENCE_TIERS.has(input as CausalEvidenceTier)
    ? (input as CausalEvidenceTier)
    : undefined
}

function originalRelation(edge: CausalEdgeLike | CausalIREdge) {
  if (typeof edge.original_relation === "string") return edge.original_relation
  return "relation" in edge ? edge.relation : edge.normalized_relation
}

function canonicalRelationAttributes(edge: CausalEdgeLike): CanonicalRelationAttributes {
  const details = normalizeRelationDetails(originalRelation(edge))
  const metadataEligible = edge.metadata?.eligible_for_attribution
  const declaredEligible = edge.eligible_for_attribution ?? (typeof metadataEligible === "boolean" ? metadataEligible : undefined)
  const metadataEvidenceTier = edge.metadata?.evidence_tier
  const metadataDerivationMethod = edge.metadata?.derivation_method
  const tier = evidenceTier(edge.evidence_tier) ?? evidenceTier(metadataEvidenceTier) ?? "confirmed"

  return {
    original_relation: details.original,
    normalized_relation: details.normalized,
    evidence_tier: tier,
    eligible_for_attribution: details.known && tier !== "temporal_advisory" && declaredEligible !== false,
    derivation_method:
      edge.derivation_method ??
      (typeof metadataDerivationMethod === "string" ? metadataDerivationMethod : undefined) ??
      (details.known ? "explicit_relation" : "unknown_relation_fallback"),
  }
}

function canonicalEdge(edge: CausalEdgeLike): CausalEdgeLike {
  return {
    ...edge,
    ...canonicalRelationAttributes(edge),
  }
}

function normalizeEdges(edges: CausalEdgeLike[]) {
  const normalized: CausalEdgeLike[] = []
  const indexes = new Map<string, number>()
  for (const edge of edges) {
    const canonical = canonicalEdge(edge)
    const index = indexes.get(canonical.edge_id)
    if (index === undefined) {
      indexes.set(canonical.edge_id, normalized.length)
      normalized.push(canonical)
    } else {
      normalized[index] = canonical
    }
  }
  return normalized
}

function unknownRelationDiagnostic(edge: CausalEdgeLike | CausalIREdge): CausalIRDiagnosticLike | undefined {
  const details = normalizeRelationDetails(originalRelation(edge))
  if (details.known) return undefined
  return {
    diagnostic_id: `unknown_relation:${edge.edge_id}`,
    kind: "unknown_relation",
    level: "warning",
    message: `Unknown causal relation: ${details.original}`,
    edge_id: edge.edge_id,
    relation: details.original,
  }
}

function isUnknownRelationDiagnostic(diagnostic: CausalIRDiagnosticLike) {
  return diagnostic.kind === "unknown_relation" && typeof diagnostic.edge_id === "string"
}

function currentUnknownRelationDiagnostics(edges: Array<CausalEdgeLike | CausalIREdge>) {
  return edges.flatMap((edge) => {
    const diagnostic = unknownRelationDiagnostic(edge)
    return diagnostic ? [diagnostic] : []
  })
}

function reconciledDiagnostics(
  edges: Array<CausalEdgeLike | CausalIREdge>,
  diagnostics: CausalIRDiagnosticLike[],
) {
  return [
    ...diagnostics.filter((diagnostic) => !isUnknownRelationDiagnostic(diagnostic)),
    ...currentUnknownRelationDiagnostics(edges),
  ]
}

function canonicalJSON(input: unknown, arrayValue = false): string | undefined {
  if (input === null) return "null"

  switch (typeof input) {
    case "boolean":
    case "number":
    case "string":
      return JSON.stringify(input)
    case "undefined":
    case "function":
    case "symbol":
      return arrayValue ? "null" : undefined
    case "bigint":
      throw new TypeError("Do not know how to serialize a BigInt")
  }

  if (Array.isArray(input)) {
    const values: string[] = []
    for (let index = 0; index < input.length; index++) {
      values.push(canonicalJSON(input[index], true) ?? "null")
    }
    return `[${values.join(",")}]`
  }

  const value = input as Record<string, unknown>
  if (typeof value.toJSON === "function") return canonicalJSON(value.toJSON(), arrayValue)

  return `{${Object.keys(value)
    .sort((left, right) => (left === right ? 0 : left < right ? -1 : 1))
    .flatMap((key) => {
      const serialized = canonicalJSON(value[key])
      return serialized === undefined ? [] : [`${JSON.stringify(key)}:${serialized}`]
    })
    .join(",")}}`
}

function payloadHash(data: unknown) {
  const payload = canonicalJSON(data) ?? "null"
  return createHash("sha256").update(payload).digest("hex")
}

function refTypeForLegacy(type: string): CausalIRRef["ref_type"] {
  if (type === "artifact") return "artifact"
  if (type === "raw_event" || type === "event") return "raw_event"
  if (["external", "file", "path", "uri", "url"].includes(type)) return "external"
  return "node"
}

function typedRef(input: string | CausalIRRef | DataflowEdge["from"]): CausalIRRef {
  if (typeof input !== "string" && "ref_type" in input && "ref_id" in input) {
    return {
      ref_type: input.ref_type,
      ref_id: input.ref_id,
      legacy_ref: input.legacy_ref,
      label: input.label,
    }
  }

  if (typeof input !== "string") {
    return {
      ref_type: refTypeForLegacy(input.type),
      ref_id: input.id,
      legacy_ref: `${input.type}:${input.id}`,
      label: input.label,
    }
  }

  const separator = input.indexOf(":")
  const type = separator === -1 ? "external" : input.slice(0, separator)
  const id = separator === -1 ? input : input.slice(separator + 1)
  return {
    ref_type: refTypeForLegacy(type),
    ref_id: id,
    legacy_ref: input,
  }
}

function legacyRef(ref: CausalIRRef) {
  return ref.legacy_ref ?? `${ref.ref_type}:${ref.ref_id}`
}

type CausalIRRefInput = string | CausalIRRef | DataflowEdge["from"]
type CausalIRRefResolver = (input: CausalIRRefInput) => CausalIRRef

function textField(input: Record<string, unknown> | undefined, keys: string[]) {
  if (!input) return undefined
  for (const key of keys) {
    const value = input[key]
    if (typeof value === "string" && value) return value
  }
  return undefined
}

function nodeAliases(node: CausalNodeLike, payload: Record<string, unknown>) {
  const aliases = new Set([...(node.aliases ?? []), `record:${node.node_id}`, `node:${node.node_id}`])
  const add = (type: string, id: unknown) => {
    if (typeof id === "string" && id) aliases.add(`${type}:${id}`)
  }
  const addTypes = (types: string[], keys: string[] = []) => {
    for (const type of types) {
      add(type, node.node_id)
      for (const key of keys) add(type, payload[key])
    }
  }

  switch (node.kind) {
    case "prompt.assembly":
      addTypes(["prompt"])
      break
    case "context.transform":
    case "context.pack":
      addTypes(["context"])
      break
    case "context.compaction_check":
      addTypes(["compaction_check"], ["check_id"])
      break
    case "context.compaction":
      addTypes(["compaction"], ["compaction_id"])
      break
    case "llm.call":
      addTypes(["llm_request"])
      add("span", node.span_id)
      break
    case "llm.turn":
      addTypes(["llm_turn", "llm"], ["turn_id"])
      break
    case "exit.gate":
      addTypes(["exit_gate"], ["gate_id"])
      break
    case "decision":
      addTypes(["decision"], ["decision_id"])
      break
    case "tool.call":
      addTypes(["tool_call"], ["call_id", "callID", "tool_call_id"])
      break
    case "tool.result":
      addTypes(["tool_result"], ["call_id", "callID", "tool_call_id"])
      break
    case "tool.error":
      addTypes(["tool_error"], ["call_id", "callID", "tool_call_id"])
      break
    case "mcp.call":
      addTypes(["mcp_call"], ["call_id", "callID"])
      break
    case "skill.load":
      addTypes(["skill"], ["skill_id"])
      break
    case "subagent.call":
      addTypes(["subagent_task"], ["task_id", "taskID"])
      break
    case "observation":
    case "execution.observation":
      addTypes(["observation"], ["observation_id"])
      break
    case "evidence.fact":
    case "evidence.semantic_fact":
      addTypes(["evidence", "repo_fact"], ["fact_id"])
      break
    case "change":
      addTypes(["change"], ["change_id"])
      break
    case "verification":
      addTypes(["verification"], ["verification_id"])
      break
    case "response.output":
    case "final.claim":
      addTypes(["response_segment", "final_response_evidence"], ["segment_id"])
      break
    case "response.claim":
      addTypes(["response_claim"], ["claim_id"])
      break
    case "claim.support_assessment":
      addTypes(["claim_support"], ["assessment_id"])
      break
    case "task.obligation": {
      const obligationType = payload.obligation_type
      if (typeof obligationType === "string") addTypes([obligationType])
      break
    }
  }

  return [...aliases]
}

function isValidTypedRef(input: unknown): input is CausalIRRef {
  if (!input || typeof input !== "object" || Array.isArray(input)) return false
  const ref = input as Partial<CausalIRRef>
  return (
    (ref.ref_type === "node" ||
      ref.ref_type === "artifact" ||
      ref.ref_type === "raw_event" ||
      ref.ref_type === "external") &&
    typeof ref.ref_id === "string" &&
    ref.ref_id.length > 0
  )
}

function validateNodeProvenance(node: CausalNodeLike) {
  const origin = node.origin ?? "observed"
  const derivation = node.derivation
  if (origin === "observed") {
    if (derivation !== undefined) throw new TypeError("observed node cannot declare derivation provenance")
    return
  }
  if (!derivation) throw new TypeError("derived node requires derivation provenance")
  if (!(node.input_refs?.length)) throw new TypeError("derived node requires non-empty input refs")
  if (!derivation.input_refs.length || !derivation.input_refs.every(isValidTypedRef))
    throw new TypeError("derived node requires non-empty typed derivation input refs")
  if (!derivation.algorithm || !derivation.algorithm_version || !derivation.derived_at)
    throw new TypeError("derived node requires algorithm, version, and derived_at provenance")
  if (typeof derivation.reproducible !== "boolean")
    throw new TypeError("derived node requires an explicit reproducible flag")
  if (origin === "deterministic_derived" && derivation.reproducible !== true)
    throw new TypeError("deterministic derived node must be reproducible")
}

function isCanonicalNode(input: unknown): input is CausalIRNode {
  if (!input || typeof input !== "object" || Array.isArray(input)) return false
  const value = input as Partial<CausalIRNode>
  return (
    typeof value.node_id === "string" &&
    typeof value.schema_version === "string" &&
    !!value.order &&
    typeof value.order === "object" &&
    !!value.scope &&
    typeof value.scope === "object" &&
    !!value.payload &&
    typeof value.payload === "object" &&
    Array.isArray(value.input_refs) &&
    Array.isArray(value.output_refs) &&
    Array.isArray(value.source_refs)
  )
}

function canonicalNodeEnvelope(
  node: CausalNodeLike,
  input: { runID: string; caseID: string; sequence: number },
  resolveRef: CausalIRRefResolver = typedRef,
): CausalIRNode {
  const payload = journalData(node.data ?? {})
  const inputRefs = (node.input_refs ?? []).map(resolveRef)
  const outputRefs = (node.output_refs ?? []).map(resolveRef)
  const sourceRefs = (node.source_refs ?? []).map(resolveRef)
  const sourceLocations = journalData(node.source_locations ?? [])
  const artifactRefs = [...(node.artifact_refs ?? [])]
  const metadata = node.metadata ? journalData(node.metadata) : undefined
  const scopeSources = [payload, metadata]
  const scoped = (keys: string[]) => scopeSources.map((item) => textField(item, keys)).find(Boolean)
  const aliases = nodeAliases(node, payload)
  const sourceHash = payloadHash({ inputRefs, outputRefs, sourceRefs, sourceLocations, artifactRefs })
  const derivation = node.derivation
    ? {
        ...journalData(node.derivation),
        input_refs: node.derivation.input_refs.map(resolveRef),
      }
    : null

  return {
    node_id: node.node_id,
    kind: node.kind,
    schema_version: node.schema_version ?? CAUSAL_IR_VERSION,
    origin: node.origin ?? "observed",
    component: node.component ?? "trace",
    status: node.status,
    title: node.title,
    order: {
      sequence: input.sequence,
      timestamp: node.timestamp,
      time_ms: node.time_ms,
    },
    scope: {
      run_id: input.runID,
      case_id: input.caseID,
      session_id: scoped(["session_id", "sessionID"]),
      message_id: scoped(["message_id", "messageID"]),
      part_id: scoped(["part_id", "partID"]),
      call_id: scoped(["call_id", "callID", "tool_call_id"]),
      span_id: node.span_id,
      parent_span_id: node.parent_span_id,
      agent_id: scoped(["agent_id", "agentID", "agent"]),
      parent_agent_id: scoped(["parent_agent_id", "parentAgentID"]),
    },
    payload,
    input_refs: inputRefs,
    output_refs: outputRefs,
    source_refs: sourceRefs,
    source_locations: sourceLocations,
    artifact_refs: artifactRefs,
    aliases,
    derivation,
    integrity: {
      payload_hash: payloadHash(payload),
      source_hash: sourceHash,
    },
    metadata,
    timestamp: node.timestamp,
    time_ms: node.time_ms,
    span_id: node.span_id,
    parent_span_id: node.parent_span_id,
    data: journalData(payload),
    typed_resources: node.typed_resources ? journalData(node.typed_resources) : undefined,
    legacy_input_refs: node.input_refs ? [...node.input_refs] : undefined,
    legacy_output_refs: node.output_refs ? [...node.output_refs] : undefined,
    legacy_source_refs: node.source_refs ? [...node.source_refs] : undefined,
  }
}

function canonicalNodeFromUnknown(
  input: unknown,
  context: { runID: string; caseID: string; sequence: number },
): CausalIRNode | undefined {
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined
  if (isCanonicalNode(input)) return journalData(input)
  return canonicalNodeEnvelope(input as CausalNodeLike, context)
}

function isCanonicalEdge(input: unknown): input is CausalIREdge {
  if (!input || typeof input !== "object" || Array.isArray(input)) return false
  const value = input as Partial<CausalIREdge>
  return Boolean(
    typeof value.edge_id === "string" &&
      value.from &&
      typeof value.from === "object" &&
      "ref_type" in value.from &&
      value.to &&
      typeof value.to === "object" &&
      "ref_type" in value.to,
  )
}

function canonicalEdgeEnvelope(
  input: CausalEdgeLike | CausalIREdge,
  resolveRef: CausalIRRefResolver = typedRef,
): CausalIREdge {
  if (isCanonicalEdge(input)) {
    const details = normalizeRelationDetails(input.original_relation)
    const tier = evidenceTier(input.evidence_tier) ?? "confirmed"
    return {
      ...journalData(input),
      from: resolveRef(input.from),
      to: resolveRef(input.to),
      original_relation: details.original,
      normalized_relation: details.normalized,
      evidence_tier: tier,
      eligible_for_attribution: details.known && tier !== "temporal_advisory" && input.eligible_for_attribution !== false,
      derivation_method: input.derivation_method ?? (details.known ? "explicit_relation" : "unknown_relation_fallback"),
      evidence_refs: (input.evidence_refs ?? []).map(resolveRef),
    }
  }

  const attributes = canonicalRelationAttributes(input)
  return {
    edge_id: input.edge_id,
    from: resolveRef(input.from),
    to: resolveRef(input.to),
    original_relation: attributes.original_relation,
    normalized_relation: attributes.normalized_relation,
    evidence_tier: attributes.evidence_tier,
    eligible_for_attribution: attributes.eligible_for_attribution,
    derivation_method: attributes.derivation_method,
    evidence_refs: (input.evidence_refs ?? []).map(resolveRef),
    confidence: input.confidence,
    label: input.label,
    metadata: input.metadata ? journalData(input.metadata) : undefined,
  }
}

function journalData<T>(data: T): T {
  return structuredClone(data)
}

function replaceByID<T extends Record<string, unknown>>(items: T[], idKey: keyof T, item: T) {
  const index = items.findIndex((candidate) => candidate[idKey] === item[idKey])
  if (index === -1) items.push(item)
  else items[index] = item
}

function replaceAll<T>(target: T[], items: T[]) {
  target.splice(0, target.length, ...items)
}

function isLifecycleJournalData(input: unknown): input is CausalIRLifecycleJournalData {
  if (!input || typeof input !== "object" || Array.isArray(input)) return false
  const data = input as Partial<CausalIRLifecycleJournalData>
  const snapshot = data.snapshot
  return (
    !!snapshot &&
    typeof snapshot === "object" &&
    Array.isArray(snapshot.nodes) &&
    Array.isArray(snapshot.edges) &&
    Array.isArray(snapshot.artifacts) &&
    Array.isArray(snapshot.diagnostics)
  )
}

function isCausalIRTraceDocument(input: unknown): input is CausalIRTraceDocument {
  if (!input || typeof input !== "object" || Array.isArray(input)) return false
  const value = input as Partial<CausalIRTraceDocument>
  return (
    value.causal_ir_version === CAUSAL_IR_VERSION &&
    typeof value.trace_version === "string" &&
    !!value.manifest &&
    typeof value.manifest === "object" &&
    Array.isArray(value.nodes) &&
    Array.isArray(value.edges) &&
    Array.isArray(value.artifacts) &&
    Array.isArray(value.diagnostics) &&
    !!value.journal &&
    typeof value.journal === "object" &&
    !!value.metrics &&
    typeof value.metrics === "object" &&
    !!value.compatibility &&
    typeof value.compatibility === "object"
  )
}

export class CausalIRStore {
  readonly nodes: CausalNodeLike[] = []
  readonly edges: CausalEdgeLike[] = []
  readonly artifacts: ArtifactLike[] = []
  readonly diagnostics: CausalIRDiagnosticLike[] = []

  private sequence = 0
  private nodeSequence = 0
  private poisoned = false
  private lastJournalPayloadHash: string | undefined
  private readonly payloadHashes = new Map<string, string>()
  private readonly nodeOrders = new Map<string, number>()
  private readonly nodeAliasIndex = new Map<string, string>()
  private readonly edgeIndexes = new Map<string, number>()
  private readonly diagnosticIndexes = new Map<string, number>()

  constructor(private readonly input: CausalIRStoreInput) {}

  createNode<T extends CausalNodeLike>(node: T): T {
    validateNodeProvenance(node)
    this.nodes.push(node)
    this.rebuildNodeAliasIndex()
    const sequence = this.nodeOrders.get(node.node_id) ?? ++this.nodeSequence
    this.nodeOrders.set(node.node_id, sequence)
    this.append("node.created", "node", node.node_id, this.canonicalNode(node, sequence))
    this.reconcileReferenceDiagnostics()
    return node
  }

  updateNode<T extends CausalNodeLike>(node: T): T {
    validateNodeProvenance(node)
    replaceByID(this.nodes, "node_id", node)
    this.rebuildNodeAliasIndex()
    const sequence = this.nodeOrders.get(node.node_id) ?? ++this.nodeSequence
    this.nodeOrders.set(node.node_id, sequence)
    this.append("node.updated", "node.update", node.node_id, this.canonicalNode(node, sequence), "node")
    this.reconcileReferenceDiagnostics()
    return node
  }

  replaceNodes(nodes: CausalNodeLike[]): void {
    for (const node of nodes) validateNodeProvenance(node)
    replaceAll(this.nodes, nodes)
    this.rebuildNodeAliasIndex()
    const nextOrders = new Map<string, number>()
    for (const node of nodes) {
      const sequence = this.nodeOrders.get(node.node_id) ?? ++this.nodeSequence
      nextOrders.set(node.node_id, sequence)
    }
    this.nodeOrders.clear()
    for (const [nodeID, sequence] of nextOrders) this.nodeOrders.set(nodeID, sequence)
    this.rebuildPayloadHashes("node", this.canonicalNodes(), "node_id")
    this.reconcileReferenceDiagnostics()
    this.appendSnapshot("case.checkpointed", "checkpoint", { reason: "nodes.replaced" })
  }

  createEdge<T extends CausalEdgeLike>(edge: T): T {
    const canonical = canonicalEdge(edge)
    const index = this.edgeIndexes.get(canonical.edge_id)
    if (index === undefined) {
      this.edgeIndexes.set(canonical.edge_id, this.edges.length)
      this.edges.push(canonical)
    } else {
      this.edges[index] = canonical
    }
    this.append("edge.created", "edge", canonical.edge_id, canonicalEdgeEnvelope(canonical, this.resolveRef))
    this.reconcileUnknownRelationDiagnostic(canonical)
    this.reconcileReferenceDiagnostics(canonical.edge_id)
    return canonical as T
  }

  replaceEdges(edges: CausalEdgeLike[]): void {
    const canonical = normalizeEdges(edges)
    replaceAll(this.edges, canonical)
    this.rebuildEdgeIndexes()
    this.rebuildPayloadHashes("edge", this.canonicalEdges(), "edge_id")
    this.reconcileAllUnknownRelationDiagnostics()
    this.reconcileReferenceDiagnostics()
    this.appendSnapshot("case.checkpointed", "checkpoint", { reason: "edges.replaced" })
  }

  createArtifact<T extends ArtifactLike>(artifact: T): T {
    this.artifacts.push(artifact)
    this.append("artifact.created", "artifact", artifact.artifact_id, artifact)
    return artifact
  }

  reuseArtifact<T extends ArtifactLike>(artifact: T): T {
    replaceByID(this.artifacts, "artifact_id", artifact)
    this.append("artifact.reused", "artifact.reuse", artifact.artifact_id, artifact, "artifact")
    return artifact
  }

  createDiagnostic<T extends CausalIRDiagnosticLike>(diagnostic: T): T {
    this.upsertDiagnostic(diagnostic)
    this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
    return diagnostic
  }

  checkpoint(data: unknown): void {
    this.appendSnapshot("case.checkpointed", "checkpoint", data)
  }

  finalize(data: unknown): CausalIRCommitResult {
    return this.appendSnapshot("case.finalized", "finish", data)
  }

  snapshot(): CausalIRStoreSnapshot {
    return {
      version: CAUSAL_IR_VERSION,
      runID: this.input.runID,
      caseID: this.input.caseID,
      nodes: this.canonicalNodes(),
      edges: this.canonicalEdges(),
      artifacts: this.artifacts,
      diagnostics: this.diagnostics,
    }
  }

  journalSummary(): CausalIRJournalSummary {
    return {
      schema_version: CAUSAL_IR_VERSION,
      format: "causal-ir-jsonl",
      path: "records.jsonl",
      summary_scope: "entries_before_lifecycle_entry",
      entry_count: this.sequence,
      last_sequence: this.sequence,
      last_payload_hash: this.lastJournalPayloadHash,
      poisoned: this.poisoned,
    }
  }

  private appendSnapshot(operation: "case.checkpointed" | "case.finalized", recordType: string, data: unknown) {
    const trace = isCausalIRTraceDocument(data) ? journalData(data) : undefined
    return this.append(
      operation,
      recordType,
      this.input.caseID,
      {
        snapshot: this.snapshot(),
        data: trace?.manifest ?? data,
        ...(trace ? { trace } : {}),
      },
      "case",
    )
  }

  private readonly resolveRef: CausalIRRefResolver = (input) => {
    const ref = typedRef(input)
    if (ref.ref_type !== "node") return ref
    const legacy = legacyRef(ref)
    const nodeID =
      this.nodeAliasIndex.get(legacy) ??
      this.nodeAliasIndex.get(`node:${ref.ref_id}`) ??
      this.nodeAliasIndex.get(`record:${ref.ref_id}`)
    if (!nodeID) {
      return {
        ...ref,
        ref_type: "external",
        legacy_ref: legacy,
      }
    }
    return {
      ...ref,
      ref_type: "node",
      ref_id: nodeID,
      legacy_ref: ref.legacy_ref ?? legacy,
    }
  }

  private rebuildNodeAliasIndex() {
    this.nodeAliasIndex.clear()
    for (const node of this.nodes) {
      for (const alias of nodeAliases(node, node.data ?? {})) {
        if (!this.nodeAliasIndex.has(alias)) this.nodeAliasIndex.set(alias, node.node_id)
      }
    }
  }

  private referenceDiagnostics(edgeID?: string) {
    const diagnostics = new Map<string, CausalIRDiagnosticLike>()
    const inspect = (
      owner: { type: "node" | "edge"; id: string },
      field: string,
      input: CausalIRRefInput,
    ) => {
      const legacy = typedRef(input)
      if (legacy.ref_type !== "node" || this.resolveRef(input).ref_type !== "external") return
      const legacyValue = legacyRef(legacy)
      const diagnostic: CausalIRDiagnosticLike = {
        diagnostic_id: `unresolved_ref:${owner.id}:${field}:${payloadHash(legacyValue).slice(0, 16)}`,
        kind: "unresolved_ref",
        level: "warning",
        message: `Unresolved causal reference: ${legacyValue}`,
        owner_type: owner.type,
        field,
        legacy_ref: legacyValue,
        ...(owner.type === "node" ? { node_id: owner.id } : { edge_id: owner.id }),
      }
      diagnostics.set(diagnostic.diagnostic_id, diagnostic)
    }

    if (edgeID === undefined) {
      for (const node of this.nodes) {
        for (const [index, ref] of (node.input_refs ?? []).entries())
          inspect({ type: "node", id: node.node_id }, `input_refs[${index}]`, ref)
        for (const [index, ref] of (node.output_refs ?? []).entries())
          inspect({ type: "node", id: node.node_id }, `output_refs[${index}]`, ref)
        for (const [index, ref] of (node.source_refs ?? []).entries())
          inspect({ type: "node", id: node.node_id }, `source_refs[${index}]`, ref)
        for (const [index, ref] of (node.derivation?.input_refs ?? []).entries())
          inspect({ type: "node", id: node.node_id }, `derivation.input_refs[${index}]`, ref)
      }
    }
    const indexedEdge = edgeID === undefined ? undefined : this.edges[this.edgeIndexes.get(edgeID) ?? -1]
    const edges = edgeID === undefined ? this.edges : indexedEdge ? [indexedEdge] : []
    for (const edge of edges) {
      inspect({ type: "edge", id: edge.edge_id }, "from", edge.from)
      inspect({ type: "edge", id: edge.edge_id }, "to", edge.to)
      for (const [index, ref] of (edge.evidence_refs ?? []).entries())
        inspect({ type: "edge", id: edge.edge_id }, `evidence_refs[${index}]`, ref)
    }
    return [...diagnostics.values()]
  }

  private reconcileReferenceDiagnostics(edgeID?: string) {
    const inScope = (diagnostic: CausalIRDiagnosticLike) =>
      diagnostic.kind === "unresolved_ref" && (edgeID === undefined || diagnostic.edge_id === edgeID)
    const current = new Map(
      this.diagnostics
        .filter(inScope)
        .map((diagnostic) => [diagnostic.diagnostic_id, diagnostic]),
    )
    const next = this.referenceDiagnostics(edgeID)
    replaceAll(
      this.diagnostics,
      [...this.diagnostics.filter((diagnostic) => !inScope(diagnostic)), ...next],
    )
    this.rebuildDiagnosticIndexes()
    for (const diagnostic of next) {
      if (current.has(diagnostic.diagnostic_id)) continue
      this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
    }
  }

  private canonicalNode(node: CausalNodeLike, sequence: number) {
    return canonicalNodeEnvelope(
      node,
      { runID: this.input.runID, caseID: this.input.caseID, sequence },
      this.resolveRef,
    )
  }

  private canonicalNodes() {
    return this.nodes.map((node, index) =>
      this.canonicalNode(node, this.nodeOrders.get(node.node_id) ?? index + 1),
    )
  }

  private canonicalEdges() {
    return this.edges.map((edge) => canonicalEdgeEnvelope(edge, this.resolveRef))
  }

  private rebuildEdgeIndexes() {
    this.edgeIndexes.clear()
    for (let index = 0; index < this.edges.length; index++) this.edgeIndexes.set(this.edges[index]!.edge_id, index)
  }

  private rebuildDiagnosticIndexes() {
    this.diagnosticIndexes.clear()
    for (let index = 0; index < this.diagnostics.length; index++) {
      this.diagnosticIndexes.set(this.diagnostics[index]!.diagnostic_id, index)
    }
  }

  private upsertDiagnostic(diagnostic: CausalIRDiagnosticLike) {
    const index = this.diagnosticIndexes.get(diagnostic.diagnostic_id)
    if (index === undefined) {
      this.diagnosticIndexes.set(diagnostic.diagnostic_id, this.diagnostics.length)
      this.diagnostics.push(diagnostic)
    } else {
      this.diagnostics[index] = diagnostic
    }
  }

  private removeDiagnostic(diagnosticID: string) {
    const index = this.diagnosticIndexes.get(diagnosticID)
    if (index === undefined) return
    this.diagnostics.splice(index, 1)
    this.rebuildDiagnosticIndexes()
  }

  private reconcileUnknownRelationDiagnostic(edge: CausalEdgeLike) {
    const next = unknownRelationDiagnostic(edge)
    const diagnosticID = `unknown_relation:${edge.edge_id}`
    const existingIndex = this.diagnosticIndexes.get(diagnosticID)
    const existing = existingIndex === undefined ? undefined : this.diagnostics[existingIndex]
    if (!next) {
      this.removeDiagnostic(diagnosticID)
      return
    }
    if (existing?.relation === next.relation) return
    this.upsertDiagnostic(next)
    this.append("diagnostic.created", "diagnostic", next.diagnostic_id, next)
  }

  private reconcileAllUnknownRelationDiagnostics() {
    const existing = new Map(
      this.diagnostics
        .filter(isUnknownRelationDiagnostic)
        .map((diagnostic) => [diagnostic.diagnostic_id, diagnostic]),
    )
    const next = reconciledDiagnostics(this.edges, this.diagnostics)
    replaceAll(this.diagnostics, next)
    this.rebuildDiagnosticIndexes()
    for (const diagnostic of next.filter(isUnknownRelationDiagnostic)) {
      if (existing.get(diagnostic.diagnostic_id)?.relation === diagnostic.relation) continue
      this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
    }
  }

  private rebuildPayloadHashes<T extends Record<string, unknown>>(hashType: string, items: T[], idKey: keyof T) {
    const prefix = `${hashType}:`
    for (const key of this.payloadHashes.keys()) {
      if (key.startsWith(prefix)) this.payloadHashes.delete(key)
    }

    for (const item of items) {
      const entityID = item[idKey]
      if (typeof entityID === "string") this.payloadHashes.set(`${hashType}:${entityID}`, payloadHash(item))
    }
  }

  private append(
    operation: CausalIRJournalEntry["operation"],
    recordType: string,
    entityID: string | undefined,
    data: unknown,
    hashType = recordType,
  ) {
    if (this.poisoned) {
      return {
        committed: false,
        operation,
        sequence: this.sequence,
        payload_hash: undefined,
        poisoned: true,
      } satisfies CausalIRCommitResult
    }
    const payloadHashValue = payloadHash(data)
    const hashKey = entityID ? `${hashType}:${entityID}` : undefined
    const entry: CausalIRJournalEntry = {
      sequence: this.sequence + 1,
      time: new Date().toISOString(),
      run_id: this.input.runID,
      case_id: this.input.caseID,
      operation,
      record_type: recordType,
      entity_id: entityID,
      previous_payload_hash: hashKey ? this.payloadHashes.get(hashKey) : undefined,
      payload_hash: payloadHashValue,
      data: journalData(data),
    }

    try {
      if (this.input.append?.(entry) === false) {
        this.poisoned = true
        return {
          committed: false,
          operation,
          sequence: this.sequence,
          payload_hash: undefined,
          poisoned: true,
        } satisfies CausalIRCommitResult
      }
    } catch {
      this.poisoned = true
      return {
        committed: false,
        operation,
        sequence: this.sequence,
        payload_hash: undefined,
        poisoned: true,
      } satisfies CausalIRCommitResult
    }

    this.sequence = entry.sequence
    if (hashKey) this.payloadHashes.set(hashKey, payloadHashValue)
    this.lastJournalPayloadHash = payloadHashValue
    return {
      committed: true,
      operation,
      sequence: entry.sequence,
      payload_hash: payloadHashValue,
      poisoned: false,
    } satisfies CausalIRCommitResult
  }
}

export function replayCausalIRJournal(journal: unknown[]): CausalIRStoreSnapshot {
  const nodes: CausalIRNode[] = []
  const edges: CausalIREdge[] = []
  const artifacts: ArtifactLike[] = []
  const diagnostics: CausalIRDiagnosticLike[] = []
  let runID = ""
  let caseID = ""

  const replaceEdges = (input: unknown[]) => {
    const next: CausalIREdge[] = []
    const indexes = new Map<string, number>()
    for (const item of input) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue
      const edge = canonicalEdgeEnvelope(item as CausalEdgeLike | CausalIREdge)
      const index = indexes.get(edge.edge_id)
      if (index === undefined) {
        indexes.set(edge.edge_id, next.length)
        next.push(edge)
      } else {
        next[index] = edge
      }
    }
    replaceAll(edges, next)
  }

  for (const item of journal) {
    if (!item || typeof item !== "object") continue
    const entry = item as Partial<CausalIRJournalEntry>
    if (typeof entry.run_id === "string") runID = entry.run_id
    if (typeof entry.case_id === "string") caseID = entry.case_id

    if ((entry.operation === "node.created" || entry.operation === "node.updated") && entry.data && typeof entry.data === "object") {
      const node = canonicalNodeFromUnknown(entry.data, {
        runID,
        caseID,
        sequence: typeof entry.sequence === "number" ? entry.sequence : nodes.length + 1,
      })
      if (node) replaceByID(nodes, "node_id", node)
      continue
    }

    if (entry.operation === "edge.created" && entry.data && typeof entry.data === "object") {
      const canonical = canonicalEdgeEnvelope(entry.data as CausalEdgeLike | CausalIREdge)
      replaceByID(edges, "edge_id", canonical)
      continue
    }

    if ((entry.operation === "artifact.created" || entry.operation === "artifact.reused") && entry.data && typeof entry.data === "object") {
      replaceByID(artifacts, "artifact_id", entry.data as ArtifactLike)
      continue
    }

    if (entry.operation === "diagnostic.created" && entry.data && typeof entry.data === "object") {
      replaceByID(diagnostics, "diagnostic_id", entry.data as CausalIRDiagnosticLike)
      continue
    }

    if ((entry.operation === "case.checkpointed" || entry.operation === "case.finalized") && isLifecycleJournalData(entry.data)) {
      const snapshot = entry.data.snapshot
      if (typeof snapshot.runID === "string") runID = snapshot.runID
      if (typeof snapshot.caseID === "string") caseID = snapshot.caseID
      replaceAll(
        nodes,
        (snapshot.nodes as unknown[]).flatMap((node, index) => {
          const canonical = canonicalNodeFromUnknown(node, { runID, caseID, sequence: index + 1 })
          return canonical ? [canonical] : []
        }),
      )
      replaceEdges(snapshot.edges as unknown[])
      replaceAll(artifacts, journalData(snapshot.artifacts))
      replaceAll(diagnostics, journalData(snapshot.diagnostics))
    }
  }

  return {
    version: CAUSAL_IR_VERSION,
    runID,
    caseID,
    nodes,
    edges,
    artifacts,
    diagnostics: reconciledDiagnostics(edges, diagnostics),
  }
}

export function replayCausalIRTrace(journal: unknown[]): CausalIRTraceDocument | undefined {
  let trace: CausalIRTraceDocument | undefined
  for (const item of journal) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue
    const entry = item as Partial<CausalIRJournalEntry>
    if (entry.operation !== "case.checkpointed" && entry.operation !== "case.finalized") continue
    if (!isLifecycleJournalData(entry.data) || !isCausalIRTraceDocument(entry.data.trace)) continue
    trace = journalData(entry.data.trace)
  }
  return trace
}

function optionalNumber(input: unknown) {
  if (input === undefined || input === null) return undefined
  const value = Number(input)
  return Number.isFinite(value) ? value : undefined
}

function compatibilityTokenUsage(input: unknown): ProvenanceRecord["token_usage"] {
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined
  const usage = input as Record<string, unknown>
  const output: NonNullable<ProvenanceRecord["token_usage"]> = {}
  for (const key of ["input", "output", "reasoning", "cached_input", "cache_write", "total", "cost"] as const) {
    const value = usage[key]
    if (typeof value === "number" && Number.isFinite(value)) output[key] = Math.max(0, value)
  }
  return Object.keys(output).length ? output : undefined
}

function provenanceRef(ref: DataflowEdge["from"]): DataflowEdge["from"] {
  return {
    ...ref,
    type: ref.type === "final_response_evidence" ? "response_segment" : ref.type,
    label: ref.label === "final.claim" ? "response.output" : ref.label,
  }
}

function compatibilityRef(ref: CausalIRRef): DataflowEdge["from"] {
  const value = legacyRef(ref)
  const separator = value.indexOf(":")
  return {
    type: separator === -1 ? ref.ref_type : value.slice(0, separator),
    id: separator === -1 ? ref.ref_id : value.slice(separator + 1),
    label: ref.label,
  }
}

function compatibilityRefs(refs: CausalIRRef[]) {
  return refs.length ? refs.map(legacyRef) : undefined
}

function provenanceLabel(label: string | undefined) {
  if (!label) return undefined
  return label.replace(/final response evidence/gi, "response output").replace(/final claim/gi, "response output")
}

function projectedEdgeMetadata(edge: CausalIREdge, attributes: CanonicalRelationAttributes) {
  const metadata = { ...edge.metadata }
  delete metadata.original_relation
  delete metadata.normalized_relation
  delete metadata.evidence_tier
  delete metadata.eligible_for_attribution
  delete metadata.derivation_method

  return {
    ...metadata,
    original_relation: attributes.original_relation,
    normalized_relation: attributes.normalized_relation,
    evidence_tier: attributes.evidence_tier,
    eligible_for_attribution: attributes.eligible_for_attribution,
    derivation_method: attributes.derivation_method,
  }
}

export function projectProvenanceTrace(
  snapshot: CausalIRStoreSnapshot,
  input: ProvenanceProjectionInput,
): ProvenanceTraceProjection {
  const records = snapshot.nodes
    .map((node) => ({ node, eventType: node.kind === "final.claim" ? "response.output" : node.kind }))
    .filter((item) => isFormalRecordType(item.eventType))
    .map(({ node, eventType }): ProvenanceCompatibilityRecord => ({
      record_id: node.node_id,
      component: node.component,
      event_type: eventType,
      span_id: node.scope.span_id,
      parent_span_id: node.scope.parent_span_id,
      timestamp: node.order.timestamp,
      time_ms: node.order.time_ms,
      title: node.title,
      status: node.status,
      duration_ms: optionalNumber(node.payload.duration_ms),
      token_usage: compatibilityTokenUsage(node.payload.token_usage),
      error: node.payload.error,
      input_refs: compatibilityRefs(node.input_refs),
      output_refs: compatibilityRefs(node.output_refs),
      source_refs: compatibilityRefs(node.source_refs),
      source_locations: node.source_locations,
      typed_resources: node.typed_resources,
      artifact_refs: node.artifact_refs,
      data: node.payload,
      metadata: node.metadata,
    }))
  const dataflowEdges = snapshot.edges.map((edge) => {
    const attributes: CanonicalRelationAttributes = {
      original_relation: edge.original_relation,
      normalized_relation: edge.normalized_relation,
      evidence_tier: edge.evidence_tier,
      eligible_for_attribution: edge.eligible_for_attribution,
      derivation_method: edge.derivation_method,
    }

    return {
      edge_id: edge.edge_id,
      from: provenanceRef(compatibilityRef(edge.from)),
      to: provenanceRef(compatibilityRef(edge.to)),
      relation: attributes.normalized_relation,
      label: provenanceLabel(edge.label),
      metadata: projectedEdgeMetadata(edge, attributes),
    } satisfies ProvenanceCompatibilityDataflowEdge
  })
  const spans = new Set(records.flatMap((record) => (record.span_id ? [record.span_id] : []))).size

  return {
    trace_version: input.traceVersion,
    manifest: input.manifest,
    records,
    dataflow_edges: dataflowEdges,
    artifacts: snapshot.artifacts,
    metrics: {
      ...input.metrics,
      spans: input.metrics.spans ?? spans,
      events: input.metrics.events ?? records.length,
      records: records.length,
      dataflow_edges: dataflowEdges.length,
      artifacts: snapshot.artifacts.length,
    },
  }
}
