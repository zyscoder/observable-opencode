import fs from "node:fs"
import path from "node:path"
import {
  CausalIRJournalValidationError,
  projectProvenanceTrace,
  replayFinalizedCausalIRTrace,
  replayCausalIRJournal,
  replayCausalIRTrace,
  validateCausalIRJournal,
  type CausalIREdge,
  type CausalIRNode,
  type CausalIRRef,
  type CausalIRStoreSnapshot,
  type CausalIRTraceDocument,
  type ProvenanceProjectionInput,
  type ProvenanceTraceProjection,
} from "opencode/observability/causal-ir"

export type CompatibilityTraceProjection = {
  trace_version: string
  manifest: { case_id: string; run_id?: string }
  records: unknown[]
  dataflow_edges: unknown[]
  artifacts: unknown[]
  metrics: {
    spans: number
    events: number
    records: number
    dataflow_edges: number
    artifacts: number
    token_usage: Record<string, unknown>
    trace_health: { issues: unknown[] }
  }
}

export type RenderableTrace = ProvenanceTraceProjection | CompatibilityTraceProjection

export type RenderableTraceLoadResult = {
  trace: RenderableTrace
  caseDir: string
  source: "trace.json" | "records.jsonl"
  incomplete: boolean
}

type TraceSource = RenderableTraceLoadResult["source"]

type CompatibilityTraceDocument = Record<string, unknown> & {
  trace_schema_version: string
  case_id: string
  run_id?: string
  records: unknown[]
  dataflow_edges: unknown[]
  artifacts?: unknown[]
}

function isRecord(input: unknown): input is Record<string, unknown> {
  return Boolean(input) && typeof input === "object" && !Array.isArray(input)
}

function isNonEmptyString(input: unknown): input is string {
  return typeof input === "string" && input.length > 0
}

function isReference(input: unknown): input is CausalIRRef {
  return (
    isRecord(input) &&
    ["node", "artifact", "raw_event", "external"].includes(input.ref_type as string) &&
    isNonEmptyString(input.ref_id)
  )
}

function isReferenceList(input: unknown) {
  return Array.isArray(input) && input.every(isReference)
}

function isCausalIRNode(input: unknown): input is CausalIRNode {
  if (!isRecord(input) || !isRecord(input.order) || !isRecord(input.scope) || !isRecord(input.payload)) return false
  return (
    isNonEmptyString(input.node_id) &&
    isNonEmptyString(input.kind) &&
    isNonEmptyString(input.schema_version) &&
    ["observed", "deterministic_derived", "offline_derived"].includes(input.origin as string) &&
    isNonEmptyString(input.component) &&
    typeof input.order.sequence === "number" &&
    Number.isFinite(input.order.sequence) &&
    isNonEmptyString(input.order.timestamp) &&
    typeof input.order.time_ms === "number" &&
    Number.isFinite(input.order.time_ms) &&
    isNonEmptyString(input.scope.run_id) &&
    isNonEmptyString(input.scope.case_id) &&
    isReferenceList(input.input_refs) &&
    isReferenceList(input.output_refs) &&
    isReferenceList(input.source_refs) &&
    Array.isArray(input.source_locations) &&
    Array.isArray(input.artifact_refs) &&
    input.artifact_refs.every((item) => typeof item === "string") &&
    Array.isArray(input.aliases) &&
    input.aliases.every((item) => typeof item === "string") &&
    (input.derivation === null || isRecord(input.derivation)) &&
    isRecord(input.integrity) &&
    isNonEmptyString(input.integrity.payload_hash)
  )
}

function isCausalIREdge(input: unknown): input is CausalIREdge {
  return (
    isRecord(input) &&
    isNonEmptyString(input.edge_id) &&
    isReference(input.from) &&
    isReference(input.to) &&
    isNonEmptyString(input.original_relation) &&
    isNonEmptyString(input.normalized_relation) &&
    ["confirmed", "content_matched", "temporal_advisory"].includes(input.evidence_tier as string) &&
    typeof input.eligible_for_attribution === "boolean" &&
    isNonEmptyString(input.derivation_method) &&
    isReferenceList(input.evidence_refs)
  )
}

