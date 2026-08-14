import { expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { CausalIRStore, type CausalIRJournalEntry } from "@/observability/causal-ir"
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

test("rejects corruption before the final journal line", async () => {
  const caseDir = await caseDirectory()
  try {
    const entries = journal(true)
    await writeJournal(caseDir, entries.map((entry, index) => (index === 1 ? { ...entry, sequence: 99 } : entry)))

    expect(() => materializeTrace({ caseDir })).toThrow("records.jsonl:2")
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})
