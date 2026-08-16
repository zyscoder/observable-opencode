import { expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

const packageDir = path.resolve(import.meta.dir, "../../..")
const fixture = path.join(import.meta.dir, "fixture", "task-7-tui-process.ts")

async function waitForFile(file: string) {
  for (let attempt = 0; attempt < 400; attempt += 1) {
    try {
      await fs.access(file)
      return
    } catch {
      await Bun.sleep(10)
    }
  }
  throw new Error(`timed out waiting for ${file}`)
}

test(
  "real TUI parent process materializes trace.json after normal exit, SIGINT, and SIGTERM",
  async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-task-7-tui-process-"))
    try {
      for (const scenario of [
        { mode: "normal", signal: undefined, exitCode: 0 },
        { mode: "sigint", signal: "SIGINT" as const, exitCode: 130 },
        { mode: "sigterm", signal: "SIGTERM" as const, exitCode: 143 },
      ]) {
        const caseID = `task-7-tui-${scenario.mode}`
        const readyFile = path.join(root, `${scenario.mode}.ready`)
        const child = Bun.spawn([process.execPath, fixture, scenario.mode], {
          cwd: packageDir,
          env: {
            ...process.env,
            OPENCODE_CASE_TRACE: "1",
            OPENCODE_CASE_TRACE_DIR: root,
            OPENCODE_CASE_ID: caseID,
            OPENCODE_CASE_TRACE_QUIET: "",
            TASK7_TUI_READY_FILE: readyFile,
          },
          stdout: "pipe",
          stderr: "pipe",
        })
        try {
          await waitForFile(readyFile)
          if (scenario.signal) child.kill(scenario.signal)
          const exitCode = await child.exited
          const stderr = await new Response(child.stderr).text()
          const caseDir = path.join(root, caseID)
          const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
          const session = JSON.parse(await fs.readFile(path.join(caseDir, "session.json"), "utf8")) as any

          expect(exitCode).toBe(scenario.exitCode)
          expect(stderr).toContain("[observable-opencode] Session trace saved")
          expect(stderr).toContain(`  directory: ${caseDir}`)
          expect(stderr).toContain(`  json: ${path.join(caseDir, "trace.json")}`)
          expect(session.segments).toHaveLength(1)
          expect(session.segments[0].status).toBe("completed")
          expect(trace.manifest.status).toBe("success")
          expect(trace.records.some((record: any) => record.data?.marker === scenario.mode)).toBe(true)
        } finally {
          try {
            child.kill("SIGKILL")
          } catch {}
          await child.exited.catch(() => undefined)
        }
      }
    } finally {
      await fs.rm(root, { recursive: true, force: true })
    }
  },
  60_000,
)
