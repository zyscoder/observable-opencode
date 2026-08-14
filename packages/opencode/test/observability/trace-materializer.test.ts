import { expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { causalIRPayloadHash, CausalIRStore, type CausalIRJournalEntry } from "@/observability/causal-ir"
import { materializeTrace } from "@/observability/trace-materializer"

async function caseDirectory() {
  return fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-materializer-"))
}

function journal(runtimeClosed: boolean) {
  const entries: CausalIRJournalEntry[] = []
  const store = new CausalIRStore({
    runID: "run_materializer",
    caseID: "case-materializer",
    append: (entry) => entries.push(entry),
  })
  store.createNode({
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-14T12:00:00.000Z",
    time_ms: 0,
    status: "running",
    data: { run_id: "run_materializer", case_id: "case-materializer" },
  })
  store.createNode({
    node_id: "response_1",
    kind: "response.output",
    component: "result",
    timestamp: "2026-08-14T12:00:01.000Z",
    time_ms: 1,
    status: "success",
    data: { text: "replayed output" },
  })
  if (runtimeClosed) {
    store.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-14T12:00:02.000Z",
      manifest: { case_id: "case-materializer", run_id: "run_materializer" },
    })
  }
  return entries
}

function legacyLifecycleJournal() {
  const entries: CausalIRJournalEntry[] = []
  const store = new CausalIRStore({
    runID: "run_materializer_legacy",
    caseID: "case-materializer-legacy",
    append: (entry) => entries.push(entry),
  })
  store.createNode({
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-14T12:00:00.000Z",
    time_ms: 0,
    status: "running",
    data: { run_id: "run_materializer_legacy", case_id: "case-materializer-legacy" },
  })
  store.checkpoint({ phase: "legacy" })
  const snapshot = store.snapshot()
  const manifest = { run_id: "run_materializer_legacy", case_id: "case-materializer-legacy", status: "success" }
  const trace = {
    trace_version: "6.0",
    causal_ir_version: "1.0",
    manifest,
    nodes: snapshot.nodes,
    edges: snapshot.edges,
    artifacts: snapshot.artifacts,
    diagnostics: snapshot.diagnostics,
    journal: store.journalSummary(),
    metrics: {},
    compatibility: {},
  }
  const data = { snapshot, data: manifest, trace }
  entries.push({
    sequence: entries.length + 1,
    time: "2026-08-14T12:00:01.000Z",
    run_id: "run_materializer_legacy",
    case_id: "case-materializer-legacy",
    operation: "case.finalized",
    record_type: "finish",
    entity_id: "case-materializer-legacy",
    previous_payload_hash: entries.at(-1)!.payload_hash,
    data,
    payload_hash: causalIRPayloadHash(data),
  })
  return entries
}

async function writeJournal(caseDir: string, entries: CausalIRJournalEntry[], suffix = "") {
  const text = entries.map((entry) => JSON.stringify(entry)).join("\n") + "\n" + suffix
  await fs.writeFile(path.join(caseDir, "records.jsonl"), text)
  return text
}

test("materializes a runtime-closed journal into a complete canonical trace", async () => {
  const caseDir = await caseDirectory()
  try {
    await writeJournal(caseDir, journal(true))

    const result = materializeTrace({ caseDir })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8"))
    const manifest = JSON.parse(await fs.readFile(result.manifestFile, "utf8"))

    expect(result).toMatchObject({ caseDir, completeness: "complete", recoveredLines: 3 })
    expect(trace).toMatchObject({
      manifest: { case_id: "case-materializer", run_id: "run_materializer", status: "success" },
    })
    expect(trace.nodes.map((node: { node_id: string }) => node.node_id)).toEqual(["run_start", "response_1"])
    expect(manifest).toMatchObject({ case_id: "case-materializer", status: "success" })
    expect(JSON.parse(await fs.readFile(result.partialFile, "utf8"))).toEqual(trace)
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})

test("marks a valid journal without runtime close as incomplete after an interrupted worker", async () => {
  const caseDir = await caseDirectory()
  try {
    await writeJournal(caseDir, journal(false))

    const result = materializeTrace({ caseDir })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8"))

    expect(result).toMatchObject({ completeness: "incomplete", recoveredLines: 2 })
    expect(trace.manifest).toMatchObject({
      status: "error",
      recovery_status: "incomplete_journal_replay",
    })
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})

test("recovers the valid journal prefix when the final JSONL line is torn", async () => {
  const caseDir = await caseDirectory()
  try {
    const original = await writeJournal(caseDir, journal(true), '{"sequence":')

    const result = materializeTrace({ caseDir })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8"))

    expect(result).toMatchObject({ completeness: "incomplete", recoveredLines: 3 })
    expect(trace.nodes.map((node: { node_id: string }) => node.node_id)).toEqual(["run_start", "response_1"])
    expect(trace.manifest).toMatchObject({
      recovery_status: "incomplete_journal_replay",
      recovery: { dropped_lines: 1 },
    })
    expect(await fs.readFile(path.join(caseDir, "records.jsonl"), "utf8")).toBe(original)
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})

test("decodes UTF-8 split across read chunks before recovering a torn multibyte tail", async () => {
  const caseDir = await caseDirectory()
  const createEntries = (padding: number) => {
    const entries: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_materializer_utf8",
      caseID: "case-materializer-utf8",
      append: (entry) => entries.push(entry),
    })
    store.createNode({
      node_id: "run_start",
      kind: "run.start",
      component: "run",
      timestamp: "2026-08-14T12:00:00.000Z",
      time_ms: 0,
      status: "running",
      data: { run_id: "run_materializer_utf8", case_id: "case-materializer-utf8" },
    })
    store.createArtifact({
      artifact_id: "artifact_utf8",
      hash: "sha256:utf8",
      path: "artifacts/utf8.txt",
      payload: `${"x".repeat(padding)}🙂汉字`,
    })
    store.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-14T12:00:01.000Z",
      manifest: { case_id: "case-materializer-utf8", run_id: "run_materializer_utf8" },
    })
    return entries
  }

  try {
    const base =
      createEntries(0)
        .map((entry) => JSON.stringify(entry))
        .join("\n") + "\n"
    const baseEmoji = base.indexOf("🙂")
    const padding = (64 * 1024 - 2 - (Buffer.byteLength(base.slice(0, baseEmoji)) % (64 * 1024))) % (64 * 1024)
    const entries = createEntries(padding)
    const validPrefix = entries.map((entry) => JSON.stringify(entry)).join("\n") + "\n"
    const emoji = validPrefix.indexOf("🙂")
    expect(Buffer.byteLength(validPrefix.slice(0, emoji)) % (64 * 1024)).toBe(64 * 1024 - 2)
    const original = Buffer.concat([Buffer.from(validPrefix), Buffer.from('{"payload":"'), Buffer.from([0xf0, 0x9f])])
    await fs.writeFile(path.join(caseDir, "records.jsonl"), original)

    const result = materializeTrace({ caseDir })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8"))

    expect(result).toMatchObject({ completeness: "incomplete", recoveredLines: 3 })
    expect(trace.artifacts).toEqual([
      expect.objectContaining({ artifact_id: "artifact_utf8", payload: `${"x".repeat(padding)}🙂汉字` }),
    ])
    expect(await fs.readFile(path.join(caseDir, "records.jsonl"))).toEqual(original)
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})

