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

function aliasCollisionDiagnostic(alias: string, owners: Iterable<string>): CausalIRDiagnosticLike | undefined {
  const ownerIDs = [...owners].sort((left, right) => (left === right ? 0 : left < right ? -1 : 1))
  if (ownerIDs.length < 2) return undefined
  return {
    diagnostic_id: `alias_collision:${payloadHash(alias).slice(0, 16)}`,
    kind: "alias_collision",
    level: "warning",
    message: `Ambiguous causal alias: ${alias}`,
    alias,
    owner_ids: ownerIDs,
  }
}

function unresolvedReferenceDiagnostic(
  owner: { type: "node" | "edge"; id: string },
  field: string,
  legacyValue: string,
): CausalIRDiagnosticLike {
  return {
    diagnostic_id: `unresolved_ref:${owner.id}:${field}:${payloadHash(legacyValue).slice(0, 16)}`,
    kind: "unresolved_ref",
    level: "warning",
    message: `Unresolved causal reference: ${legacyValue}`,
    owner_type: owner.type,
    field,
    legacy_ref: legacyValue,
    ...(owner.type === "node" ? { node_id: owner.id } : { edge_id: owner.id }),
  }
}

function currentAliasCollisionDiagnostics(nodes: CausalIRNode[]) {
  const owners = new Map<string, Set<string>>()
  for (const node of nodes) {
    for (const alias of node.aliases) {
      const current = owners.get(alias) ?? new Set<string>()
      current.add(node.node_id)
      owners.set(alias, current)
    }
  }
  return [...owners.entries()].flatMap(([alias, nodeIDs]) => {
    const diagnostic = aliasCollisionDiagnostic(alias, nodeIDs)
    return diagnostic ? [diagnostic] : []
  })
}

function currentCanonicalReferenceDiagnostics(nodes: CausalIRNode[], edges: CausalIREdge[]) {
  const diagnostics = new Map<string, CausalIRDiagnosticLike>()
  const inspect = (owner: { type: "node" | "edge"; id: string }, field: string, ref: CausalIRRef) => {
    if (ref.ref_type !== "external" || !ref.legacy_ref) return
    const legacy = typedRef(ref.legacy_ref)
    if (legacy.ref_type !== "node") return
    const diagnostic = unresolvedReferenceDiagnostic(owner, field, legacyRef(legacy))
    diagnostics.set(diagnostic.diagnostic_id, diagnostic)
  }
  for (const node of nodes) {
    for (const [index, ref] of node.input_refs.entries())
      inspect({ type: "node", id: node.node_id }, `input_refs[${index}]`, ref)
    for (const [index, ref] of node.output_refs.entries())
      inspect({ type: "node", id: node.node_id }, `output_refs[${index}]`, ref)
    for (const [index, ref] of node.source_refs.entries())
      inspect({ type: "node", id: node.node_id }, `source_refs[${index}]`, ref)
    for (const [index, ref] of (node.derivation?.input_refs ?? []).entries())
      inspect({ type: "node", id: node.node_id }, `derivation.input_refs[${index}]`, ref)
  }
  for (const edge of edges) {
    inspect({ type: "edge", id: edge.edge_id }, "from", edge.from)
    inspect({ type: "edge", id: edge.edge_id }, "to", edge.to)
    for (const [index, ref] of edge.evidence_refs.entries())
      inspect({ type: "edge", id: edge.edge_id }, `evidence_refs[${index}]`, ref)
  }
  return [...diagnostics.values()]
}

