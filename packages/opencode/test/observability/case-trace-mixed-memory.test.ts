import { expect, test } from "bun:test"
import fs from "node:fs"
import fsp from "node:fs/promises"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { materializeTrace } from "@/observability/trace-materializer"
import { tmpdir } from "../fixture/fixture"

const childMode = process.env.OPENCODE_CASE_TRACE_MIXED_MEMORY_CHILD === "1"

if (childMode) {
  test("mixed memory measurement child", async () => {
    process.env.OPENCODE_CASE_TRACE = "1"
    process.env.OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH = "20000"
    const { CaseTrace } = await import("@/observability/case-trace")
    const traceDir = process.env.OPENCODE_CASE_TRACE_MIXED_MEMORY_DIR!
    CaseTrace.configure({ caseID: "mixed-memory-acceptance", traceDir })

    const lastIDs: Record<string, string | undefined> = {}
    const record = (index: number) => {
      const detail = `${index}:${String(index % 10).repeat(18_000)}`
      switch (index % 10) {
        case 0: {
          const span = CaseTrace.startSpan({
            component: "tool",
            operation: "mixed-memory",
            input: { detail },
          })
          span?.end({ status: "error", error: new Error(detail), output: { detail } })
          break
        }
        case 1:
          CaseTrace.event({ component: "runtime", event_type: "mixed.memory", data: { detail } })
          break
        case 2:
          lastIDs.context = CaseTrace.contextSnapshot({ phase: "other", messages: { detail } })?.snapshot_id
          break
        case 3:
          lastIDs.decision = CaseTrace.decision({
            component: "processor",
            decision_type: "mixed_memory",
            chosen_action: `action-${index}`,
            rationale: detail,
          })?.decision_id
          break
        case 4:
          lastIDs.verification = CaseTrace.verification({
            command: `verify-${index}`,
            status: "passed",
            parsed_failures: [],
            stdout: detail,
          })?.verification_id
          break
        case 5:
          lastIDs.change = CaseTrace.change({ files: [`src/mixed-${index}.ts`], diff: detail })?.change_id
          break
        case 6:
          lastIDs.constraint = CaseTrace.constraint({
            source: "runtime",
            constraint: detail,
            status: "unknown",
          })?.constraint_id
          break
        case 7:
          lastIDs.response = CaseTrace.responseOutput({
            text: detail,
            response_role: "intermediate_summary",
            visibility: "debug",
          })?.segment_id
          break
        case 8:
          lastIDs.design = CaseTrace.designRecord({ source: "context", selected_solution: detail })?.design_id
          break
        default:
          CaseTrace.observation({
            source: "runtime",
            category: "mixed_memory",
            summary: `observation ${index}`,
            data: { detail },
          })
      }
    }

    for (let index = 0; index < 3_000; index++) record(index)
    Bun.gc(true)
    const warmRSS = process.memoryUsage().rss

    for (let index = 3_000; index < 10_000; index++) {
      record(index)
      if ((index + 1) % 500 === 0) Bun.gc(true)
    }
    Bun.gc(true)
    const finalRSS = process.memoryUsage().rss
    const requests = CaseTrace.closeAll({ status: "success" })
    process.stdout.write(
      `\nCASE_TRACE_MIXED_MEMORY ${JSON.stringify({ warmRSS, finalRSS, lastIDs, requests: requests.length })}\n`,
    )
  })
} else {
  test("10,000 mixed records stay below 128 MiB RSS with monotonic compatibility IDs", async () => {
    await using tmp = await tmpdir()
    const traceDir = path.join(tmp.path, "trace")
    fs.writeFileSync(traceDir, "unavailable trace root")
    const child = Bun.spawn(
      [
        process.execPath,
        "--expose-gc",
        "test",
        "test/observability/case-trace-mixed-memory.test.ts",
        "--timeout",
        "120000",
      ],
      {
        cwd: path.join(import.meta.dir, "../.."),
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE_MIXED_MEMORY_CHILD: "1",
          OPENCODE_CASE_TRACE_MIXED_MEMORY_DIR: traceDir,
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
    const match = stdout.match(/CASE_TRACE_MIXED_MEMORY (\{[^\n]+\})/)
    expect(match, stdout).not.toBeNull()
    const measurement = JSON.parse(match![1]!) as {
      warmRSS: number
      finalRSS: number
      lastIDs: Record<string, string>
      requests: number
    }
    const growthRSS = measurement.finalRSS - measurement.warmRSS
    console.info("case-trace-mixed-memory-rss", JSON.stringify({ ...measurement, growthRSS }))
    expect(measurement.requests).toBe(0)
    expect(measurement.lastIDs.context).toMatch(/^ctx_1000_/)
    expect(measurement.lastIDs.decision).toMatch(/^dec_1000_/)
    expect(measurement.lastIDs.verification).toMatch(/^ver_1000_/)
    expect(measurement.lastIDs.change).toMatch(/^chg_1000_/)
    expect(measurement.lastIDs.constraint).toMatch(/^constraint_1000_/)
    expect(measurement.lastIDs.response).toMatch(/^segment_1000_/)
    expect(measurement.lastIDs.design).toMatch(/^design_1000_/)
    expect(growthRSS).toBeLessThan(128 * 1024 * 1024)
  }, 120_000)

  test("disk materialization preserves every compatibility record evicted from hot windows", async () => {
    await using tmp = await tmpdir()
    const packageDir = path.resolve(import.meta.dir, "../..")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const scenarios: Array<{
      name: string
      emit: string
      assertProjection: (legacy: any) => void
    }> = [
      {
        name: "spans-and-errors",
        emit: `for (let index = 0; index < 257; index++) { const span = CaseTrace.startSpan({ component: "tool", operation: "mixed-" + index }); span?.end({ status: "error", error: new Error("error-" + index) }) }`,
        assertProjection: (legacy) => {
          expect(legacy.spans).toHaveLength(257)
          expect(legacy.spans.every((span: any) => span.status === "error" && span.error)).toBe(true)
          expect(legacy.errors).toHaveLength(257)
        },
      },
      {
        name: "events",
        emit: `for (let index = 0; index < 257; index++) CaseTrace.event({ component: "runtime", event_type: "mixed.persisted", data: { index } })`,
        assertProjection: (legacy) => {
          expect(legacy.events.filter((event: any) => event.event_type === "mixed.persisted")).toHaveLength(257)
        },
      },
      {
        name: "contexts",
        emit: `for (let index = 0; index < 257; index++) CaseTrace.contextSnapshot({ phase: "other", messages: { index } })`,
        assertProjection: (legacy) => expect(legacy.context_snapshots).toHaveLength(257),
      },
      {
        name: "decisions",
        emit: `for (let index = 0; index < 257; index++) CaseTrace.decision({ component: "processor", decision_type: "mixed", chosen_action: "action-" + index })`,
        assertProjection: (legacy) => expect(legacy.semantic_decisions).toHaveLength(257),
      },
      {
        name: "changes",
        emit: `for (let index = 0; index < 257; index++) CaseTrace.change({ files: ["src/mixed-" + index + ".ts"], diff: "+" + index })`,
        assertProjection: (legacy) => expect(legacy.change_records).toHaveLength(257),
      },
      {
        name: "verifications",
        emit: `for (let index = 0; index < 257; index++) CaseTrace.verification({ command: "verify-" + index, status: "passed", parsed_failures: [] })`,
        assertProjection: (legacy) => expect(legacy.verification_records).toHaveLength(257),
      },
      {
        name: "constraints",
        emit: `for (let index = 0; index < 257; index++) CaseTrace.constraint({ source: "runtime", constraint: "constraint-" + index, status: "unknown" })`,
        assertProjection: (legacy) => expect(legacy.constraint_records).toHaveLength(257),
      },
      {
        name: "responses",
        emit: `for (let index = 0; index < 257; index++) CaseTrace.responseOutput({ text: "response-" + index, response_role: "intermediate_summary", visibility: "debug" })`,
        assertProjection: (legacy) => expect(legacy.response_segments).toHaveLength(257),
      },
      {
        name: "designs",
        emit: `for (let index = 0; index < 257; index++) CaseTrace.designRecord({ source: "context", selected_solution: "solution-" + index })`,
        assertProjection: (legacy) => expect(legacy.design_records).toHaveLength(257),
      },
    ]

    for (const scenario of scenarios) {
      const script = path.join(tmp.path, `${scenario.name}.ts`)
      const traceRoot = path.join(tmp.path, `trace-${scenario.name}`)
      const caseID = `mixed-projection-${scenario.name}`
      await fsp.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `CaseTrace.configure()`,
          scenario.emit,
          `process.stdout.write(String(CaseTrace.closeAll({ status: "success" }).length))`,
        ].join("\n"),
      )

      const child = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: caseID,
          OPENCODE_CASE_TRACE_DIR: traceRoot,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const [exitCode, stdout, stderr] = await Promise.all([
        child.exited,
        new Response(child.stdout).text(),
        new Response(child.stderr).text(),
      ])
      expect(exitCode, `${scenario.name}: ${stderr}`).toBe(0)
      expect(stdout, scenario.name).toBe("1")

      const caseDir = path.join(traceRoot, caseID)
      materializeTrace({ caseDir })
      const legacy = JSON.parse(await fsp.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any
      scenario.assertProjection(legacy)
    }
  }, 240_000)
}
