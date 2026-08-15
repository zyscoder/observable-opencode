import { Database } from "bun:sqlite"
import fs from "node:fs"
import path from "node:path"
import {
  CAUSAL_IR_VERSION,
  CausalIRJournalValidationError,
  canonicalCausalIREdge,
  causalIRLegacyRef,
  causalIRPayloadHash,
  projectProvenanceTrace,
  typedCausalIRReference,
  validateCausalIRJournal,
  type ArtifactLike,
  type CausalIREdge,
  type CausalIRJournalEntry,
  type CausalIRNode,
  type CausalIRRef,
  type CausalIRRuntimeCloseData,
  type CausalIRStoreSnapshot,
  type CausalIRDiagnosticLike,
} from "./causal-ir"
import {
  streamingJsonArray,
  streamingJsonRawChunks,
  streamingJsonRawItem,
  writeStreamingJsonObjectAtomic,
  type StreamingJsonObjectMember,
} from "./streaming-json-writer"
import { isFormalRecordType, TRACE_VERSION } from "./trace-semantic-contract"
import type { TraceSegmentDescriptor, TraceSessionManifest } from "./trace-segment"

export type TraceMaterializationResult = {
  caseDir: string
  traceFile: string
  manifestFile: string
  partialFile: string
  completeness: "complete" | "incomplete"
  recoveredLines: number
}

type JsonRow = { json: string }
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
  return scope.namespace ? `${scope.segmentID}::${type}::${id}` : id
}

function scopedLegacyReference(value: string | undefined, scope: SegmentReplayScope) {
  if (!value || !scope.namespace) return value
  const separator = value.indexOf(":")
  if (separator < 1 || separator === value.length - 1) return value
  const reference = typedCausalIRReference(value)
  const type = reference.ref_type === "node" ? "node" : reference.ref_type === "artifact" ? "artifact" : undefined
  if (!type) return value
  return `${value.slice(0, separator)}:${scopedEntityID(scope, type, value.slice(separator + 1))}`
}

function scopedPayloadReferences(value: unknown, scope: SegmentReplayScope): unknown {
  if (!scope.namespace) return value
  if (typeof value === "string") return scopedLegacyReference(value, scope) ?? value
  if (Array.isArray(value)) return value.map((item) => scopedPayloadReferences(item, scope))
  const object = record(value)
  if (!object) return value
  return Object.fromEntries(Object.entries(object).map(([key, item]) => [key, scopedPayloadReferences(item, scope)]))
}

function scopedReference(ref: CausalIRRef, scope: SegmentReplayScope): CausalIRRef {
  if (ref.ref_type === "node")
    return {
      ...ref,
      ref_id: scopedEntityID(scope, "node", ref.ref_id),
      ...(ref.legacy_ref ? { legacy_ref: scopedLegacyReference(ref.legacy_ref, scope) } : {}),
    }
  if (ref.ref_type === "artifact")
    return {
      ...ref,
      ref_id: scopedEntityID(scope, "artifact", ref.ref_id),
      ...(ref.legacy_ref ? { legacy_ref: scopedLegacyReference(ref.legacy_ref, scope) } : {}),
    }
  if (ref.ref_type === "raw_event" && scope.namespace)
    return { ...ref, ref_id: `${scope.runID}::raw_event::${ref.ref_id}` }
  return ref
}

