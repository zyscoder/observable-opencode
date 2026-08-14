import { Database } from "bun:sqlite"
import fs from "node:fs"
import path from "node:path"
import { StringDecoder } from "node:string_decoder"
import {
  CAUSAL_IR_VERSION,
  CausalIRJournalValidationError,
  canonicalCausalIREdge,
  causalIRAliasCollisionDiagnostic,
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
  streamingJsonRawItem,
  writeStreamingJsonObjectAtomic,
  type StreamingJsonObjectMember,
} from "./streaming-json-writer"
import { isFormalRecordType, TRACE_VERSION } from "./trace-semantic-contract"

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

function validateEntryShape(entry: CausalIRJournalEntry, requireInitialRunNode: boolean) {
  validateCausalIRJournal([normalizedValidationEntry(entry)], { requireInitialRunNode })
}

class ReplayIndex {
  readonly db: Database
  private runID = ""
  private caseID = ""
  private runtimeClosed = false
  private terminalClose: CausalIRRuntimeCloseData | undefined
  private lastOperation: CausalIRJournalEntry["operation"] | undefined
  private lastPayloadHash: string | undefined
  recoveredLines = 0

  constructor(readonly indexPath: string) {
    this.db = new Database(indexPath, { create: true, strict: true })
    this.db.exec("PRAGMA journal_mode = OFF")
    this.db.exec("PRAGMA synchronous = OFF")
    this.db.exec("PRAGMA temp_store = FILE")
    this.db.exec("PRAGMA cache_size = -8192")
    this.db.exec(`
      CREATE TABLE nodes (
        entity_id TEXT PRIMARY KEY,
        ordinal INTEGER NOT NULL,
        kind TEXT NOT NULL,
        json TEXT NOT NULL
      );
      CREATE TABLE edges (
        entity_id TEXT PRIMARY KEY,
        ordinal INTEGER NOT NULL,
        json TEXT NOT NULL
      );
      CREATE TABLE artifacts (
        entity_id TEXT PRIMARY KEY,
        ordinal INTEGER NOT NULL,
        json TEXT NOT NULL
      );
      CREATE TABLE diagnostics (
        entity_id TEXT PRIMARY KEY,
        ordinal INTEGER NOT NULL,
        kind TEXT,
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
      CREATE TABLE generated_diagnostics (
        diagnostic_id TEXT PRIMARY KEY,
        json TEXT NOT NULL
      );
    `)
  }

  get identities() {
    return { runID: this.runID, caseID: this.caseID }
  }

  get close() {
    return this.lastOperation === "case.runtime_closed" ? this.terminalClose : undefined
  }

  get finalPayloadHash() {
    return this.lastPayloadHash
  }

  private validationError(message: string): never {
    throw new CausalIRJournalValidationError(this.recoveredLines + 1, message)
  }

  private validate(entry: CausalIRJournalEntry) {
    const line = this.recoveredLines + 1
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
    if (entry.payload_hash !== causalIRPayloadHash(entry.data))
      this.validationError("journal payload hash does not match data")
    const previous = this.db
      .query<HashRow, [string]>("SELECT payload_hash FROM payload_hashes WHERE hash_key = ?")
      .get(hashKey(entry.operation, entry.entity_id))?.payload_hash
    if (entry.previous_payload_hash !== previous)
      this.validationError("previous payload hash does not match entity history")
    try {
      validateEntryShape(entry, line === 1)
    } catch (error) {
      if (error instanceof CausalIRJournalValidationError) this.validationError(error.message)
      throw error
    }
  }

