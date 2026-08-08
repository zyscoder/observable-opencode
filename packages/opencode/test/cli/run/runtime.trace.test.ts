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
  const originalFinishSession = CaseTrace.finishSession
  ;(CaseTrace as unknown as { finishSession: (sessionID: string) => void }).finishSession = (sessionID) => {
    finished.push(sessionID)
  }

  try {
    await expect(
      replaceInteractiveTraceSession({
        previousSessionID: "ses_a",
        create: async () => {
          throw new Error("create failed")
        },
      }),
    ).rejects.toThrow("create failed")
  } finally {
    ;(CaseTrace as unknown as { finishSession: typeof CaseTrace.finishSession }).finishSession = originalFinishSession
  }

  expect(finished).toEqual([])
})

test("finalizes the old root only after the replacement is prepared for switching", async () => {
  const order: string[] = []
  const originalFinishSession = CaseTrace.finishSession
  ;(CaseTrace as unknown as { finishSession: () => void }).finishSession = () => {
    order.push("finish")
  }

  try {
    await replaceInteractiveTraceSession({
      previousSessionID: "ses_a",
      create: async () => {
        order.push("create")
        return "ses_b"
      },
      prepare: async () => {
        order.push("prepare")
      },
    })
  } finally {
    ;(CaseTrace as unknown as { finishSession: typeof CaseTrace.finishSession }).finishSession = originalFinishSession
  }

  expect(order).toEqual(["create", "prepare", "finish"])
})

test("does not propagate the active root failure into process fallback result", () => {
  const all: Array<Record<string, unknown> | undefined> = []
  const originalFinishSession = CaseTrace.finishSession
  const originalFinishAll = CaseTrace.finishAll
  ;(CaseTrace as unknown as { finishSession: () => void }).finishSession = () => {}
  ;(CaseTrace as unknown as { finishAll: (input?: Record<string, unknown>) => void }).finishAll = (input) => {
    all.push(input)
  }

  try {
    finishRunTraces({ sessionID: "ses_b", failure: new Error("B failed") })
  } finally {
    ;(CaseTrace as unknown as { finishSession: typeof CaseTrace.finishSession }).finishSession = originalFinishSession
    ;(CaseTrace as unknown as { finishAll: typeof CaseTrace.finishAll }).finishAll = originalFinishAll
  }

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

test("finalizes a replaced interactive root independently from a later failed root", () => {
  const finished: Array<{ sessionID: string; input: Record<string, unknown> | undefined }> = []
  const all: Array<Record<string, unknown> | undefined> = []
  const originalFinishSession = CaseTrace.finishSession
  const originalFinishAll = CaseTrace.finishAll

  ;(
    CaseTrace as unknown as {
      finishSession: (sessionID: string, input?: Record<string, unknown>) => void
    }
  ).finishSession = (sessionID, input) => {
    finished.push({ sessionID, input })
  }
  ;(CaseTrace as unknown as { finishAll: (input?: Record<string, unknown>) => void }).finishAll = (input) => {
    all.push(input)
  }

  try {
    finishReplacedTraceSession("ses_a")
    finishInteractiveTraceSessions({
      sessionID: "ses_b",
      error: new Error("B failed"),
    })
  } finally {
    ;(CaseTrace as unknown as { finishSession: typeof CaseTrace.finishSession }).finishSession = originalFinishSession
    ;(CaseTrace as unknown as { finishAll: typeof CaseTrace.finishAll }).finishAll = originalFinishAll
  }

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
