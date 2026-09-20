import { afterEach, expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
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
  runtime.close("completed")
})
