import { expect, test } from "bun:test"
import fs from "node:fs"
import path from "node:path"
import { tmpdir } from "../fixture/fixture"

const childMode = process.env.OPENCODE_CASE_TRACE_TERMINAL_MEMORY_CHILD === "1"

if (childMode) {
  test("terminal memory measurement child", async () => {
    process.env.OPENCODE_CASE_TRACE = "1"
    const { CaseTrace } = await import("@/observability/case-trace")
    const traceDir = process.env.OPENCODE_CASE_TRACE_TERMINAL_MEMORY_DIR!
    CaseTrace.configure({ caseID: "terminal-memory", traceDir })

    for (let index = 0; index < 10_000; index += 1) {
      CaseTrace.responseOutput({
        text: `terminal response ${index}`,
        response_role: "intermediate",
        visibility: "internal",
        is_final_for_case: false,
        finality_source: "inferred",
      })
    }
    Bun.gc(true)
    const beforeCloseRSS = process.memoryUsage().rss
    const request = CaseTrace.closeAll({ status: "success", result: { reason: "terminal.memory" } })[0]!
    Bun.gc(true)
    const afterCloseRSS = process.memoryUsage().rss
    const operations = fs
      .readFileSync(request.recordsFile, "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line).operation)
    process.stdout.write(
      `\nCASE_TRACE_TERMINAL_MEMORY ${JSON.stringify({
        beforeCloseRSS,
        afterCloseRSS,
        lastOperation: operations.at(-1),
        finalizedOperations: operations.filter((operation) => operation === "case.finalized").length,
      })}\n`,
    )
  })
} else {
  test("10,000 records close journal-only within the terminal RSS budget", async () => {
    await using tmp = await tmpdir()
    const traceDir = path.join(tmp.path, "trace")
    fs.mkdirSync(traceDir, { recursive: true })
    const child = Bun.spawn(
      [
        process.execPath,
        "--expose-gc",
        "test",
        "test/observability/case-trace-terminal-memory.test.ts",
        "--timeout",
        "120000",
      ],
      {
        cwd: path.join(import.meta.dir, "../.."),
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE_TERMINAL_MEMORY_CHILD: "1",
          OPENCODE_CASE_TRACE_TERMINAL_MEMORY_DIR: traceDir,
        },
        stdout: "pipe",
        stderr: "pipe",
      },
    )
    const [exitCode, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ])
    expect(exitCode, stderr).toBe(0)
    const match = stdout.match(/CASE_TRACE_TERMINAL_MEMORY (\{[^\n]+\})/)
    expect(match, stdout).not.toBeNull()
    const measurement = JSON.parse(match![1]!) as {
      beforeCloseRSS: number
      afterCloseRSS: number
      lastOperation: string
      finalizedOperations: number
    }
    const growthRSS = measurement.afterCloseRSS - measurement.beforeCloseRSS
    console.info("case-trace-terminal-memory-rss", JSON.stringify({ ...measurement, growthRSS }))
    expect(measurement.lastOperation).toBe("case.runtime_closed")
    expect(measurement.finalizedOperations).toBe(0)
    expect(growthRSS).toBeLessThanOrEqual(32 * 1024 * 1024)
  }, 120_000)
}
