import { Database } from "bun:sqlite"
import {
  CAUSAL_IR_VERSION,
  canonicalCausalEdge,
  canonicalCausalIREdge,
  canonicalCausalIRNode,
  causalIRAliasCollisionDiagnostic,
  causalIRFinalizationIntegrityHash,
  causalIRLegacyRef,
  causalIRNodeAliases,
  causalIRPayloadHash,
  causalIRReferenceAliasCandidates,
  causalIRUnknownRelationDiagnostic,
  causalIRUnresolvedReferenceOccurrenceDiagnostic,
  copyCausalIRJournalData,
  normalizeCausalIREdges,
  reconcileCausalIRGraphDiagnostics,
  typedCausalIRReference,
  validateCausalIRNodeProvenance,
  type ArtifactLike,
  type CausalEdgeLike,
  type CausalIRCommitResult,
  type CausalIRDiagnosticLike,
  type CausalIREdge,
  type CausalIRJournalEntry,
  type CausalIRJournalSummary,
  type CausalIRNode,
  type CausalIRRef,
  type CausalIRRuntimeCloseData,
  type CausalIRStoreSnapshot,
  type CausalIRTraceDocument,
  type CausalNodeLike,
} from "./causal-ir"

export type CausalIRNodeQuery = {
  ids?: string[]
  kinds?: string[]
  component?: string
  status?: string
  sessionID?: string
  messageID?: string
  callID?: string
  limit?: number
  reverse?: boolean
}

export type CausalIREdgeQuery = {
  ids?: string[]
  fromIDs?: string[]
  toIDs?: string[]
  relations?: string[]
  limit?: number
  reverse?: boolean
}

export type CausalIRArtifactQuery = {
  ids?: string[]
  hashes?: string[]
  limit?: number
  reverse?: boolean
}

export type CausalIRDiagnosticQuery = {
  ids?: string[]
  kinds?: string[]
  limit?: number
  reverse?: boolean
}

type CausalIRRuntimeStoreInput = {
  runID: string
  caseID: string
  indexPath: string
  append?: (entry: CausalIRJournalEntry) => unknown
  hotNodeLimit?: number
  hotEdgeLimit?: number
}

type EntityRow = {
  json: string
}

type IDRow = {
  id: string
}

type SequenceRow = {
  sequence: number
}

type HashRow = {
  payload_hash: string
}

type AliasOwnerRow = {
  node_id: string
}

type ReferenceRow = {
  field: string
  reference: string
}

const ROLLBACK = Symbol("causal-ir-runtime-rollback")

class LRU<T> {
  private readonly values = new Map<string, T>()

  constructor(private readonly limit: number) {}

  get size() {
    return this.values.size
  }

  get(id: string) {
    const value = this.values.get(id)
    if (value === undefined) return undefined
    this.values.delete(id)
    this.values.set(id, value)
    return value
  }

  set(id: string, value: T) {
    this.values.delete(id)
    this.values.set(id, value)
    while (this.values.size > this.limit) this.values.delete(this.values.keys().next().value!)
  }

  delete(id: string) {
    this.values.delete(id)
  }

  clear() {
    this.values.clear()
  }
}

function parse<T>(row: EntityRow | null | undefined) {
  return row ? (JSON.parse(row.json) as T) : undefined
}

function placeholders(values: unknown[]) {
  return values.map(() => "?").join(", ")
}

function boundedLimit(input: number | undefined) {
  if (input === undefined) return undefined
  if (!Number.isSafeInteger(input) || input < 0) throw new TypeError("query limit must be a non-negative integer")
  return input
}

function record(input: unknown) {
  return input && typeof input === "object" && !Array.isArray(input) ? (input as Record<string, unknown>) : undefined
}

function runtimeScopeValue(node: CausalNodeLike, keys: string[]) {
  const data = node.data ?? {}
  const sources = [data, record(data.input), record(data.data), record(data.metadata), node.metadata]
  for (const source of sources) {
    for (const key of keys) {
      const value = source?.[key]
      if (typeof value === "string" && value) return value
    }
  }
  return undefined
}

function forEachNodeReference(node: CausalNodeLike, visit: (field: string, ref: string | CausalIRRef) => void) {
  for (const [index, ref] of (node.input_refs ?? []).entries()) visit(`input_refs[${index}]`, ref)
  for (const [index, ref] of (node.output_refs ?? []).entries()) visit(`output_refs[${index}]`, ref)
  for (const [index, ref] of (node.source_refs ?? []).entries()) visit(`source_refs[${index}]`, ref)
  for (const [index, ref] of (node.derivation?.input_refs ?? []).entries())
    visit(`derivation.input_refs[${index}]`, ref)
}

function forEachEdgeReference(edge: CausalEdgeLike, visit: (field: string, ref: CausalEdgeLike["from"] | string) => void) {
  visit("from", edge.from)
  visit("to", edge.to)
  for (const [index, ref] of (edge.evidence_refs ?? []).entries()) visit(`evidence_refs[${index}]`, ref)
}

function traceDocument(input: unknown): input is CausalIRTraceDocument {
  if (!input || typeof input !== "object" || Array.isArray(input)) return false
  const value = input as Partial<CausalIRTraceDocument>
  return (
    typeof value.trace_version === "string" &&
    value.causal_ir_version === CAUSAL_IR_VERSION &&
    Array.isArray(value.nodes) &&
    Array.isArray(value.edges) &&
    Array.isArray(value.artifacts) &&
    Array.isArray(value.diagnostics)
  )
}

export class CausalIRRuntimeStore {
  private readonly db: Database
  private readonly hotNodes: LRU<CausalNodeLike>
  private readonly hotEdges: LRU<CausalEdgeLike>
  private sequence = 0
  private nodeSequence = 0
  private edgeSequence = 0
  private artifactSequence = 0
  private diagnosticSequence = 0
  private poisoned = false
  private lastJournalPayloadHash: string | undefined
  private closed = false

