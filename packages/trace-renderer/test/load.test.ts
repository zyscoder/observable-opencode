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
import {
  acquireTraceSessionLock,
  openTraceSegment,
  traceSessionLockRootForCase,
  TRACE_MANIFEST_LOCK_KEY,
} from "opencode/observability/trace-segment"
import { materializeTrace } from "opencode/observability/trace-materializer"
import { loadRenderableTrace } from "../src/load"

function withCaseDirectory(run: (caseDir: string) => void) {
  const caseDir = fs.mkdtempSync(path.join(os.tmpdir(), "trace-renderer-load-"))
  try {
    run(caseDir)
  } finally {
    fs.rmSync(caseDir, { recursive: true, force: true })
  }
}

function treeHashes(root: string) {
  const result: Array<[string, string]> = []
  const visit = (directory: string) => {
    for (const entry of fs
      .readdirSync(directory, { withFileTypes: true })
      .sort((a, b) => a.name.localeCompare(b.name))) {
      const file = path.join(directory, entry.name)
      if (entry.isDirectory()) visit(file)
      else if (entry.isFile())
        result.push([path.relative(root, file), createHash("sha256").update(fs.readFileSync(file)).digest("hex")])
    }
  }
  visit(root)
  return result
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

function segmentedJournal(runID: string, marker: string, complete = true) {
  const journal: unknown[] = []
  const store = new CausalIRStore({ runID, caseID: "segmented-renderer", append: (entry) => journal.push(entry) })
  store.createNode({
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-15T00:00:00.000Z",
    time_ms: 0,
    status: "running",
    data: { run_id: runID, case_id: "segmented-renderer" },
  })
  store.createNode({
    node_id: "shared_response",
    kind: "response.output",
    component: "result",
    timestamp: "2026-08-15T00:00:01.000Z",
    time_ms: 1,
    status: "success",
    data: { text: `${marker} response` },
  })
  if (complete)
    store.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-15T00:00:02.000Z",
      manifest: { case_id: "segmented-renderer", run_id: runID, session_id: "ses_segmented_renderer" },
    })
  return journal
}

async function waitForFile(file: string, timeoutMilliseconds = 5_000) {
  const deadline = Date.now() + timeoutMilliseconds
  while (Date.now() < deadline) {
    if (fs.existsSync(file)) return true
    await Bun.sleep(10)
  }
  return fs.existsSync(file)
}

