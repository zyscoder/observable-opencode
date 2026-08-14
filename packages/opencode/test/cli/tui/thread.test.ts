import { describe, expect, test } from "bun:test"
import fs from "fs/promises"
import path from "path"
import { tmpdir } from "../../fixture/fixture"
import {
  finalizeTuiWorker,
  resolveThreadDirectory,
  resolveTuiWorkerShutdownTimeout,
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

  test("gives traced worker shutdown enough time to finalize after the legacy five-second boundary", async () => {
    let finalized = false
    const shutdown = Bun.sleep(5_100).then(() => {
      finalized = true
    })

    await waitForTuiWorkerShutdown(shutdown, { OPENCODE_CASE_TRACE: "1" })

    expect(finalized).toBe(true)
  }, 10_000)

  test("honors an explicit TUI shutdown timeout", async () => {
    const env = {
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_TUI_SHUTDOWN_TIMEOUT_MS: "10",
    }

    expect(resolveTuiWorkerShutdownTimeout(env)).toBe(10)
    await expect(waitForTuiWorkerShutdown(Bun.sleep(100), env)).rejects.toThrow(
      "TUI worker shutdown timed out after 10ms",
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

    for (const exit of ["normal", "ctrl-c"]) {
      const calls: string[] = []
      await finalizeTuiWorker({
        shutdown: async () => {
          calls.push(`${exit}:close`)
          return [request]
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
        `${exit}:terminate`,
        `${exit}:materialize:1`,
        `${exit}:publish`,
      ])
    }
  })

  test("keeps the TUI shutdown result when trace materialization fails", async () => {
    const calls: string[] = []
    await expect(
      finalizeTuiWorker({
        shutdown: async () => [{ caseDir: "/tmp/case", caseID: "case", runID: "run", recordsFile: "/tmp/case/records.jsonl" }],
        terminate: () => {
          calls.push("terminate")
        },
        materialize: async () => {
          throw new Error("materializer failed")
        },
        publish: () => {
          calls.push("publish")
        },
      }),
    ).resolves.toBeUndefined()
    expect(calls).toEqual(["terminate"])
  })
})
