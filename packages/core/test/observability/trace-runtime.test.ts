import { afterEach, expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { materializeTrace } from "@opencode-ai/core/observability/trace-materializer"
import { TraceRuntime } from "@opencode-ai/core/observability/trace-runtime"

const roots: string[] = []

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => fs.rm(root, { recursive: true, force: true })))
})

test("records a bounded fact without exposing runtime failures to the caller", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-latest-runtime-"))
  roots.push(root)
  const runtime = TraceRuntime.open({
    rootDir: root,
    caseID: "case_runtime",
    runID: "run_runtime",
  })

  expect(runtime.record({ operation: "observation", component: "runtime", data: { ok: true } })).toBeUndefined()
  const artifact = runtime.recordArtifact({ value: { full: "semantic payload" }, mediaType: "application/json" })
  expect(artifact?.artifact_id).toMatch(/^artifact_/)
  runtime.close("completed")

  const result = materializeTrace({ caseDir: path.join(root, "case_runtime") })
  expect(result.completeness).toBe("complete")
  expect(JSON.parse(await fs.readFile(result.traceFile, "utf8"))).toMatchObject({
    causal_ir_version: "1.0",
    nodes: expect.arrayContaining([expect.objectContaining({ kind: "observation" })]),
    artifacts: expect.arrayContaining([expect.objectContaining({ artifact_id: artifact?.artifact_id })]),
  })
})