function scopedNode(node: CausalIRNode, scope: SegmentReplayScope): CausalIRNode {
  if (!scope.namespace) return node
  const nodeID = scopedEntityID(scope, "node", node.node_id)
  const payload = scopedPayloadReferences(node.payload, scope) as Record<string, unknown>
  return {
    ...node,
    node_id: nodeID,
    scope: { ...node.scope, run_id: scope.runID, case_id: scope.caseID },
    payload,
    data: payload,
    input_refs: node.input_refs.map((ref) => scopedReference(ref, scope)),
    output_refs: node.output_refs.map((ref) => scopedReference(ref, scope)),
    source_refs: node.source_refs.map((ref) => scopedReference(ref, scope)),
    artifact_refs: node.artifact_refs.map((id) => scopedEntityID(scope, "artifact", id)),
    aliases: [...node.aliases, `run:${scope.runID}:node:${node.node_id}`],
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
  return {
    ...edge,
    edge_id: scopedEntityID(scope, "edge", edge.edge_id),
    from: scopedReference(edge.from, scope),
    to: scopedReference(edge.to, scope),
    evidence_refs: edge.evidence_refs.map((ref) => scopedReference(ref, scope)),
    scope: { run_id: scope.runID, case_id: scope.caseID },
    metadata: {
      ...(edge.metadata ?? {}),
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
  return {
    ...artifact,
    artifact_id: scopedEntityID(scope, "artifact", artifact.artifact_id),
    path: scopedPath,
    original_artifact_id: artifact.artifact_id,
    scope: { segment_id: scope.segmentID, run_id: scope.runID, case_id: scope.caseID },
  }
}

function scopedDiagnostic(diagnostic: CausalIRDiagnosticLike, scope: SegmentReplayScope): CausalIRDiagnosticLike {
  if (!scope.namespace) return diagnostic
  return {
    ...diagnostic,
    diagnostic_id: scopedEntityID(scope, "diagnostic", diagnostic.diagnostic_id),
    original_diagnostic_id: diagnostic.diagnostic_id,
    scope: { segment_id: scope.segmentID, run_id: scope.runID, case_id: scope.caseID },
    ...(typeof diagnostic.artifact_id === "string"
      ? { artifact_id: scopedEntityID(scope, "artifact", diagnostic.artifact_id) }
      : {}),
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
    this.terminalClose = this.runtimeClosed ? (entry.data as CausalIRRuntimeCloseData) : finalizedClose(entry)
    this.terminal = terminalEnvelope(entry)
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

function linkCompatibilityArtifacts(sourceDirectory: string, destinationDirectory: string) {
  if (!fs.existsSync(sourceDirectory)) return
  for (const entry of fs.readdirSync(sourceDirectory, { withFileTypes: true })) {
    const source = path.join(sourceDirectory, entry.name)
    const destination = path.join(destinationDirectory, entry.name)
    if (entry.isDirectory()) {
      fs.mkdirSync(destination, { recursive: true })
      linkCompatibilityArtifacts(source, destination)
      continue
    }
    if (!entry.isFile() || fs.existsSync(destination)) continue
    fs.mkdirSync(path.dirname(destination), { recursive: true })
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

type MaterializationSource = {
  key: string
  runID?: string
  caseID?: string
  pathPrefix: string
  recordsFile: string
  descriptor?: TraceSegmentDescriptor
}

function resolveWithin(root: string, relative: string) {
  const resolved = path.resolve(root, relative)
  if (resolved !== root && !resolved.startsWith(root + path.sep))
    throw new Error(`${relative}: path escapes case directory`)
  return resolved
}

function readSession(caseDir: string) {
  const file = path.join(caseDir, "session.json")
  if (!fs.existsSync(file)) return undefined
  const input = record(JSON.parse(fs.readFileSync(file, "utf8")) as unknown)
  if (
    !input ||
    input.schema_version !== "1.0" ||
    !nonemptyString(input.logical_case_id) ||
    !Array.isArray(input.segments)
  )
    throw new Error(`${file}: invalid trace session manifest`)
  const session = input as TraceSessionManifest
  if (!session.segments.length) throw new Error(`${file}: trace session has no segments`)
  const runIDs = new Set<string>()
  for (const descriptor of session.segments) {
    if (
      !record(descriptor) ||
      !nonemptyString(descriptor.segment_id) ||
      !nonemptyString(descriptor.run_id) ||
      !nonemptyString(descriptor.case_id) ||
      !nonemptyString(descriptor.path) ||
      !nonemptyString(descriptor.records)
    )
      throw new Error(`${file}: invalid trace segment descriptor`)
    if (runIDs.has(descriptor.run_id)) throw new Error(`${file}: duplicate run ${descriptor.run_id}`)
    runIDs.add(descriptor.run_id)
  }
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
    recordsFile: resolveWithin(caseDir, descriptor.records),
    descriptor,
  }))
}

export function materializeTrace(input: { caseDir: string }): TraceMaterializationResult {
  const caseDir = path.resolve(input.caseDir)
  if (!fs.statSync(caseDir).isDirectory()) throw new Error(`${caseDir}: expected a case directory`)
  const session = readSession(caseDir)
  const sources = materializationSources(caseDir, session)
  const indexPath = path.join(
    caseDir,
    `.trace-materializer.${process.pid}.${Math.random().toString(16).slice(2)}.sqlite`,
  )
  const index = new ReplayIndex(indexPath)
  try {
    memoryPhase("materialize_start")
    const namespace = session !== undefined && sources.length > 1
    const replays = sources.map((source) => {
      index.beginSegment({
        key: source.key,
        segmentID: source.key,
        runID: source.runID ?? "",
        caseID: source.caseID ?? "",
        pathPrefix: source.pathPrefix,
        namespace,
      })
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
    index.reconcileDiagnostics()
    memoryPhase("diagnostics_complete", journalBytes)
    const latest = replays.at(-1)!
    const terminal = replays.findLast((replay) => replay.close && replay.envelope?.manifest) ?? latest
    const complete = droppedLines === 0 && replays.every((replay) => replay.close !== undefined)
    const completeness = complete ? "complete" : "incomplete"
    const terminalManifest = terminal.envelope?.manifest ?? {}
    const status = complete
      ? typeof terminalManifest.status === "string"
        ? terminalManifest.status
        : latest.close!.status
      : "error"
    const runID = latest.identities.runID
    const caseID = session?.logical_case_id ?? latest.identities.caseID
    const sourceFiles = record(terminalManifest.files) ?? {}
    const activeRecords = latest.source.descriptor?.records ?? "records.jsonl"
    const activeRawEvents = latest.source.descriptor
      ? path.join(latest.source.descriptor.path, "raw-events.jsonl")
      : "raw-events.jsonl"
    const manifest: Record<string, unknown> = {
      ...terminalManifest,
      trace_version: TRACE_VERSION,
      case_id: caseID,
      run_id: runID,
      status,
      server_status: terminalManifest.server_status ?? status,
      process_status: terminalManifest.process_status ?? status,
      case_status: terminalManifest.case_status ?? status,
      ...((session?.session_id ?? latest.close?.manifest.session_id)
        ? { session_id: session?.session_id ?? latest.close?.manifest.session_id }
        : {}),
      ...(session
        ? {
            segments: session.segments,
            segment_summary: {
              count: session.segments.length,
              completed: session.segments.filter((segment) => segment.status === "completed").length,
              failed: session.segments.filter((segment) => segment.status === "failed").length,
              cancelled: session.segments.filter((segment) => segment.status === "cancelled").length,
              interrupted_unfinalized: session.segments.filter(
                (segment) => segment.status === "interrupted_unfinalized",
              ).length,
              running: session.segments.filter((segment) => segment.status === "running").length,
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
          }),
    }
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
    const traceFile = path.join(caseDir, "trace.json")
    const manifestFile = path.join(caseDir, "manifest.json")
    const partialFile = path.join(caseDir, "partial", "latest.json")
    const provenanceFile = path.join(caseDir, "provenance-trace.json")
    const legacyFile = path.join(caseDir, "legacy-trace.json")
    writeStreamingJsonObjectAtomic(traceFile, traceMembers(index, manifest, metrics, journal))
    memoryPhase("trace_complete", journalBytes)
    writeStreamingJsonObjectAtomic(manifestFile, Object.entries(manifest))
    memoryPhase("manifest_complete", journalBytes)
    linkFileAtomic(traceFile, partialFile)
    memoryPhase("partial_complete", journalBytes)
    writeStreamingJsonObjectAtomic(provenanceFile, provenanceMembers(index, manifest, metrics))
    memoryPhase("provenance_complete", journalBytes)
    const legacySource = replays.findLast((replay) =>
      fs.existsSync(path.join(path.dirname(replay.source.recordsFile), "legacy-trace.json")),
    )
    if (legacySource) {
      copyFileAtomic(path.join(path.dirname(legacySource.source.recordsFile), "legacy-trace.json"), legacyFile)
      linkCompatibilityArtifacts(
        path.join(path.dirname(legacySource.source.recordsFile), "artifacts"),
        path.join(caseDir, "artifacts"),
      )
    }
    return { caseDir, traceFile, manifestFile, partialFile, completeness, recoveredLines: index.recoveredLines }
  } finally {
    index.closeIndex()
    try {
      fs.unlinkSync(indexPath)
    } catch {}
  }
}
