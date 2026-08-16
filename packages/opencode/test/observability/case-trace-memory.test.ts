import { expect, test } from "bun:test"
import fs from "node:fs"
import path from "node:path"
import { tmpdir } from "../fixture/fixture"

const childMode = process.env.OPENCODE_CASE_TRACE_MEMORY_CHILD === "1"

if (childMode) {
  test("memory measurement child", async () => {
    process.env.OPENCODE_CASE_TRACE = "1"
    process.env.OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH = "20000"
    const { CaseTrace } = await import("@/observability/case-trace")
    const traceDir = process.env.OPENCODE_CASE_TRACE_MEMORY_DIR!
    CaseTrace.configure({ caseID: "memory-acceptance", traceDir })

    const record = (index: number) => {
      CaseTrace.observation({
        source: "runtime",
        category: "memory_acceptance",
        summary: `observation ${index}`,
        data: {
          sequence: index,
          detail: `${index}:${"x".repeat(14_000)}`,
        },
      })
    }
    for (let index = 0; index < 1_000; index++) record(index)
    Bun.gc(true)
    const warmRSS = process.memoryUsage().rss

    for (let index = 1_000; index < 10_000; index++) {
      record(index)
      if ((index + 1) % 500 === 0) Bun.gc(true)
    }
    Bun.gc(true)
    const finalRSS = process.memoryUsage().rss
    CaseTrace.closeAll({ status: "success" })
    process.stdout.write(`\nCASE_TRACE_MEMORY ${JSON.stringify({ warmRSS, finalRSS })}\n`)
  })
} else {
  test("10,000 observations stay within the 128 MB runtime RSS growth bound", async () => {
    await using tmp = await tmpdir()
    const traceDir = path.join(tmp.path, "trace")
    fs.mkdirSync(traceDir, { recursive: true })
    const child = Bun.spawn(
      [
        process.execPath,
        "--expose-gc",
        "test",
        "test/observability/case-trace-memory.test.ts",
        "--timeout",
        "120000",
      ],
      {
        cwd: path.join(import.meta.dir, "../.."),
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE_MEMORY_CHILD: "1",
          OPENCODE_CASE_TRACE_MEMORY_DIR: traceDir,
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
    const match = stdout.match(/CASE_TRACE_MEMORY (\{[^\n]+\})/)
    expect(match, stdout).not.toBeNull()
    const measurement = JSON.parse(match![1]!) as { warmRSS: number; finalRSS: number }
    console.info(
      "case-trace-memory-rss",
      JSON.stringify({ ...measurement, growthRSS: measurement.finalRSS - measurement.warmRSS }),
    )
    expect(measurement.finalRSS - measurement.warmRSS).toBeLessThanOrEqual(128 * 1024 * 1024)
  }, 120_000)
}