describe("loadRenderableTrace", () => {
  test("holds the global trace-root lock while reading a legacy-flat snapshot", () => {
    const previousWait = process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
    withCaseDirectory((caseDir) => {
      const { journal } = createJournal()
      fs.writeFileSync(path.join(caseDir, "records.jsonl"), journal.map((entry) => JSON.stringify(entry)).join("\n"))
      const before = treeHashes(caseDir)
      const release = acquireTraceSessionLock(traceSessionLockRootForCase(caseDir), TRACE_MANIFEST_LOCK_KEY)
      try {
        process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = "0"
        expect(() => loadRenderableTrace(caseDir)).toThrow("timed out waiting for trace session lock")
      } finally {
        release()
        if (previousWait === undefined) delete process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
        else process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = previousWait
      }
      expect(treeHashes(caseDir)).toEqual(before)
    })
  })

  test("releases the allocation lock while reading and retries after the session generation changes", async () => {
    const traceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "trace-renderer-generation-lock-"))
    const readyFile = path.join(traceRoot, "renderer.ready")
    const gateFile = path.join(traceRoot, "renderer.gate")
    const resultFile = path.join(traceRoot, "renderer.result.json")
    const previousWait = process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
    let child: ReturnType<typeof Bun.spawn> | undefined
    try {
      const first = openTraceSegment({
        rootDir: traceRoot,
        logicalCaseID: "segmented-renderer",
        sessionID: "ses_segmented_renderer",
        runID: "run_renderer_first",
      })
      fs.writeFileSync(
        path.join(first.segmentDir, "records.jsonl"),
        segmentedJournal("run_renderer_first", "first").map((entry) => JSON.stringify(entry)).join("\n") + "\n",
      )
      expect(first.finalize("completed")).toBe(true)
      materializeTrace({ caseDir: first.logicalRoot })

      const script = [
        'import fs from "node:fs"',
        `const traceFile = ${JSON.stringify(path.join(first.logicalRoot, "trace.json"))}`,
        "const physicalTraceFile = fs.realpathSync(traceFile)",
        `const readyFile = ${JSON.stringify(readyFile)}`,
        `const gateFile = ${JSON.stringify(gateFile)}`,
        `const resultFile = ${JSON.stringify(resultFile)}`,
        "const originalOpenSync = fs.openSync.bind(fs)",
        "let paused = false",
        "fs.openSync = function(file, ...args) {",
        "  const value = originalOpenSync(file, ...args)",
        "  if (!paused && String(file) === physicalTraceFile) {",
        "    paused = true",
        '    fs.writeFileSync(readyFile, "ready")',
        "    while (!fs.existsSync(gateFile)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5)",
        "  }",
        "  return value",
        "}",
        `const { loadRenderableTrace } = await import(${JSON.stringify(new URL("../src/load.ts", import.meta.url).href)})`,
        `const loaded = loadRenderableTrace(${JSON.stringify(first.logicalRoot)})`,
        "fs.writeFileSync(resultFile, JSON.stringify({",
        "  incomplete: loaded.incomplete,",
        "  runID: loaded.trace.manifest.run_id,",
        "  sessionGeneration: loaded.trace.manifest.session_generation,",
        "}))",
      ].join("\n")
      child = Bun.spawn([process.execPath, "-e", script], {
        cwd: import.meta.dir,
        stdout: "pipe",
        stderr: "pipe",
      })
      expect(await waitForFile(readyFile)).toBe(true)

      process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = "200"
      const second = openTraceSegment({
        rootDir: traceRoot,
        logicalCaseID: "segmented-renderer",
        sessionID: "ses_segmented_renderer",
        runID: "run_renderer_second",
      })
      fs.writeFileSync(
        path.join(second.segmentDir, "records.jsonl"),
        segmentedJournal("run_renderer_second", "second", false)
          .map((entry) => JSON.stringify(entry))
          .join("\n") + "\n",
      )
      fs.writeFileSync(gateFile, "continue")

      expect(await child.exited).toBe(0)
      expect(JSON.parse(fs.readFileSync(resultFile, "utf8"))).toEqual({
        incomplete: true,
        runID: "run_renderer_second",
        sessionGeneration: 3,
      })
    } finally {
      fs.writeFileSync(gateFile, "continue")
      if (child && child.exitCode === null) child.kill()
      if (previousWait === undefined) delete process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
      else process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = previousWait
      fs.rmSync(traceRoot, { recursive: true, force: true })
    }
  })

  test("rejects renderer source symlinks that escape the case directory", () => {
    withCaseDirectory((caseDir) => {
      const outsideTrace = `${caseDir}-outside-trace.json`
      const outsideJournal = `${caseDir}-outside-records.jsonl`
      try {
        fs.writeFileSync(
          outsideTrace,
          JSON.stringify({
            trace_schema_version: "1.3",
            case_id: "outside-case",
            records: [],
            dataflow_edges: [],
          }),
        )
        fs.symlinkSync(outsideTrace, path.join(caseDir, "trace.json"))
        expect(() => loadRenderableTrace(caseDir)).toThrow(/symbolic link|escapes/)

        fs.unlinkSync(path.join(caseDir, "trace.json"))
        fs.writeFileSync(outsideJournal, createJournal().journal.map((entry) => JSON.stringify(entry)).join("\n"))
        fs.symlinkSync(outsideJournal, path.join(caseDir, "records.jsonl"))
        expect(() => loadRenderableTrace(caseDir)).toThrow(/symbolic link|escapes/)
      } finally {
        fs.rmSync(outsideTrace, { force: true })
        fs.rmSync(outsideJournal, { force: true })
      }
    })
  })

  test("discovers session.json and loads all immutable segments as one renderable trace", () => {
    withCaseDirectory((caseDir) => {
      const descriptors = [
        { runID: "run_renderer_first", marker: "first" },
        { runID: "run_renderer_second", marker: "second" },
      ].map((input, index) => {
        const segment = path.join("segments", input.runID)
        fs.mkdirSync(path.join(caseDir, segment), { recursive: true })
        fs.writeFileSync(
          path.join(caseDir, segment, "records.jsonl"),
          segmentedJournal(input.runID, input.marker, index === 0)
            .map((entry) => JSON.stringify(entry))
            .join("\n") + "\n",
        )
        return {
          segment_id: input.runID,
          run_id: input.runID,
          case_id: "segmented-renderer",
          session_id: "ses_segmented_renderer",
          path: segment,
          records: path.join(segment, "records.jsonl"),
          artifacts: path.join(segment, "artifacts"),
          index: path.join(segment, "index.sqlite"),
          started_at: `2026-08-15T00:00:0${index}.000Z`,
          status: index === 0 ? "completed" : "running",
          ...(index === 1 ? { continuation_of: "run_renderer_first" } : {}),
        }
      })
      for (const descriptor of descriptors)
        fs.writeFileSync(path.join(caseDir, descriptor.path, "segment.json"), JSON.stringify(descriptor) + "\n")
      fs.writeFileSync(
        path.join(caseDir, "session.json"),
        JSON.stringify({
          schema_version: "1.0",
          logical_case_id: "segmented-renderer",
          session_id: "ses_segmented_renderer",
          lock_key: "trace-root-manifest-v1",
          generation: 2,
          created_at: "2026-08-15T00:00:00.000Z",
          updated_at: "2026-08-15T00:00:01.000Z",
          segments: descriptors,
        }),
      )
      const before = treeHashes(caseDir)

      const result = loadRenderableTrace(caseDir)

      expect(result).toMatchObject({ caseDir, source: "trace.json", incomplete: true })
      expect(result.trace.manifest).toMatchObject({
        case_id: "segmented-renderer",
        run_id: "run_renderer_second",
        historical_interruptions: true,
      })
      expect(
        result.trace.records
          .filter((item: any) => item.event_type === "response.output")
          .map((item: any) => item.data.text),
      ).toEqual(["first response", "second response"])
      expect(result.trace.dataflow_edges).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            relation: "continued_from",
            metadata: expect.objectContaining({ provenance_type: "run.continuation" }),
          }),
        ]),
      )
      expect(fs.existsSync(path.join(caseDir, "trace.json"))).toBe(false)
      expect(treeHashes(caseDir)).toEqual(before)
    })
  })

  test("ignores a stale root trace and materializes every current segment without mutating the root", () => {
    withCaseDirectory((caseDir) => {
      const descriptors = [
        { runID: "run_renderer_first", marker: "first", complete: true },
        { runID: "run_renderer_second", marker: "second", complete: false },
      ].map((input, index) => {
        const segment = path.join("segments", input.runID)
        fs.mkdirSync(path.join(caseDir, segment), { recursive: true })
        fs.writeFileSync(
          path.join(caseDir, segment, "records.jsonl"),
          segmentedJournal(input.runID, input.marker, input.complete)
            .map((entry) => JSON.stringify(entry))
            .join("\n") + "\n",
        )
        return {
          segment_id: input.runID,
          run_id: input.runID,
          case_id: "segmented-renderer",
          session_id: "ses_segmented_renderer",
          path: segment,
          records: path.join(segment, "records.jsonl"),
          artifacts: path.join(segment, "artifacts"),
          index: path.join(segment, "index.sqlite"),
          started_at: `2026-08-15T00:00:0${index}.000Z`,
          status: input.complete ? "completed" : "running",
          ...(index ? { continuation_of: "run_renderer_first" } : {}),
        }
      })
      for (const descriptor of descriptors)
        fs.writeFileSync(path.join(caseDir, descriptor.path, "segment.json"), JSON.stringify(descriptor) + "\n")
      fs.writeFileSync(
        path.join(caseDir, "session.json"),
        JSON.stringify({
          schema_version: "1.0",
          logical_case_id: "segmented-renderer",
          session_id: "ses_segmented_renderer",
          lock_key: "trace-root-manifest-v1",
          generation: 2,
          created_at: "2026-08-15T00:00:00.000Z",
          updated_at: "2026-08-15T00:00:01.000Z",
          segments: descriptors,
        }),
      )
      const { snapshot, store } = createJournal()
      const stale = canonicalTrace(snapshot, store.journalSummary())
      ;(stale.manifest as Record<string, unknown>).session_generation = 1
      const traceFile = path.join(caseDir, "trace.json")
      fs.writeFileSync(traceFile, JSON.stringify(stale))
      const terminalSegment = path.join(caseDir, descriptors[0]!.path)
      const sourceArtifact = path.join(terminalSegment, "artifacts", "renderer.txt")
      fs.mkdirSync(path.dirname(sourceArtifact), { recursive: true })
      fs.writeFileSync(sourceArtifact, "immutable renderer artifact")
      fs.writeFileSync(
        path.join(terminalSegment, "legacy-trace.json"),
        JSON.stringify({
          trace_version: "1.3",
          artifacts: [{ artifact_id: "renderer_artifact", path: "artifacts/renderer.txt" }],
        }),
      )
      const before = treeHashes(caseDir)
      const artifactBefore = fs.statSync(sourceArtifact, { bigint: true })
      const artifactBytes = fs.readFileSync(sourceArtifact)

      for (const input of [caseDir, traceFile]) {
        const result = loadRenderableTrace(input)
        expect(result).toMatchObject({ caseDir, source: "trace.json", incomplete: true })
        expect(
          result.trace.records
            .filter((item: any) => item.event_type === "response.output")
            .map((item: any) => item.data.text),
        ).toEqual(["first response", "second response"])
      }
      expect(treeHashes(caseDir)).toEqual(before)
      expect(JSON.parse(fs.readFileSync(traceFile, "utf8")).manifest.session_generation).toBe(1)
      const artifactAfter = fs.statSync(sourceArtifact, { bigint: true })
      expect(fs.readFileSync(sourceArtifact)).toEqual(artifactBytes)
      expect(artifactAfter.ctimeNs).toBe(artifactBefore.ctimeNs)
      expect(artifactAfter.nlink).toBe(artifactBefore.nlink)
    })
  })

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
      const before = treeHashes(caseDir)

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
      expect(treeHashes(caseDir)).toEqual(before)
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