  apply(input: unknown) {
    if (!record(input)) this.validationError("expected a journal entry object")
    const entry = input as CausalIRJournalEntry
    this.validate(entry)
    const snapshot = lifecycleSnapshot(entry.data)

    this.db.transaction(() => {
      if ((entry.operation === "node.created" || entry.operation === "node.updated") && record(entry.data)) {
        const node = entry.data as CausalIRNode
        this.putNode(node, entry.sequence)
      } else if (entry.operation === "edge.created" && record(entry.data)) {
        this.putEdge(canonicalCausalIREdge(entry.data as CausalIREdge), entry.sequence)
      } else if (
        (entry.operation === "artifact.created" || entry.operation === "artifact.reused") &&
        record(entry.data)
      ) {
        this.putArtifact(entry.data as ArtifactLike, entry.sequence)
      } else if (entry.operation === "diagnostic.created" && record(entry.data)) {
        this.putDiagnostic(entry.data as CausalIRDiagnosticLike, entry.sequence)
      } else if ((entry.operation === "case.checkpointed" || entry.operation === "case.finalized") && snapshot) {
        this.replaceSnapshot(snapshot)
      }

      const data = record(entry.data)
      if (entry.operation === "case.checkpointed" && data?.hash_state_replacement === "nodes_and_edges") {
        this.replacePayloadHashes("node", snapshot?.nodes ?? [], "node_id")
        this.replacePayloadHashes("edge", snapshot?.edges ?? [], "edge_id")
      } else if (entry.operation === "case.checkpointed" && data?.hash_state_replacement === "edges") {
        this.replacePayloadHashes("edge", snapshot?.edges ?? [], "edge_id")
      }
      this.db
        .query("INSERT OR REPLACE INTO payload_hashes(hash_key, payload_hash) VALUES (?, ?)")
        .run(hashKey(entry.operation, entry.entity_id!), entry.payload_hash!)
    })()

    if (!this.runID) {
      this.runID = entry.run_id
      this.caseID = entry.case_id
    }
    this.recoveredLines = entry.sequence
    this.lastOperation = entry.operation
    this.lastPayloadHash = entry.payload_hash
    this.runtimeClosed = entry.operation === "case.runtime_closed"
    this.terminalClose = this.runtimeClosed ? (entry.data as CausalIRRuntimeCloseData) : undefined
  }

  private putNode(node: CausalIRNode, ordinal: number) {
    this.db
      .query(
        `
        INSERT INTO nodes(entity_id, ordinal, kind, json) VALUES (?, ?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET kind = excluded.kind, json = excluded.json
      `,
      )
      .run(node.node_id, ordinal, node.kind, JSON.stringify(node))
  }

  private putEdge(edge: CausalIREdge, ordinal: number) {
    this.db
      .query(
        `
        INSERT INTO edges(entity_id, ordinal, json) VALUES (?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET json = excluded.json
      `,
      )
      .run(edge.edge_id, ordinal, JSON.stringify(edge))
  }

  private putArtifact(artifact: ArtifactLike, ordinal: number) {
    this.db
      .query(
        `
        INSERT INTO artifacts(entity_id, ordinal, json) VALUES (?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET json = excluded.json
      `,
      )
      .run(artifact.artifact_id, ordinal, JSON.stringify(artifact))
  }

  private putDiagnostic(diagnostic: CausalIRDiagnosticLike, ordinal: number) {
    this.db
      .query(
        `
        INSERT INTO diagnostics(entity_id, ordinal, kind, json) VALUES (?, ?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET kind = excluded.kind, json = excluded.json
      `,
      )
      .run(
        diagnostic.diagnostic_id,
        ordinal,
        typeof diagnostic.kind === "string" ? diagnostic.kind : null,
        JSON.stringify(diagnostic),
      )
  }

