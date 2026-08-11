import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import {
  CausalIRStore,
  projectProvenanceTrace,
  replayFinalizedCausalIRTrace,
  validateCausalIRJournal,
} from "opencode/observability/causal-ir"
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
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-11T00:00:00.000Z",
    time_ms: 0,
    status: "running",
    data: { case_id: "case_load_test", run_id: "run_load_test" },
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
  store.createArtifact({
    artifact_id: "artifact_1",
    hash: "artifact-hash",
    path: "artifacts/input.txt",
    metadata: { role: "input" },
  })
  store.createDiagnostic({
    diagnostic_id: "diagnostic_1",
    kind: "integrity_notice",
    message: "retained diagnostic",
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

function canonicalTrace(
  snapshot: ReturnType<CausalIRStore["snapshot"]>,
  journal: ReturnType<CausalIRStore["journalSummary"]>,
) {
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
    journal,
    metrics: { ...metrics(), ...projection.metrics },
    compatibility: { provenance_projection: "provenance-trace.json" },
    records: projection.records,
    dataflow_edges: projection.dataflow_edges,
  }
}

function createFinalizedJournal() {
  const { journal, snapshot, store } = createJournal()
  store.finalize(canonicalTrace(snapshot, store.journalSummary()))
  return journal
}

function createLegacyLifecycleFinalizedJournal() {
  const { journal, store } = createJournal()
  store.checkpoint({ phase: "legacy-before-finalization" })
  const snapshot = store.snapshot()
  const trace = canonicalTrace(snapshot, store.journalSummary())
  const terminal = {
    sequence: journal.length + 1,
    time: "2026-08-11T00:00:01.000Z",
    run_id: "run_load_test",
    case_id: "case_load_test",
    operation: "case.finalized",
    record_type: "finish",
    entity_id: "case_load_test",
    previous_payload_hash: (journal.at(-1) as any).payload_hash,
    data: { snapshot, data: trace.manifest, trace },
  }
  rehashEntry(terminal)
  journal.push(terminal)
  return journal
}

function createJournalWithEntityChains() {
  const { journal, store } = createJournal()
  const response = store.nodes.find((node) => node.node_id === "response_1")!
  response.title = "updated response"
  store.updateNode(response)
  store.createEdge({
    edge_id: "input_to_response",
    from: { type: "external", id: "input_2" },
    to: { type: "node", id: "response_1" },
    relation: "produced",
    eligible_for_attribution: true,
  })
  store.reuseArtifact({
    artifact_id: "artifact_1",
    hash: "artifact-hash-reused",
    path: "artifacts/input-reused.txt",
    metadata: { role: "input" },
  })
  store.createDiagnostic({
    diagnostic_id: "diagnostic_1",
    kind: "integrity_notice",
    message: "updated diagnostic",
  })
  store.checkpoint({ phase: "first" })
  store.checkpoint({ phase: "second" })
  return journal as any[]
}

function canonicalJSON(input: unknown): string {
  if (input === null || typeof input !== "object") return JSON.stringify(input) ?? "null"
  if (Array.isArray(input)) return `[${input.map(canonicalJSON).join(",")}]`
  const value = input as Record<string, unknown>
  return `{${Object.keys(value)
    .sort()
    .filter((key) => value[key] !== undefined)
    .map((key) => `${JSON.stringify(key)}:${canonicalJSON(value[key])}`)
    .join(",")}}`
}

function rehashEntry(entry: any) {
  entry.payload_hash = createHash("sha256").update(canonicalJSON(entry.data)).digest("hex")
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
            entry_count: 5,
            last_sequence: 5,
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
      expect(result.trace.records).toEqual(
        expect.arrayContaining([expect.objectContaining({ record_id: "response_1", event_type: "response.output" })]),
      )
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
      expect(result.trace.records).toEqual(
        expect.arrayContaining([expect.objectContaining({ record_id: "response_1", component: "result" })]),
      )
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
      fs.writeFileSync(
        journalFile,
        createFinalizedJournal()
          .map((entry) => JSON.stringify(entry))
          .join("\n"),
      )

      const result = loadRenderableTrace(journalFile)

      expect(result).toMatchObject({ caseDir, source: "records.jsonl", incomplete: false })
      expect(result.trace.manifest).toMatchObject({ status: "success", case_status: "success" })
      expect(result.trace.records).toEqual(
        expect.arrayContaining([expect.objectContaining({ record_id: "response_1" })]),
      )
    })
  })

  test("loads a terminal valid legacy lifecycle finalization", () => {
    withCaseDirectory((caseDir) => {
      const journalFile = path.join(caseDir, "records.jsonl")
      fs.writeFileSync(
        journalFile,
        createLegacyLifecycleFinalizedJournal()
          .map((entry) => JSON.stringify(entry))
          .join("\n"),
      )

      const result = loadRenderableTrace(journalFile)

      expect(result).toMatchObject({ caseDir, source: "records.jsonl", incomplete: false })
      expect(result.trace.manifest).toMatchObject({
        run_id: "run_load_test",
        case_id: "case_load_test",
        status: "success",
      })
      expect(result.trace.records).toEqual(
        expect.arrayContaining([expect.objectContaining({ record_id: "response_1" })]),
      )
    })
  })

  test("rejects invalid legacy lifecycle structure, identity, and terminal payload hash at its line", () => {
    const cases: Array<[string, (terminal: any) => void]> = [
      [
        "snapshot identity",
        (terminal) => {
          terminal.data.snapshot.runID = "run_tampered"
          rehashEntry(terminal)
        },
      ],
      [
        "trace identity",
        (terminal) => {
          terminal.data.trace.manifest.case_id = "case_tampered"
          rehashEntry(terminal)
        },
      ],
      [
        "snapshot and trace graph mismatch",
        (terminal) => {
          terminal.data.trace.nodes = []
          rehashEntry(terminal)
        },
      ],
      [
        "lifecycle canonical node payload hash",
        (terminal) => {
          terminal.data.snapshot.nodes[0].integrity.payload_hash = "0".repeat(64)
          terminal.data.trace.nodes[0].integrity.payload_hash = "0".repeat(64)
          rehashEntry(terminal)
        },
      ],
      [
        "terminal payload hash",
        (terminal) => {
          terminal.data.data.status = "error"
        },
      ],
    ]

    for (const [label, tamper] of cases) {
      withCaseDirectory((caseDir) => {
        const journal = createLegacyLifecycleFinalizedJournal() as any[]
        tamper(journal.at(-1))
        const terminalLine = journal.length
        const journalFile = path.join(caseDir, "records.jsonl")
        fs.writeFileSync(journalFile, journal.map((entry) => JSON.stringify(entry)).join("\n"))

        expect(() => loadRenderableTrace(journalFile), label).toThrow(`records.jsonl:${terminalLine}`)
      })
    }
  })

  test("binds a legacy lifecycle journal summary to the actual terminal prefix", () => {
    const cases: Array<[string, (summary: any) => void]> = [
      [
        "entry count and sequence",
        (summary) => {
          summary.entry_count = 999
          summary.last_sequence = 999
        },
      ],
      [
        "preceding payload hash",
        (summary) => {
          summary.last_payload_hash = "0".repeat(64)
        },
      ],
      [
        "poisoned journal",
        (summary) => {
          summary.poisoned = true
        },
      ],
    ]

    for (const [label, tamper] of cases) {
      withCaseDirectory((caseDir) => {
        const journal = createLegacyLifecycleFinalizedJournal() as any[]
        const terminal = journal.at(-1)
        tamper(terminal.data.trace.journal)
        rehashEntry(terminal)
        const journalFile = path.join(caseDir, "records.jsonl")
        fs.writeFileSync(journalFile, journal.map((entry) => JSON.stringify(entry)).join("\n"))

        expect(() => loadRenderableTrace(journalFile), label).toThrow(`records.jsonl:${journal.length}`)
      })
    }
  })

  test("requires an empty legacy lifecycle prefix to omit its last payload hash", () => {
    const terminal = structuredClone((createLegacyLifecycleFinalizedJournal() as any[]).at(-1))
    terminal.sequence = 1
    terminal.previous_payload_hash = undefined
    terminal.data.trace.journal.entry_count = 0
    terminal.data.trace.journal.last_sequence = 0
    terminal.data.trace.journal.last_payload_hash = undefined
    rehashEntry(terminal)

    expect(() => validateCausalIRJournal([terminal])).not.toThrow()

    terminal.data.trace.journal.last_payload_hash = "0".repeat(64)
    rehashEntry(terminal)
    expect(() => validateCausalIRJournal([terminal])).toThrow("malformed lifecycle finalization entry")
  })

  test("rejects a compact finalization whose integrity hash does not match the replayed graph", () => {
    const journal = createFinalizedJournal()
    const finalization = journal.at(-1) as { data: { graph: { integrity_hash: string } } }
    finalization.data.graph.integrity_hash = "0".repeat(64)
    rehashEntry(finalization)

    expect(replayFinalizedCausalIRTrace(journal)).toBeUndefined()

    withCaseDirectory((caseDir) => {
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

  test("rejects compact finalization tampering across every replay-relevant category", () => {
    const cases: Array<[string, (journal: any[]) => void]> = [
      [
        "node",
        (journal) => {
          const entry = journal.find((item) => item.operation === "node.created" && item.data.node_id === "response_1")
          entry.data.component = "tampered-component"
          rehashEntry(entry)
        },
      ],
      [
        "edge",
        (journal) => {
          const entry = journal.find((item) => item.operation === "edge.created")
          entry.data.label = "tampered edge"
          rehashEntry(entry)
        },
      ],
      [
        "artifact",
        (journal) => {
          const entry = journal.find((item) => item.operation === "artifact.created")
          entry.data.path = "artifacts/tampered.txt"
          rehashEntry(entry)
        },
      ],
      [
        "diagnostic",
        (journal) => {
          const entry = journal.find((item) => item.operation === "diagnostic.created")
          entry.data.message = "tampered diagnostic"
          rehashEntry(entry)
        },
      ],
      [
        "canonical manifest",
        (journal) => {
          const entry = journal.at(-1)
          entry.data.canonical.manifest.status = "error"
          rehashEntry(entry)
        },
      ],
      [
        "canonical metrics",
        (journal) => {
          const entry = journal.at(-1)
          entry.data.canonical.metrics.events = 999
          rehashEntry(entry)
        },
      ],
    ]

    for (const [category, tamper] of cases) {
      const journal = createFinalizedJournal() as any[]
      tamper(journal)
      expect(replayFinalizedCausalIRTrace(journal), category).toBeUndefined()
    }
  })

  test("validates terminal payload hash and sequence, run, case, and finalization binding", () => {
    const cases: Array<[string, (terminal: any) => void]> = [
      [
        "payload hash",
        (terminal) => {
          terminal.payload_hash = "0".repeat(64)
        },
      ],
      [
        "sequence",
        (terminal) => {
          terminal.sequence += 1
        },
      ],
      [
        "run",
        (terminal) => {
          terminal.run_id = "run_tampered"
        },
      ],
      [
        "case",
        (terminal) => {
          terminal.case_id = "case_tampered"
        },
      ],
      [
        "entity",
        (terminal) => {
          terminal.entity_id = "case_tampered"
        },
      ],
      [
        "record type",
        (terminal) => {
          terminal.record_type = "checkpoint"
        },
      ],
      [
        "journal summary",
        (terminal) => {
          terminal.data.canonical.journal.last_sequence -= 1
          rehashEntry(terminal)
        },
      ],
    ]

    for (const [binding, tamper] of cases) {
      const journal = createFinalizedJournal() as any[]
      tamper(journal.at(-1))
      expect(replayFinalizedCausalIRTrace(journal), binding).toBeUndefined()
    }
  })

  test("recovers a success checkpoint followed by a corrupt finalization as incomplete", () => {
    withCaseDirectory((caseDir) => {
      const { journal, snapshot, store } = createJournal()
      store.checkpoint(canonicalTrace(snapshot, store.journalSummary()))
      store.finalize(canonicalTrace(snapshot, store.journalSummary()))
      const finalization = journal.at(-1) as any
      finalization.data.graph.integrity_hash = "0".repeat(64)
      rehashEntry(finalization)
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
      const journal = createFinalizedJournal() as any[]
      const terminal = journal.at(-1)
      const afterFinalization = {
        sequence: terminal.sequence + 1,
        time: "2026-08-11T00:00:01.000Z",
        run_id: terminal.run_id,
        case_id: terminal.case_id,
        operation: "diagnostic.created",
        record_type: "diagnostic",
        entity_id: "after_finalization",
        data: { diagnostic_id: "after_finalization", message: "late diagnostic" },
      }
      rehashEntry(afterFinalization)
      journal.push(afterFinalization)
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
    const traceFile = path.resolve(import.meta.dir, "fixtures/sphinx-projection-only/trace.json")
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
      fs.writeFileSync(journalFile, `${JSON.stringify(createJournal().journal[0])}\nnot-json`)

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

  test("rejects nonterminal entry and canonical node payload hash tampering at the affected line", () => {
    const cases: Array<[string, (journal: any[]) => void, number]> = [
      [
        "entry payload hash",
        (journal) => {
          journal[1].data.component = "tampered-component"
        },
        2,
      ],
      [
        "canonical node payload hash",
        (journal) => {
          journal[1].data.payload.text = "tampered response"
          rehashEntry(journal[1])
        },
        2,
      ],
    ]

    for (const [label, tamper, line] of cases) {
      withCaseDirectory((caseDir) => {
        const journal = createJournal().journal as any[]
        tamper(journal)
        const journalFile = path.join(caseDir, "records.jsonl")
        fs.writeFileSync(journalFile, journal.map((entry) => JSON.stringify(entry)).join("\n"))

        expect(() => loadRenderableTrace(journalFile), label).toThrow(`records.jsonl:${line}`)
      })
    }
  })

  test("rejects broken per-entity previous payload hash chains at the affected line", () => {
    const targets: Array<[string, (entry: any) => boolean]> = [
      ["node", (entry) => entry.operation === "node.updated" && entry.entity_id === "response_1"],
      ["edge", (entry) => entry.operation === "edge.created" && entry.data.from.ref_id === "input_2"],
      ["artifact", (entry) => entry.operation === "artifact.reused"],
      [
        "diagnostic",
        (entry) => entry.operation === "diagnostic.created" && entry.data.message === "updated diagnostic",
      ],
      ["case", (entry) => entry.operation === "case.checkpointed" && entry.data.data.phase === "second"],
    ]

    for (const [label, matches] of targets) {
      withCaseDirectory((caseDir) => {
        const journal = createJournalWithEntityChains()
        const index = journal.findIndex(matches)
        expect(index, label).toBeGreaterThan(0)
        journal[index].previous_payload_hash = "0".repeat(64)
        const journalFile = path.join(caseDir, "records.jsonl")
        fs.writeFileSync(journalFile, journal.map((entry) => JSON.stringify(entry)).join("\n"))

        expect(() => loadRenderableTrace(journalFile), label).toThrow(`records.jsonl:${index + 1}`)
      })
    }
  })

  test("rejects syntactically valid but structurally invalid journals with a line number", () => {
    const cases: Array<[string, unknown[]]> = [
      ["empty object", [{}]],
      ["unknown operation", [{ ...(createJournal().journal[0] as any), operation: "node.deleted" }]],
      ["malformed node", [{ ...(createJournal().journal[0] as any), data: { node_id: "run_start" } }]],
      [
        "inconsistent identity",
        (createJournal().journal as any[])
          .slice(0, 2)
          .map((entry, index) => (index === 1 ? { ...entry, run_id: "another-run" } : entry)),
      ],
    ]

    for (const [label, journal] of cases) {
      withCaseDirectory((caseDir) => {
        const journalFile = path.join(caseDir, "records.jsonl")
        fs.writeFileSync(journalFile, journal.map((entry) => JSON.stringify(entry)).join("\n"))

        expect(() => loadRenderableTrace(journalFile), label).toThrow(/records\.jsonl:[12]/)
      })
    }
  })

  test("rejects a structurally valid journal without the initial run node", () => {
    withCaseDirectory((caseDir) => {
      const response = structuredClone((createJournal().journal as any[])[1])
      response.sequence = 1
      response.previous_payload_hash = undefined
      const journalFile = path.join(caseDir, "records.jsonl")
      fs.writeFileSync(journalFile, JSON.stringify(response))

      expect(() => loadRenderableTrace(journalFile)).toThrow("records.jsonl:1")
    })
  })
})
