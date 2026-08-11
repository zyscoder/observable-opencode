import { describe, expect, test } from "bun:test"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { CausalIRStore, projectProvenanceTrace } from "opencode/observability/causal-ir"
import { loadRenderableTrace } from "../src/load"

function withCaseDirectory(run: (caseDir: string) => void) {
  const caseDir = fs.mkdtempSync(path.join(os.tmpdir(), "trace-renderer-load-"))
  try {
    run(caseDir)
  } finally {
    fs.rmSync(caseDir, { recursive: true, force: true })
  }
}

function createJournal() {
  const journal: unknown[] = []
  const store = new CausalIRStore({
    runID: "run_load_test",
    caseID: "case_load_test",
    append: (entry) => journal.push(entry),
  })
  store.createNode({
    node_id: "response_1",
    kind: "response.output",
    component: "result",
    timestamp: "2026-08-11T00:00:00.000Z",
    time_ms: 10,
    status: "success",
    data: { text: "completed response" },
  })
  store.createEdge({
    edge_id: "input_to_response",
    from: { type: "external", id: "input_1" },
    to: { type: "node", id: "response_1" },
    relation: "produced",
    eligible_for_attribution: true,
  })
  return { journal, snapshot: store.snapshot(), store }
}

function manifest() {
  return {
    trace_version: "6.0",
    case_id: "case_load_test",
    run_id: "run_load_test",
    started_at: "2026-08-11T00:00:00.000Z",
    duration_ms: 10,
    status: "success",
    server_status: "success",
    process_status: "success",
    case_status: "success",
    collection_mode: "passive_sidecar",
    behavior_impact: "none",
    environment: {},
    token_usage: {},
    files: { trace: "trace.json", records: "records.jsonl" },
  }
}

function metrics() {
  return {
    spans: 0,
    events: 1,
    token_usage: {},
    trace_health: { issues: [] },
  }
}

function canonicalTrace(snapshot: ReturnType<CausalIRStore["snapshot"]>) {
  const projection = projectProvenanceTrace(snapshot, {
    traceVersion: "6.0",
    manifest: manifest(),
    metrics: metrics(),
  })
  return {
    trace_version: "6.0",
    causal_ir_version: "1.0",
    manifest: manifest(),
    nodes: snapshot.nodes,
    edges: snapshot.edges,
    artifacts: snapshot.artifacts,
    diagnostics: snapshot.diagnostics,
    journal: {
      schema_version: "1.0",
      format: "causal-ir-jsonl",
      path: "records.jsonl",
      summary_scope: "entries_before_lifecycle_entry",
      entry_count: 2,
      last_sequence: 2,
      poisoned: false,
    },
    metrics: { ...metrics(), ...projection.metrics },
    compatibility: { provenance_projection: "provenance-trace.json" },
    records: projection.records,
    dataflow_edges: projection.dataflow_edges,
  }
}

function createFinalizedJournal() {
  const { journal, snapshot, store } = createJournal()
  store.finalize(canonicalTrace(snapshot))
  return journal
}