  private replaceSnapshot(snapshot: CausalIRStoreSnapshot) {
    this.db.exec("DELETE FROM nodes; DELETE FROM edges; DELETE FROM artifacts; DELETE FROM diagnostics")
    for (const [index, node] of snapshot.nodes.entries()) this.putNode(node, index + 1)
    for (const [index, edge] of snapshot.edges.entries()) this.putEdge(canonicalCausalIREdge(edge), index + 1)
    for (const [index, artifact] of snapshot.artifacts.entries()) this.putArtifact(artifact, index + 1)
    for (const [index, diagnostic] of snapshot.diagnostics.entries()) this.putDiagnostic(diagnostic, index + 1)
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
    this.db.exec("DELETE FROM aliases; DELETE FROM unresolved_occurrences; DELETE FROM generated_diagnostics")
    for (const row of this.db.query<JsonRow, []>("SELECT json FROM nodes ORDER BY ordinal").iterate()) {
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
    for (const row of this.db.query<JsonRow, []>("SELECT json FROM edges ORDER BY ordinal").iterate()) {
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
      const owners = this.db
        .query<OwnerRow, [string]>("SELECT node_id FROM aliases WHERE alias = ? ORDER BY node_id")
        .all(row.alias)
        .map((owner) => owner.node_id)
      const diagnostic = causalIRAliasCollisionDiagnostic(row.alias, owners)
      if (diagnostic) this.putGeneratedDiagnostic(diagnostic)
    }
    for (const row of this.db
      .query<LegacyRow, []>("SELECT legacy_ref FROM unresolved_occurrences GROUP BY legacy_ref")
      .iterate()) {
      const affected = this.db
        .query<
          OccurrenceRow,
          [string]
        >("SELECT owner_type, owner_id, field FROM unresolved_occurrences WHERE legacy_ref = ? ORDER BY ordinal")
        .all(row.legacy_ref)
      const first = affected[0]
      this.putGeneratedDiagnostic({
        diagnostic_id: `unresolved_ref:${causalIRPayloadHash(row.legacy_ref).slice(0, 16)}`,
        kind: "unresolved_ref",
        level: "warning",
        message: `Unresolved causal reference: ${row.legacy_ref}`,
        legacy_ref: row.legacy_ref,
        occurrence_count: affected.length,
        affected_owners: affected,
        owner_type: first?.owner_type,
        field: first?.field,
        ...(first?.owner_type === "node" ? { node_id: first.owner_id } : first ? { edge_id: first.owner_id } : {}),
      })
    }
  }

  private putGeneratedDiagnostic(diagnostic: CausalIRDiagnosticLike) {
    this.db
      .query("INSERT OR REPLACE INTO generated_diagnostics(diagnostic_id, json) VALUES (?, ?)")
      .run(diagnostic.diagnostic_id, JSON.stringify(diagnostic))
  }

  count(table: "nodes" | "edges" | "artifacts") {
    return this.db.query<CountRow, []>(`SELECT COUNT(*) AS count FROM ${table}`).get()!.count
  }

  recordCount() {
    let count = 0
    for (const row of this.db.query<KindRow, []>("SELECT kind FROM nodes").iterate())
      if (isFormalRecordType(row.kind === "final.claim" ? "response.output" : row.kind)) count += 1
    return count
  }

  *rawEntities(table: "nodes" | "edges" | "artifacts") {
    for (const row of this.db.query<JsonRow, []>(`SELECT json FROM ${table} ORDER BY ordinal`).iterate())
      yield streamingJsonRawItem(row.json)
  }

  *diagnosticItems() {
    for (const row of this.db
      .query<
        JsonRow,
        []
      >("SELECT json FROM diagnostics WHERE kind IS NULL OR kind NOT IN ('unknown_relation', 'alias_collision', 'unresolved_ref') ORDER BY ordinal")
      .iterate())
      yield streamingJsonRawItem(row.json)
    for (const row of this.db
      .query<JsonRow, []>("SELECT json FROM generated_diagnostics ORDER BY diagnostic_id")
      .iterate())
      yield streamingJsonRawItem(row.json)
  }

  *compatibilityRecords(manifest: Record<string, unknown>, metrics: Record<string, unknown>) {
    for (const row of this.db.query<JsonRow, []>("SELECT json FROM nodes ORDER BY ordinal").iterate()) {
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
    for (const row of this.db.query<JsonRow, []>("SELECT json FROM edges ORDER BY ordinal").iterate()) {
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
}

function* physicalLines(file: string) {
  const fd = fs.openSync(file, "r")
  const decoder = new StringDecoder("utf8")
  const chunk = Buffer.allocUnsafe(64 * 1024)
  let pending = ""
  let lineNumber = 0
  try {
    while (true) {
      const bytes = fs.readSync(fd, chunk, 0, chunk.length, null)
      if (!bytes) break
      const text = decoder.write(chunk.subarray(0, bytes))
      let start = 0
      for (let index = text.indexOf("\n", start); index !== -1; index = text.indexOf("\n", start)) {
        let line = pending + text.slice(start, index)
        pending = ""
        if (line.endsWith("\r")) line = line.slice(0, -1)
        yield { line, lineNumber: ++lineNumber }
        start = index + 1
      }
      pending += text.slice(start)
    }
    pending += decoder.end()
    if (pending) {
      if (pending.endsWith("\r")) pending = pending.slice(0, -1)
      yield { line: pending, lineNumber: ++lineNumber }
    }
  } finally {
    fs.closeSync(fd)
  }
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
  let pending: { line: string; lineNumber: number } | undefined
  let sawLine = false

  const process = (current: { line: string; lineNumber: number }, terminal: boolean) => {
    try {
      index.apply(parseLine(current.line))
    } catch (error) {
      if (terminal && index.recoveredLines > 0) return 1
      if (terminal && index.recoveredLines === 0) throw new Error(`${recordsFile}:1: journal is empty`)
      if (error instanceof JournalJsonError)
        throw new Error(`${recordsFile}:${current.lineNumber}: invalid JSONL entry (${error.message})`)
      const detail = error instanceof Error ? error.message : String(error)
      throw new Error(`${recordsFile}:${current.lineNumber}: ${detail}`)
    }
    return 0
  }

  for (const current of physicalLines(recordsFile)) {
    sawLine = true
    if (pending) process(pending, false)
    pending = current
  }
  if (!sawLine || !pending) throw new Error(`${recordsFile}:1: journal is empty`)
  return process(pending, true)
}

function traceMembers(
  index: ReplayIndex,
  manifest: Record<string, unknown>,
  metrics: Record<string, unknown>,
): StreamingJsonObjectMember[] {
  return [
    ["trace_version", TRACE_VERSION],
    ["causal_ir_version", CAUSAL_IR_VERSION],
    ["manifest", manifest],
    ["nodes", streamingJsonArray(index.rawEntities("nodes"))],
    ["edges", streamingJsonArray(index.rawEntities("edges"))],
    ["artifacts", streamingJsonArray(index.rawEntities("artifacts"))],
    [
      "journal",
      {
        schema_version: CAUSAL_IR_VERSION,
        format: "causal-ir-jsonl",
        path: "records.jsonl",
        summary_scope: "entries_before_lifecycle_entry",
        entry_count: index.recoveredLines,
        last_sequence: index.recoveredLines,
        last_payload_hash: index.finalPayloadHash,
        poisoned: false,
      },
    ],
    ["metrics", metrics],
    ["diagnostics", streamingJsonArray(index.diagnosticItems())],
    ["compatibility", { provenance_projection: "provenance-trace.json" }],
    ["records", streamingJsonArray(index.compatibilityRecords(manifest, metrics))],
    ["dataflow_edges", streamingJsonArray(index.compatibilityEdges(manifest, metrics))],
  ]
}

export function materializeTrace(input: { caseDir: string }): TraceMaterializationResult {
  const caseDir = path.resolve(input.caseDir)
  if (!fs.statSync(caseDir).isDirectory()) throw new Error(`${caseDir}: expected a case directory`)
  const recordsFile = path.join(caseDir, "records.jsonl")
  const indexPath = path.join(
    caseDir,
    `.trace-materializer.${process.pid}.${Math.random().toString(16).slice(2)}.sqlite`,
  )
  const index = new ReplayIndex(indexPath)
  try {
    const droppedLines = recoverJournal(recordsFile, index)
    index.reconcileDiagnostics()
    const close = index.close
    const complete = droppedLines === 0 && close !== undefined
    const completeness = complete ? "complete" : "incomplete"
    const status = complete ? close.status : "error"
    const { runID, caseID } = index.identities
    const manifest: Record<string, unknown> = {
      trace_version: TRACE_VERSION,
      case_id: caseID,
      run_id: runID,
      status,
      server_status: status,
      process_status: status,
      case_status: status,
      ...(close?.manifest.session_id ? { session_id: close.manifest.session_id } : {}),
      ...(close?.result === undefined ? {} : { result: close.result }),
      ...(close?.error === undefined ? {} : { error: close.error }),
      files: {
        trace: "trace.json",
        records: "records.jsonl",
        partial_latest: "partial/latest.json",
      },
      ...(completeness === "complete"
        ? {}
        : {
            recovery_status: "incomplete_journal_replay",
            recovery: { dropped_lines: droppedLines },
          }),
    }
    const metrics = {
      spans: 0,
      events: index.count("nodes"),
      token_usage: {},
      trace_health: { issues: [] },
      records: index.recordCount(),
      dataflow_edges: index.count("edges"),
      artifacts: index.count("artifacts"),
    }
    const traceFile = path.join(caseDir, "trace.json")
    const manifestFile = path.join(caseDir, "manifest.json")
    const partialFile = path.join(caseDir, "partial", "latest.json")
    writeStreamingJsonObjectAtomic(traceFile, traceMembers(index, manifest, metrics))
    writeStreamingJsonObjectAtomic(manifestFile, Object.entries(manifest))
    writeStreamingJsonObjectAtomic(partialFile, traceMembers(index, manifest, metrics))
    return { caseDir, traceFile, manifestFile, partialFile, completeness, recoveredLines: index.recoveredLines }
  } finally {
    index.closeIndex()
    try {
      fs.unlinkSync(indexPath)
    } catch {}
  }
}
