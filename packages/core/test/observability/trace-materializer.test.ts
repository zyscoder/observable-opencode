import { expect, test } from "bun:test"
import fsSync from "node:fs"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { materializeTrace } from "@opencode-ai/core/observability/trace-materializer"
import { CausalIRRuntimeStore } from "@opencode-ai/core/observability/causal-ir-runtime-store"
import { openTraceSegment } from "@opencode-ai/core/observability/trace-segment"

test("materializes a segmented journal into the canonical Causal IR files", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-latest-materializer-"))
  try {
    const segment = openTraceSegment({
      rootDir: root,
      logicalCaseID: "case_materializer",
      sessionID: "ses_materializer",
      runID: "run_materializer",
    })
    const recordsFile = path.join(segment.logicalRoot, segment.descriptor.records)
    const store = new CausalIRRuntimeStore({
      indexPath: path.join(segment.logicalRoot, segment.descriptor.index),
      caseID: "case_materializer",
      runID: "run_materializer",
      append: (entry) => fsSync.appendFileSync(recordsFile, `${JSON.stringify(entry)}\n`),
    })
    const timestamp = new Date().toISOString()
    store.createNode({
      node_id: "run_materializer",
      kind: "run.start",
      component: "run",
      timestamp,
      time_ms: 1,
      data: { run_id: "run_materializer", case_id: "case_materializer" },
    })
    store.createNode({
      node_id: "decision_materializer",
      kind: "decision",
      component: "processor",
      timestamp,
      time_ms: 2,
      data: { action: "inspect" },
    })
    store.createEdge({
      edge_id: "edge_materializer",
      from: { type: "node", id: "run_materializer" },
      to: { type: "node", id: "decision_materializer" },
      relation: "produced",
    })
    store.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: timestamp,
      manifest: { case_id: "case_materializer", run_id: "run_materializer", session_id: "ses_materializer" },
    })
    store.close()
    segment.finalize("completed")

    const result = materializeTrace({ caseDir: segment.logicalRoot })
    expect(result.completeness).toBe("complete")
    expect(JSON.parse(await fs.readFile(result.traceFile, "utf8"))).toMatchObject({
      trace_version: "6.0",
      causal_ir_version: "1.0",
      nodes: expect.arrayContaining([expect.objectContaining({ node_id: "decision_materializer" })]),
      edges: expect.arrayContaining([expect.objectContaining({ edge_id: "edge_materializer" })]),
    })
    expect(JSON.parse(await fs.readFile(result.partialFile, "utf8"))).toMatchObject({
      manifest: expect.objectContaining({ case_id: "case_materializer" }),
    })
  } finally {
    await fs.rm(root, { recursive: true, force: true })
  }
})