test("rejects corruption before the final journal line", async () => {
  const caseDir = await caseDirectory()
  try {
    const entries = journal(true)
    await writeJournal(
      caseDir,
      entries.map((entry, index) => (index === 1 ? { ...entry, sequence: 99 } : entry)),
    )

    expect(() => materializeTrace({ caseDir })).toThrow("records.jsonl:2")
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})

test("binds the original legacy lifecycle summary to its preceding journal prefix", async () => {
  const cases: Array<[string, (summary: Record<string, unknown>) => void]> = [
    ["entry count", (summary) => (summary.entry_count = 999)],
    ["last sequence", (summary) => (summary.last_sequence = 999)],
    ["last payload hash", (summary) => (summary.last_payload_hash = "0".repeat(64))],
  ]

  for (const [label, tamper] of cases) {
    const caseDir = await caseDirectory()
    try {
      const entries = legacyLifecycleJournal()
      const terminal = entries.at(-1)!
      const data = terminal.data as { trace: { journal: Record<string, unknown> } }
      tamper(data.trace.journal)
      terminal.payload_hash = causalIRPayloadHash(terminal.data)
      await writeJournal(caseDir, entries)

      expect(() => materializeTrace({ caseDir }), label).toThrow(`records.jsonl:${entries.length}`)
    } finally {
      await fs.rm(caseDir, { recursive: true, force: true })
    }
  }
})

test("preserves snapshot replacement order and appends later new entities at the table tail", async () => {
  const caseDir = await caseDirectory()
  try {
    const entries: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_materializer_order",
      caseID: "case-materializer-order",
      append: (entry) => entries.push(entry),
    })
    const runStart = store.createNode({
      node_id: "run_start",
      kind: "run.start",
      component: "run",
      timestamp: "2026-08-14T12:00:00.000Z",
      time_ms: 0,
      status: "running",
      data: { run_id: "run_materializer_order", case_id: "case-materializer-order" },
    })
    const replacement = [
      runStart,
      ...Array.from({ length: 6 }, (_, index) => ({
        node_id: `replacement_${index + 1}`,
        kind: "response.output" as const,
        component: "result" as const,
        timestamp: "2026-08-14T12:00:01.000Z",
        time_ms: index + 1,
        status: "success" as const,
        data: { text: `replacement ${index + 1}` },
      })),
    ]
    store.replaceNodes(replacement)
    store.updateNode({ ...replacement[2]!, title: "updated in place" })
    store.createNode({
      node_id: "appended_after_snapshot",
      kind: "response.output",
      component: "result",
      timestamp: "2026-08-14T12:00:02.000Z",
      time_ms: 8,
      status: "success",
      data: { text: "appended" },
    })
    store.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-14T12:00:03.000Z",
      manifest: { case_id: "case-materializer-order", run_id: "run_materializer_order" },
    })
    await writeJournal(caseDir, entries)

    const result = materializeTrace({ caseDir })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8"))

    expect(trace.nodes.map((node: { node_id: string }) => node.node_id)).toEqual([
      "run_start",
      "replacement_1",
      "replacement_2",
      "replacement_3",
      "replacement_4",
      "replacement_5",
      "replacement_6",
      "appended_after_snapshot",
    ])
    expect(trace.nodes[2]).toEqual(expect.objectContaining({ node_id: "replacement_2", title: "updated in place" }))
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})
