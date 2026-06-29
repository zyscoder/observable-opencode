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
  test("writes causal trace v2 bundle with graph nodes, records, partial snapshot, and viewer", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-causal-trace-bundle-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-bundle.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "fix pricing bug" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test" })`,
        `const ctx = CaseTrace.contextSnapshot({ span_id: span?.id, phase: "llm_request", provider_id: "deepseek", model_id: "unit-test", agent: "build", message_count: 1, messages: [{ role: "user", content: "fix pricing bug" }] })`,
        `const obs = CaseTrace.observation({ source: "tool", category: "file", summary: "pricing.mjs owns discount calculation", data: { file: "src/pricing.mjs", lines: "1-20" }, evidence_refs: ctx ? ["context:" + ctx.snapshot_id] : [] })`,
        `CaseTrace.finalEvidence({ claim: "Discount bug is in pricing.mjs.", evidence_refs: obs ? ["observation:" + obs.node_id] : [], confidence: "high" })`,
        `span?.end({ output: { completed: true } })`,
        `CaseTrace.finish({ status: "success", result: { exit_code: 0 } })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "causal-bundle-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "causal-bundle-case")
    for (const file of ["manifest.json", "causal-trace.json", "records.jsonl", "raw-events.jsonl", "viewer.html"]) {
      expect(await exists(path.join(caseDir, file))).toBe(true)
    }
    expect(await exists(path.join(caseDir, "partial", "latest.json"))).toBe(true)

    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const causal = JSON.parse(await fs.readFile(path.join(caseDir, "causal-trace.json"), "utf8")) as any
    const records = await fs.readFile(path.join(caseDir, "records.jsonl"), "utf8")
    const viewer = await fs.readFile(path.join(caseDir, "viewer.html"), "utf8")

    expect(manifest.trace_version).toBe("2.0")
    expect(manifest.case_id).toBe("causal-bundle-case")
    expect(causal.trace_version).toBe("2.0")
    expect(causal.nodes.map((node: any) => node.kind)).toContain("run.start")
    expect(causal.nodes.map((node: any) => node.kind)).toContain("context.pack")
    expect(causal.nodes.map((node: any) => node.kind)).toContain("llm.call")
    expect(causal.nodes.map((node: any) => node.kind)).toContain("observation")
    expect(causal.nodes.map((node: any) => node.kind)).toContain("final.claim")
    expect(causal.edges.some((edge: any) => edge.relation === "observation_to_claim")).toBe(true)
    expect(records).toContain('"record_type":"node"')
    expect(viewer).toContain("Causal Graph")
    expect(viewer).toContain("Timeline")
    expect(viewer).toContain("Evidence Inspector")
    expect(viewer).toContain("Context Analyzer")
  })

  test("deduplicates artifact-backed causal payloads by hash", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-causal-trace-dedupe-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-dedupe.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const repeated = "same-large-observation:" + "x".repeat(6000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.observation({ source: "mcp", category: "repo_fact", summary: ${JSON.stringify(repeated)}, data: { payload: ${JSON.stringify(repeated)} } })`,
        `CaseTrace.observation({ source: "mcp", category: "repo_fact", summary: ${JSON.stringify(repeated)}, data: { payload: ${JSON.stringify(repeated)} } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "causal-dedupe-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const causal = JSON.parse(await fs.readFile(path.join(dir, "causal-dedupe-case", "causal-trace.json"), "utf8")) as any
    const samePayloadArtifacts = causal.artifacts.filter((artifact: any) => artifact.label === "observation.data")

    expect(samePayloadArtifacts).toHaveLength(1)
    expect(samePayloadArtifacts[0].occurrences).toBe(2)
    expect(samePayloadArtifacts[0].path).toMatch(/^artifacts\/sha256\//)
  })

  test("finalizes causal trace v2 bundle when a traced process receives SIGINT", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-causal-trace-sigint-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-sigint.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "runtime", event_type: "turn.start", data: { prompt: "long running" } })`,
        `setInterval(() => {}, 1000)`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "causal-sigint-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    await Bun.sleep(300)
    proc.kill("SIGINT")
    await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    const caseDir = path.join(dir, "causal-sigint-case")
    expect(await exists(path.join(caseDir, "manifest.json"))).toBe(true)
    expect(await exists(path.join(caseDir, "causal-trace.json"))).toBe(true)
    expect(await exists(path.join(caseDir, "viewer.html"))).toBe(true)
    expect(await exists(path.join(caseDir, "partial", "latest.json"))).toBe(true)

    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const causal = JSON.parse(await fs.readFile(path.join(caseDir, "causal-trace.json"), "utf8")) as any

    expect(manifest.status).toBe("cancelled")
    expect(manifest.result.reason).toBe("SIGINT")
    expect(causal.manifest.status).toBe("cancelled")
  })

  test("records observation and compaction nodes for causal analysis", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-causal-trace-semantics-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-semantics.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const previousSummary = "Previous summary with pricing facts " + "x".repeat(3000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const compaction = CaseTrace.compaction({ trigger: "overflow", provider_id: "deepseek", model_id: "unit-test", input_tokens: 21000, context_limit: 20000, selected_head_messages: 8, selected_tail_messages: 2, hidden_compaction_messages: 1, previous_summary: ${JSON.stringify(previousSummary)}, serialized_tail: "tail message", output_summary: "pricing facts preserved", auto_continue: true })`,
        `const obs = CaseTrace.observation({ source: "compaction", category: "preserved_fact", summary: "pricing fact preserved after compaction", data: { fact: "pricing owns discounts" }, evidence_refs: compaction ? ["compaction:" + compaction.node_id] : [] })`,
        `CaseTrace.finalEvidence({ claim: "Pricing owns discounts.", evidence_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "causal-semantics-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "96",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const causal = JSON.parse(await fs.readFile(path.join(dir, "causal-semantics-case", "causal-trace.json"), "utf8")) as any
    const compaction = causal.nodes.find((node: any) => node.kind === "context.compaction")
    const observation = causal.nodes.find((node: any) => node.kind === "observation")

    expect(compaction).toBeTruthy()
    expect(compaction.data.trigger).toBe("overflow")
    expect(compaction.data.auto_continue).toBe(true)
    expect(compaction.data.previous_summary.artifact_id).toBeTruthy()
    expect(observation.data.source).toBe("compaction")
    expect(causal.edges.some((edge: any) => edge.relation === "compaction_to_observation")).toBe(true)
    expect(causal.edges.some((edge: any) => edge.relation === "observation_to_claim")).toBe(true)
  })

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

  test("persists semantic trace records with artifacts and redaction", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-semantic-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const secret = "sk-test-secret-value"
    const longMessage = "semantic model message: " + "x".repeat(5000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/test" })`,
        `CaseTrace.contextSnapshot({ span_id: span?.id, phase: "llm_request", provider_id: "deepseek", model_id: "deepseek-v4-pro", agent: "build", message_count: 1, system_count: 1, tool_count: 1, token_estimate: 128, messages: [{ role: "user", content: ${JSON.stringify(longMessage)}, apiKey: ${JSON.stringify(secret)} }], system: ["system prompt"], tools: { bash: { description: "run command", authorization: "Bearer abc" } } })`,
        `CaseTrace.decision({ span_id: span?.id, component: "llm", decision_type: "tool_call", intent: "run tests", chosen_action: "bash", rationale: "Need verification", evidence_refs: ["ctx_1"] })`,
        `CaseTrace.verification({ span_id: span?.id, tool_call_id: "call_1", command: "node test.js", cwd: "/tmp/project", purpose: "Run unit tests", stage: "baseline", exit_code: 1, status: "failed", stdout: "Error: expected 170, got 30", stderr: "" })`,
        `CaseTrace.change({ span_id: span?.id, tool_call_id: "call_2", files: ["src/pricing.mjs"], intent: "Fix discount formula", diff: "- old\\\\n+ new", evidence_refs: ["ver_1"] })`,
        `CaseTrace.constraint({ source: "user", constraint: "do not modify files", status: "observed_satisfied", evidence_refs: ["span_1"] })`,
        `CaseTrace.finalEvidence({ response_artifact: "artifact_final", claim: "The formula returned discount amount instead of discounted price.", evidence_refs: ["ver_1", "chg_1"], confidence: "high" })`,
        `CaseTrace.edge({ from: { type: "verification", id: "ver_1" }, to: { type: "change", id: "chg_1" }, relation: "failure_to_change", label: "test failure led to edit" })`,
        `span?.end({ output: { completed: true } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-case",
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

    const caseDir = path.join(dir, "semantic-case")
    const traceText = await fs.readFile(path.join(caseDir, "trace.json"), "utf8")
    const trace = JSON.parse(traceText) as any

    expect(trace.trace_version).toBe("1.2")
    expect(trace.context_snapshots).toHaveLength(1)
    expect(trace.semantic_decisions).toHaveLength(1)
    expect(trace.verification_records).toHaveLength(1)
    expect(trace.change_records).toHaveLength(1)
    expect(trace.constraint_records).toHaveLength(1)
    expect(trace.final_response_evidence).toHaveLength(1)
    expect(trace.semantic_edges.length).toBeGreaterThanOrEqual(1)
    expect(trace.semantic_edges.some((edge: any) => edge.relation === "failure_to_change")).toBe(true)
    expect(trace.context_snapshots[0].messages.artifact_id).toBeTruthy()
    expect(trace.verification_records[0].parsed_failures[0]).toMatchObject({ expected: "170", actual: "30" })
    expect(traceText).not.toContain(secret)
    expect(traceText).not.toContain("Bearer abc")

    const artifactText = await fs.readFile(path.join(caseDir, trace.artifacts[0].path), "utf8")
    expect(artifactText).toContain("semantic model message")
    expect(artifactText).not.toContain(secret)
    expect(artifactText).not.toContain("Bearer abc")

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    expect(html).toContain("Evidence Chain")
    expect(html).toContain("LLM Context")
    expect(html).toContain("Changes & Verification")
    expect(html).toContain("Constraints")
  })

  test("preserves token metrics while redacting credentials", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-redaction-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "redaction-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "token-test", metadata: { token_usage: { total: 9 }, apiKey: "sk-test-secret-value" } })`,
        `CaseTrace.contextSnapshot({ span_id: span?.id, phase: "llm_request", token_estimate: 128, message_count: 1, metadata: { tokens: 128, token_usage: { input: 3, output: 6 }, authorization: "Bearer abcdefgh" }, messages: [{ role: "user", content: "hello", access_token: "sk-another-secret-value" }] })`,
        `span?.end({ tokenUsage: { inputTokens: 3, outputTokens: 6, totalTokens: 9 } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "redaction-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const traceText = await fs.readFile(path.join(dir, "redaction-case", "trace.json"), "utf8")
    const trace = JSON.parse(traceText) as TraceSummary

    expect(trace.token_usage.total).toBe(9)
    expect(trace.spans[0].token_usage?.total).toBe(9)
    expect(trace.context_snapshots?.[0]?.token_estimate).toBe(128)
    expect(trace.context_snapshots?.[0]?.metadata?.tokens).toBe(128)
    expect((trace.context_snapshots?.[0]?.metadata as any)?.token_usage).toEqual({ input: 3, output: 6 })
    expect(traceText).not.toContain("sk-test-secret-value")
    expect(traceText).not.toContain("sk-another-secret-value")
    expect(traceText).not.toContain("Bearer abcdefgh")
  })

  test("parses concrete expected and actual values from verification failures", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-failure-parse-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "failure-parse-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const stdout = [
      "file:///tmp/project/test/pricing.test.mjs:11",
      "throw new Error(`expected ${item.expected}, got ${actual}`)",
      "Error: expected 170, got 30",
      "    at file:///tmp/project/test/pricing.test.mjs:11:13",
    ].join("\\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.verification({ command: "node test/pricing.test.mjs", exit_code: 1, stdout: ${JSON.stringify(stdout)}, stderr: "" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "failure-parse-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "failure-parse-case", "trace.json"), "utf8")) as any
    expect(trace.verification_records[0].parsed_failures[0]).toMatchObject({
      message: "Error: expected 170, got 30",
      expected: "170",
      actual: "30",
      file: "file:///tmp/project/test/pricing.test.mjs",
      line: 11,
      column: 13,
    })
  })

  test("evaluates read-only constraints at trace finish", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-constraint-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "constraint-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ caseID: "readonly-ok" })`,
        `CaseTrace.constraint({ source: "user", constraint: "Do not modify repository files", status: "unknown" })`,
        `CaseTrace.finish({ status: "success" })`,
        `CaseTrace.configure({ caseID: "readonly-violated" })`,
        `CaseTrace.constraint({ source: "user", constraint: "Do not modify repository files", status: "unknown" })`,
        `CaseTrace.change({ files: ["src/pricing.mjs"], intent: "unexpected edit" })`,
        `CaseTrace.finish({ status: "success" })`,
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
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const ok = JSON.parse(await fs.readFile(path.join(dir, "readonly-ok", "trace.json"), "utf8")) as any
    const violated = JSON.parse(await fs.readFile(path.join(dir, "readonly-violated", "trace.json"), "utf8")) as any

    expect(ok.constraint_records[0]).toMatchObject({
      constraint: "Do not modify repository files",
      status: "observed_satisfied",
    })
    expect(violated.constraint_records[0]).toMatchObject({
      constraint: "Do not modify repository files",
      status: "observed_violated",
    })
  })

  test("uses concrete evidence refs for final response claims", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-evidence-refs-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "evidence-ref-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "tool", operation: "execute", name: "bash" })`,
        `const ctx = CaseTrace.contextSnapshot({ phase: "llm_request", messages: [{ role: "user", content: "fix tests" }] })`,
        `const ver = CaseTrace.verification({ span_id: span?.id, command: "node test/pricing.test.mjs", exit_code: 1, stdout: "Error: expected 170, got 30" })`,
        `const chg = CaseTrace.change({ span_id: span?.id, files: ["src/pricing.mjs"], intent: "Fix discount formula" })`,
        `span?.end({ output: { ok: true } })`,
        `CaseTrace.finalEvidence({ claim: "Fixed discount calculation.", evidence_refs: ["recent_tool_results", "recent_verification_records", "recent_change_records"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "evidence-ref-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "evidence-ref-case", "trace.json"), "utf8")) as any
    const refs = trace.final_response_evidence[0].evidence_refs

    expect(refs).toContain(`context_snapshot:${trace.context_snapshots[0].snapshot_id}`)
    expect(refs).toContain(`tool_span:${trace.spans[0].span_id}`)
    expect(refs).toContain(`verification:${trace.verification_records[0].verification_id}`)
    expect(refs).toContain(`change:${trace.change_records[0].change_id}`)
    expect(refs.some((item: string) => item.startsWith("recent_"))).toBe(false)
  })

  test("persists and renders design records", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-design-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "design-record-trace.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const designText = [
      "方案设计：在 checkout 层新增折扣策略接口。",
      "架构边界：pricing 负责折扣，tax 负责税费。",
      "取舍：保持 API 稳定，但增加策略注入。",
      "风险：历史订单回放需要兼容旧字段。",
      "测试策略：补充 pricing 单测和 checkout 集成测试。",
    ].join("\\n")

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.contextSnapshot({ phase: "llm_request", messages: [{ role: "user", content: "设计折扣能力扩展方案" }] })`,
        `CaseTrace.designRecord({ source: "final_response", requirement_summary: "设计折扣能力扩展方案", existing_boundaries: "pricing/tax/checkout", selected_solution: ${JSON.stringify(designText)}, tradeoffs: "保持 API 稳定", risks: "历史订单兼容", test_strategy: "pricing 单测和 checkout 集成测试" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "design-record-case",
        OPENCODE_CASE_TRACE_DIR: dir,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const caseDir = path.join(dir, "design-record-case")
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    expect(trace.design_records).toHaveLength(1)
    expect(trace.design_records[0].selected_solution.artifact_id).toBeTruthy()

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    expect(html).toContain("Design Records")
    expect(html).toContain("查看完整内容")
    expect(html).toContain("checkout 层新增折扣策略接口")
  })
})
