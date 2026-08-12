import { describe, expect, test } from "bun:test"
import fs from "fs/promises"
import path from "path"
import { tmpdir } from "../../fixture/fixture"
import {
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
})
