import { createHash } from "node:crypto"
import {
  isFormalRecordType,
  normalizeRelationDetails,
  type FormalDataflowRelation,
} from "./trace-semantic-contract"
import type { DataflowEdge, ProvenanceRecord } from "./case-trace"

export const CAUSAL_IR_VERSION = "1.0" as const

export type CausalNodeLike = {
  node_id: string
  kind: string
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
  metadata?: Record<string, unknown>
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

export type CausalIRStoreSnapshot = {
  version: typeof CAUSAL_IR_VERSION
  runID: string
  caseID: string
  nodes: CausalNodeLike[]
  edges: CausalEdgeLike[]
  artifacts: ArtifactLike[]
  diagnostics: CausalIRDiagnosticLike[]
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
}

type CausalIRStoreInput = {
  runID: string
  caseID: string
  append?: (entry: CausalIRJournalEntry) => void
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

function originalRelation(edge: CausalEdgeLike) {
  return edge.original_relation ?? edge.relation
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

function unknownRelationDiagnostic(edge: CausalEdgeLike): CausalIRDiagnosticLike | undefined {
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

function currentUnknownRelationDiagnostics(edges: CausalEdgeLike[]) {
  return edges.flatMap((edge) => {
    const diagnostic = unknownRelationDiagnostic(edge)
    return diagnostic ? [diagnostic] : []
  })
}

function reconciledDiagnostics(edges: CausalEdgeLike[], diagnostics: CausalIRDiagnosticLike[]) {
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

export class CausalIRStore {
  readonly nodes: CausalNodeLike[] = []
  readonly edges: CausalEdgeLike[] = []
  readonly artifacts: ArtifactLike[] = []
  readonly diagnostics: CausalIRDiagnosticLike[] = []

  private sequence = 0
  private readonly payloadHashes = new Map<string, string>()

  constructor(private readonly input: CausalIRStoreInput) {}

  createNode<T extends CausalNodeLike>(node: T): T {
    this.nodes.push(node)
    this.append("node.created", "node", node.node_id, node)
    return node
  }

  updateNode<T extends CausalNodeLike>(node: T): T {
    replaceByID(this.nodes, "node_id", node)
    this.append("node.updated", "node.update", node.node_id, node, "node")
    return node
  }

  replaceNodes(nodes: CausalNodeLike[]): void {
    replaceAll(this.nodes, nodes)
    this.rebuildPayloadHashes("node", this.nodes, "node_id")
    this.appendSnapshot("case.checkpointed", "checkpoint", { reason: "nodes.replaced" })
  }

  createEdge<T extends CausalEdgeLike>(edge: T): T {
    const canonical = canonicalEdge(edge)
    replaceByID(this.edges, "edge_id", canonical)
    this.append("edge.created", "edge", canonical.edge_id, canonical)
    this.reconcileUnknownRelationDiagnostics()
    return canonical as T
  }

  replaceEdges(edges: CausalEdgeLike[]): void {
    const canonical = edges.map(canonicalEdge)
    replaceAll(this.edges, canonical)
    this.rebuildPayloadHashes("edge", this.edges, "edge_id")
    this.reconcileUnknownRelationDiagnostics()
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
    replaceByID(this.diagnostics, "diagnostic_id", diagnostic)
    this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
    return diagnostic
  }

  checkpoint(data: unknown): void {
    this.appendSnapshot("case.checkpointed", "checkpoint", data)
  }

  finalize(data: unknown): void {
    this.appendSnapshot("case.finalized", "finish", data)
  }

  snapshot(): CausalIRStoreSnapshot {
    return {
      version: CAUSAL_IR_VERSION,
      runID: this.input.runID,
      caseID: this.input.caseID,
      nodes: this.nodes,
      edges: this.edges,
      artifacts: this.artifacts,
      diagnostics: this.diagnostics,
    }
  }

  private appendSnapshot(operation: "case.checkpointed" | "case.finalized", recordType: string, data: unknown) {
    this.append(operation, recordType, this.input.caseID, { snapshot: this.snapshot(), data }, "case")
  }

  private reconcileUnknownRelationDiagnostics() {
    const existing = new Map(
      this.diagnostics
        .filter(isUnknownRelationDiagnostic)
        .map((diagnostic) => [diagnostic.diagnostic_id, diagnostic]),
    )
    const next = reconciledDiagnostics(this.edges, this.diagnostics)
    replaceAll(this.diagnostics, next)

    for (const diagnostic of next.filter(isUnknownRelationDiagnostic)) {
      const previous = existing.get(diagnostic.diagnostic_id)
      if (previous?.relation === diagnostic.relation) continue
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
    const payloadHashValue = payloadHash(data)
    const hashKey = entityID ? `${hashType}:${entityID}` : undefined
    const entry: CausalIRJournalEntry = {
      sequence: ++this.sequence,
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

    if (hashKey) this.payloadHashes.set(hashKey, payloadHashValue)
    this.input.append?.(entry)
  }
}

export function replayCausalIRJournal(journal: unknown[]): CausalIRStoreSnapshot {
  const nodes: CausalNodeLike[] = []
  const edges: CausalEdgeLike[] = []
  const artifacts: ArtifactLike[] = []
  const diagnostics: CausalIRDiagnosticLike[] = []
  let runID = ""
  let caseID = ""

  for (const item of journal) {
    if (!item || typeof item !== "object") continue
    const entry = item as Partial<CausalIRJournalEntry>
    if (typeof entry.run_id === "string") runID = entry.run_id
    if (typeof entry.case_id === "string") caseID = entry.case_id

    if ((entry.operation === "node.created" || entry.operation === "node.updated") && entry.data && typeof entry.data === "object") {
      replaceByID(nodes, "node_id", entry.data as CausalNodeLike)
      continue
    }

    if (entry.operation === "edge.created" && entry.data && typeof entry.data === "object") {
      replaceByID(edges, "edge_id", canonicalEdge(entry.data as CausalEdgeLike))
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
      replaceAll(nodes, journalData(snapshot.nodes))
      replaceAll(edges, journalData(snapshot.edges).map(canonicalEdge))
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

function optionalNumber(input: unknown) {
  if (input === undefined || input === null) return undefined
  const value = Number(input)
  return Number.isFinite(value) ? value : undefined
}

function safeNumber(input: unknown) {
  const value = Number(input ?? 0)
  return Number.isFinite(value) ? Math.max(0, value) : 0
}

function compatibilityTokenUsage(input: unknown): ProvenanceRecord["token_usage"] {
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined
  const usage = input as Record<string, unknown>
  return {
    input: safeNumber(usage.input),
    output: safeNumber(usage.output),
    reasoning: safeNumber(usage.reasoning),
    cached_input: safeNumber(usage.cached_input),
    cache_write: safeNumber(usage.cache_write),
    total: safeNumber(usage.total),
    cost: safeNumber(usage.cost),
  }
}

function provenanceRef(ref: DataflowEdge["from"]): DataflowEdge["from"] {
  return {
    ...ref,
    type: ref.type === "final_response_evidence" ? "response_segment" : ref.type,
    label: ref.label === "final.claim" ? "response.output" : ref.label,
  }
}

function provenanceLabel(label: string | undefined) {
  if (!label) return undefined
  return label.replace(/final response evidence/gi, "response output").replace(/final claim/gi, "response output")
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
      span_id: node.span_id,
      parent_span_id: node.parent_span_id,
      timestamp: node.timestamp,
      time_ms: node.time_ms,
      title: node.title,
      status: node.status,
      duration_ms: optionalNumber(node.data?.duration_ms),
      token_usage: compatibilityTokenUsage(node.data?.token_usage),
      error: node.data?.error,
      input_refs: node.input_refs,
      output_refs: node.output_refs,
      source_refs: node.source_refs,
      source_locations: node.source_locations,
      typed_resources: node.typed_resources,
      artifact_refs: node.artifact_refs,
      data: node.data,
      metadata: node.metadata,
    }))
  const dataflowEdges = snapshot.edges.map((edge) => {
    const attributes = canonicalRelationAttributes(edge)

    return {
      edge_id: edge.edge_id,
      from: provenanceRef(edge.from),
      to: provenanceRef(edge.to),
      relation: attributes.normalized_relation,
      label: provenanceLabel(edge.label),
      metadata: {
        ...edge.metadata,
        original_relation: attributes.original_relation,
        evidence_tier: attributes.evidence_tier,
        eligible_for_attribution: attributes.eligible_for_attribution,
        derivation_method: attributes.derivation_method,
      },
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
