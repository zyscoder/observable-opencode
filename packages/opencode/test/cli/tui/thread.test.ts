import { describe, expect, test } from "bun:test"
import fs from "fs/promises"
import path from "path"
import { tmpdir } from "../../fixture/fixture"
import {
  finalizeTuiWorker,
  resolveThreadDirectory,
  resolveTuiWorkerTraceCloseTimeout,
  resolveTuiWorkerShutdownTimeout,
  waitForTuiWorkerTraceClose,
  waitForTuiWorkerShutdown,
} from "../../../src/cli/cmd/tui/thread"

describe("tui thread", () => {
  async function check(project?: string) {
    await using tmp = await tmpdir({ git: true })
    const link = path.join(path.dirname(tmp.path), path.basename(tmp.path) + "-link")
    const type = process.platform === "win32" ? "junction" : "dir"

    try {
      await fs.symlink(tmp.path, link, type)
      expect(resolveThreadDirectory(project, link, tmp.path)).toBe(tmp.path)
    } finally {
      await fs.rm(link, { recursive: true, force: true }).catch(() => undefined)
    }
  }

  test("uses the real cwd when PWD points at a symlink", async () => {
    await check()
  })

  test("uses the real cwd after resolving a relative project from PWD", async () => {
    await check(".")
  })

  test("gives only traced journal close enough time to finalize after the legacy five-second boundary", async () => {
    let finalized = false
    const shutdown = Bun.sleep(5_100).then(() => {
      finalized = true
    })

    await waitForTuiWorkerTraceClose(shutdown, { OPENCODE_CASE_TRACE: "1" })

    expect(finalized).toBe(true)
  }, 10_000)

  test("keeps ordinary worker shutdown timeout separate from the trace close timeout", async () => {
    const env = {
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_TUI_SHUTDOWN_TIMEOUT_MS: "10",
    }

    expect(resolveTuiWorkerShutdownTimeout(env)).toBe(5_000)
    expect(resolveTuiWorkerTraceCloseTimeout(env)).toBe(10)
    await expect(waitForTuiWorkerTraceClose(Bun.sleep(100), env)).rejects.toThrow(
      "TUI worker trace close timed out after 10ms",
    )
  })

  test("closes and terminates the worker before materializing and publishing traces for normal and Ctrl-C exits", async () => {
    const request = { caseDir: "/tmp/case", caseID: "case", runID: "run", recordsFile: "/tmp/case/records.jsonl" }
    const publication = {
      caseDir: "/tmp/case",
      caseID: "case",
      status: "completed" as const,
      traceFile: "/tmp/case/trace.json",
    }

    for (const exit of ["normal", "ctrl-c"] as const) {
      const calls: string[] = []
      await finalizeTuiWorker({
        ...(exit === "ctrl-c" ? { signal: "SIGINT" as const } : {}),
        shutdown: async () => {
          calls.push(`${exit}:close`)
          return {}
        },
        closeTraces: async (input) => {
          calls.push(`${exit}:journal-close:${input?.signal ?? "none"}`)
          return { requests: [request] }
        },
        terminate: () => {
          calls.push(`${exit}:terminate`)
        },
        materialize: async (requests) => {
          calls.push(`${exit}:materialize:${requests.length}`)
          return [publication]
        },
        publish: () => {
          calls.push(`${exit}:publish`)
        },
      })

      expect(calls).toEqual([
        `${exit}:close`,
        `${exit}:journal-close:${exit === "ctrl-c" ? "SIGINT" : "none"}`,
        `${exit}:terminate`,
        `${exit}:materialize:1`,
        `${exit}:publish`,
      ])
    }
  })

  test("materializes requests returned with a runtime shutdown failure", async () => {
    const calls: string[] = []
    await finalizeTuiWorker({
      shutdown: async () => ({ failure: "server stop failed" }),
      closeTraces: async () => ({
        requests: [{ caseDir: "/tmp/case", caseID: "case", runID: "run", recordsFile: "/tmp/case/records.jsonl" }],
        failure: "server stop failed",
      }),
      terminate: () => {
        calls.push("terminate")
      },
      materialize: async (requests) => {
        calls.push(`materialize:${requests.length}`)
        return []
      },
      publish: () => {},
      onShutdownFailure: () => {
        calls.push("shutdown-warning")
      },
    })
    expect(calls).toEqual(["shutdown-warning", "terminate", "materialize:1"])
  })

  test("continues trace materialization after worker termination rejects", async () => {
    const calls: string[] = []
    await expect(
      finalizeTuiWorker({
        shutdown: async () => ({}),
        closeTraces: async () => ({
          requests: [{ caseDir: "/tmp/case", caseID: "case", runID: "run", recordsFile: "/tmp/case/records.jsonl" }],
        }),
        terminate: async () => {
          calls.push("terminate")
          throw new Error("terminate failed")
        },
        materialize: async (requests) => {
          calls.push(`materialize:${requests.length}`)
          return []
        },
        publish: () => {},
        onTerminateFailure: () => {
          calls.push("terminate-warning")
        },
      }),
    ).resolves.toBeUndefined()
    expect(calls).toEqual(["terminate", "terminate-warning", "materialize:1"])
  })
})
