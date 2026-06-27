import { describe, expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { renderCaseTraceHtml } from "@/observability/case-trace-html"
import type { TraceSummary } from "@/observability/case-trace"

async function exists(file: string) {
  return fs
    .access(file)
    .then(() => true)
    .catch(() => false)
}

describe("case trace", () => {
  test("finalizes trace.json and trace.html when a traced process exits", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "exit-with-active-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "runtime", event_type: "turn.start", data: { prompt: "hello" } })`,
        `process.exit(0)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "exit-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)
    expect(await exists(path.join(dir, "exit-case", "events.jsonl"))).toBe(true)
    expect(await exists(path.join(dir, "exit-case", "trace.json"))).toBe(true)
    expect(await exists(path.join(dir, "exit-case", "trace.html"))).toBe(true)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "exit-case", "trace.json"), "utf8")) as TraceSummary
    expect(trace.status).toBe("success")
    expect(trace.events.some((event) => event.event_type === "turn.start")).toBe(true)
  })

  test("renders component data flow and agent process sections", () => {
    const trace: TraceSummary = {
      trace_version: "1.0",
      case_id: "visual-case",
      run_id: "run_visual",
      started_at: "2026-06-27T00:00:00.000Z",
      ended_at: "2026-06-27T00:00:01.000Z",
      duration_ms: 1000,
      status: "success",
      environment: {},
      token_usage: { input: 10, output: 20, total: 30 },
      errors: [],
      result: { exit_code: 0 },
      spans: [
        {
          span_id: "span_run",
          component: "runtime",
          operation: "turn",
          name: "interactive.turn",
          status: "success",
          start_time: "2026-06-27T00:00:00.000Z",
          start_ms: 0,
          end_time: "2026-06-27T00:00:00.200Z",
          end_ms: 200,
          duration_ms: 200,
          input_summary: { type: "text", preview: "实现一个需求" },
          output_summary: { type: "object", preview: '{"queue":0}' },
        },
        {
          span_id: "span_llm",
          component: "llm",
          operation: "stream",
          name: "openai/gpt-test",
          status: "success",
          start_time: "2026-06-27T00:00:00.220Z",
          start_ms: 220,
          end_time: "2026-06-27T00:00:00.900Z",
          end_ms: 900,
          duration_ms: 680,
          input_summary: { type: "object", preview: '{"message_count":3}' },
          output_summary: { type: "object", preview: '{"completed":true}' },
          token_usage: { input: 10, output: 20, total: 30 },
        },
      ],
      events: [
        {
          event_id: "evt_1",
          component: "runtime",
          event_type: "turn.send",
          timestamp: "2026-06-27T00:00:00.010Z",
          time_ms: 10,
        },
        {
          event_id: "evt_2",
          span_id: "span_llm",
          component: "llm",
          event_type: "stream.text-delta",
          timestamp: "2026-06-27T00:00:00.300Z",
          time_ms: 300,
        },
      ],
    }

    const html = renderCaseTraceHtml(trace)

    expect(html).toContain("Agent 运行流程")
    expect(html).toContain("组件数据流转")
    expect(html).toContain("runtime")
    expect(html).toContain("llm")
  })

  test("renders all agent process items with scrollable input and output cells", () => {
    const spans = Array.from({ length: 130 }, (_, index) => {
      const item = index + 1
      return {
        span_id: `span_${item}`,
        component: "runtime" as const,
        operation: "turn",
        name: `interactive.turn.${item}`,
        status: "success" as const,
        start_time: "2026-06-27T00:00:00.000Z",
        start_ms: item * 10,
        end_time: "2026-06-27T00:00:00.010Z",
        end_ms: item * 10 + 5,
        duration_ms: 5,
        input_summary: {
          type: "text",
          preview: `input-${item}-` + "x".repeat(320),
        },
        output_summary: {
          type: "text",
          preview: `output-${item}-` + "y".repeat(320),
        },
      }
    })

    const trace: TraceSummary = {
      trace_version: "1.0",
      case_id: "all-process-case",
      run_id: "run_all_process",
      started_at: "2026-06-27T00:00:00.000Z",
      ended_at: "2026-06-27T00:00:02.000Z",
      duration_ms: 2000,
      status: "success",
      environment: {},
      token_usage: {},
      errors: [],
      spans,
      events: [],
    }

    const html = renderCaseTraceHtml(trace)

    expect(html).toContain("interactive.turn.1")
    expect(html).toContain("interactive.turn.130")
    expect(html).not.toContain("已展示前 120 条")
    expect(html).toContain('class="process-scroll"')
    expect(html).toContain('class="io-scroll"')
  })

  test("stores large semantic payloads as artifacts and keeps trace.json lightweight", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-artifact-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "large-trace-payload.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const payload = "semantic-context-before-compaction:" + "x".repeat(5000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "context", event_type: "context.before_compaction", data: { payload: ${JSON.stringify(payload)} } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "artifact-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "128",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "artifact-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any

    expect(trace.artifacts.length).toBeGreaterThanOrEqual(1)
    expect(JSON.stringify(trace).includes(payload)).toBe(false)

    const artifact = trace.artifacts.find((item: any) => item.kind === "json")
    expect(artifact).toBeTruthy()
    const artifactText = await fs.readFile(path.join(caseDir, artifact.path), "utf8")
    expect(artifactText).toContain(payload)

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    expect(html).toContain("查看完整内容")
    expect(html).toContain(payload)
  })

  test("renders artifact-backed summaries with expandable full content", () => {
    const trace = {
      trace_version: "1.0",
      case_id: "artifact-html-case",
      run_id: "run_artifact_html",
      started_at: "2026-06-27T00:00:00.000Z",
      ended_at: "2026-06-27T00:00:01.000Z",
      duration_ms: 1000,
      status: "success",
      environment: {},
      token_usage: {},
      errors: [],
      artifacts: [
        {
          artifact_id: "artifact_1",
          kind: "text",
          label: "llm.final_model_messages",
          path: "artifacts/artifact_1.txt",
          length: 55,
          hash: "hash",
          preview: "short preview",
          created_at: "2026-06-27T00:00:00.000Z",
        },
      ],
      spans: [
        {
          span_id: "span_1",
          component: "llm",
          operation: "stream",
          name: "model call",
          status: "success",
          start_time: "2026-06-27T00:00:00.000Z",
          start_ms: 0,
          end_time: "2026-06-27T00:00:00.010Z",
          end_ms: 10,
          duration_ms: 10,
          input_summary: {
            type: "text",
            length: 55,
            hash: "hash",
            preview: "short preview",
            artifact_id: "artifact_1",
          },
        },
      ],
      events: [],
    } as TraceSummary

    const html = renderCaseTraceHtml(trace, {
      artifactContents: new Map([["artifact_1", "full semantic model messages payload"]]),
    } as any)

    expect(html).toContain("Artifacts")
    expect(html).toContain("查看完整内容")
    expect(html).toContain("full semantic model messages payload")
  })
})
