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

export type CausalIRNodeInput = CausalNodeLike
export type CausalIREdgeInput = CausalEdgeLike

export type CausalIRJournalEntry = {
  sequence: number
  time: string
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
}

type CausalIRStoreInput = {
  runID: string
  caseID: string
  append?: (entry: CausalIRJournalEntry) => void
}

function canonicalize(input: unknown): unknown {
  if (Array.isArray(input)) return input.map(canonicalize)
  if (!input || typeof input !== "object") return input

  return Object.fromEntries(
    Object.entries(input)
      .filter(([, value]) => value !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, value]) => [key, canonicalize(value)]),
  )
}

function payloadHash(data: unknown) {
  const payload = JSON.stringify(canonicalize(data)) ?? "null"
  return createHash("sha256").update(payload).digest("hex")
}

function journalData(data: unknown) {
  return structuredClone(data)
}

function replaceByID<T extends Record<string, unknown>>(items: T[], idKey: keyof T, item: T) {
  const index = items.findIndex((candidate) => candidate[idKey] === item[idKey])
  if (index === -1) items.push(item)
  else items[index] = item
}

export class CausalIRStore {
  readonly nodes: CausalNodeLike[] = []
  readonly edges: CausalEdgeLike[] = []
  readonly artifacts: ArtifactLike[] = []

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
    this.append("node.updated", "node", node.node_id, node)
    return node
  }

  replaceNodes(nodes: CausalNodeLike[]): void {
    this.nodes.splice(0, this.nodes.length, ...nodes)
  }

  createEdge<T extends CausalEdgeLike>(edge: T): T {
    this.edges.push(edge)
    this.append("edge.created", "edge", edge.edge_id, edge)
    return edge
  }

  replaceEdges(edges: CausalEdgeLike[]): void {
    this.edges.splice(0, this.edges.length, ...edges)
  }

  createArtifact<T extends ArtifactLike>(artifact: T): T {
    this.artifacts.push(artifact)
    this.append("artifact.created", "artifact", artifact.artifact_id, artifact)
    return artifact
  }

  reuseArtifact<T extends ArtifactLike>(artifact: T): T {
    this.append("artifact.reused", "artifact", artifact.artifact_id, artifact)
    return artifact
  }

  checkpoint(data: unknown): void {
    this.append("case.checkpointed", "case", this.input.caseID, data)
  }

  finalize(data: unknown): void {
    this.append("case.finalized", "case", this.input.caseID, data)
  }

  snapshot(): CausalIRStoreSnapshot {
    return {
      version: CAUSAL_IR_VERSION,
      runID: this.input.runID,
      caseID: this.input.caseID,
      nodes: this.nodes,
      edges: this.edges,
      artifacts: this.artifacts,
    }
  }

  private append(
    operation: CausalIRJournalEntry["operation"],
    recordType: string,
    entityID: string | undefined,
    data: unknown,
  ) {
    const payloadHashValue = payloadHash(data)
    const hashKey = entityID ? `${recordType}:${entityID}` : undefined
    const entry: CausalIRJournalEntry = {
      sequence: ++this.sequence,
      time: new Date().toISOString(),
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

  for (const item of journal) {
    if (!item || typeof item !== "object") continue
    const entry = item as Partial<CausalIRJournalEntry>
    if (!entry.data || typeof entry.data !== "object") continue

    if (entry.operation === "node.created" || entry.operation === "node.updated") {
      replaceByID(nodes, "node_id", entry.data as CausalNodeLike)
      continue
    }

    if (entry.operation === "edge.created") {
      replaceByID(edges, "edge_id", entry.data as CausalEdgeLike)
      continue
    }

    if (entry.operation === "artifact.created") {
      replaceByID(artifacts, "artifact_id", entry.data as ArtifactLike)
    }
  }

  return {
    version: CAUSAL_IR_VERSION,
    runID: "",
    caseID: "",
    nodes,
    edges,
    artifacts,
  }
}
