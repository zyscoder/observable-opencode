import { expect, test } from "bun:test"
import { Database } from "bun:sqlite"
import fs from "node:fs/promises"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { CausalIRRuntimeStore } from "@/observability/causal-ir-runtime-store"
import {
  CausalIRStore,
  replayCausalIRJournal,
  type CausalIRJournalEntry,
  type CausalIRNodeInput,
} from "@/observability/causal-ir"
import { tmpdir } from "../fixture/fixture"

function node(nodeID: string, data: Record<string, unknown> = {}): CausalIRNodeInput {
  return {
    node_id: nodeID,
    kind: "decision",
    component: "task",
    timestamp: "2026-08-14T00:00:00.000Z",
    time_ms: 1,
    status: "success",
    data,
  }
}

function comparableJournal(entries: CausalIRJournalEntry[]) {
  return entries.map(({ time: _time, ...entry }) => entry)
}

function exerciseStore(store: CausalIRStore | CausalIRRuntimeStore) {
  store.createArtifact({
    artifact_id: "artifact_1",
    hash: "sha256:artifact_1",
    path: "artifacts/artifact_1.txt",
    occurrences: 1,
  })
  store.createNode({
    ...node("consumer", { session_id: "session_1", message_id: "message_1", call_id: "call_1" }),
    source_refs: ["decision:provider"],
  })
  store.createNode({
    ...node("provider", { version: 1 }),
    aliases: ["decision:provider", "decision:provider_alias"],
  })
  store.updateNode({
    ...node("provider", { version: 2 }),
    aliases: ["decision:provider", "decision:provider_alias"],
  })
  store.createNode({
    ...node("derived", { artifact: "artifact_1" }),
    origin: "deterministic_derived",
    input_refs: ["decision:provider"],
    derivation: {
      algorithm: "test-derivation",
      algorithm_version: "1",
      derived_at: "2026-08-14T00:00:01.000Z",
      input_refs: [{ ref_type: "node", ref_id: "provider", legacy_ref: "decision:provider" }],
      reproducible: true,
    },
  })
  store.createEdge({
    edge_id: "edge_1",
    from: { type: "decision", id: "provider" },
    to: { type: "node", id: "derived" },
    relation: "produced",
  })
  store.createEdge({
    edge_id: "edge_alias",
    from: { type: "decision", id: "provider_alias" },
    to: { type: "node", id: "derived" },
    relation: "produced",
  })
  store.createEdge({
    edge_id: "edge_unknown",
    from: { type: "node", id: "consumer" },
    to: { type: "node", id: "derived" },
    relation: "test-only-unknown-relation",
  })
  store.reuseArtifact({
    artifact_id: "artifact_1",
    hash: "sha256:artifact_1",
    path: "artifacts/artifact_1.txt",
    occurrences: 2,
  })
  store.createDiagnostic({
    diagnostic_id: "diagnostic_1",
    kind: "test_diagnostic",
    level: "info",
  })
}

test("disk runtime store preserves journal replay, hash chains, aliases, and directed queries", async () => {
  await using tmp = await tmpdir()
  const memoryJournal: CausalIRJournalEntry[] = []
  const runtimeJournal: CausalIRJournalEntry[] = []
  const memory = new CausalIRStore({
    runID: "run_1",
    caseID: "case_1",
    append: (entry) => memoryJournal.push(entry),
  })
  const runtime = new CausalIRRuntimeStore({
    runID: "run_1",
    caseID: "case_1",
    indexPath: path.join(tmp.path, "index.sqlite"),
    append: (entry) => runtimeJournal.push(entry),
  })

  try {
    exerciseStore(memory)
    exerciseStore(runtime)

    expect(replayCausalIRJournal(runtimeJournal)).toEqual(replayCausalIRJournal(memoryJournal))
    expect(comparableJournal(runtimeJournal)).toEqual(comparableJournal(memoryJournal))
    expect(runtime.resolveReference("decision:provider")).toEqual(memory.resolveReference("decision:provider"))
    expect(runtime.queryNodes({ ids: ["consumer", "provider", "missing"] }).map((item) => item.node_id)).toEqual([
      "consumer",
      "provider",
    ])
    expect(
      runtime
        .queryNodes({
          kinds: ["decision"],
          component: "task",
          status: "success",
          sessionID: "session_1",
          messageID: "message_1",
          callID: "call_1",
        })
        .map((item) => item.node_id),
    ).toEqual(["consumer"])
    expect(runtime.queryEdges({ ids: ["edge_1"] }).map((item) => item.edge_id)).toEqual(["edge_1"])
    expect(
      runtime
        .queryEdges({ fromIDs: ["provider_alias"], toIDs: ["derived"], relations: ["produced"] })
        .map((item) => item.edge_id),
    ).toEqual(["edge_alias"])
    expect(runtime.queryArtifacts({ ids: ["artifact_1"] })).toEqual([
      {
        artifact_id: "artifact_1",
        hash: "sha256:artifact_1",
        path: "artifacts/artifact_1.txt",
        occurrences: 2,
      },
    ])
    expect(runtime.queryDiagnostics({ kinds: ["test_diagnostic"] }).map((item) => item.diagnostic_id)).toEqual([
      "diagnostic_1",
    ])
  } finally {
    runtime.close()
  }
})