describe("loadRenderableTrace", () => {
  test("loads a finalized trace.json through the compatibility projection", () => {
    withCaseDirectory((caseDir) => {
      const { snapshot } = createJournal()
      const projection = projectProvenanceTrace(snapshot, {
        traceVersion: "6.0",
        manifest: manifest(),
        metrics: metrics(),
      })
      const traceFile = path.join(caseDir, "trace.json")
      fs.writeFileSync(
        traceFile,
        JSON.stringify({
          trace_version: "6.0",
          causal_ir_version: "1.0",
          manifest: manifest(),
          nodes: snapshot.nodes,
          edges: snapshot.edges,
          artifacts: snapshot.artifacts,
          diagnostics: snapshot.diagnostics,
          journal: {
            schema_version: "1.0",
            format: "causal-ir-jsonl",
            path: "records.jsonl",
            summary_scope: "entries_before_lifecycle_entry",
            entry_count: 2,
            last_sequence: 2,
            poisoned: false,
          },
          metrics: { ...metrics(), ...projection.metrics },
          compatibility: { provenance_projection: "provenance-trace.json" },
          records: [],
          dataflow_edges: [],
        }),
      )

      const result = loadRenderableTrace(caseDir)
      const directResult = loadRenderableTrace(traceFile)

      expect(result).toMatchObject({ caseDir, source: "trace.json", incomplete: false })
      expect(result.trace.records).toMatchObject([{ record_id: "response_1", event_type: "response.output" }])
      expect(result.trace.dataflow_edges).toMatchObject([
        { edge_id: "input_to_response", relation: "produced", eligible_for_attribution: true },
      ])
      expect(directResult).toMatchObject({ caseDir, source: "trace.json", incomplete: false })
    })
  })

  test("rejects a trace.json that does not contain a strict Causal IR document", () => {
    withCaseDirectory((caseDir) => {
      const traceFile = path.join(caseDir, "trace.json")
      fs.writeFileSync(traceFile, JSON.stringify({ trace_version: "6.0", manifest: manifest() }))

      expect(() => loadRenderableTrace(traceFile)).toThrow("trace.json: invalid Causal IR trace document")
    })
  })

  test("recovers an incomplete journal through Causal IR replay without inferring success", () => {
    withCaseDirectory((caseDir) => {
      const { journal } = createJournal()
      fs.writeFileSync(path.join(caseDir, "records.jsonl"), journal.map((entry) => JSON.stringify(entry)).join("\n"))

      const result = loadRenderableTrace(path.join(caseDir, "records.jsonl"))

      expect(result).toMatchObject({ caseDir, source: "records.jsonl", incomplete: true })
      expect(result.trace.manifest).toMatchObject({
        recovery_status: "incomplete_journal_replay",
        status: "error",
        server_status: "error",
        process_status: "error",
        case_status: "error",
      })
      expect(result.trace.records).toMatchObject([{ record_id: "response_1", component: "result" }])
      expect(result.trace.dataflow_edges).toMatchObject([
        {
          edge_id: "input_to_response",
          relation: "produced",
          metadata: {
            original_relation: "produced",
            normalized_relation: "produced",
            eligible_for_attribution: true,
          },
        },
      ])
    })
  })

  test("loads a terminal valid compact finalization", () => {
    withCaseDirectory((caseDir) => {
      const journalFile = path.join(caseDir, "records.jsonl")
      fs.writeFileSync(journalFile, createFinalizedJournal().map((entry) => JSON.stringify(entry)).join("\n"))

      const result = loadRenderableTrace(journalFile)

      expect(result).toMatchObject({ caseDir, source: "records.jsonl", incomplete: false })
      expect(result.trace.manifest).toMatchObject({ status: "success", case_status: "success" })
      expect(result.trace.records).toMatchObject([{ record_id: "response_1" }])
    })
  })

  test("recovers a success checkpoint followed by a corrupt finalization as incomplete", () => {
    withCaseDirectory((caseDir) => {
      const { journal, snapshot, store } = createJournal()
      store.checkpoint(canonicalTrace(snapshot))
      journal.push({ operation: "case.finalized", data: { unexpected: true } })
      const journalFile = path.join(caseDir, "records.jsonl")
      fs.writeFileSync(journalFile, journal.map((entry) => JSON.stringify(entry)).join("\n"))

      const result = loadRenderableTrace(journalFile)

      expect(result).toMatchObject({ incomplete: true })
      expect(result.trace.manifest).toMatchObject({
        recovery_status: "incomplete_journal_replay",
        status: "error",
        case_status: "error",
      })
    })
  })

  test("treats a valid finalization followed by another operation as incomplete", () => {
    withCaseDirectory((caseDir) => {
      const journal = createFinalizedJournal()
      journal.push({ operation: "diagnostic.created", data: { diagnostic_id: "after_finalization" } })
      const journalFile = path.join(caseDir, "records.jsonl")
      fs.writeFileSync(journalFile, journal.map((entry) => JSON.stringify(entry)).join("\n"))

      const result = loadRenderableTrace(journalFile)

      expect(result).toMatchObject({ incomplete: true })
      expect(result.trace.manifest).toMatchObject({
        recovery_status: "incomplete_journal_replay",
        status: "error",
        case_status: "error",
      })
    })
  })

  test("loads the repository's finalized projection-only compatibility trace unchanged", () => {
    const traceFile = path.resolve(
      import.meta.dir,
      "../../../.benchmark-runs/attribution-convergence-vnext-20260803/sphinx-flash-probe/input/trace.json",
    )
    const fixture = JSON.parse(fs.readFileSync(traceFile, "utf8")) as { records: unknown[]; dataflow_edges: unknown[] }

    const result = loadRenderableTrace(traceFile)

    expect(result).toMatchObject({ source: "trace.json", incomplete: false })
    expect(result.trace.records).toEqual(fixture.records)
    expect(result.trace.dataflow_edges).toEqual(fixture.dataflow_edges)
    expect(result.trace.manifest).toEqual({ case_id: "sphinx-recursive-minimal" })
    expect(result.trace.metrics).toMatchObject({ records: 6, dataflow_edges: 0 })
  })

  test("reports the one-based line for malformed journal JSON", () => {
    withCaseDirectory((caseDir) => {
      const journalFile = path.join(caseDir, "records.jsonl")
      fs.writeFileSync(journalFile, `${JSON.stringify({ operation: "node.created" })}\nnot-json`)

      expect(() => loadRenderableTrace(journalFile)).toThrow("records.jsonl:2")
    })
  })

  test("reports the first line for an empty journal", () => {
    withCaseDirectory((caseDir) => {
      const journalFile = path.join(caseDir, "records.jsonl")
      fs.writeFileSync(journalFile, "")

      expect(() => loadRenderableTrace(journalFile)).toThrow("records.jsonl:1")
    })
  })
})
