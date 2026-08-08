import { expect, test } from "bun:test"
import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { finalizeWorkerTraces } from "@/cli/cmd/tui/worker-trace"

test("worker trace helper finalizes every root and process trace with a receipt", async () => {
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
        `await finalizeWorkerTraces()`,
        `const count = readdirSync(${JSON.stringify(dir)}, { withFileTypes: true }).filter((entry) => entry.isDirectory() && existsSync(${JSON.stringify(dir)} + "/" + entry.name + "/trace.json")).length`,
        `if (count !== 3) process.exitCode = 4`,
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
    expect(stderr.match(/Session trace saved/g)).toHaveLength(3)

    const traces = await Promise.all(
      directories.map(async (entry) => JSON.parse(await readFile(path.join(dir, entry.name, "trace.json"), "utf8")) as any),
    )
    expect(traces.map((trace) => trace.manifest.session_id).sort()).toEqual(["ses_worker_a", "ses_worker_b", undefined])
    for (const trace of traces) {
      expect(trace.manifest).toMatchObject({ status: "success", case_status: "success" })
      expect(trace.manifest.result).toMatchObject({ reason: "worker.shutdown" })
    }
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test("worker trace helper reports failure and swallows trace finalization errors", async () => {
  const calls: unknown[] = []
  const failure = new Error("server stop failed")
  await expect(
    finalizeWorkerTraces({
      failure,
      trace: {
        finishAll(input) {
          calls.push(input)
          throw new Error("trace write failed")
        },
      },
    }),
  ).resolves.toBeUndefined()
  expect(calls).toEqual([
    expect.objectContaining({
      status: "error",
      error: failure,
      result: { reason: "worker.shutdown" },
    }),
  ])
})
