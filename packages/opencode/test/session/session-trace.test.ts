import { expect, test } from "bun:test"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { existsSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { finalizeRemovedRootSessionTrace } from "@/session/session"

test("finalizes the trace only after a root session has been removed", () => {
  const calls: unknown[] = []
  finalizeRemovedRootSessionTrace(
    { id: "ses_root" as any },
    {
      finishSession(sessionID, input) {
        calls.push({ sessionID, input })
      },
    },
  )

  expect(calls).toEqual([
    {
      sessionID: "ses_root",
      input: { status: "success", result: { reason: "session.deleted" } },
    },
  ])
})

test("does not finalize a parent trace when a child session is removed", () => {
  let calls = 0
  finalizeRemovedRootSessionTrace(
    { id: "ses_child" as any, parentID: "ses_root" as any },
    {
      finishSession() {
        calls++
      },
    },
  )
  expect(calls).toBe(0)
})

test("does not expose trace finalization errors during successful removal", () => {
  expect(() =>
    finalizeRemovedRootSessionTrace(
      { id: "ses_root" as any },
      {
        finishSession() {
          throw new Error("trace write failed")
        },
      },
    ),
  ).not.toThrow()
})

test("real session removal finalizes only after the root is successfully removed", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "opencode-session-remove-trace-"))
  const packageDir = path.resolve(import.meta.dir, "../..")
  const script = path.join(dir, "session-remove-trace.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
  const sessionModule = pathToFileURL(path.join(packageDir, "src/session/session.ts")).href
  const instanceModule = pathToFileURL(path.join(packageDir, "src/project/with-instance.ts")).href
  const runtimeModule = pathToFileURL(path.join(packageDir, "src/effect/app-runtime.ts")).href

  try {
    await writeFile(
      script,
      [
        `import { existsSync } from "node:fs"`,
        `import path from "node:path"`,
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `import { Session } from ${JSON.stringify(sessionModule)}`,
        `import { WithInstance } from ${JSON.stringify(instanceModule)}`,
        `import { AppRuntime } from ${JSON.stringify(runtimeModule)}`,
        `const run = (effect) => AppRuntime.runPromise(Session.Service.use((service) => effect(service)))`,
        `CaseTrace.configure({ caseID: "session-root-remove" })`,
        `await WithInstance.provide({ directory: ${JSON.stringify(packageDir)}, fn: async () => {`,
        `  const root = await run((service) => service.create({ title: "trace root" }))`,
        `  CaseTrace.setSessionID(root.id)`,
        `  const child = await run((service) => service.create({ parentID: root.id, title: "trace child" }))`,
        `  await run((service) => service.remove(child.id))`,
        `  if (existsSync(path.join(${JSON.stringify(dir)}, "session-root-remove", "trace.json"))) throw new Error("child removal finalized root trace")`,
        `  await run((service) => service.remove(root.id))`,
        `  if (!existsSync(path.join(${JSON.stringify(dir)}, "session-root-remove", "trace.json"))) throw new Error("root removal did not finalize trace")`,
        `} })`,
        `process.exit(0)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_QUIET: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const exitCode = await proc.exited
    const stderr = await new Response(proc.stderr).text()
    if (exitCode !== 0) throw new Error(stderr)
    expect(existsSync(path.join(dir, "session-root-remove", "trace.json"))).toBe(true)
    const trace = JSON.parse(await readFile(path.join(dir, "session-root-remove", "trace.json"), "utf8")) as any
    expect(trace.manifest).toMatchObject({ status: "success", result: { reason: "session.deleted" } })
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test("a failed real session removal does not finalize its trace", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "opencode-session-remove-failure-trace-"))
  const packageDir = path.resolve(import.meta.dir, "../..")
  const script = path.join(dir, "session-remove-failure-trace.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
  const sessionModule = pathToFileURL(path.join(packageDir, "src/session/session.ts")).href
  const instanceModule = pathToFileURL(path.join(packageDir, "src/project/with-instance.ts")).href
  const runtimeModule = pathToFileURL(path.join(packageDir, "src/effect/app-runtime.ts")).href

  try {
    await writeFile(
      script,
      [
        `import { existsSync } from "node:fs"`,
        `import path from "node:path"`,
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `import { Session } from ${JSON.stringify(sessionModule)}`,
        `import { WithInstance } from ${JSON.stringify(instanceModule)}`,
        `import { AppRuntime } from ${JSON.stringify(runtimeModule)}`,
        `const run = (effect) => AppRuntime.runPromise(Session.Service.use((service) => effect(service)))`,
        `CaseTrace.configure({ caseID: "session-remove-failure" })`,
        `CaseTrace.setSessionID("ses_missing")`,
        `let failed = false`,
        `await WithInstance.provide({ directory: ${JSON.stringify(packageDir)}, fn: async () => {`,
        `  try { await run((service) => service.remove("ses_missing")) } catch { failed = true }`,
        `} })`,
        `if (!failed) throw new Error("missing session removal unexpectedly succeeded")`,
        `console.log(JSON.stringify({ finalizedBeforeExit: existsSync(path.join(${JSON.stringify(dir)}, "session-remove-failure", "trace.json")) }))`,
        `process.exit(0)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_QUIET: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const exitCode = await proc.exited
    const stdout = await new Response(proc.stdout).text()
    const stderr = await new Response(proc.stderr).text()
    if (exitCode !== 0) throw new Error(stderr)
    expect(JSON.parse(stdout.trim())).toEqual({ finalizedBeforeExit: false })
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})