function reconciledGraphDiagnostics(
  nodes: CausalIRNode[],
  edges: CausalIREdge[],
  diagnostics: CausalIRDiagnosticLike[],
) {
  const generatedKinds = new Set(["unknown_relation", "alias_collision", "unresolved_ref"])
  const generated = [
    ...currentUnknownRelationDiagnostics(edges),
    ...currentAliasCollisionDiagnostics(nodes),
    ...currentCanonicalReferenceDiagnostics(nodes, edges),
  ].sort((left, right) =>
    left.diagnostic_id === right.diagnostic_id ? 0 : left.diagnostic_id < right.diagnostic_id ? -1 : 1,
  )
  return [...diagnostics.filter((diagnostic) => !generatedKinds.has(String(diagnostic.kind))), ...generated]
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

function referenceAliasCandidates(input: CausalIRRefInput) {
  const ref = typedRef(input)
  if (ref.ref_type !== "node") return []
  return [...new Set([legacyRef(ref), `node:${ref.ref_id}`, `record:${ref.ref_id}`])]
}

function resolveReferenceWithOwners(input: CausalIRRefInput, aliasOwners: Map<string, Set<string>>): CausalIRRef {
  const ref = typedRef(input)
  if (ref.ref_type !== "node") return ref
  const legacy = legacyRef(ref)
  let nodeID: string | undefined
  for (const alias of referenceAliasCandidates(input)) {
    const owners = aliasOwners.get(alias)
    if (!owners?.size) continue
    if (owners.size > 1) {
      nodeID = undefined
      break
    }
    nodeID = owners.values().next().value
    break
  }
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

const OMIT_TEMPORAL_REFERENCE = Symbol("omit-temporal-reference")

function temporalSelector(input: unknown) {
  return typeof input === "string" && input.startsWith("recent_") ? input : undefined
}

function temporalSelectorFromRef(input: unknown) {
  const direct = temporalSelector(input)
  if (direct) return direct
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined
  const ref = input as Record<string, unknown>
  return (
    temporalSelector(ref.legacy_ref) ??
    temporalSelector(ref.ref_id) ??
    temporalSelector(ref.id) ??
    temporalSelector(ref.type) ??
    temporalSelector(ref.source_ref)
  )
}

function semanticRefField(input: string) {
  const normalized = input
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
  return /(^|_)(?:ref|refs)(?:_|$)/.test(normalized)
}

function structuredReferenceObject(input: Record<string, unknown>) {
  return [
    "ref_type",
    "ref_id",
    "legacy_ref",
    "type",
    "id",
    "source_ref",
    "target_ref",
    "relation",
    "inference",
    "confidence",
    "label",
  ].some((key) => key in input)
}

export function normalizeTemporalReferences<T>(input: T): { value: T; selectors: string[] } {
  const selectors = new Set<string>()
  const omit = (selector: string) => {
    selectors.add(selector)
    return OMIT_TEMPORAL_REFERENCE
  }
  const visit = (value: unknown, inRefField: boolean): unknown | typeof OMIT_TEMPORAL_REFERENCE => {
    if (typeof value === "string") {
      const selector = inRefField ? temporalSelector(value) : undefined
      return selector ? omit(selector) : value
    }
    if (!value || typeof value !== "object") return value
    if (inRefField) {
      const selector = temporalSelectorFromRef(value)
      if (selector) return omit(selector)
    }
    if (Array.isArray(value)) {
      if (inRefField) {
        return value.flatMap((item) => {
          const normalized = visit(item, true)
          return normalized === OMIT_TEMPORAL_REFERENCE ? [] : [normalized]
        })
      }
      const output = new Array(value.length)
      for (let index = 0; index < value.length; index++) {
        if (!(index in value)) continue
        const normalized = visit(value[index], false)
        if (normalized !== OMIT_TEMPORAL_REFERENCE) output[index] = normalized
      }
      return output
    }
    const prototype = Object.getPrototypeOf(value)
    if (prototype !== Object.prototype && prototype !== null) return value

    const record = value as Record<string, unknown>
    const inheritRefField = inRefField && !structuredReferenceObject(record)
    const output: Record<string, unknown> = {}
    for (const [key, child] of Object.entries(record)) {
      const childIsRef = semanticRefField(key)
      const normalized = visit(child, inheritRefField || childIsRef)
      if (normalized === OMIT_TEMPORAL_REFERENCE) continue
      output[key] = normalized
    }
    return output
  }

  const value = visit(input, false)
  return {
    value: (value === OMIT_TEMPORAL_REFERENCE ? undefined : value) as T,
    selectors: [...selectors],
  }
}

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

function canonicalRefKey(ref: CausalIRRef) {
  return `${ref.ref_type}:${ref.ref_id}`
}

function dedupeCanonicalRefs(refs: CausalIRRef[]) {
  const output = new Map<string, CausalIRRef>()
  for (const ref of refs) {
    const key = canonicalRefKey(ref)
    if (!output.has(key)) output.set(key, ref)
  }
  return [...output.values()]
}

function sameCanonicalRefSet(left: CausalIRRef[], right: CausalIRRef[]) {
  if (left.length !== right.length) return false
  const rightKeys = new Set(right.map(canonicalRefKey))
  return left.every((ref) => rightKeys.has(canonicalRefKey(ref)))
}

type NodeProvenanceValidationContext = {
  resolveRef: CausalIRRefResolver
  hasNode: (nodeID: string) => boolean
  hasArtifact: (artifactID: string) => boolean
}

function validateDerivedInputRef(input: CausalIRRefInput, context: NodeProvenanceValidationContext): CausalIRRef {
  const declared = typedRef(input)
  if (!declared.ref_id) throw new TypeError("derived node requires valid non-empty input refs")
  if (declared.ref_type === "node") {
    const resolved = context.resolveRef(input)
    if (resolved.ref_type !== "node" || !context.hasNode(resolved.ref_id)) {
      throw new TypeError("derived node input ref must resolve to an existing node")
    }
    return resolved
  }
  if (declared.ref_type === "artifact" && !context.hasArtifact(declared.ref_id)) {
    throw new TypeError("derived node input ref must resolve to an existing artifact")
  }
  return declared
}

function validateNodeProvenance(node: CausalNodeLike, context: NodeProvenanceValidationContext) {
  const normalized = normalizeTemporalReferences(node).value
  const origin = normalized.origin ?? "observed"
  const derivation = normalized.derivation
  if (origin === "observed") {
    if (derivation !== undefined) throw new TypeError("observed node cannot declare derivation provenance")
    return
  }
  if (!derivation) throw new TypeError("derived node requires derivation provenance")
  if (!normalized.input_refs?.length) throw new TypeError("derived node requires non-empty input refs")
  if (!derivation.input_refs.length || !derivation.input_refs.every(isValidTypedRef))
    throw new TypeError("derived node requires non-empty typed derivation input refs")
  if (!derivation.algorithm || !derivation.algorithm_version || !derivation.derived_at)
    throw new TypeError("derived node requires algorithm, version, and derived_at provenance")
  if (typeof derivation.reproducible !== "boolean")
    throw new TypeError("derived node requires an explicit reproducible flag")
  if (origin === "deterministic_derived" && derivation.reproducible !== true)
    throw new TypeError("deterministic derived node must be reproducible")
  const inputRefs = dedupeCanonicalRefs(normalized.input_refs.map((ref) => validateDerivedInputRef(ref, context)))
  const derivationRefs = dedupeCanonicalRefs(derivation.input_refs.map((ref) => validateDerivedInputRef(ref, context)))
  if (!inputRefs.length || !derivationRefs.length)
    throw new TypeError("derived node requires valid non-empty input refs")
  if (!sameCanonicalRefSet(inputRefs, derivationRefs))
    throw new TypeError("derived node input refs must canonically match derivation input refs")
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
  const normalized = normalizeTemporalReferences(node).value
  const payload = journalData(normalized.data ?? {})
  const inputRefs = dedupeCanonicalRefs((normalized.input_refs ?? []).map(resolveRef))
  const outputRefs = (normalized.output_refs ?? []).map(resolveRef)
  const sourceRefs = (normalized.source_refs ?? []).map(resolveRef)
  const sourceLocations = journalData(normalized.source_locations ?? [])
  const artifactRefs = [...(normalized.artifact_refs ?? [])]
  const metadata = normalized.metadata ? journalData(normalized.metadata) : undefined
  const scopeSources = [payload, metadata]
  const scoped = (keys: string[]) => scopeSources.map((item) => textField(item, keys)).find(Boolean)
  const aliases = nodeAliases(normalized, payload)
  const sourceHash = payloadHash({ inputRefs, outputRefs, sourceRefs, sourceLocations, artifactRefs })
  const resolvedDerivationRefs = normalized.derivation
    ? dedupeCanonicalRefs(normalized.derivation.input_refs.map(resolveRef))
    : []
  const derivation = normalized.derivation
    ? {
        ...journalData(normalized.derivation),
        input_refs: sameCanonicalRefSet(inputRefs, resolvedDerivationRefs)
          ? inputRefs.map((ref) => ({ ...ref }))
          : resolvedDerivationRefs,
      }
    : null

  return {
    node_id: normalized.node_id,
    kind: normalized.kind,
    schema_version: normalized.schema_version ?? CAUSAL_IR_VERSION,
    origin: normalized.origin ?? "observed",
    component: normalized.component ?? "trace",
    status: normalized.status,
    title: normalized.title,
    order: {
      sequence: input.sequence,
      timestamp: normalized.timestamp,
      time_ms: normalized.time_ms,
    },
    scope: {
      run_id: input.runID,
      case_id: input.caseID,
      session_id: scoped(["session_id", "sessionID"]),
      message_id: scoped(["message_id", "messageID"]),
      part_id: scoped(["part_id", "partID"]),
      call_id: scoped(["call_id", "callID", "tool_call_id"]),
      span_id: normalized.span_id,
      parent_span_id: normalized.parent_span_id,
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
    timestamp: normalized.timestamp,
    time_ms: normalized.time_ms,
    span_id: normalized.span_id,
    parent_span_id: normalized.parent_span_id,
    data: journalData(payload),
    typed_resources: normalized.typed_resources ? journalData(normalized.typed_resources) : undefined,
    legacy_input_refs: normalized.input_refs ? [...normalized.input_refs] : undefined,
    legacy_output_refs: normalized.output_refs ? [...normalized.output_refs] : undefined,
    legacy_source_refs: normalized.source_refs ? [...normalized.source_refs] : undefined,
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
  if (temporalSelectorFromRef(input.from) || temporalSelectorFromRef(input.to)) {
    throw new TypeError("temporal selector cannot be a canonical edge endpoint")
  }
  const normalized = normalizeTemporalReferences(input).value
  if (isCanonicalEdge(normalized)) {
    const details = normalizeRelationDetails(normalized.original_relation)
    const tier = evidenceTier(normalized.evidence_tier) ?? "confirmed"
    return {
      ...journalData(normalized),
      from: resolveRef(normalized.from),
      to: resolveRef(normalized.to),
      original_relation: details.original,
      normalized_relation: details.normalized,
      evidence_tier: tier,
      eligible_for_attribution:
        details.known && tier !== "temporal_advisory" && normalized.eligible_for_attribution !== false,
      derivation_method:
        normalized.derivation_method ?? (details.known ? "explicit_relation" : "unknown_relation_fallback"),
      evidence_refs: (normalized.evidence_refs ?? []).map(resolveRef),
    }
  }

  const attributes = canonicalRelationAttributes(normalized)
  return {
    edge_id: normalized.edge_id,
    from: resolveRef(normalized.from),
    to: resolveRef(normalized.to),
    original_relation: attributes.original_relation,
    normalized_relation: attributes.normalized_relation,
    evidence_tier: attributes.evidence_tier,
    eligible_for_attribution: attributes.eligible_for_attribution,
    derivation_method: attributes.derivation_method,
    evidence_refs: (normalized.evidence_refs ?? []).map(resolveRef),
    confidence: normalized.confidence,
    label: normalized.label,
    metadata: normalized.metadata ? journalData(normalized.metadata) : undefined,
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
  private readonly nodeById = new Map<string, CausalNodeLike>()
  private readonly nodeIndexes = new Map<string, number>()
  private readonly aliasesByNodeID = new Map<string, Set<string>>()
  private readonly aliasOwners = new Map<string, Set<string>>()
  private readonly referencedAliasesByNodeID = new Map<string, Set<string>>()
  private readonly referencedAliasesByEdgeID = new Map<string, Set<string>>()
  private readonly aliasAffectedNodeIDs = new Map<string, Set<string>>()
  private readonly aliasAffectedEdgeIDs = new Map<string, Set<string>>()
  private readonly referenceDiagnosticIDsByOwner = new Map<string, Set<string>>()
  private readonly edgeIndexes = new Map<string, number>()
  private readonly artifactIndexes = new Map<string, number>()
  private readonly artifactById = new Map<string, ArtifactLike>()
  private readonly diagnosticIndexes = new Map<string, number>()

  constructor(private readonly input: CausalIRStoreInput) {}

  resolveReference(input: string | CausalIRRef | DataflowEdge["from"]): CausalIRRef {
    return this.resolveRef(input)
  }

  createNode<T extends CausalNodeLike>(node: T): T {
    return this.upsertNode(node, "node.created", "node")
  }

  updateNode<T extends CausalNodeLike>(node: T): T {
    return this.upsertNode(node, "node.updated", "node.update")
  }

  replaceNodes(nodes: CausalNodeLike[]): void {
    const nodeIDs = new Set(nodes.map((node) => node.node_id))
    const aliasOwners = new Map<string, Set<string>>()
    for (const node of nodes) {
      for (const alias of this.aliasesForNode(node)) {
        const owners = aliasOwners.get(alias) ?? new Set<string>()
        owners.add(node.node_id)
        aliasOwners.set(alias, owners)
      }
    }
    const validationContext: NodeProvenanceValidationContext = {
      resolveRef: (input) => resolveReferenceWithOwners(input, aliasOwners),
      hasNode: (nodeID) => nodeIDs.has(nodeID),
      hasArtifact: (artifactID) => this.artifactById.has(artifactID),
    }
    for (const node of nodes) this.validateNodeProvenance(node, validationContext)
    replaceAll(this.nodes, nodes)
    this.rebuildNodeIndexesAndAliases()
    this.rebuildNodeReferenceIndexes()
    const nextOrders = new Map<string, number>()
    for (const node of nodes) {
      const sequence = this.nodeOrders.get(node.node_id) ?? ++this.nodeSequence
      nextOrders.set(node.node_id, sequence)
    }
    this.nodeOrders.clear()
    for (const [nodeID, sequence] of nextOrders) this.nodeOrders.set(nodeID, sequence)
    this.rebuildPayloadHashes("node", this.canonicalNodes(), "node_id")
    this.rebuildPayloadHashes("edge", this.canonicalEdges(), "edge_id")
    this.reconcileAllAliasCollisionDiagnostics()
    this.reconcileAllReferenceDiagnostics()
    this.appendSnapshot("case.checkpointed", "checkpoint", { reason: "nodes.replaced" })
  }

  createEdge<T extends CausalEdgeLike>(edge: T): T {
    const canonical = canonicalEdge(edge)
    const envelope = canonicalEdgeEnvelope(canonical, this.resolveRef)
    const index = this.edgeIndexes.get(canonical.edge_id)
    if (index === undefined) {
      this.edgeIndexes.set(canonical.edge_id, this.edges.length)
      this.edges.push(canonical)
    } else {
      this.edges[index] = canonical
    }
    this.updateEdgeReferenceIndexes(canonical)
    this.append("edge.created", "edge", canonical.edge_id, envelope)
    this.reconcileUnknownRelationDiagnostic(canonical)
    this.reconcileReferenceDiagnosticsForOwner("edge", canonical.edge_id)
    return canonical as T
  }

  replaceEdges(edges: CausalEdgeLike[]): void {
    const canonical = normalizeEdges(edges)
    const envelopes = canonical.map((edge) => canonicalEdgeEnvelope(edge, this.resolveRef))
    replaceAll(this.edges, canonical)
    this.rebuildEdgeIndexes()
    this.rebuildPayloadHashes("edge", envelopes, "edge_id")
    this.reconcileAllUnknownRelationDiagnostics()
    this.reconcileAllReferenceDiagnostics()
    this.appendSnapshot("case.checkpointed", "checkpoint", { reason: "edges.replaced" })
  }

  createArtifact<T extends ArtifactLike>(artifact: T): T {
    this.artifactIndexes.set(artifact.artifact_id, this.artifacts.length)
    this.artifactById.set(artifact.artifact_id, artifact)
    this.artifacts.push(artifact)
    this.append("artifact.created", "artifact", artifact.artifact_id, artifact)
    return artifact
  }

  reuseArtifact<T extends ArtifactLike>(artifact: T): T {
    const index = this.artifactIndexes.get(artifact.artifact_id)
    if (index === undefined) {
      this.artifactIndexes.set(artifact.artifact_id, this.artifacts.length)
      this.artifacts.push(artifact)
    } else {
      this.artifacts[index] = artifact
    }
    this.artifactById.set(artifact.artifact_id, artifact)
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
    const nodes = this.canonicalNodes()
    const edges = this.canonicalEdges()
    return {
      version: CAUSAL_IR_VERSION,
      runID: this.input.runID,
      caseID: this.input.caseID,
      nodes,
      edges,
      artifacts: this.artifacts,
      diagnostics: reconciledGraphDiagnostics(nodes, edges, this.diagnostics),
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

  private readonly resolveRef: CausalIRRefResolver = (input) => resolveReferenceWithOwners(input, this.aliasOwners)

  private validateNodeProvenance(node: CausalNodeLike, context?: NodeProvenanceValidationContext) {
    validateNodeProvenance(
      node,
      context ?? {
        resolveRef: this.resolveRef,
        hasNode: (nodeID) => this.nodeById.has(nodeID),
        hasArtifact: (artifactID) => this.artifactById.has(artifactID),
      },
    )
  }

  private upsertNode<T extends CausalNodeLike>(
    node: T,
    operation: "node.created" | "node.updated",
    recordType: "node" | "node.update",
  ): T {
    this.validateNodeProvenance(node)
    const nextAliases = this.aliasesForNode(node)
    const previousAliases = this.aliasesByNodeID.get(node.node_id) ?? new Set<string>()
    const changedAliases = new Set<string>()
    for (const alias of previousAliases) if (!nextAliases.has(alias)) changedAliases.add(alias)
    for (const alias of nextAliases) if (!previousAliases.has(alias)) changedAliases.add(alias)
    const affected = this.captureAliasAffectedEntities(changedAliases, node.node_id)

    const index = this.nodeIndexes.get(node.node_id)
    if (index === undefined) {
      this.nodeIndexes.set(node.node_id, this.nodes.length)
      this.nodes.push(node)
    } else {
      this.nodes[index] = node
    }
    this.nodeById.set(node.node_id, node)
    this.applyNodeAliases(node.node_id, previousAliases, nextAliases)
    this.updateNodeReferenceIndexes(node)

    const sequence = this.nodeOrders.get(node.node_id) ?? ++this.nodeSequence
    this.nodeOrders.set(node.node_id, sequence)
    this.append(operation, recordType, node.node_id, this.canonicalNode(node, sequence), "node")
    for (const alias of changedAliases) this.reconcileAliasCollisionDiagnostic(alias)
    this.reconcileReferenceDiagnosticsForOwner("node", node.node_id)
    this.reconcileAliasAffectedEntities(affected)
    return node
  }

  private aliasesForNode(node: CausalNodeLike) {
    const normalized = normalizeTemporalReferences(node).value
    return new Set(nodeAliases(normalized, normalized.data ?? {}))
  }

  private applyNodeAliases(nodeID: string, previous: Set<string>, next: Set<string>) {
    for (const alias of previous) {
      if (next.has(alias)) continue
      const owners = this.aliasOwners.get(alias)
      owners?.delete(nodeID)
      if (!owners?.size) this.aliasOwners.delete(alias)
    }
    for (const alias of next) {
      if (previous.has(alias)) continue
      const owners = this.aliasOwners.get(alias) ?? new Set<string>()
      owners.add(nodeID)
      this.aliasOwners.set(alias, owners)
    }
    this.aliasesByNodeID.set(nodeID, next)
  }

  private captureAliasAffectedEntities(aliases: Iterable<string>, excludedNodeID: string) {
    const nodes = new Map<string, CausalIRNode>()
    const edges = new Map<string, CausalIREdge>()
    for (const alias of aliases) {
      for (const nodeID of this.aliasAffectedNodeIDs.get(alias) ?? []) {
        if (nodeID === excludedNodeID || nodes.has(nodeID)) continue
        const node = this.nodeById.get(nodeID)
        if (!node) continue
        nodes.set(nodeID, this.canonicalNode(node, this.nodeOrders.get(nodeID) ?? 1))
      }
      for (const edgeID of this.aliasAffectedEdgeIDs.get(alias) ?? []) {
        if (edges.has(edgeID)) continue
        const index = this.edgeIndexes.get(edgeID)
        const edge = index === undefined ? undefined : this.edges[index]
        if (edge) edges.set(edgeID, canonicalEdgeEnvelope(edge, this.resolveRef))
      }
    }
    return { nodes, edges }
  }

  private reconcileAliasAffectedEntities(affected: {
    nodes: Map<string, CausalIRNode>
    edges: Map<string, CausalIREdge>
  }) {
    for (const [nodeID, before] of [...affected.nodes.entries()].sort(([left], [right]) =>
      left === right ? 0 : left < right ? -1 : 1,
    )) {
      const node = this.nodeById.get(nodeID)
      if (!node) continue
      const after = this.canonicalNode(node, this.nodeOrders.get(nodeID) ?? 1)
      if (payloadHash(before) !== payloadHash(after)) {
        this.append("node.updated", "node.update", nodeID, after, "node")
      }
      this.reconcileReferenceDiagnosticsForOwner("node", nodeID)
    }
    for (const [edgeID, before] of [...affected.edges.entries()].sort(([left], [right]) =>
      left === right ? 0 : left < right ? -1 : 1,
    )) {
      const index = this.edgeIndexes.get(edgeID)
      const edge = index === undefined ? undefined : this.edges[index]
      if (!edge) continue
      const after = canonicalEdgeEnvelope(edge, this.resolveRef)
      if (payloadHash(before) !== payloadHash(after)) {
        this.append("edge.created", "edge", edgeID, after)
      }
      this.reconcileReferenceDiagnosticsForOwner("edge", edgeID)
    }
  }

  private rebuildNodeIndexesAndAliases() {
    this.nodeById.clear()
    this.nodeIndexes.clear()
    this.aliasesByNodeID.clear()
    this.aliasOwners.clear()
    for (let index = 0; index < this.nodes.length; index++) {
      const node = this.nodes[index]!
      this.nodeById.set(node.node_id, node)
      this.nodeIndexes.set(node.node_id, index)
      const aliases = this.aliasesForNode(node)
      this.aliasesByNodeID.set(node.node_id, aliases)
      for (const alias of aliases) {
        const owners = this.aliasOwners.get(alias) ?? new Set<string>()
        owners.add(node.node_id)
        this.aliasOwners.set(alias, owners)
      }
    }
  }

  private forEachNodeReference(node: CausalNodeLike, visit: (field: string, ref: CausalIRRefInput) => void) {
    for (const [index, ref] of (node.input_refs ?? []).entries()) visit(`input_refs[${index}]`, ref)
    for (const [index, ref] of (node.output_refs ?? []).entries()) visit(`output_refs[${index}]`, ref)
    for (const [index, ref] of (node.source_refs ?? []).entries()) visit(`source_refs[${index}]`, ref)
    for (const [index, ref] of (node.derivation?.input_refs ?? []).entries())
      visit(`derivation.input_refs[${index}]`, ref)
  }

  private forEachEdgeReference(edge: CausalEdgeLike, visit: (field: string, ref: CausalIRRefInput) => void) {
    visit("from", edge.from)
    visit("to", edge.to)
    for (const [index, ref] of (edge.evidence_refs ?? []).entries()) visit(`evidence_refs[${index}]`, ref)
  }

  private updateReferenceIndexes(
    ownerID: string,
    referencedByOwner: Map<string, Set<string>>,
    affectedByAlias: Map<string, Set<string>>,
    next: Set<string>,
  ) {
    const previous = referencedByOwner.get(ownerID) ?? new Set<string>()
    for (const alias of previous) {
      if (next.has(alias)) continue
      const owners = affectedByAlias.get(alias)
      owners?.delete(ownerID)
      if (!owners?.size) affectedByAlias.delete(alias)
    }
    for (const alias of next) {
      if (previous.has(alias)) continue
      const owners = affectedByAlias.get(alias) ?? new Set<string>()
      owners.add(ownerID)
      affectedByAlias.set(alias, owners)
    }
    if (next.size) referencedByOwner.set(ownerID, next)
    else referencedByOwner.delete(ownerID)
  }

  private updateNodeReferenceIndexes(node: CausalNodeLike) {
    const aliases = new Set<string>()
    this.forEachNodeReference(node, (_field, ref) => {
      for (const alias of referenceAliasCandidates(ref)) aliases.add(alias)
    })
    this.updateReferenceIndexes(node.node_id, this.referencedAliasesByNodeID, this.aliasAffectedNodeIDs, aliases)
  }

  private updateEdgeReferenceIndexes(edge: CausalEdgeLike) {
    const aliases = new Set<string>()
    this.forEachEdgeReference(edge, (_field, ref) => {
      for (const alias of referenceAliasCandidates(ref)) aliases.add(alias)
    })
    this.updateReferenceIndexes(edge.edge_id, this.referencedAliasesByEdgeID, this.aliasAffectedEdgeIDs, aliases)
  }

  private rebuildNodeReferenceIndexes() {
    this.referencedAliasesByNodeID.clear()
    this.aliasAffectedNodeIDs.clear()
    for (const node of this.nodes) this.updateNodeReferenceIndexes(node)
  }

  private reconcileAliasCollisionDiagnostic(alias: string) {
    const next = aliasCollisionDiagnostic(alias, this.aliasOwners.get(alias) ?? [])
    const diagnosticID = `alias_collision:${payloadHash(alias).slice(0, 16)}`
    const existingIndex = this.diagnosticIndexes.get(diagnosticID)
    const existing = existingIndex === undefined ? undefined : this.diagnostics[existingIndex]
    if (!next) {
      this.removeDiagnostic(diagnosticID)
      return
    }
    if (existing && payloadHash(existing) === payloadHash(next)) return
    this.upsertDiagnostic(next)
    this.append("diagnostic.created", "diagnostic", next.diagnostic_id, next)
  }

  private reconcileAllAliasCollisionDiagnostics() {
    const existing = new Map(
      this.diagnostics
        .filter((diagnostic) => diagnostic.kind === "alias_collision")
        .map((diagnostic) => [diagnostic.diagnostic_id, diagnostic]),
    )
    replaceAll(
      this.diagnostics,
      this.diagnostics.filter((diagnostic) => diagnostic.kind !== "alias_collision"),
    )
    this.rebuildDiagnosticIndexes()
    for (const alias of [...this.aliasOwners.keys()].sort()) {
      const diagnostic = aliasCollisionDiagnostic(alias, this.aliasOwners.get(alias) ?? [])
      if (!diagnostic) continue
      this.upsertDiagnostic(diagnostic)
      if (payloadHash(existing.get(diagnostic.diagnostic_id)) === payloadHash(diagnostic)) continue
      this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
    }
  }

  private referenceDiagnosticsForOwner(type: "node" | "edge", id: string) {
    const diagnostics = new Map<string, CausalIRDiagnosticLike>()
    const inspect = (field: string, input: CausalIRRefInput) => {
      const declared = typedRef(input)
      if (declared.ref_type !== "node" || this.resolveRef(input).ref_type !== "external") return
      const diagnostic = unresolvedReferenceDiagnostic({ type, id }, field, legacyRef(declared))
      diagnostics.set(diagnostic.diagnostic_id, diagnostic)
    }
    if (type === "node") {
      const node = this.nodeById.get(id)
      if (node) this.forEachNodeReference(node, inspect)
    } else {
      const index = this.edgeIndexes.get(id)
      const edge = index === undefined ? undefined : this.edges[index]
      if (edge) this.forEachEdgeReference(edge, inspect)
    }
    return diagnostics
  }

  private reconcileReferenceDiagnosticsForOwner(type: "node" | "edge", id: string) {
    const ownerKey = `${type}:${id}`
    const previous = this.referenceDiagnosticIDsByOwner.get(ownerKey) ?? new Set<string>()
    const next = this.referenceDiagnosticsForOwner(type, id)
    for (const diagnosticID of previous) {
      if (!next.has(diagnosticID)) this.removeDiagnostic(diagnosticID)
    }
    for (const diagnostic of next.values()) {
      const index = this.diagnosticIndexes.get(diagnostic.diagnostic_id)
      const existing = index === undefined ? undefined : this.diagnostics[index]
      if (existing && payloadHash(existing) === payloadHash(diagnostic)) continue
      this.upsertDiagnostic(diagnostic)
      this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
    }
    if (next.size) this.referenceDiagnosticIDsByOwner.set(ownerKey, new Set(next.keys()))
    else this.referenceDiagnosticIDsByOwner.delete(ownerKey)
  }

  private reconcileAllReferenceDiagnostics() {
    const existing = new Map(
      this.diagnostics
        .filter((diagnostic) => diagnostic.kind === "unresolved_ref")
        .map((diagnostic) => [diagnostic.diagnostic_id, diagnostic]),
    )
    replaceAll(
      this.diagnostics,
      this.diagnostics.filter((diagnostic) => diagnostic.kind !== "unresolved_ref"),
    )
    this.rebuildDiagnosticIndexes()
    this.referenceDiagnosticIDsByOwner.clear()
    const reconcile = (type: "node" | "edge", id: string) => {
      const ownerKey = `${type}:${id}`
      const next = this.referenceDiagnosticsForOwner(type, id)
      if (next.size) this.referenceDiagnosticIDsByOwner.set(ownerKey, new Set(next.keys()))
      for (const diagnostic of next.values()) {
        this.upsertDiagnostic(diagnostic)
        if (payloadHash(existing.get(diagnostic.diagnostic_id)) === payloadHash(diagnostic)) continue
        this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
      }
    }
    for (const node of this.nodes) reconcile("node", node.node_id)
    for (const edge of this.edges) reconcile("edge", edge.edge_id)
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
    this.referencedAliasesByEdgeID.clear()
    this.aliasAffectedEdgeIDs.clear()
    for (let index = 0; index < this.edges.length; index++) {
      const edge = this.edges[index]!
      this.edgeIndexes.set(edge.edge_id, index)
      this.updateEdgeReferenceIndexes(edge)
    }
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
    const lastIndex = this.diagnostics.length - 1
    const last = this.diagnostics[lastIndex]
    if (index !== lastIndex && last) {
      this.diagnostics[index] = last
      this.diagnosticIndexes.set(last.diagnostic_id, index)
    }
    this.diagnostics.pop()
    this.diagnosticIndexes.delete(diagnosticID)
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
    diagnostics: reconciledGraphDiagnostics(nodes, edges, diagnostics),
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