function isArtifact(input: unknown) {
  return isRecord(input) && isNonEmptyString(input.artifact_id) && isNonEmptyString(input.hash) && isNonEmptyString(input.path)
}

function isDiagnostic(input: unknown) {
  return isRecord(input) && isNonEmptyString(input.diagnostic_id)
}

function isManifest(input: unknown) {
  return isRecord(input) && isNonEmptyString(input.case_id) && isNonEmptyString(input.run_id)
}

function isMetrics(input: unknown) {
  return isRecord(input) && isRecord(input.token_usage) && isRecord(input.trace_health) && Array.isArray(input.trace_health.issues)
}

function isCausalIRTraceDocument(input: unknown): input is CausalIRTraceDocument {
  if (!isRecord(input) || !isManifest(input.manifest) || !isMetrics(input.metrics) || !isRecord(input.journal)) return false
  return (
    isNonEmptyString(input.trace_version) &&
    input.causal_ir_version === "1.0" &&
    Array.isArray(input.nodes) &&
    input.nodes.every(isCausalIRNode) &&
    Array.isArray(input.edges) &&
    input.edges.every(isCausalIREdge) &&
    Array.isArray(input.artifacts) &&
    input.artifacts.every(isArtifact) &&
    Array.isArray(input.diagnostics) &&
    input.diagnostics.every(isDiagnostic) &&
    Array.isArray(input.records) &&
    Array.isArray(input.dataflow_edges) &&
    isRecord(input.compatibility) &&
    input.journal.schema_version === "1.0" &&
    input.journal.format === "causal-ir-jsonl" &&
    input.journal.path === "records.jsonl" &&
    input.journal.summary_scope === "entries_before_lifecycle_entry" &&
    typeof input.journal.entry_count === "number" &&
    typeof input.journal.last_sequence === "number" &&
    typeof input.journal.poisoned === "boolean"
  )
}

function selectSource(input: string): { file: string; caseDir: string; source: TraceSource } {
  const target = path.resolve(input)
  const stats = fs.statSync(target)
  if (stats.isDirectory()) {
    const trace = path.join(target, "trace.json")
    if (fs.existsSync(trace)) return { file: trace, caseDir: target, source: "trace.json" }
    const journal = path.join(target, "records.jsonl")
    if (fs.existsSync(journal)) return { file: journal, caseDir: target, source: "records.jsonl" }
    throw new Error(`${target}: expected trace.json or records.jsonl`)
  }
  if (!stats.isFile()) throw new Error(`${target}: expected a case directory, trace.json, or records.jsonl`)

  const source = path.basename(target) as TraceSource
  if (source !== "trace.json" && source !== "records.jsonl")
    throw new Error(`${target}: expected a case directory, trace.json, or records.jsonl`)
  return { file: target, caseDir: path.dirname(target), source }
}

function readJSON(file: string) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8")) as unknown
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`${file}: invalid JSON (${detail})`)
  }
}

function project(snapshot: CausalIRStoreSnapshot, traceVersion: string, manifest: Record<string, unknown>, metrics: Record<string, unknown>) {
  return projectProvenanceTrace(snapshot, {
    traceVersion,
    manifest: manifest as ProvenanceProjectionInput["manifest"],
    metrics: metrics as ProvenanceProjectionInput["metrics"],
  })
}

function projectDocument(document: CausalIRTraceDocument) {
  return project(
    {
      version: document.causal_ir_version,
      runID: document.manifest.run_id as string,
      caseID: document.manifest.case_id as string,
      nodes: document.nodes,
      edges: document.edges,
      artifacts: document.artifacts,
      diagnostics: document.diagnostics,
    },
    document.trace_version,
    document.manifest,
    document.metrics,
  )
}

function isCompatibilityTraceDocument(input: unknown): input is CompatibilityTraceDocument {
  return (
    isRecord(input) &&
    isNonEmptyString(input.trace_schema_version) &&
    isNonEmptyString(input.case_id) &&
    Array.isArray(input.records) &&
    Array.isArray(input.dataflow_edges) &&
    (input.artifacts === undefined || Array.isArray(input.artifacts))
  )
}

