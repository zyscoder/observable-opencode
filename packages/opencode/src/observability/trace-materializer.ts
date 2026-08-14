import fs from "node:fs"
import path from "node:path"
import {
  CAUSAL_IR_VERSION,
  CausalIRJournalValidationError,
  projectProvenanceTrace,
  replayCausalIRJournal,
  validateCausalIRJournal,
  writeJsonDocumentAtomic,
  type CausalIRJournalEntry,
  type CausalIRRuntimeCloseData,
  type CausalIRTraceDocument,
} from "./causal-ir"
import { TRACE_VERSION } from "./trace-semantic-contract"

export type TraceMaterializationResult = {
  caseDir: string
  traceFile: string
  manifestFile: string
  partialFile: string
  completeness: "complete" | "incomplete"
  recoveredLines: number
}

function journalEntries(recordsFile: string) {
  const lines = fs.readFileSync(recordsFile, "utf8").split(/\r?\n/)
  if (lines.at(-1) === "") lines.pop()
  if (!lines.length) throw new Error(`${recordsFile}:1: journal is empty`)

  const entries: unknown[] = []
  for (const [index, line] of lines.entries()) {
    const lineNumber = index + 1
    try {
      if (!line.trim()) throw new Error("expected a JSON object")
      const entry = JSON.parse(line) as unknown
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw new Error("expected a JSON object")
      entries.push(entry)
    } catch (error) {
      if (lineNumber !== lines.length) {
        const detail = error instanceof Error ? error.message : String(error)
        throw new Error(`${recordsFile}:${lineNumber}: invalid JSONL entry (${detail})`)
      }
      return { entries, droppedLines: 1 }
    }
  }
  return { entries, droppedLines: 0 }
}

function recoverJournal(recordsFile: string) {
  const parsed = journalEntries(recordsFile)
  try {
    validateCausalIRJournal(parsed.entries, { requireInitialRunNode: true })
    return parsed
  } catch (error) {
    if (!(error instanceof CausalIRJournalValidationError) || error.line !== parsed.entries.length)
      throw new Error(`${recordsFile}:${error instanceof CausalIRJournalValidationError ? error.line : 1}: ${error instanceof Error ? error.message : String(error)}`)
    const entries = parsed.entries.slice(0, -1)
    try {
      validateCausalIRJournal(entries, { requireInitialRunNode: true })
    } catch (prefixError) {
      const line = prefixError instanceof CausalIRJournalValidationError ? prefixError.line : 1
      throw new Error(`${recordsFile}:${line}: ${prefixError instanceof Error ? prefixError.message : String(prefixError)}`)
    }
    return { entries, droppedLines: parsed.droppedLines + 1 }
  }
}

function runtimeClose(entries: unknown[]) {
  const terminal = entries.at(-1) as Partial<CausalIRJournalEntry> | undefined
  if (terminal?.operation !== "case.runtime_closed") return undefined
  return terminal.data as CausalIRRuntimeCloseData
}

export function materializeTrace(input: { caseDir: string }): TraceMaterializationResult {
  const caseDir = path.resolve(input.caseDir)
  if (!fs.statSync(caseDir).isDirectory()) throw new Error(`${caseDir}: expected a case directory`)
  const recordsFile = path.join(caseDir, "records.jsonl")
  const recovered = recoverJournal(recordsFile)
  const snapshot = replayCausalIRJournal(recovered.entries)
  const close = runtimeClose(recovered.entries)
  const complete = recovered.droppedLines === 0 && close !== undefined
  const completeness = complete ? "complete" : "incomplete"
  const status = complete ? close.status : "error"
  const manifest = {
    trace_version: TRACE_VERSION,
    case_id: snapshot.caseID,
    run_id: snapshot.runID,
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
          recovery: { dropped_lines: recovered.droppedLines },
        }),
  }
  const projection = projectProvenanceTrace(snapshot, {
    traceVersion: TRACE_VERSION,
    manifest,
    metrics: {
      spans: 0,
      events: snapshot.nodes.length,
      token_usage: {},
      trace_health: { issues: [] },
    },
  })
  const trace: CausalIRTraceDocument = {
    trace_version: TRACE_VERSION,
    causal_ir_version: CAUSAL_IR_VERSION,
    manifest,
    nodes: snapshot.nodes,
    edges: snapshot.edges,
    artifacts: snapshot.artifacts,
    journal: {
      schema_version: CAUSAL_IR_VERSION,
      format: "causal-ir-jsonl",
      path: "records.jsonl",
      summary_scope: "entries_before_lifecycle_entry",
      entry_count: recovered.entries.length,
      last_sequence: recovered.entries.length,
      last_payload_hash: (recovered.entries.at(-1) as Partial<CausalIRJournalEntry> | undefined)?.payload_hash,
      poisoned: false,
    },
    metrics: projection.metrics,
    diagnostics: snapshot.diagnostics,
    compatibility: { provenance_projection: "provenance-trace.json" },
    records: projection.records,
    dataflow_edges: projection.dataflow_edges,
  }
  const traceFile = path.join(caseDir, "trace.json")
  const manifestFile = path.join(caseDir, "manifest.json")
  const partialFile = path.join(caseDir, "partial", "latest.json")
  fs.mkdirSync(path.dirname(partialFile), { recursive: true })
  writeJsonDocumentAtomic(traceFile, trace)
  writeJsonDocumentAtomic(manifestFile, manifest)
  writeJsonDocumentAtomic(partialFile, trace)
  return { caseDir, traceFile, manifestFile, partialFile, completeness, recoveredLines: recovered.entries.length }
}
