import { createHash } from "node:crypto"
import {
  isFormalRecordType,
  normalizeRelationDetails,
  type FormalDataflowRelation,
} from "./trace-semantic-contract"

export const CAUSAL_IR_VERSION = "1.0" as const

export type CausalNodeLike = {
  node_id: string
  kind: string
  component?: string
  span_id?: string
  timestamp: string
  time_ms: number
  title?: string
  status?: string
  data?: Record<string, unknown>
  source_refs?: string[]
  source_locations?: Record<string, unknown>[]
  typed_resources?: Record<string, unknown>[]
  artifact_refs?: string[]
  metadata?: Record<string, unknown>
}

export type CausalEdgeLike = {
  edge_id: string
  from: { type: string; id: string; label?: string }
  to: { type: string; id: string; label?: string }
  relation: string
  original_relation?: string
  normalized_relation?: FormalDataflowRelation
  evidence_tier?: string
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
    token_usage: Record<string, unknown>
    trace_health: {
      issues: unknown[]
      [key: string]: unknown
    }
    [key: string]: unknown
  }
}

export type ProvenanceTraceProjection = {
  trace_version: string
  manifest: ProvenanceProjectionInput["manifest"]
  records: Array<{
    record_id: string
    component?: string
    event_type: string
    span_id?: string
    timestamp: string
    time_ms: number
    title?: string
    status?: string
    source_refs?: string[]
    source_locations?: Record<string, unknown>[]
    typed_resources?: Record<string, unknown>[]
    artifact_refs?: string[]
    data?: Record<string, unknown>
    metadata?: Record<string, unknown>
  }>
  dataflow_edges: Array<{
    edge_id: string
    from: CausalEdgeLike["from"]
    to: CausalEdgeLike["to"]
    relation: FormalDataflowRelation
    label?: string
    metadata: Record<string, unknown>
  }>
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
  evidence_tier: string
  eligible_for_attribution: boolean
  derivation_method: string
}

function canonicalRelationAttributes(edge: CausalEdgeLike): CanonicalRelationAttributes {
  const details = normalizeRelationDetails(edge.original_relation ?? edge.relation)
  const metadataEligible = edge.metadata?.eligible_for_attribution
  const declaredEligible = edge.eligible_for_attribution ?? (typeof metadataEligible === "boolean" ? metadataEligible : undefined)
  const metadataEvidenceTier = edge.metadata?.evidence_tier
  const metadataDerivationMethod = edge.metadata?.derivation_method

  return {
    original_relation: details.original,
    normalized_relation: details.normalized,
    evidence_tier: edge.evidence_tier ?? (typeof metadataEvidenceTier === "string" ? metadataEvidenceTier : "unspecified"),
    eligible_for_attribution: details.known && declaredEligible !== false,
    derivation_method:
      edge.derivation_method ??
      (typeof metadataDerivationMethod === "string" ? metadataDerivationMethod : undefined) ??
      (details.known ? "explicit_relation" : "unknown_relation_fallback"),
  }
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
    const canonical = this.canonicalEdge(edge)
    this.edges.push(canonical)
    this.append("edge.created", "edge", canonical.edge_id, canonical)
    this.recordUnknownRelationDiagnostic(canonical)
    return canonical as T
  }

  replaceEdges(edges: CausalEdgeLike[]): void {
    const canonical = edges.map((edge) => this.canonicalEdge(edge))
    replaceAll(this.edges, canonical)
    this.rebuildPayloadHashes("edge", this.edges, "edge_id")
    canonical.forEach((edge) => this.recordUnknownRelationDiagnostic(edge))
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

  private canonicalEdge(edge: CausalEdgeLike): CausalEdgeLike {
    return {
      ...edge,
      ...canonicalRelationAttributes(edge),
    }
  }

  private recordUnknownRelationDiagnostic(edge: CausalEdgeLike) {
    if (edge.eligible_for_attribution) return
    const diagnosticID = `unknown_relation:${edge.edge_id}`
    if (this.diagnostics.some((diagnostic) => diagnostic.diagnostic_id === diagnosticID)) return
    this.createDiagnostic({
      diagnostic_id: diagnosticID,
      kind: "unknown_relation",
      level: "warning",
      message: `Unknown causal relation: ${edge.original_relation ?? edge.relation}`,
      edge_id: edge.edge_id,
      relation: edge.original_relation ?? edge.relation,
    })
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
      replaceByID(edges, "edge_id", entry.data as CausalEdgeLike)
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
      replaceAll(edges, journalData(snapshot.edges))
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
    diagnostics,
  }
}

export function projectProvenanceTrace(
  snapshot: CausalIRStoreSnapshot,
  input: ProvenanceProjectionInput,
): ProvenanceTraceProjection {
  const records = snapshot.nodes
    .filter((node) => isFormalRecordType(node.kind))
    .map((node) => ({
      record_id: node.node_id,
      component: node.component,
      event_type: node.kind,
      span_id: node.span_id,
      timestamp: node.timestamp,
      time_ms: node.time_ms,
      title: node.title,
      status: node.status,
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
      from: edge.from,
      to: edge.to,
      relation: attributes.normalized_relation,
      label: edge.label,
      metadata: {
        ...edge.metadata,
        original_relation: attributes.original_relation,
        evidence_tier: attributes.evidence_tier,
        eligible_for_attribution: attributes.eligible_for_attribution,
        derivation_method: attributes.derivation_method,
      },
    }
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
      spans,
      events: records.length,
      records: records.length,
      dataflow_edges: dataflowEdges.length,
      artifacts: snapshot.artifacts.length,
    },
  }
}
