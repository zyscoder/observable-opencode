import { createHash } from "node:crypto"

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

type CausalIRLifecycleJournalData = {
  snapshot: CausalIRStoreSnapshot
  data: unknown
}

type CausalIRStoreInput = {
  runID: string
  caseID: string
  append?: (entry: CausalIRJournalEntry) => void
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
    this.edges.push(edge)
    this.append("edge.created", "edge", edge.edge_id, edge)
    return edge
  }

  replaceEdges(edges: CausalEdgeLike[]): void {
    replaceAll(this.edges, edges)
    this.rebuildPayloadHashes("edge", this.edges, "edge_id")
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