  constructor(private readonly input: CausalIRRuntimeStoreInput) {
    this.hotNodes = new LRU(input.hotNodeLimit ?? 512)
    this.hotEdges = new LRU(input.hotEdgeLimit ?? 1_024)
    this.db = new Database(input.indexPath, { create: true, strict: true })
    this.db.exec("PRAGMA journal_mode = WAL")
    this.db.exec("PRAGMA synchronous = NORMAL")
    this.db.exec("PRAGMA foreign_keys = ON")
    this.db.exec("PRAGMA temp_store = MEMORY")
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS nodes (
        node_id TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        kind TEXT NOT NULL,
        component TEXT,
        status TEXT,
        session_id TEXT,
        message_id TEXT,
        call_id TEXT,
        runtime_json TEXT NOT NULL,
        canonical_json TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS nodes_kind_idx ON nodes(kind, sequence);
      CREATE INDEX IF NOT EXISTS nodes_component_idx ON nodes(component, sequence);
      CREATE INDEX IF NOT EXISTS nodes_status_idx ON nodes(status, sequence);
      CREATE INDEX IF NOT EXISTS nodes_session_idx ON nodes(session_id, sequence);
      CREATE INDEX IF NOT EXISTS nodes_message_idx ON nodes(message_id, sequence);
      CREATE INDEX IF NOT EXISTS nodes_call_idx ON nodes(call_id, sequence);
      CREATE TABLE IF NOT EXISTS edges (
        edge_id TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        relation TEXT NOT NULL,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        runtime_json TEXT NOT NULL,
        canonical_json TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS edges_relation_idx ON edges(relation, sequence);
      CREATE INDEX IF NOT EXISTS edges_from_idx ON edges(from_id, sequence);
      CREATE INDEX IF NOT EXISTS edges_to_idx ON edges(to_id, sequence);
      CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        hash TEXT NOT NULL,
        json TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS artifacts_hash_idx ON artifacts(hash, sequence);
      CREATE TABLE IF NOT EXISTS diagnostics (
        diagnostic_id TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        kind TEXT,
        owner_type TEXT,
        owner_id TEXT,
        json TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS diagnostics_kind_idx ON diagnostics(kind, sequence);
      CREATE INDEX IF NOT EXISTS diagnostics_owner_idx ON diagnostics(owner_type, owner_id);
      CREATE TABLE IF NOT EXISTS aliases (
        alias TEXT NOT NULL,
        node_id TEXT NOT NULL,
        PRIMARY KEY (alias, node_id),
        FOREIGN KEY (node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
      );
      CREATE INDEX IF NOT EXISTS aliases_node_idx ON aliases(node_id);
      CREATE TABLE IF NOT EXISTS refs (
        owner_type TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        field TEXT NOT NULL,
        reference TEXT NOT NULL,
        alias TEXT NOT NULL,
        PRIMARY KEY (owner_type, owner_id, field, alias)
      );
      CREATE INDEX IF NOT EXISTS refs_alias_idx ON refs(alias, owner_type, owner_id);
      CREATE INDEX IF NOT EXISTS refs_reference_idx ON refs(reference, owner_type, owner_id);
      CREATE TABLE IF NOT EXISTS payload_hashes (
        hash_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        PRIMARY KEY (hash_type, entity_id)
      );
    `)
  }

  get hotNodeCount() {
    return this.hotNodes.size
  }

  get hotEdgeCount() {
    return this.hotEdges.size
  }

  resolveReference(input: string | CausalIRRef | CausalEdgeLike["from"]): CausalIRRef {
    const ref = typedCausalIRReference(input)
    if (ref.ref_type !== "node") return ref
    const legacy = causalIRLegacyRef(ref)
    for (const alias of causalIRReferenceAliasCandidates(input)) {
      const owners = this.db
        .query<AliasOwnerRow, [string]>("SELECT node_id FROM aliases WHERE alias = ? ORDER BY node_id LIMIT 2")
        .all(alias)
      if (!owners.length) continue
      if (owners.length > 1) break
      return {
        ...ref,
        ref_type: "node",
        ref_id: owners[0]!.node_id,
        legacy_ref: ref.legacy_ref ?? legacy,
      }
    }
    return { ...ref, ref_type: "external", legacy_ref: legacy }
  }

  createNode<T extends CausalNodeLike>(node: T): T {
    return this.upsertNode(copyCausalIRJournalData(node), "node.created", "node")
  }

  updateNode<T extends CausalNodeLike>(node: T): T {
    return this.upsertNode(copyCausalIRJournalData(node), "node.updated", "node.update")
  }

  replaceNodes(nodes: CausalNodeLike[]): void {
    const copied = copyCausalIRJournalData(nodes)
    const nodeIDs = new Set(copied.map((node) => node.node_id))
    const aliases = new Map<string, Set<string>>()
    for (const node of copied) {
      for (const alias of causalIRNodeAliases(node, node.data ?? {})) {
        const owners = aliases.get(alias) ?? new Set<string>()
        owners.add(node.node_id)
        aliases.set(alias, owners)
      }
    }
    const resolve = (input: string | CausalIRRef | CausalEdgeLike["from"]): CausalIRRef => {
      const ref = typedCausalIRReference(input)
      if (ref.ref_type !== "node") return ref
      const legacy = causalIRLegacyRef(ref)
      for (const alias of causalIRReferenceAliasCandidates(input)) {
        const owners = aliases.get(alias)
        if (!owners?.size) continue
        if (owners.size > 1) break
        return { ...ref, ref_id: owners.values().next().value!, legacy_ref: ref.legacy_ref ?? legacy }
      }
      return { ...ref, ref_type: "external", legacy_ref: legacy }
    }
    for (const node of copied) {
      validateCausalIRNodeProvenance(node, {
        resolveRef: resolve,
        hasNode: (nodeID: string) => nodeIDs.has(nodeID),
        hasArtifact: (artifactID: string) => this.hasArtifact(artifactID),
      })
    }

    this.transact(() => {
      const previousOrders = new Map(
        this.db.query<{ node_id: string; sequence: number }, []>("SELECT node_id, sequence FROM nodes").all().map((row) => [
          row.node_id,
          row.sequence,
        ]),
      )
      this.db.exec("DELETE FROM aliases; DELETE FROM refs WHERE owner_type = 'node'; DELETE FROM nodes")
      this.clearPayloadHashes("node")
      for (const node of copied) {
        const order = previousOrders.get(node.node_id) ?? ++this.nodeSequence
        if (order > this.nodeSequence) this.nodeSequence = order
        const canonical = canonicalCausalIRNode(node, { runID: this.input.runID, caseID: this.input.caseID, sequence: order }, resolve)
        this.putNode(node, canonical, order)
        for (const alias of causalIRNodeAliases(node, node.data ?? {}))
          this.db.query("INSERT INTO aliases(alias, node_id) VALUES (?, ?)").run(alias, node.node_id)
        this.indexNodeReferences(node)
        this.setPayloadHash("node", node.node_id, causalIRPayloadHash(canonical))
      }
      this.recanonicalizeAllEdges()
      this.reconcileAllAliasCollisionDiagnostics()
      this.reconcileAllReferenceDiagnostics()
      this.appendSnapshot("nodes.replaced", "nodes_and_edges")
    })
    this.hotNodes.clear()
    this.hotEdges.clear()
  }

  createEdge<T extends CausalEdgeLike>(edge: T): T {
    const copied = canonicalCausalEdge(copyCausalIRJournalData(edge))
    const canonical = canonicalCausalIREdge(copied, this.resolveReference.bind(this))
    const stored = this.transact(() => {
      const order = this.edgeOrder(copied.edge_id) ?? ++this.edgeSequence
      const committed = this.append("edge.created", "edge", copied.edge_id, canonical)
      if (!committed.committed) throw ROLLBACK
      this.putEdge(copied, canonical, order)
      this.indexEdgeReferences(copied)
      this.reconcileUnknownRelationDiagnostic(copied)
      this.reconcileReferenceDiagnostics("edge", copied.edge_id)
      return true
    })
    if (stored) this.hotEdges.set(copied.edge_id, copied)
    return copyCausalIRJournalData(copied) as T
  }

  replaceEdges(edges: CausalEdgeLike[]): void {
    const copied = normalizeCausalIREdges(copyCausalIRJournalData(edges))
    this.transact(() => {
      this.db.exec("DELETE FROM refs WHERE owner_type = 'edge'; DELETE FROM edges")
      this.clearPayloadHashes("edge")
      for (const edge of copied) {
        const order = ++this.edgeSequence
        const canonical = canonicalCausalIREdge(edge, this.resolveReference.bind(this))
        this.putEdge(edge, canonical, order)
        this.indexEdgeReferences(edge)
        this.setPayloadHash("edge", edge.edge_id, causalIRPayloadHash(canonical))
      }
      this.reconcileAllUnknownRelationDiagnostics()
      this.reconcileAllReferenceDiagnostics()
      this.appendSnapshot("edges.replaced", "edges")
    })
    this.hotEdges.clear()
  }

  createArtifact<T extends ArtifactLike>(artifact: T): T {
    const copied = copyCausalIRJournalData(artifact)
    this.transact(() => {
      const order = ++this.artifactSequence
      const committed = this.append("artifact.created", "artifact", copied.artifact_id, copied)
      if (!committed.committed) throw ROLLBACK
      this.putArtifact(copied, order)
    })
    return copyCausalIRJournalData(copied)
  }

  reuseArtifact<T extends ArtifactLike>(artifact: T): T {
    const copied = copyCausalIRJournalData(artifact)
    this.transact(() => {
      const order = this.artifactOrder(copied.artifact_id) ?? ++this.artifactSequence
      const committed = this.append("artifact.reused", "artifact.reuse", copied.artifact_id, copied, "artifact")
      if (!committed.committed) throw ROLLBACK
      this.putArtifact(copied, order)
    })
    return copyCausalIRJournalData(copied)
  }

  createDiagnostic<T extends CausalIRDiagnosticLike>(diagnostic: T): T {
    const copied = copyCausalIRJournalData(diagnostic)
    this.transact(() => {
      const committed = this.append("diagnostic.created", "diagnostic", copied.diagnostic_id, copied)
      if (!committed.committed) throw ROLLBACK
      this.putDiagnostic(copied)
    })
    return copyCausalIRJournalData(copied)
  }

  queryNodes(query: CausalIRNodeQuery = {}): CausalNodeLike[] {
    if (query.ids?.length === 0 || query.kinds?.length === 0) return []
    const clauses: string[] = []
    const parameters: Array<string | number> = []
    const addList = (column: string, values: string[] | undefined) => {
      if (!values) return
      clauses.push(`${column} IN (${placeholders(values)})`)
      parameters.push(...values)
    }
    addList("node_id", query.ids)
    addList("kind", query.kinds)
    for (const [column, value] of [
      ["component", query.component],
      ["status", query.status],
      ["session_id", query.sessionID],
      ["message_id", query.messageID],
      ["call_id", query.callID],
    ] as const) {
      if (value === undefined) continue
      clauses.push(`${column} = ?`)
      parameters.push(value)
    }
    const limit = boundedLimit(query.limit)
    if (limit !== undefined) parameters.push(limit)
    const rows = this.db
      .query<EntityRow, Array<string | number>>(
        `SELECT runtime_json AS json FROM nodes${clauses.length ? ` WHERE ${clauses.join(" AND ")}` : ""} ORDER BY sequence ${query.reverse ? "DESC" : "ASC"}${limit === undefined ? "" : " LIMIT ?"}`,
      )
      .all(...parameters)
    return rows.map((row) => {
      const node = JSON.parse(row.json) as CausalNodeLike
      this.hotNodes.set(node.node_id, node)
      return copyCausalIRJournalData(node)
    })
  }

  queryNodesReferencing(refs: string[], query: CausalIRNodeQuery = {}) {
    if (!refs.length) return []
    const ids = this.db
      .query<IDRow, string[]>(
        `SELECT DISTINCT owner_id AS id FROM refs WHERE owner_type = 'node' AND (reference IN (${placeholders(refs)}) OR alias IN (${placeholders(refs)}))`,
      )
      .all(...refs, ...refs)
      .map((row) => row.id)
    return this.queryNodes({ ...query, ids: query.ids ? ids.filter((id) => query.ids!.includes(id)) : ids })
  }

  queryEdges(query: CausalIREdgeQuery = {}): CausalEdgeLike[] {
    if (query.ids?.length === 0 || query.fromIDs?.length === 0 || query.toIDs?.length === 0 || query.relations?.length === 0)
      return []
    const clauses: string[] = []
    const parameters: Array<string | number> = []
    for (const [column, values] of [
      ["edge_id", query.ids],
      ["from_id", query.fromIDs],
      ["to_id", query.toIDs],
      ["relation", query.relations],
    ] as const) {
      if (!values) continue
      clauses.push(`${column} IN (${placeholders(values)})`)
      parameters.push(...values)
    }
    const limit = boundedLimit(query.limit)
    if (limit !== undefined) parameters.push(limit)
    const rows = this.db
      .query<EntityRow, Array<string | number>>(
        `SELECT runtime_json AS json FROM edges${clauses.length ? ` WHERE ${clauses.join(" AND ")}` : ""} ORDER BY sequence ${query.reverse ? "DESC" : "ASC"}${limit === undefined ? "" : " LIMIT ?"}`,
      )
      .all(...parameters)
    return rows.map((row) => {
      const edge = JSON.parse(row.json) as CausalEdgeLike
      this.hotEdges.set(edge.edge_id, edge)
      return copyCausalIRJournalData(edge)
    })
  }

  queryEdgesReferencing(nodeIDs: string[], query: CausalIREdgeQuery = {}) {
    if (!nodeIDs.length) return []
    const ids = this.db
      .query<IDRow, string[]>(
        `SELECT DISTINCT owner_id AS id FROM refs WHERE owner_type = 'edge' AND alias IN (${placeholders(nodeIDs.flatMap((id) => [`node:${id}`, `record:${id}`]))})`,
      )
      .all(...nodeIDs.flatMap((id) => [`node:${id}`, `record:${id}`]))
      .map((row) => row.id)
    return this.queryEdges({ ...query, ids: query.ids ? ids.filter((id) => query.ids!.includes(id)) : ids })
  }

  queryArtifacts(query: CausalIRArtifactQuery = {}): ArtifactLike[] {
    if (query.ids?.length === 0 || query.hashes?.length === 0) return []
    const clauses: string[] = []
    const parameters: Array<string | number> = []
    for (const [column, values] of [
      ["artifact_id", query.ids],
      ["hash", query.hashes],
    ] as const) {
      if (!values) continue
      clauses.push(`${column} IN (${placeholders(values)})`)
      parameters.push(...values)
    }
    const limit = boundedLimit(query.limit)
    if (limit !== undefined) parameters.push(limit)
    return this.db
      .query<EntityRow, Array<string | number>>(
        `SELECT json FROM artifacts${clauses.length ? ` WHERE ${clauses.join(" AND ")}` : ""} ORDER BY sequence ${query.reverse ? "DESC" : "ASC"}${limit === undefined ? "" : " LIMIT ?"}`,
      )
      .all(...parameters)
      .map((row) => JSON.parse(row.json) as ArtifactLike)
  }

  queryDiagnostics(query: CausalIRDiagnosticQuery = {}): CausalIRDiagnosticLike[] {
    if (query.ids?.length === 0 || query.kinds?.length === 0) return []
    const clauses: string[] = []
    const parameters: Array<string | number> = []
    for (const [column, values] of [
      ["diagnostic_id", query.ids],
      ["kind", query.kinds],
    ] as const) {
      if (!values) continue
      clauses.push(`${column} IN (${placeholders(values)})`)
      parameters.push(...values)
    }
    const limit = boundedLimit(query.limit)
    if (limit !== undefined) parameters.push(limit)
    return this.db
      .query<EntityRow, Array<string | number>>(
        `SELECT json FROM diagnostics${clauses.length ? ` WHERE ${clauses.join(" AND ")}` : ""} ORDER BY sequence ${query.reverse ? "DESC" : "ASC"}${limit === undefined ? "" : " LIMIT ?"}`,
      )
      .all(...parameters)
      .map((row) => JSON.parse(row.json) as CausalIRDiagnosticLike)
  }

  nodeOrder(nodeID: string) {
    return this.db.query<SequenceRow, [string]>("SELECT sequence FROM nodes WHERE node_id = ?").get(nodeID)?.sequence
  }

  checkpoint(data: unknown): void {
    this.transact(() => this.appendSnapshotData(data))
  }

  finalize(data: unknown): CausalIRCommitResult {
    const snapshot = this.synchronize()
    const trace = traceDocument(data) ? data : undefined
    const canonical = trace
      ? {
          trace_version: trace.trace_version,
          causal_ir_version: trace.causal_ir_version,
          manifest: trace.manifest,
          journal: trace.journal,
          metrics: trace.metrics,
          compatibility: trace.compatibility,
        }
      : undefined
    return this.transact(() =>
      this.append(
        "case.finalized",
        "finish",
        this.input.caseID,
        {
          format: "compact_causal_ir_finalization",
          data: trace?.manifest ?? data,
          graph: {
            version: CAUSAL_IR_VERSION,
            nodes: snapshot.nodes.length,
            edges: snapshot.edges.length,
            artifacts: snapshot.artifacts.length,
            diagnostics: snapshot.diagnostics.length,
            integrity_hash: causalIRFinalizationIntegrityHash(snapshot, canonical),
          },
          canonical_trace_path: "trace.json",
          ...(canonical ? { canonical } : {}),
        },
        "case",
      ),
    ) as CausalIRCommitResult
  }

  closeRuntime(data: CausalIRRuntimeCloseData): CausalIRCommitResult {
    return this.transact(() =>
      this.append("case.runtime_closed", "runtime_close", this.input.caseID, copyCausalIRJournalData(data), "case"),
    ) as CausalIRCommitResult
  }

  snapshot(): CausalIRStoreSnapshot {
    const nodes = this.db
      .query<{ runtime_json: string; sequence: number }, []>(
        "SELECT runtime_json, sequence FROM nodes ORDER BY sequence",
      )
      .all()
      .map((row) =>
        canonicalCausalIRNode(
          JSON.parse(row.runtime_json) as CausalNodeLike,
          { runID: this.input.runID, caseID: this.input.caseID, sequence: row.sequence },
          this.resolveReference.bind(this),
        ),
      )
    const edges = this.db
      .query<EntityRow, []>("SELECT runtime_json AS json FROM edges ORDER BY sequence")
      .all()
      .map((row) => canonicalCausalIREdge(JSON.parse(row.json) as CausalEdgeLike, this.resolveReference.bind(this)))
    const diagnostics = this.queryDiagnostics()
    return {
      version: CAUSAL_IR_VERSION,
      runID: this.input.runID,
      caseID: this.input.caseID,
      nodes,
      edges,
      artifacts: this.queryArtifacts(),
      diagnostics: reconcileCausalIRGraphDiagnostics(nodes, edges, diagnostics),
    }
  }

  synchronize(): CausalIRStoreSnapshot {
    return this.snapshot()
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

  close() {
    if (this.closed) return
    this.closed = true
    this.hotNodes.clear()
    this.hotEdges.clear()
    this.db.close(false)
  }

  private upsertNode<T extends CausalNodeLike>(
    node: T,
    operation: "node.created" | "node.updated",
    recordType: "node" | "node.update",
  ): T {
    validateCausalIRNodeProvenance(node, {
      resolveRef: this.resolveReference.bind(this),
      hasNode: (nodeID: string) => this.hasNode(nodeID),
      hasArtifact: (artifactID: string) => this.hasArtifact(artifactID),
    })
    const stored = this.transact(() => {
      const previousAliases = new Set(
        this.db
          .query<{ alias: string }, [string]>("SELECT alias FROM aliases WHERE node_id = ?")
          .all(node.node_id)
          .map((row) => row.alias),
      )
      const nextAliases = new Set(causalIRNodeAliases(node, node.data ?? {}))
      const changedAliases = new Set<string>()
      for (const alias of previousAliases) if (!nextAliases.has(alias)) changedAliases.add(alias)
      for (const alias of nextAliases) if (!previousAliases.has(alias)) changedAliases.add(alias)
      const affected = this.affectedEntities(changedAliases, node.node_id)
      const order = this.nodeOrder(node.node_id) ?? ++this.nodeSequence

      this.db.query("DELETE FROM aliases WHERE node_id = ?").run(node.node_id)
      const placeholder = canonicalCausalIRNode(node, { runID: this.input.runID, caseID: this.input.caseID, sequence: order })
      this.putNode(node, placeholder, order)
      for (const alias of nextAliases) this.db.query("INSERT INTO aliases(alias, node_id) VALUES (?, ?)").run(alias, node.node_id)
      const canonical = canonicalCausalIRNode(
        node,
        { runID: this.input.runID, caseID: this.input.caseID, sequence: order },
        this.resolveReference.bind(this),
      )
      const committed = this.append(operation, recordType, node.node_id, canonical, "node")
      if (!committed.committed) throw ROLLBACK
      this.putNode(node, canonical, order)
      this.indexNodeReferences(node)
      for (const alias of changedAliases) this.reconcileAliasCollisionDiagnostic(alias)
      this.reconcileReferenceDiagnostics("node", node.node_id)
      this.reconcileAffectedEntities(affected)
      return true
    })
    if (stored) this.hotNodes.set(node.node_id, node)
    return copyCausalIRJournalData(node)
  }

  private transact<T>(fn: () => T): T | undefined {
    if (this.closed) throw new Error("CausalIRRuntimeStore is closed")
    try {
      return this.db.transaction(fn)()
    } catch (error) {
      if (error === ROLLBACK) return undefined
      this.poisoned = true
      throw error
    }
  }

  private append(
    operation: CausalIRJournalEntry["operation"],
    recordType: string,
    entityID: string | undefined,
    data: unknown,
    hashType = recordType,
  ): CausalIRCommitResult {
    if (this.poisoned) {
      return { committed: false, operation, sequence: this.sequence, payload_hash: undefined, poisoned: true }
    }
    const payloadHash = causalIRPayloadHash(data)
    const previous = entityID ? this.payloadHash(hashType, entityID) : undefined
    const entry: CausalIRJournalEntry = {
      sequence: this.sequence + 1,
      time: new Date().toISOString(),
      run_id: this.input.runID,
      case_id: this.input.caseID,
      operation,
      record_type: recordType,
      entity_id: entityID,
      previous_payload_hash: previous,
      payload_hash: payloadHash,
      data: copyCausalIRJournalData(data),
    }
    try {
      if (this.input.append?.(entry) === false) {
        this.poisoned = true
        return { committed: false, operation, sequence: this.sequence, payload_hash: undefined, poisoned: true }
      }
    } catch {
      this.poisoned = true
      return { committed: false, operation, sequence: this.sequence, payload_hash: undefined, poisoned: true }
    }
    this.sequence = entry.sequence
    this.lastJournalPayloadHash = payloadHash
    if (entityID) this.setPayloadHash(hashType, entityID, payloadHash)
    this.setMetadata("sequence", this.sequence)
    this.setMetadata("last_journal_payload_hash", payloadHash)
    return { committed: true, operation, sequence: entry.sequence, payload_hash: payloadHash, poisoned: false }
  }

  private appendSnapshot(reason: string, replacement: "nodes_and_edges" | "edges") {
    const result = this.append(
      "case.checkpointed",
      "checkpoint",
      this.input.caseID,
      {
        snapshot: this.snapshot(),
        data: { reason },
        hash_state_replacement: replacement,
      },
      "case",
    )
    if (!result.committed) throw ROLLBACK
    return result
  }

  private appendSnapshotData(data: unknown) {
    const result = this.append(
      "case.checkpointed",
      "checkpoint",
      this.input.caseID,
      { snapshot: this.snapshot(), data: copyCausalIRJournalData(data) },
      "case",
    )
    if (!result.committed) throw ROLLBACK
    return result
  }

  private putNode(node: CausalNodeLike, canonical: CausalIRNode, sequence: number) {
    this.db
      .query(`
        INSERT INTO nodes(
          node_id, sequence, kind, component, status, session_id, message_id, call_id, runtime_json, canonical_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
          sequence = excluded.sequence,
          kind = excluded.kind,
          component = excluded.component,
          status = excluded.status,
          session_id = excluded.session_id,
          message_id = excluded.message_id,
          call_id = excluded.call_id,
          runtime_json = excluded.runtime_json,
          canonical_json = excluded.canonical_json
      `)
      .run(
        node.node_id,
        sequence,
        node.kind,
        node.component ?? null,
        node.status ?? null,
        runtimeScopeValue(node, ["session_id", "sessionID", "parent_session_id"]) ??
          canonical.scope.session_id ??
          null,
        runtimeScopeValue(node, ["message_id", "messageID"]) ?? canonical.scope.message_id ?? null,
        runtimeScopeValue(node, ["call_id", "callID", "tool_call_id", "toolCallID"]) ??
          canonical.scope.call_id ??
          null,
        JSON.stringify(node),
        JSON.stringify(canonical),
      )
  }

  private putEdge(edge: CausalEdgeLike, canonical: CausalIREdge, sequence: number) {
    this.db
      .query(`
        INSERT INTO edges(edge_id, sequence, relation, from_id, to_id, runtime_json, canonical_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(edge_id) DO UPDATE SET
          sequence = excluded.sequence,
          relation = excluded.relation,
          from_id = excluded.from_id,
          to_id = excluded.to_id,
          runtime_json = excluded.runtime_json,
          canonical_json = excluded.canonical_json
      `)
      .run(
        edge.edge_id,
        sequence,
        canonical.original_relation,
        edge.from.id,
        edge.to.id,
        JSON.stringify(edge),
        JSON.stringify(canonical),
      )
  }

  private putArtifact(artifact: ArtifactLike, sequence: number) {
    this.db
      .query(`
        INSERT INTO artifacts(artifact_id, sequence, hash, json) VALUES (?, ?, ?, ?)
        ON CONFLICT(artifact_id) DO UPDATE SET sequence = excluded.sequence, hash = excluded.hash, json = excluded.json
      `)
      .run(artifact.artifact_id, sequence, artifact.hash, JSON.stringify(artifact))
  }

  private putDiagnostic(diagnostic: CausalIRDiagnosticLike) {
    const existing = this.db
      .query<SequenceRow, [string]>("SELECT sequence FROM diagnostics WHERE diagnostic_id = ?")
      .get(diagnostic.diagnostic_id)
    const sequence = existing?.sequence ?? ++this.diagnosticSequence
    this.db
      .query(`
        INSERT INTO diagnostics(diagnostic_id, sequence, kind, owner_type, owner_id, json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(diagnostic_id) DO UPDATE SET
          sequence = excluded.sequence,
          kind = excluded.kind,
          owner_type = excluded.owner_type,
          owner_id = excluded.owner_id,
          json = excluded.json
      `)
      .run(
        diagnostic.diagnostic_id,
        sequence,
        typeof diagnostic.kind === "string" ? diagnostic.kind : null,
        typeof diagnostic.owner_type === "string" ? diagnostic.owner_type : null,
        typeof diagnostic.node_id === "string"
          ? diagnostic.node_id
          : typeof diagnostic.edge_id === "string"
            ? diagnostic.edge_id
            : null,
        JSON.stringify(diagnostic),
      )
  }

  private indexNodeReferences(node: CausalNodeLike) {
    this.db.query("DELETE FROM refs WHERE owner_type = 'node' AND owner_id = ?").run(node.node_id)
    forEachNodeReference(node, (field, ref) => this.putReference("node", node.node_id, field, ref))
  }

  private indexEdgeReferences(edge: CausalEdgeLike) {
    this.db.query("DELETE FROM refs WHERE owner_type = 'edge' AND owner_id = ?").run(edge.edge_id)
    forEachEdgeReference(edge, (field, ref) => this.putReference("edge", edge.edge_id, field, ref))
  }

  private putReference(ownerType: "node" | "edge", ownerID: string, field: string, input: string | CausalIRRef | CausalEdgeLike["from"]) {
    const reference = causalIRLegacyRef(typedCausalIRReference(input))
    const aliases = causalIRReferenceAliasCandidates(input)
    if (!aliases.length) {
      this.db
        .query("INSERT OR IGNORE INTO refs(owner_type, owner_id, field, reference, alias) VALUES (?, ?, ?, ?, ?)")
        .run(ownerType, ownerID, field, reference, reference)
      return
    }
    for (const alias of aliases) {
      this.db
        .query("INSERT OR IGNORE INTO refs(owner_type, owner_id, field, reference, alias) VALUES (?, ?, ?, ?, ?)")
        .run(ownerType, ownerID, field, reference, alias)
    }
  }

  private affectedEntities(aliases: Iterable<string>, excludedNodeID: string) {
    const values = [...aliases]
    if (!values.length) return { nodes: new Map<string, CausalIRNode>(), edges: new Map<string, CausalIREdge>() }
    const rows = this.db
      .query<{ owner_type: "node" | "edge"; owner_id: string }, string[]>(
        `SELECT DISTINCT owner_type, owner_id FROM refs WHERE alias IN (${placeholders(values)})`,
      )
      .all(...values)
    const nodes = new Map<string, CausalIRNode>()
    const edges = new Map<string, CausalIREdge>()
    for (const row of rows) {
      if (row.owner_type === "node") {
        if (row.owner_id === excludedNodeID || nodes.has(row.owner_id)) continue
        const canonical = parse<CausalIRNode>(
          this.db.query<EntityRow, [string]>("SELECT canonical_json AS json FROM nodes WHERE node_id = ?").get(row.owner_id),
        )
        if (canonical) nodes.set(row.owner_id, canonical)
        continue
      }
      if (edges.has(row.owner_id)) continue
      const canonical = parse<CausalIREdge>(
        this.db.query<EntityRow, [string]>("SELECT canonical_json AS json FROM edges WHERE edge_id = ?").get(row.owner_id),
      )
      if (canonical) edges.set(row.owner_id, canonical)
    }
    return { nodes, edges }
  }

  private reconcileAffectedEntities(affected: { nodes: Map<string, CausalIRNode>; edges: Map<string, CausalIREdge> }) {
    for (const [nodeID, before] of [...affected.nodes].sort(([left], [right]) => left.localeCompare(right))) {
      const node = this.getNode(nodeID)
      const order = this.nodeOrder(nodeID)
      if (!node || order === undefined) continue
      const canonical = canonicalCausalIRNode(
        node,
        { runID: this.input.runID, caseID: this.input.caseID, sequence: order },
        this.resolveReference.bind(this),
      )
      if (causalIRPayloadHash(before) !== causalIRPayloadHash(canonical)) {
        const committed = this.append("node.updated", "node.update", nodeID, canonical, "node")
        if (!committed.committed) throw ROLLBACK
        this.putNode(node, canonical, order)
      }
      this.reconcileReferenceDiagnostics("node", nodeID)
    }
    for (const [edgeID, before] of [...affected.edges].sort(([left], [right]) => left.localeCompare(right))) {
      const edge = this.getEdge(edgeID)
      const order = this.edgeOrder(edgeID)
      if (!edge || order === undefined) continue
      const canonical = canonicalCausalIREdge(edge, this.resolveReference.bind(this))
      if (causalIRPayloadHash(before) !== causalIRPayloadHash(canonical)) {
        const committed = this.append("edge.created", "edge", edgeID, canonical)
        if (!committed.committed) throw ROLLBACK
        this.putEdge(edge, canonical, order)
      }
      this.reconcileReferenceDiagnostics("edge", edgeID)
    }
  }

  private reconcileAliasCollisionDiagnostic(alias: string) {
    const owners = this.db
      .query<AliasOwnerRow, [string]>("SELECT node_id FROM aliases WHERE alias = ? ORDER BY node_id")
      .all(alias)
      .map((row) => row.node_id)
    const diagnostic = causalIRAliasCollisionDiagnostic(alias, owners)
    const diagnosticID = `alias_collision:${causalIRPayloadHash(alias).slice(0, 16)}`
    const existing = this.getDiagnostic(diagnosticID)
    if (!diagnostic) {
      this.deleteDiagnostic(diagnosticID)
      return
    }
    if (existing && causalIRPayloadHash(existing) === causalIRPayloadHash(diagnostic)) return
    this.putDiagnostic(diagnostic)
    const committed = this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
    if (!committed.committed) throw ROLLBACK
  }

  private reconcileUnknownRelationDiagnostic(edge: CausalEdgeLike) {
    const diagnostic = causalIRUnknownRelationDiagnostic(edge)
    const diagnosticID = `unknown_relation:${edge.edge_id}`
    const existing = this.getDiagnostic(diagnosticID)
    if (!diagnostic) {
      this.deleteDiagnostic(diagnosticID)
      return
    }
    if (existing?.relation === diagnostic.relation) return
    this.putDiagnostic(diagnostic)
    const committed = this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
    if (!committed.committed) throw ROLLBACK
  }

  private reconcileReferenceDiagnostics(type: "node" | "edge", id: string) {
    const previous = this.db
      .query<IDRow, [string, string]>(
        "SELECT diagnostic_id AS id FROM diagnostics WHERE kind = 'unresolved_ref' AND owner_type = ? AND owner_id = ?",
      )
      .all(type, id)
      .map((row) => row.id)
    const next = new Map<string, CausalIRDiagnosticLike>()
    const inspect = (field: string, input: string | CausalIRRef | CausalEdgeLike["from"]) => {
      const declared = typedCausalIRReference(input)
      if (declared.ref_type !== "node" || this.resolveReference(input).ref_type !== "external") return
      const diagnostic = causalIRUnresolvedReferenceOccurrenceDiagnostic(
        { type, id },
        field,
        causalIRLegacyRef(declared),
      )
      next.set(diagnostic.diagnostic_id, diagnostic)
    }
    if (type === "node") {
      const node = this.getNode(id)
      if (node) forEachNodeReference(node, inspect)
    } else {
      const edge = this.getEdge(id)
      if (edge) forEachEdgeReference(edge, inspect)
    }
    for (const diagnosticID of previous) if (!next.has(diagnosticID)) this.deleteDiagnostic(diagnosticID)
    for (const diagnostic of next.values()) {
      const existing = this.getDiagnostic(diagnostic.diagnostic_id)
      if (existing && causalIRPayloadHash(existing) === causalIRPayloadHash(diagnostic)) continue
      this.putDiagnostic(diagnostic)
      const committed = this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
      if (!committed.committed) throw ROLLBACK
    }
  }

  private reconcileAllAliasCollisionDiagnostics() {
    const existing = new Map(
      this.queryDiagnostics({ kinds: ["alias_collision"] }).map((diagnostic) => [diagnostic.diagnostic_id, diagnostic]),
    )
    const desired = new Map<string, CausalIRDiagnosticLike>()
    for (const row of this.db.query<{ alias: string }, []>("SELECT DISTINCT alias FROM aliases ORDER BY alias").all()) {
      const owners = this.db
        .query<AliasOwnerRow, [string]>("SELECT node_id FROM aliases WHERE alias = ? ORDER BY node_id")
        .all(row.alias)
        .map((owner) => owner.node_id)
      const diagnostic = causalIRAliasCollisionDiagnostic(row.alias, owners)
      if (diagnostic) desired.set(diagnostic.diagnostic_id, diagnostic)
    }
    for (const diagnosticID of existing.keys()) if (!desired.has(diagnosticID)) this.deleteDiagnostic(diagnosticID)
    for (const diagnostic of desired.values()) {
      this.putDiagnostic(diagnostic)
      if (causalIRPayloadHash(existing.get(diagnostic.diagnostic_id)) === causalIRPayloadHash(diagnostic)) continue
      const committed = this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
      if (!committed.committed) throw ROLLBACK
    }
  }

  private reconcileAllUnknownRelationDiagnostics() {
    const existing = new Map(
      this.queryDiagnostics({ kinds: ["unknown_relation"] }).map((diagnostic) => [diagnostic.diagnostic_id, diagnostic]),
    )
    const desired = new Map<string, CausalIRDiagnosticLike>()
    for (const edge of this.queryEdges()) {
      const diagnostic = causalIRUnknownRelationDiagnostic(edge)
      if (diagnostic) desired.set(diagnostic.diagnostic_id, diagnostic)
    }
    for (const diagnosticID of existing.keys()) if (!desired.has(diagnosticID)) this.deleteDiagnostic(diagnosticID)
    for (const diagnostic of desired.values()) {
      this.putDiagnostic(diagnostic)
      if (existing.get(diagnostic.diagnostic_id)?.relation === diagnostic.relation) continue
      const committed = this.append("diagnostic.created", "diagnostic", diagnostic.diagnostic_id, diagnostic)
      if (!committed.committed) throw ROLLBACK
    }
  }

  private reconcileAllReferenceDiagnostics() {
    this.db.exec(`
      DELETE FROM diagnostics
      WHERE kind = 'unresolved_ref'
        AND (
          (owner_type = 'node' AND owner_id NOT IN (SELECT node_id FROM nodes))
          OR (owner_type = 'edge' AND owner_id NOT IN (SELECT edge_id FROM edges))
        )
    `)
    for (const node of this.queryNodes()) this.reconcileReferenceDiagnostics("node", node.node_id)
    for (const edge of this.queryEdges()) this.reconcileReferenceDiagnostics("edge", edge.edge_id)
  }

  private recanonicalizeAllEdges() {
    for (const edge of this.queryEdges()) {
      const order = this.edgeOrder(edge.edge_id)
      if (order === undefined) continue
      const canonical = canonicalCausalIREdge(edge, this.resolveReference.bind(this))
      this.putEdge(edge, canonical, order)
      this.setPayloadHash("edge", edge.edge_id, causalIRPayloadHash(canonical))
    }
  }

  private getNode(nodeID: string) {
    const hot = this.hotNodes.get(nodeID)
    if (hot) return copyCausalIRJournalData(hot)
    const node = parse<CausalNodeLike>(
      this.db.query<EntityRow, [string]>("SELECT runtime_json AS json FROM nodes WHERE node_id = ?").get(nodeID),
    )
    if (node) this.hotNodes.set(nodeID, node)
    return node ? copyCausalIRJournalData(node) : undefined
  }

  private getEdge(edgeID: string) {
    const hot = this.hotEdges.get(edgeID)
    if (hot) return copyCausalIRJournalData(hot)
    const edge = parse<CausalEdgeLike>(
      this.db.query<EntityRow, [string]>("SELECT runtime_json AS json FROM edges WHERE edge_id = ?").get(edgeID),
    )
    if (edge) this.hotEdges.set(edgeID, edge)
    return edge ? copyCausalIRJournalData(edge) : undefined
  }

  private getDiagnostic(diagnosticID: string) {
    return parse<CausalIRDiagnosticLike>(
      this.db.query<EntityRow, [string]>("SELECT json FROM diagnostics WHERE diagnostic_id = ?").get(diagnosticID),
    )
  }

  private deleteDiagnostic(diagnosticID: string) {
    this.db.query("DELETE FROM diagnostics WHERE diagnostic_id = ?").run(diagnosticID)
  }

  private hasNode(nodeID: string) {
    return Boolean(this.db.query<{ found: number }, [string]>("SELECT 1 AS found FROM nodes WHERE node_id = ?").get(nodeID))
  }

  private hasArtifact(artifactID: string) {
    return Boolean(
      this.db.query<{ found: number }, [string]>("SELECT 1 AS found FROM artifacts WHERE artifact_id = ?").get(artifactID),
    )
  }

  private edgeOrder(edgeID: string) {
    return this.db.query<SequenceRow, [string]>("SELECT sequence FROM edges WHERE edge_id = ?").get(edgeID)?.sequence
  }

  private artifactOrder(artifactID: string) {
    return this.db
      .query<SequenceRow, [string]>("SELECT sequence FROM artifacts WHERE artifact_id = ?")
      .get(artifactID)?.sequence
  }

  private payloadHash(hashType: string, entityID: string) {
    return this.db
      .query<HashRow, [string, string]>(
        "SELECT payload_hash FROM payload_hashes WHERE hash_type = ? AND entity_id = ?",
      )
      .get(hashType, entityID)?.payload_hash
  }

  private setPayloadHash(hashType: string, entityID: string, payloadHash: string) {
    this.db
      .query(`
        INSERT INTO payload_hashes(hash_type, entity_id, payload_hash) VALUES (?, ?, ?)
        ON CONFLICT(hash_type, entity_id) DO UPDATE SET payload_hash = excluded.payload_hash
      `)
      .run(hashType, entityID, payloadHash)
  }

  private clearPayloadHashes(hashType: string) {
    this.db.query("DELETE FROM payload_hashes WHERE hash_type = ?").run(hashType)
  }

  private setMetadata(key: string, value: string | number) {
    this.db
      .query(`
        INSERT INTO metadata(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
      `)
      .run(key, String(value))
  }
}
