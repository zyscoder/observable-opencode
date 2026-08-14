import { expect, test } from "bun:test"
import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { finalizeWorkerTraces } from "@/cli/cmd/tui/worker-trace"

test("worker trace helper returns journal materialization requests with the shutdown failure", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "opencode-worker-trace-"))
  const packageDir = path.resolve(import.meta.dir, "../../..")
  const script = path.join(dir, "worker-trace.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
  const workerTraceModule = pathToFileURL(path.join(packageDir, "src/cli/cmd/tui/worker-trace.ts")).href

  try {
    await writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `import { finalizeWorkerTraces } from ${JSON.stringify(workerTraceModule)}`,
        `import { existsSync, readdirSync } from "node:fs"`,
        `CaseTrace.configure({ caseID: "worker-normal-shutdown" })`,
        `CaseTrace.event({ component: "runtime", event_type: "root.a", data: { sessionID: "ses_worker_a" } })`,
        `CaseTrace.event({ component: "runtime", event_type: "root.b", data: { sessionID: "ses_worker_b" } })`,
        `CaseTrace.startSpan({ component: "run", operation: "execute", trace_scope: "process" })?.end({ status: "success" })`,
        `const result = await finalizeWorkerTraces()`,
        `const requests = result.requests`,
        `const count = readdirSync(${JSON.stringify(dir)}, { withFileTypes: true }).filter((entry) => entry.isDirectory() && existsSync(${JSON.stringify(dir)} + "/" + entry.name + "/trace.json")).length`,
        `if (count !== 0 || requests.length !== 3) process.exitCode = 4`,
        `process.stdout.write(JSON.stringify(result))`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_QUIET: "",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await proc.exited).toBe(0)
    const stderr = await new Response(proc.stderr).text()
    const directories = (await readdir(dir, { withFileTypes: true })).filter((entry) => entry.isDirectory())
    expect(directories).toHaveLength(3)
    expect(stderr).toBe("")

    const result = JSON.parse(await new Response(proc.stdout).text()) as {
      requests: Array<{
        caseDir: string
        caseID: string
        runID: string
        sessionID?: string
        recordsFile: string
      }>
      failure?: string
    }
    expect(result.failure).toBeUndefined()
    const requests = result.requests
    expect(requests.map((request) => request.sessionID).sort()).toEqual(["ses_worker_a", "ses_worker_b", undefined])
    for (const request of requests) {
      expect(request.recordsFile).toBe(path.join(request.caseDir, "records.jsonl"))
      expect(await readFile(request.recordsFile, "utf8")).toContain('"operation":"case.runtime_closed"')
    }
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test("worker trace helper preserves runtime shutdown failure alongside materialization requests", async () => {
  const calls: unknown[] = []
  const failure = new Error("server stop failed")
  const request = {
    caseDir: "/tmp/case",
    caseID: "case",
    runID: "run",
    recordsFile: "/tmp/case/records.jsonl",
  }
  await expect(
    finalizeWorkerTraces({
      failure,
      trace: {
        closeAll(input) {
          calls.push(input)
          return [request]
        },
      },
    }),
  ).resolves.toEqual({ requests: [request], failure: "server stop failed" })
  expect(calls).toEqual([
    expect.objectContaining({
      status: "error",
      error: failure,
      result: { reason: "worker.shutdown" },
    }),
  ])
})

test("worker trace helper returns an empty request list when journal close throws", async () => {
  const failure = new Error("server stop failed")
  await expect(
    finalizeWorkerTraces({
      failure,
      trace: {
        closeAll() {
          throw new Error("trace write failed")
        },
      },
    }),
  ).resolves.toEqual({ requests: [], failure: "server stop failed" })
})