function projectCompatibilityDocument(document: CompatibilityTraceDocument): CompatibilityTraceProjection {
  const artifacts = Array.isArray(document.artifacts) ? document.artifacts : []
  const manifest: CompatibilityTraceProjection["manifest"] = { case_id: document.case_id }
  if (isNonEmptyString(document.run_id)) manifest.run_id = document.run_id
  return {
    trace_version: document.trace_schema_version as string,
    manifest,
    records: document.records,
    dataflow_edges: document.dataflow_edges,
    artifacts,
    metrics: {
      spans: 0,
      events: document.records.length,
      records: document.records.length,
      dataflow_edges: document.dataflow_edges.length,
      artifacts: artifacts.length,
      token_usage: {},
      trace_health: { issues: [] },
    },
  }
}

function readJournal(file: string) {
  const content = fs.readFileSync(file, "utf8")
  const lines = content.split(/\r?\n/)
  if (lines.at(-1) === "") lines.pop()
  if (!lines.length) throw new Error(`${file}:1: journal is empty`)

  return lines.map((line, index) => {
    const lineNumber = index + 1
    if (!line.trim()) throw new Error(`${file}:${lineNumber}: expected a JSON object`)
    try {
      const entry = JSON.parse(line) as unknown
      if (!isRecord(entry)) throw new Error("expected a JSON object")
      return entry
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      throw new Error(`${file}:${lineNumber}: invalid JSONL entry (${detail})`)
    }
  })
}

function recoveryManifest(
  snapshot: CausalIRStoreSnapshot,
  trace: CausalIRTraceDocument | undefined,
): Record<string, unknown> {
  const manifest = trace?.manifest && isManifest(trace.manifest) ? trace.manifest : {}
  return {
    ...manifest,
    trace_version: typeof manifest.trace_version === "string" ? manifest.trace_version : "6.0",
    case_id: typeof manifest.case_id === "string" && manifest.case_id ? manifest.case_id : snapshot.caseID || "recovered-case",
    run_id: typeof manifest.run_id === "string" && manifest.run_id ? manifest.run_id : snapshot.runID || "recovered-run",
    status: "error",
    server_status: "error",
    process_status: "error",
    case_status: "error",
    recovery_status: "incomplete_journal_replay",
  }
}

function recoveryMetrics(trace: CausalIRTraceDocument | undefined): Record<string, unknown> {
  const metrics = trace?.metrics && isMetrics(trace.metrics) ? trace.metrics : {}
  return {
    ...metrics,
    token_usage: isRecord(metrics.token_usage) ? metrics.token_usage : {},
    trace_health: isRecord(metrics.trace_health) && Array.isArray(metrics.trace_health.issues)
      ? metrics.trace_health
      : { issues: [] },
  }
}

export function loadRenderableTrace(input: string): RenderableTraceLoadResult {
  const selected = selectSource(input)
  if (selected.source === "trace.json") {
    const document = readJSON(selected.file)
    if (isCompatibilityTraceDocument(document)) {
      return {
        trace: projectCompatibilityDocument(document),
        caseDir: selected.caseDir,
        source: selected.source,
        incomplete: false,
      }
    }
    if (!isCausalIRTraceDocument(document)) throw new Error(`${selected.file}: invalid Causal IR trace document`)
    return {
      trace: projectDocument(document),
      caseDir: selected.caseDir,
      source: selected.source,
      incomplete: document.manifest.recovery_status === "incomplete_journal_replay",
    }
  }

  const journal = readJournal(selected.file)
  try {
    validateCausalIRJournal(journal, { requireInitialRunNode: true })
  } catch (error) {
    if (error instanceof CausalIRJournalValidationError) {
      throw new Error(`${selected.file}:${error.line}: ${error.message}`)
    }
    throw error
  }
  const replayedTrace = replayCausalIRTrace(journal)
  const finalizedTrace = replayFinalizedCausalIRTrace(journal)
  if (finalizedTrace) {
    return {
      trace: projectDocument(finalizedTrace),
      caseDir: selected.caseDir,
      source: selected.source,
      incomplete: false,
    }
  }

  const snapshot = replayCausalIRJournal(journal)
  return {
    trace: project(
      snapshot,
      replayedTrace?.trace_version ?? "6.0",
      recoveryManifest(snapshot, replayedTrace),
      recoveryMetrics(replayedTrace),
    ),
    caseDir: selected.caseDir,
    source: selected.source,
    incomplete: true,
  }
}
