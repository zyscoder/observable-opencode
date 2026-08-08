import { expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { CaseTrace } from "@/observability/case-trace"
import {
  captureInteractiveTurnOutcome,
  createInteractiveTraceOutcomeTracker,
  finishInteractiveTraceSessions,
  finishReplacedTraceSession,
  replaceInteractiveTraceSession,
} from "@/cli/cmd/run/runtime"
import { finishRunTraces } from "@/cli/cmd/run"
import * as RunCommand from "@/cli/cmd/run"

test("preserves a caught transport rejection as a trace-only error outcome", async () => {
  const reported: unknown[] = []
  const failure = new Error("transport rejected")

  const outcome = await captureInteractiveTurnOutcome({
    run: async () => {
      throw failure
    },
    cancelled: () => false,
    report: async (error) => {
      reported.push(error)
    },
  })

  expect(outcome).toEqual({ status: "error", error: failure })
  expect(reported).toEqual([failure])
})

test("clears a session turn failure after a later successful turn", () => {
  const tracker = createInteractiveTraceOutcomeTracker()
  const failure = new Error("first turn failed")

  tracker.record("ses_a", { status: "error", error: failure })
  expect(tracker.failure("ses_a")).toBe(failure)

  tracker.record("ses_a", { status: "success" })
  expect(tracker.failure("ses_a")).toBeUndefined()
})

test("does not finalize the old root when replacement creation fails", async () => {
  const finished: string[] = []
  await expect(
    replaceInteractiveTraceSession({
      previousSessionID: "ses_a",
      create: async () => {
        throw new Error("create failed")
      },
      trace: {
        finishSession: (sessionID) => finished.push(sessionID),
      },
    }),
  ).rejects.toThrow("create failed")

  expect(finished).toEqual([])
})

test("finalizes the old root only after the replacement is prepared for switching", async () => {
  const order: string[] = []
  await replaceInteractiveTraceSession({
    previousSessionID: "ses_a",
    create: async () => {
      order.push("create")
      return "ses_b"
    },
    prepare: async () => {
      order.push("prepare")
    },
    trace: {
      finishSession: () => order.push("finish"),
    },
  })

  expect(order).toEqual(["create", "prepare", "finish"])
})

test("does not propagate the active root failure into process fallback result", () => {
  const all: Array<Record<string, unknown> | undefined> = []
  finishRunTraces(
    { sessionID: "ses_b", failure: new Error("B failed") },
    {
      finishSession: () => {},
      finishAll: (input) => all.push(input),
    },
  )

  expect(all).toEqual([
    {
      status: "success",
      result: {
        exit_code: process.exitCode ?? 0,
        reason: "run.closed",
      },
    },
  ])
})

test("finalizes the process trace as error when startup fails before a session exists", () => {
  const all: Array<Record<string, unknown> | undefined> = []
  const failure = new Error("Session not found")
  finishRunTraces(
    { failure },
    {
      finishSession: () => {},
      finishAll: (input) => all.push(input),
    },
  )

  expect(all).toEqual([
    {
      status: "error",
      error: failure,
      result: {
        exit_code: process.exitCode ?? 0,
        reason: "run.closed",
      },
    },
  ])
})

test("closes and finalizes a failed startup trace before preserving exit code one", () => {
  const exitRunWithTrace = (
    RunCommand as unknown as {
      exitRunWithTrace?: (input: {
        failure: unknown
        closeRunSpan: (failure?: unknown, exitCode?: number) => void
        trace?: {
          finishSession: (sessionID: string, input?: Record<string, unknown>) => void
          finishAll: (input?: Record<string, unknown>) => void
        }
        exit: (code: number) => never
      }) => never
    }
  ).exitRunWithTrace
  expect(exitRunWithTrace).toBeFunction()
  if (!exitRunWithTrace) return

  const order: string[] = []
  const failure = new Error("Session not found")
  const exitSentinel = new Error("exit called")
  expect(() =>
    exitRunWithTrace({
      failure,
      closeRunSpan: (received, exitCode) => {
        expect(received).toBe(failure)
        expect(exitCode).toBe(1)
        order.push("close")
      },
      trace: {
        finishSession: () => {},
        finishAll: (input) => {
          const result = input?.result as Record<string, unknown> | undefined
          order.push(`finish:${input?.status}:${result?.exit_code}`)
        },
      },
      exit: (code) => {
        order.push(`exit:${code}`)
        throw exitSentinel
      },
    }),
  ).toThrow(exitSentinel)

  expect(order).toEqual(["close", "finish:error:1", "exit:1"])
})

test("persists exit code one when session startup exits before finally", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-runtime-startup-exit-"))
  const packageDir = path.resolve(import.meta.dir, "../../..")
  const script = path.join(dir, "runtime-startup-exit.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
  const runModule = pathToFileURL(path.join(packageDir, "src/cli/cmd/run.ts")).href

  await fs.writeFile(
    script,
    [
      `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
      `import { exitRunWithTrace } from ${JSON.stringify(runModule)}`,
      `CaseTrace.configure({ caseID: "startup-exit" })`,
      `const run = CaseTrace.startSpan({ component: "run", operation: "execute", trace_scope: "process" })`,
      `const failure = new Error("Session not found")`,
      `exitRunWithTrace({`,
      `  failure,`,
      `  closeRunSpan: (error, exitCode) => run?.end({ status: "error", error, output: { exit_code: exitCode } }),`,
      `})`,
    ].join("\n"),
  )

  const proc = Bun.spawn([process.execPath, script], {
    cwd: packageDir,
    env: {
      ...process.env,
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_CASE_TRACE_DIR: dir,
    },
    stdout: "pipe",
    stderr: "pipe",
  })
  expect(await proc.exited).toBe(1)

  const traces = await Promise.all(
    (await fs.readdir(dir, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map(async (entry) => ({
        trace: JSON.parse(await fs.readFile(path.join(dir, entry.name, "trace.json"), "utf8")) as any,
        legacy: JSON.parse(await fs.readFile(path.join(dir, entry.name, "legacy-trace.json"), "utf8")) as any,
      })),
  )
  expect(traces.length).toBeGreaterThan(0)
  for (const item of traces) {
    expect(item.trace.manifest).toMatchObject({
      status: "error",
      case_status: "error",
      result: expect.objectContaining({ exit_code: 1, reason: "run.closed" }),
    })
  }
  const processTrace = traces.find((item) => item.legacy.spans.length > 0)!
  const runSpan = processTrace.legacy.spans.find(
    (span: any) => span.component === "run" && span.operation === "execute",
  )
  expect(runSpan).toMatchObject({ status: "error" })
  expect(runSpan.output_summary.preview).toContain('"exit_code":1')
  expect(runSpan.metadata?.finalized_status).toBeUndefined()
})

test("finalizes a replaced interactive root independently from a later failed root", () => {
  const finished: Array<{ sessionID: string; input: Record<string, unknown> | undefined }> = []
  const all: Array<Record<string, unknown> | undefined> = []
  const trace = {
    finishSession: (sessionID: string, input?: Record<string, unknown>) => {
      finished.push({ sessionID, input })
    },
    finishAll: (input?: Record<string, unknown>) => all.push(input),
  }

  finishReplacedTraceSession("ses_a", undefined, trace)
  finishInteractiveTraceSessions(
    {
      sessionID: "ses_b",
      error: new Error("B failed"),
    },
    trace,
  )

  expect(finished).toEqual([
    {
      sessionID: "ses_a",
      input: {
        status: "success",
        result: { reason: "session.replaced" },
      },
    },
    {
      sessionID: "ses_b",
      input: expect.objectContaining({
        status: "error",
        error: expect.any(Error),
      }),
    },
  ])
  expect(all).toEqual([
    {
      status: "success",
      result: { reason: "interactive.runtime.closed" },
    },
  ])
})

test("closes the outer run span before interactive trace finalization preserves its terminal output", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-runtime-trace-finalizer-order-"))
  const packageDir = path.resolve(import.meta.dir, "../../..")
  const script = path.join(dir, "runtime-trace-finalizer-order.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
  const runtimeModule = pathToFileURL(path.join(packageDir, "src/cli/cmd/run/runtime.ts")).href

  await fs.writeFile(
    script,
    [
      `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
      `import { finishInteractiveTraceSessions } from ${JSON.stringify(runtimeModule)}`,
      `CaseTrace.configure({ caseID: "interactive-finalizer-order" })`,
      `CaseTrace.setSessionID("ses_final")`,
      `const run = CaseTrace.startSpan({ component: "run", operation: "execute", name: "interactive" })`,
      `let finalized = false`,
      `finishInteractiveTraceSessions({`,
      `  sessionID: "ses_final",`,
      `  beforeTraceFinalize: ({ failure }) => {`,
      `    finalized = true`,
      `    run?.end({ status: failure ? "error" : "success", output: { exit_code: 23, terminal: "outer-run-complete" } })`,
      `  },`,
      `})`,
      `if (!finalized) process.exitCode = 2`,
    ].join("\n"),
  )

  const proc = Bun.spawn([process.execPath, script], {
    cwd: packageDir,
    env: {
      ...process.env,
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_CASE_TRACE_DIR: dir,
    },
    stdout: "pipe",
    stderr: "pipe",
  })
  expect(await proc.exited).toBe(0)

  const traces = await Promise.all(
    (await fs.readdir(dir, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map(async (entry) =>
        JSON.parse(await fs.readFile(path.join(dir, entry.name, "legacy-trace.json"), "utf8")) as any,
      ),
  )
  const trace = traces.find((item) => item.session_id === "ses_final")
  expect(trace).toBeDefined()
  const runSpan = trace!.spans.find((span: any) => span.component === "run" && span.operation === "execute")
  expect(runSpan).toMatchObject({ status: "success" })
  expect(runSpan.output_summary.preview).toContain('"exit_code":23')
  expect(runSpan.output_summary.preview).toContain('"terminal":"outer-run-complete"')
  expect(runSpan.metadata?.finalized_status).toBeUndefined()
})

test("keeps an interactive outer run span in the process trace across root replacement", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-runtime-process-run-span-"))
  const packageDir = path.resolve(import.meta.dir, "../../..")
  const script = path.join(dir, "runtime-process-run-span.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

  await fs.writeFile(
    script,
    [
      `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
      `CaseTrace.configure({ caseID: "interactive-process-run" })`,
      `const run = CaseTrace.startSpan({ component: "run", operation: "execute", trace_scope: "process" })`,
      `CaseTrace.setSessionID("ses_a")`,
      `CaseTrace.event({ component: "runtime", event_type: "root.a", data: { sessionID: "ses_a" } })`,
      `CaseTrace.finishSession("ses_a", { status: "success", result: { reason: "session.replaced" } })`,
      `CaseTrace.setSessionID("ses_b")`,
      `CaseTrace.event({ component: "runtime", event_type: "root.b", data: { sessionID: "ses_b" } })`,
      `run?.end({ status: "success", output: { exit_code: 29, terminal: "outer-run-complete" } })`,
      `CaseTrace.finishSession("ses_b", { status: "success", result: { reason: "interactive.runtime.closed" } })`,
      `CaseTrace.finishAll({ status: "success", result: { reason: "run.closed" } })`,
    ].join("\n"),
  )

  const proc = Bun.spawn([process.execPath, script], {
    cwd: packageDir,
    env: {
      ...process.env,
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_CASE_TRACE_DIR: dir,
    },
    stdout: "pipe",
    stderr: "pipe",
  })
  expect(await proc.exited).toBe(0)

  const traces = await Promise.all(
    (await fs.readdir(dir, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map(async (entry) => ({
        directory: entry.name,
        trace: JSON.parse(await fs.readFile(path.join(dir, entry.name, "trace.json"), "utf8")) as any,
        legacy: JSON.parse(await fs.readFile(path.join(dir, entry.name, "legacy-trace.json"), "utf8")) as any,
      })),
  )
  expect(traces).toHaveLength(3)

  const rootA = traces.find((item) => item.trace.manifest.session_id === "ses_a")!
  const rootB = traces.find((item) => item.trace.manifest.session_id === "ses_b")!
  const processTrace = traces.find((item) => item.trace.manifest.session_id === undefined)!
  expect(rootA.directory).toBe("interactive-process-run")
  expect(rootA.trace.manifest).toMatchObject({ status: "success", case_status: "success" })
  expect(rootB.trace.manifest).toMatchObject({ status: "success", case_status: "success" })

  const runSpan = processTrace.legacy.spans.find(
    (span: any) => span.component === "run" && span.operation === "execute",
  )
  expect(runSpan).toMatchObject({ status: "success" })
  expect(runSpan.output_summary.preview).toContain('"exit_code":29')
  expect(runSpan.output_summary.preview).toContain('"terminal":"outer-run-complete"')
  expect(runSpan.metadata?.finalized_status).toBeUndefined()
})

test("writes independent real traces for A success, replacement, B transport failure, and process success", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-runtime-trace-outcomes-"))
  const packageDir = path.resolve(import.meta.dir, "../../..")
  const script = path.join(dir, "runtime-trace-outcomes.ts")
  const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
  const runtimeModule = pathToFileURL(path.join(packageDir, "src/cli/cmd/run/runtime.ts")).href
  const queueModule = pathToFileURL(path.join(packageDir, "src/cli/cmd/run/runtime.queue.ts")).href

  await fs.writeFile(
    script,
    [
      `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
      `import { captureInteractiveTurnOutcome, createInteractiveTraceOutcomeTracker, finishInteractiveTraceSessions, replaceInteractiveTraceSession } from ${JSON.stringify(runtimeModule)}`,
      `import { runPromptQueue } from ${JSON.stringify(queueModule)}`,
      `const prompts = new Set()`,
      `const closes = new Set()`,
      `let closed = false`,
      `const footer = {`,
      `  get isClosed() { return closed },`,
      `  onPrompt(fn) { prompts.add(fn); return () => prompts.delete(fn) },`,
      `  onClose(fn) { closes.add(fn); return () => closes.delete(fn) },`,
      `  event() {}, append() {}, idle() { return Promise.resolve() },`,
      `  close() { if (closed) return; closed = true; for (const fn of [...closes]) fn() },`,
      `  destroy() { this.close(); prompts.clear(); closes.clear() },`,
      `}`,
      `const submit = (text) => { for (const fn of [...prompts]) fn({ text, parts: [] }) }`,
      `CaseTrace.configure({ caseID: "interactive-real-outcomes" })`,
      `let sessionID = "ses_a"`,
      `CaseTrace.setSessionID(sessionID)`,
      `const outcomes = createInteractiveTraceOutcomeTracker()`,
      `let resolveB`,
      `const bRecorded = new Promise((resolve) => { resolveB = resolve })`,
      `const task = runPromptQueue({`,
      `  footer,`,
      `  sessionID: () => sessionID,`,
      `  onNewSession: async () => {`,
      `    const previousSessionID = sessionID`,
      `    const created = await replaceInteractiveTraceSession({`,
      `      previousSessionID,`,
      `      previousError: outcomes.failure(previousSessionID),`,
      `      create: async () => "ses_b",`,
      `    })`,
      `    outcomes.clear(previousSessionID)`,
      `    sessionID = created`,
      `    CaseTrace.setSessionID(sessionID)`,
      `    CaseTrace.event({ component: "runtime", event_type: "process.marker", data: { marker: "process" } })`,
      `  },`,
      `  onTraceOutcome: ({ sessionID: owner, outcome }) => {`,
      `    outcomes.record(owner, outcome)`,
      `    if (owner === "ses_b" && outcome.status === "error") resolveB()`,
      `  },`,
      `  run: async (prompt) => captureInteractiveTurnOutcome({`,
      `    run: async () => { if (prompt.text === "B") throw new Error("B transport failed") },`,
      `    cancelled: () => false,`,
      `    report: async () => {},`,
      `  }),`,
      `})`,
      `submit("A")`,
      `submit("/new")`,
      `submit("B")`,
      `await bRecorded`,
      `await Promise.resolve()`,
      `footer.close()`,
      `await task`,
      `finishInteractiveTraceSessions({ sessionID, error: outcomes.failure(sessionID) })`,
      `finishInteractiveTraceSessions({ sessionID, error: outcomes.failure(sessionID) })`,
    ].join("\n"),
  )

  const proc = Bun.spawn([process.execPath, script], {
    cwd: packageDir,
    env: {
      ...process.env,
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_CASE_TRACE_DIR: dir,
    },
    stdout: "pipe",
    stderr: "pipe",
  })
  expect(await proc.exited).toBe(0)

  const directories = (await fs.readdir(dir, { withFileTypes: true }))
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
  expect(directories).toHaveLength(3)

  const traces = await Promise.all(
    directories.map(async (directory) => ({
      directory,
      trace: JSON.parse(await fs.readFile(path.join(dir, directory, "trace.json"), "utf8")) as any,
    })),
  )
  const rootA = traces.find((item) => item.trace.manifest.session_id === "ses_a")!
  const rootB = traces.find((item) => item.trace.manifest.session_id === "ses_b")!
  const processTrace = traces.find((item) => item.trace.manifest.session_id === undefined)!

  expect(rootA.trace.manifest).toMatchObject({ status: "success", case_status: "success" })
  expect(rootB.trace.manifest).toMatchObject({ status: "error", case_status: "error" })
  expect(processTrace.trace.manifest).toMatchObject({ status: "success", case_status: "success" })
  expect(rootB.trace.records).toContainEqual(
    expect.objectContaining({
      component: "runtime",
      event_type: "task.loop",
      status: "error",
      data: expect.objectContaining({ operation: "turn" }),
    }),
  )
})
