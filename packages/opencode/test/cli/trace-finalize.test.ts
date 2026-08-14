import { expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { CausalIRStore } from "@/observability/causal-ir"

const index = path.resolve(import.meta.dir, "../../src/index.ts")

async function caseDirectory() {
  const caseDir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-trace-finalize-command-"))
  const entries: unknown[] = []
  const store = new CausalIRStore({
    runID: "run_cli_finalize",
    caseID: "case-cli-finalize",
    append: (entry) => entries.push(entry),
  })
  store.createNode({
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-14T12:00:00.000Z",
    time_ms: 0,
    data: { run_id: "run_cli_finalize", case_id: "case-cli-finalize" },
  })
  store.closeRuntime({
    format: "runtime_close",
    status: "success",
    closed_at: "2026-08-14T12:00:01.000Z",
    manifest: { case_id: "case-cli-finalize", run_id: "run_cli_finalize" },
  })
  await fs.writeFile(path.join(caseDir, "records.jsonl"), `${entries.map((entry) => JSON.stringify(entry)).join("\n")}\n`)
  return caseDir
}

function run(...args: string[]) {
  return Bun.spawnSync({ cmd: [process.execPath, index, ...args], stdout: "pipe", stderr: "pipe" })
}

test("finalizes a case directory through the hidden OpenCode command", async () => {
  const caseDir = await caseDirectory()
  try {
    const result = run("trace-finalize", caseDir)
    const stdout = Buffer.from(result.stdout).toString()

    expect(result.exitCode).toBe(0)
    expect(stdout).toContain("completeness: complete")
    expect(stdout).toContain(`trace: ${path.join(caseDir, "trace.json")}`)
    expect(stdout).toContain(`manifest: ${path.join(caseDir, "manifest.json")}`)
    expect(stdout).toContain(`partial: ${path.join(caseDir, "partial", "latest.json")}`)
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})

test("rejects invalid arguments and HTML files for trace finalization", async () => {
  const caseDir = await caseDirectory()
  const html = path.join(caseDir, "trace.html")
  try {
    await fs.writeFile(html, "<html></html>")

    expect(run("trace-finalize", caseDir, "--unknown").exitCode).not.toBe(0)
    expect(Buffer.from(run("trace-finalize", html).stderr).toString()).toContain("expected a case directory")
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})