test("disk runtime store evicts parsed nodes and edges while cold rows remain queryable", async () => {
  await using tmp = await tmpdir()
  const runtime = new CausalIRRuntimeStore({
    runID: "run_eviction",
    caseID: "case_eviction",
    indexPath: path.join(tmp.path, "index.sqlite"),
    append: () => true,
  })

  try {
    for (let index = 0; index < 2_000; index++) {
      runtime.createNode(node(`node_${index}`, { index }))
    }
    for (let index = 0; index < 1_500; index++) {
      runtime.createEdge({
        edge_id: `edge_${index}`,
        from: { type: "external", id: `source_${index}` },
        to: { type: "external", id: `target_${index}` },
        relation: "produced",
      })
    }

    expect(runtime.hotNodeCount).toBeLessThanOrEqual(512)
    expect(runtime.hotEdgeCount).toBeLessThanOrEqual(1_024)
    expect(runtime.queryNodes({ ids: ["node_0", "node_1999"] }).map((item) => item.node_id)).toEqual([
      "node_0",
      "node_1999",
    ])
    expect(runtime.queryEdges({ ids: ["edge_0", "edge_1499"] }).map((item) => item.edge_id)).toEqual([
      "edge_0",
      "edge_1499",
    ])
    expect(runtime.hotNodeCount).toBeLessThanOrEqual(512)
    expect(runtime.hotEdgeCount).toBeLessThanOrEqual(1_024)
  } finally {
    runtime.close()
  }
})

test("failed journal append does not commit a SQLite index mutation", async () => {
  await using tmp = await tmpdir()
  const runtime = new CausalIRRuntimeStore({
    runID: "run_append_failure",
    caseID: "case_append_failure",
    indexPath: path.join(tmp.path, "index.sqlite"),
    append: () => false,
  })

  try {
    runtime.createNode(node("uncommitted"))
    expect(runtime.queryNodes({ ids: ["uncommitted"] })).toEqual([])
    expect(runtime.journalSummary()).toMatchObject({ entry_count: 0, poisoned: true })
  } finally {
    runtime.close()
  }
})

test("a failed second append preserves the SQLite materialization of the durable prefix", async () => {
  await using tmp = await tmpdir()
  const indexPath = path.join(tmp.path, "index.sqlite")
  const durable: CausalIRJournalEntry[] = []
  let attempts = 0
  const runtime = new CausalIRRuntimeStore({
    runID: "run_durable_prefix",
    caseID: "case_durable_prefix",
    indexPath,
    append: (entry) => {
      attempts += 1
      if (attempts === 2) return false
      durable.push(entry)
      return true
    },
  })

  try {
    runtime.createNode({ ...node("durable_node"), source_refs: ["node:missing_source"] })

    expect(durable.map((entry) => entry.operation)).toEqual(["node.created"])
    expect(runtime.queryNodes()).toEqual([])
    const index = new Database(indexPath)
    try {
      expect(index.query("SELECT node_id FROM nodes ORDER BY sequence").all()).toEqual([
        { node_id: "durable_node" },
      ])
      expect(index.query("SELECT diagnostic_id FROM diagnostics").all()).toEqual([])
      expect(index.query("SELECT value FROM metadata WHERE key = 'sequence'").get()).toEqual({ value: "1" })
    } finally {
      index.close(false)
    }
  } finally {
    runtime.close()
  }
})

test("post-initialization SQLite faults and closed stores stay passive", async () => {
  await using tmp = await tmpdir()
  let fault: "mutation" | "query" | undefined
  const runtime = new CausalIRRuntimeStore({
    runID: "run_passive_fault",
    caseID: "case_passive_fault",
    indexPath: path.join(tmp.path, "fault.sqlite"),
    append: () => true,
    fault: (operation) => {
      if (operation === fault) throw new Error(`injected ${operation} fault`)
    },
  })

  runtime.createNode(node("warm_node"))
  fault = "mutation"
  expect(() => runtime.createNode(node("after_mutation_fault"))).not.toThrow()
  expect(runtime.queryNodes()).toEqual([])
  expect(runtime.journalSummary()).toMatchObject({ poisoned: true })
  expect(() => runtime.close()).not.toThrow()
  expect(() => runtime.createNode(node("after_close"))).not.toThrow()
  expect(runtime.queryNodes()).toEqual([])

  fault = undefined
  const queryRuntime = new CausalIRRuntimeStore({
    runID: "run_passive_query_fault",
    caseID: "case_passive_query_fault",
    indexPath: path.join(tmp.path, "query-fault.sqlite"),
    append: () => true,
    fault: (operation) => {
      if (operation === fault) throw new Error(`injected ${operation} fault`)
    },
  })
  queryRuntime.createNode(node("query_warm_node"))
  fault = "query"
  expect(() => queryRuntime.queryNodes()).not.toThrow()
  expect(queryRuntime.queryNodes()).toEqual([])
  expect(queryRuntime.journalSummary()).toMatchObject({ poisoned: true })
  expect(() => queryRuntime.close()).not.toThrow()

  let transactionFault = false
  const transactionRuntime = new CausalIRRuntimeStore({
    runID: "run_passive_transaction_fault",
    caseID: "case_passive_transaction_fault",
    indexPath: path.join(tmp.path, "transaction-fault.sqlite"),
    append: () => true,
    fault: (operation) => {
      if (operation === "transaction" && transactionFault) throw new Error("injected transaction fault")
    },
  })
  transactionRuntime.createNode(node("transaction_warm_node"))
  transactionFault = true
  expect(() => transactionRuntime.finalize({ status: "success" })).not.toThrow()
  expect(transactionRuntime.finalize({ status: "success" })).toMatchObject({ committed: false, poisoned: true })
  expect(transactionRuntime.queryNodes()).toEqual([])
  expect(() => transactionRuntime.close()).not.toThrow()

  let snapshotFault = false
  const snapshotRuntime = new CausalIRRuntimeStore({
    runID: "run_passive_snapshot_fault",
    caseID: "case_passive_snapshot_fault",
    indexPath: path.join(tmp.path, "snapshot-fault.sqlite"),
    append: () => true,
    fault: (operation) => {
      if (operation === "query" && snapshotFault) throw new Error("injected snapshot fault")
    },
  })
  snapshotRuntime.createNode(node("snapshot_warm_node"))
  snapshotFault = true
  expect(snapshotRuntime.finalize({ status: "success" })).toMatchObject({ committed: false, poisoned: true })
  expect(
    snapshotRuntime.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-14T00:00:00.000Z",
      manifest: { case_id: "case_passive_snapshot_fault", run_id: "run_passive_snapshot_fault" },
    }),
  ).toMatchObject({ committed: false, poisoned: true })
  expect(() => snapshotRuntime.close()).not.toThrow()
})

test("bulk node and edge replacements preserve replay and reference indexes", async () => {
  await using tmp = await tmpdir()
  const memoryJournal: CausalIRJournalEntry[] = []
  const runtimeJournal: CausalIRJournalEntry[] = []
  const memory = new CausalIRStore({
    runID: "run_replace",
    caseID: "case_replace",
    append: (entry) => memoryJournal.push(entry),
  })
  const runtime = new CausalIRRuntimeStore({
    runID: "run_replace",
    caseID: "case_replace",
    indexPath: path.join(tmp.path, "index.sqlite"),
    append: (entry) => runtimeJournal.push(entry),
  })

  try {
    for (const store of [memory, runtime]) {
      store.createNode({ ...node("provider"), aliases: ["decision:provider"] })
      store.createNode({ ...node("consumer"), source_refs: ["decision:provider"] })
      store.createEdge({
        edge_id: "edge_before",
        from: { type: "decision", id: "provider" },
        to: { type: "node", id: "consumer" },
        relation: "produced",
      })
      store.replaceNodes([{ ...node("consumer", { replaced: true }), source_refs: ["decision:provider"] }])
      store.replaceEdges([
        {
          edge_id: "edge_after",
          from: { type: "external", id: "replacement_source" },
          to: { type: "node", id: "consumer" },
          relation: "produced",
        },
      ])
    }

    expect(replayCausalIRJournal(runtimeJournal)).toEqual(replayCausalIRJournal(memoryJournal))
    expect(comparableJournal(runtimeJournal)).toEqual(comparableJournal(memoryJournal))
    expect(runtime.queryNodesReferencing(["decision:provider"]).map((item) => item.node_id)).toEqual(["consumer"])
  } finally {
    runtime.close()
  }
})

test("unavailable SQLite paths remain passive to the agent-visible process result", async () => {
  await using tmp = await tmpdir()
  const packageDir = path.resolve(import.meta.dir, "../..")
  const blockedTraceRoot = path.join(tmp.path, "blocked-trace-root")
  const script = path.join(tmp.path, "passive-runtime-store-failure.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
  await fs.writeFile(blockedTraceRoot, "not a directory")
  await fs.writeFile(
    script,
    [
      `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
      `const result = { answer: "stable agent result" }`,
      `CaseTrace.configure({ caseID: "blocked-store", traceDir: ${JSON.stringify(blockedTraceRoot)} })`,
      `CaseTrace.observation({ source: "runtime", category: "passive", summary: "still passive" })`,
      `CaseTrace.finish({ status: "success", result })`,
      `process.stdout.write(JSON.stringify(result))`,
    ].join("\n"),
  )

  const child = Bun.spawn([process.execPath, script], {
    cwd: packageDir,
    env: { ...process.env, OPENCODE_CASE_TRACE: "1" },
    stdout: "pipe",
    stderr: "pipe",
  })
  const [exitCode, stdout] = await Promise.all([child.exited, new Response(child.stdout).text()])

  expect(exitCode).toBe(0)
  expect(stdout).toBe('{"answer":"stable agent result"}')
})

test("failed-case finalization retains the latest sixteen open record references", async () => {
  await using tmp = await tmpdir()
  const packageDir = path.resolve(import.meta.dir, "../..")
  const script = path.join(tmp.path, "latest-open-records.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
  await fs.writeFile(
    script,
    [
      `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
      `CaseTrace.configure({ caseID: "latest-open-records", traceDir: ${JSON.stringify(tmp.path)} })`,
      `for (let index = 0; index < 20; index++) CaseTrace.node({ node_id: \`open_\${index}\`, kind: "test.open", component: "runtime", status: "cancelled", data: { finalized_status: "finalized_without_close" } })`,
      `CaseTrace.finish({ status: "cancelled" })`,
    ].join("\n"),
  )

  const child = Bun.spawn([process.execPath, script], {
    cwd: packageDir,
    env: { ...process.env, OPENCODE_CASE_TRACE: "1" },
    stdout: "pipe",
    stderr: "pipe",
  })
  const exitCode = await child.exited
  expect(exitCode, await new Response(child.stderr).text()).toBe(0)

  const trace = JSON.parse(await fs.readFile(path.join(tmp.path, "latest-open-records", "trace.json"), "utf8"))
  const failure = trace.nodes.find((item: any) => item.kind === "case.failed")
  expect(failure.data.finalized_open_record_refs).toHaveLength(16)
  expect(
    failure.data.finalized_open_record_refs.filter((ref: string) => ref.startsWith("node:open_")),
  ).toEqual(
    Array.from({ length: 16 }, (_, offset) => `node:open_${offset + 4}`),
  )
})
