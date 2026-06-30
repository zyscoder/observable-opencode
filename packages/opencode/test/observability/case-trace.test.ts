import { describe, expect, test } from "bun:test"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { renderCaseTraceHtml } from "@/observability/case-trace-html"
import { renderProvenanceTraceHtml } from "@/observability/causal-trace-viewer"
import type { ProvenanceTraceSummary, TraceSummary } from "@/observability/case-trace"

async function exists(file: string) {
  return fs
    .access(file)
    .then(() => true)
    .catch(() => false)
}

async function waitForExists(file: string, timeoutMs = 2000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    if (await exists(file)) return true
    await Bun.sleep(50)
  }
  return exists(file)
}

describe("case trace", () => {
  test("writes trace semantic contract v4.2 bundle with trace.html as the only HTML entry point", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-bundle-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-bundle.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ input: { prompt: "fix pricing bug" }, environment: { model: "unit-test" } })`,
        `const span = CaseTrace.get()?.startSpan({ component: "llm", operation: "stream", name: "deepseek/unit-test", input: { sessionID: "ses_test", agent: "build", model: { providerID: "deepseek", id: "unit-test" }, message_count: 1, system_count: 0, tool_count: 2 } })`,
        `const ctx = CaseTrace.contextSnapshot({ span_id: span?.id, phase: "llm_request", provider_id: "deepseek", model_id: "unit-test", agent: "build", message_count: 1, messages: [{ role: "user", content: "fix pricing bug" }] })`,
        `const obs = CaseTrace.observation({ source: "tool", category: "file", summary: "pricing.mjs owns discount calculation", data: { file: "src/pricing.mjs", lines: "1-20" }, source_refs: ctx ? ["context:" + ctx.snapshot_id] : [] })`,
        `CaseTrace.responseOutput({ text: "Discount bug is in pricing.mjs.", source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `span?.end({ output: { completed: true, finish_reason: "stop" }, tokenUsage: { inputTokens: 10, outputTokens: 5, cachedInputTokens: 3, totalTokens: 15 } })`,
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
    for (const file of ["manifest.json", "trace.json", "legacy-trace.json", "records.jsonl", "raw-events.jsonl", "trace.html"]) {
      expect(await exists(path.join(caseDir, file))).toBe(true)
    }
    expect(await exists(path.join(caseDir, "viewer.html"))).toBe(false)
    expect(await exists(path.join(caseDir, "partial", "latest.json"))).toBe(true)

    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const provenance = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
    const legacy = JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any
    const records = await fs.readFile(path.join(caseDir, "records.jsonl"), "utf8")
    const traceHtml = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    const provenanceText = JSON.stringify(provenance)
    const allowedRelations = new Set([
      "selected_into_context",
      "prompted",
      "produced",
      "consumed",
      "compressed_from",
      "compressed_to",
      "spawned",
      "continued_from",
      "derived_from",
      "verified_by",
      "modified_by",
      "failed_before",
      "read_from",
      "returned_by",
    ])

    expect(manifest.trace_version).toBe("4.2")
    expect(manifest.case_id).toBe("causal-bundle-case")
    expect(manifest.files.trace).toBe("trace.json")
    expect(manifest.files.legacy_trace).toBe("legacy-trace.json")
    expect(manifest.files.trace_html).toBe("trace.html")
    expect(manifest.files.viewer_alias).toBeUndefined()
    expect(provenance.trace_version).toBe("4.2")
    expect(legacy.trace_version).toBe("1.3")
    expect(provenance.metrics.token_usage.total).toBe(15)
    expect(provenanceText).not.toContain("[Circular]")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("run.start")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("context.pack")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("llm.call")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("observation")
    expect(provenance.records.map((record: any) => record.event_type)).toContain("response.output")
    expect(provenance.records.map((record: any) => record.event_type)).not.toContain("runtime.event")
    expect(provenance.dataflow_edges.every((edge: any) => allowedRelations.has(edge.relation))).toBe(true)
    expect(provenance.dataflow_edges.some((edge: any) => edge.relation === "consumed")).toBe(true)
    expect(provenanceText).not.toContain("diagnostics_hints")
    expect(provenanceText).not.toContain("evidence_refs")
    expect(provenanceText).not.toContain("final.claim")
    expect(records).toContain('"record_type":"node"')
    const llm = provenance.records.find((record: any) => record.event_type === "llm.call")
    expect(llm.data.agent).toBe("build")
    expect(llm.data.provider_id).toBe("deepseek")
    expect(llm.data.model_id).toBe("unit-test")
    expect(llm.data.message_count).toBe(1)
    expect(llm.data.tool_count).toBe(2)
    expect(llm.token_usage.total).toBe(15)
    const response = provenance.records.find((record: any) => record.event_type === "response.output")
    expect(response.data.response_role).toBe("final_answer")
    expect(response.data.is_final_for_case).toBe(true)
    expect(traceHtml).toContain("Trace v4.2")
    expect(traceHtml).toContain('id="overview"')
    expect(traceHtml).toContain('id="agent-flow"')
    expect(traceHtml).toContain("Component Dataflow")
    expect(traceHtml).toContain("IO Inspector")
    expect(traceHtml).toContain("Context And Compaction")
    expect(traceHtml).not.toContain("Evidence Inspector")
  })

  test("filters low-value runtime events from formal provenance and normalizes legacy relations", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-contract-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-contract.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.event({ component: "prompt", event_type: "prompt.parts.resolved", data: { part_count: 1, part_types: ["text"], tool_overrides: {} } })`,
        `CaseTrace.event({ component: "processor", event_type: "tool-input-delta", data: { id: "call_1", delta: " owns" } })`,
        `CaseTrace.edge({ from: { type: "span", id: "span_tool", label: "edit" }, to: { type: "change", id: "chg_1" }, relation: "tool_to_change", label: "legacy relation" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-contract-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "semantic-contract-case", "trace.json"), "utf8"),
    ) as any
    const eventTypes = provenance.records.map((record: any) => record.event_type)
    const relations = provenance.dataflow_edges.map((edge: any) => edge.relation)

    expect(eventTypes).not.toContain("runtime.event")
    expect(JSON.stringify(provenance.records)).not.toContain("tool-input-delta")
    expect(JSON.stringify(provenance.records)).not.toContain("prompt.parts.resolved")
    expect(relations).toContain("modified_by")
    expect(relations).not.toContain("tool_to_change")
  })

  test("adds v4.2 semantic fields for source locations, compaction ledger, response visibility, and honest subagent trace refs", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-v4-semantics-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "semantic-fields.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const task = CaseTrace.get()?.startSpan({ component: "task", operation: "subagent", name: "general", input: { description: "inspect pricing" } })`,
        `task?.end({ output: { task_id: "ses_child_1", output: "pricing owns discount math " + "x".repeat(5000) } })`,
        `const compaction = CaseTrace.compaction({ trigger: "auto", input_tokens: 24000, context_limit: 12000, selected_head_messages: 1, selected_tail_messages: 2, hidden_compaction_messages: 3, output_summary: "kept pricing fact" })`,
        `const obs = CaseTrace.observation({ source: "tool", category: "file_read", summary: "read pricing", data: { path: "src/pricing.mjs", line_start: 10, line_end: 14, snippet: "return price * (1 - percent)" }, source_refs: compaction ? ["compaction:" + compaction.node_id] : [] })`,
        `CaseTrace.responseOutput({ text: "Intermediate task note.", response_role: "intermediate_summary" })`,
        `CaseTrace.responseOutput({ text: "Pricing owns discount math.", source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "semantic-fields-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "semantic-fields-case", "trace.json"), "utf8"),
    ) as any
    const compaction = provenance.records.find((record: any) => record.event_type === "context.compaction")
    const observation = provenance.records.find((record: any) => record.event_type === "observation")
    const responses = provenance.records.filter((record: any) => record.event_type === "response.output")
    const response = responses.find((record: any) => record.data.response_role === "final_answer")
    const intermediate = responses.find((record: any) => record.data.response_role === "intermediate_summary")
    const subagent = provenance.records.find((record: any) => record.event_type === "subagent.call")

    expect(compaction.data.context_ledger.algorithm).toBe("head-tail-summary")
    expect(compaction.data.context_ledger.ledger_id_quality).toBe("estimated")
    expect(compaction.data.context_ledger.retained_message_ids).toHaveLength(3)
    expect(compaction.data.context_ledger.dropped_message_ids).toHaveLength(3)
    expect(observation.source_locations[0]).toMatchObject({ path: "src/pricing.mjs", line_start: 10, line_end: 14 })
    expect(response.data.visibility).toBe("user_visible")
    expect(response.data.is_final_for_case).toBe(true)
    expect(intermediate.data.is_final_for_case).toBe(false)
    expect(subagent.data.child_session_id).toBe("ses_child_1")
    expect(subagent.data.child_trace_available).toBe(false)
    expect(subagent.data.child_trace_dir).toBeUndefined()
    expect(subagent.data.trace_ref.child_trace_dir).toBeUndefined()
    expect(subagent.data.child_status).toBe("success")
    expect(subagent.data.output_artifact_id).toBeTruthy()
  })

  test("promotes MCP JSON text facts onto mcp.call records as well as observations", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-mcp-call-facts-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "mcp-call-facts.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const fact = {
      key: "pricing-owner",
      path: "src/pricing.mjs",
      line_start: 1,
      line_end: 20,
      fact: "pricing.mjs owns coupon math and promotion stacking.",
    }

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const span = CaseTrace.get()?.startSpan({ component: "mcp", operation: "tool.call", name: "trace-facts:audit_facts", input: { server: "trace-facts", tool: "audit_facts", args: { topic: "pricing" } } })`,
        `const output = { server: "trace-facts", tool: "audit_facts", content: [{ type: "text", text: ${JSON.stringify(JSON.stringify(fact))} }] }`,
        `span?.end({ output })`,
        `CaseTrace.observation({ source: "mcp", category: "trace-facts:audit_facts", summary: "pricing owner", data: output, source_refs: span ? ["span:" + span.id] : [] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "mcp-call-facts-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "mcp-call-facts-case", "trace.json"), "utf8")) as any
    const mcpCall = trace.records.find((record: any) => record.event_type === "mcp.call")
    const observation = trace.records.find((record: any) => record.event_type === "observation")

    expect(mcpCall.typed_resources[0]).toMatchObject({
      type: "repo_fact",
      key: "pricing-owner",
      fact: "pricing.mjs owns coupon math and promotion stacking.",
    })
    expect(mcpCall.source_locations[0]).toMatchObject({
      path: "src/pricing.mjs",
      line_start: 1,
      line_end: 20,
    })
    expect(observation.typed_resources[0]).toMatchObject(mcpCall.typed_resources[0])
  })

  test("records loop decisions as formal facts when processor loop events are observed", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-loop-decision-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "loop-decision.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "already answered" })`,
        `CaseTrace.event({ component: "processor", event_type: "step.finish", data: { agent: "build", messageID: "msg_1", reason: "stop", part_count: 2, part_types: ["text"], synthetic_continue: false, compaction_continue: false } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "loop-decision-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "loop-decision-case", "trace.json"), "utf8")) as any
    const loopDecision = trace.records.find((record: any) => record.event_type === "loop.decision")

    expect(loopDecision).toBeTruthy()
    expect(loopDecision.data).toMatchObject({
      decision: "stop",
      reason: "stop",
      agent: "build",
      message_id: "msg_1",
      part_count: 2,
      part_types: ["text"],
      has_user_visible_response: true,
      has_final_answer: true,
      synthetic_continue: false,
      compaction_continue: false,
    })
  })

  test("promotes MCP JSON text facts into typed resources and source locations", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-mcp-facts-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "mcp-facts.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.observation({ source: "mcp", category: "repo_fact", summary: "pricing owner", data: { server: "audit_facts", tool: "repo_fact", content: [{ type: "text", text: JSON.stringify({ key: "pricing-owner", path: "src/pricing.mjs", line_start: 1, line_end: 20, fact: "pricing.mjs owns coupon math and promotion stacking." }) }] } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "mcp-facts-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "mcp-facts-case", "trace.json"), "utf8")) as any
    const observation = trace.records.find((record: any) => record.event_type === "observation")

    expect(observation.typed_resources[0]).toMatchObject({
      type: "repo_fact",
      key: "pricing-owner",
      fact: "pricing.mjs owns coupon math and promotion stacking.",
    })
    expect(observation.typed_resources[0].source_location).toMatchObject({
      path: "src/pricing.mjs",
      line_start: 1,
      line_end: 20,
    })
    expect(observation.source_locations[0]).toMatchObject({
      path: "src/pricing.mjs",
      line_start: 1,
      line_end: 20,
    })
    const html = await fs.readFile(path.join(dir, "mcp-facts-case", "trace.html"), "utf8")
    expect(html).toContain("Typed Resources")
    expect(html).toContain("repo_fact")
  })

  test("suppresses weak path-only tool output observations from formal provenance", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-weak-observation-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "weak-observation.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.observation({ source: "tool", category: "tool_output", summary: "private/tmp/project/src/pricing.mjs", data: { path: "/private/tmp/project/src/pricing.mjs" } })`,
        `CaseTrace.observation({ source: "tool", category: "file_read", summary: "pricing formula", data: { path: "src/pricing.mjs", line_start: 7, line_end: 7, snippet: "return subtotal * percent" } })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "weak-observation-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "weak-observation-case", "trace.json"), "utf8")) as any
    const observations = trace.records.filter((record: any) => record.event_type === "observation")

    expect(observations).toHaveLength(1)
    expect(observations[0].title).toBe("file_read")
    expect(JSON.stringify(trace.records)).not.toContain("private/tmp/project/src/pricing.mjs")
  })

  test("downgrades earlier default user-visible responses to intermediate summaries", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-response-roles-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "response-roles.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.responseOutput({ text: "subagent-style preliminary answer" })`,
        `CaseTrace.responseOutput({ text: "final answer" })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_ID: "response-roles-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "response-roles-case", "trace.json"), "utf8")) as any
    const responses = trace.records.filter((record: any) => record.event_type === "response.output")

    expect(responses[0].data.response_role).toBe("intermediate_summary")
    expect(responses[0].data.is_final_for_case).toBe(false)
    expect(responses[1].data.response_role).toBe("final_answer")
    expect(responses[1].data.is_final_for_case).toBe(true)
  })

  test("deduplicates artifact-backed provenance payloads by hash", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-dedupe-"))
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

    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "causal-dedupe-case", "provenance-trace.json"), "utf8"),
    ) as any
    const samePayloadArtifacts = provenance.artifacts.filter((artifact: any) => artifact.label === "observation.data")

    expect(samePayloadArtifacts).toHaveLength(1)
    expect(samePayloadArtifacts[0].occurrences).toBe(2)
    expect(samePayloadArtifacts[0].path).toMatch(/^artifacts\/sha256\//)
  })

  test("finalizes trace semantic contract v4 bundle when a traced process receives SIGINT", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-sigint-"))
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
    const caseDir = path.join(dir, "causal-sigint-case")
    expect(await waitForExists(path.join(caseDir, "events.jsonl"))).toBe(true)
    proc.kill("SIGINT")
    await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(await waitForExists(path.join(caseDir, "manifest.json"))).toBe(true)
    expect(await exists(path.join(caseDir, "provenance-trace.json"))).toBe(true)
    expect(await exists(path.join(caseDir, "trace.html"))).toBe(true)
    expect(await exists(path.join(caseDir, "partial", "latest.json"))).toBe(true)

    const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8")) as any
    const provenance = JSON.parse(await fs.readFile(path.join(caseDir, "provenance-trace.json"), "utf8")) as any

    expect(manifest.status).toBe("cancelled")
    expect(manifest.result.reason).toBe("SIGINT")
    expect(provenance.manifest.status).toBe("cancelled")
  })

  test("records observation and compaction facts for offline provenance analysis", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-provenance-trace-semantics-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "causal-semantics.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
    const previousSummary = "Previous summary with pricing facts " + "x".repeat(3000)

    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const compaction = CaseTrace.compaction({ trigger: "overflow", provider_id: "deepseek", model_id: "unit-test", input_tokens: 21000, context_limit: 20000, selected_head_messages: 8, selected_tail_messages: 2, hidden_compaction_messages: 1, previous_summary: ${JSON.stringify(previousSummary)}, serialized_tail: "tail message", output_summary: "pricing facts preserved", auto_continue: true })`,
        `const obs = CaseTrace.observation({ source: "compaction", category: "preserved_fact", summary: "pricing fact preserved after compaction", data: { fact: "pricing owns discounts" }, source_refs: compaction ? ["compaction:" + compaction.node_id] : [] })`,
        `CaseTrace.responseOutput({ text: "Pricing owns discounts.", source_refs: obs ? ["observation:" + obs.node_id] : [] })`,
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

    const provenance = JSON.parse(
      await fs.readFile(path.join(dir, "causal-semantics-case", "provenance-trace.json"), "utf8"),
    ) as any
    const compaction = provenance.records.find((record: any) => record.event_type === "context.compaction")
    const observation = provenance.records.find((record: any) => record.event_type === "observation")

    expect(compaction).toBeTruthy()
    expect(compaction.data.trigger).toBe("overflow")
    expect(compaction.data.auto_continue).toBe(true)
    expect(compaction.data.previous_summary.artifact_id).toBeTruthy()
    expect(observation.data.source).toBe("compaction")
    expect(observation.source_refs).toContain(`compaction:${compaction.record_id}`)
    expect(provenance.dataflow_edges.some((edge: any) => edge.relation === "derived_from")).toBe(true)
    expect(provenance.dataflow_edges.some((edge: any) => edge.relation === "consumed")).toBe(true)
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
    expect(await exists(path.join(dir, "exit-case", "legacy-trace.json"))).toBe(true)
    expect(await exists(path.join(dir, "exit-case", "trace.html"))).toBe(true)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "exit-case", "legacy-trace.json"), "utf8")) as TraceSummary
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

  test("renders v4.2 provenance report with flow-style sections and scrollable IO panes", () => {
    const trace: ProvenanceTraceSummary = {
      trace_version: "4.2",
      manifest: {
        trace_version: "4.2",
        case_id: "viewer-v42-case",
        run_id: "run_viewer_v42",
        started_at: "2026-06-30T00:00:00.000Z",
        ended_at: "2026-06-30T00:00:01.000Z",
        duration_ms: 1000,
        status: "success",
        input: { prompt: "fix pricing" },
        environment: { model: "deepseek/deepseek-v4-pro" },
        token_usage: { input: 10, output: 5, total: 15 },
        files: {
          trace: "trace.json",
          legacy_trace: "legacy-trace.json",
          provenance_trace: "provenance-trace.json",
          trace_html: "trace.html",
          records: "records.jsonl",
          raw_events: "raw-events.jsonl",
          partial_latest: "partial/latest.json",
        },
      },
      records: [
        {
          record_id: "llm_1",
          component: "llm",
          event_type: "llm.call",
          timestamp: "2026-06-30T00:00:00.100Z",
          time_ms: 100,
          title: "deepseek/deepseek-v4-pro",
          status: "success",
          duration_ms: 500,
          token_usage: { input: 10, output: 5, total: 15 },
          data: {
            input: { prompt: "fix pricing" },
            output: { text: "need tools" },
            agent: "build",
            provider_id: "deepseek",
            model_id: "deepseek-v4-pro",
          },
        },
        {
          record_id: "mcp_1",
          component: "mcp",
          event_type: "mcp.call",
          timestamp: "2026-06-30T00:00:00.300Z",
          time_ms: 300,
          title: "trace-facts:audit_facts",
          status: "success",
          source_locations: [{ path: "src/pricing.mjs", line_start: 1, line_end: 20 }],
          typed_resources: [
            {
              type: "repo_fact",
              key: "pricing-owner",
              fact: "pricing.mjs owns coupon math.",
              source_location: { path: "src/pricing.mjs", line_start: 1, line_end: 20 },
            },
          ],
          data: {
            input: { tool: "audit_facts" },
            output: { fact: "pricing.mjs owns coupon math." },
          },
        },
        {
          record_id: "resp_1",
          component: "result",
          event_type: "response.output",
          timestamp: "2026-06-30T00:00:00.900Z",
          time_ms: 900,
          title: "Response output 1",
          source_refs: ["tool_span:span_1"],
          data: {
            text: "pricing bug is line 5",
            response_role: "final_answer",
            is_final_for_case: true,
          },
        },
      ],
      dataflow_edges: [
        {
          edge_id: "edge_1",
          from: { type: "node", id: "mcp_1" },
          to: { type: "node", id: "resp_1" },
          relation: "consumed",
          label: "MCP fact used by final response",
        },
      ],
      artifacts: [
        {
          artifact_id: "artifact_1",
          kind: "text",
          label: "llm.output",
          path: "artifacts/sha256/artifact_1.txt",
          length: 120,
          hash: "hash",
          preview: "artifact preview",
          created_at: "2026-06-30T00:00:00.000Z",
        },
      ],
      metrics: {
        spans: 1,
        events: 2,
        records: 3,
        dataflow_edges: 1,
        artifacts: 1,
        token_usage: { input: 10, output: 5, total: 15 },
      },
    }

    const html = renderProvenanceTraceHtml(trace)

    for (const id of [
      "overview",
      "agent-flow",
      "component-dataflow",
      "io-inspector",
      "semantic-facts",
      "context-compaction",
      "artifacts",
    ]) {
      expect(html).toContain(`id="${id}"`)
    }
    expect(html).toContain("Trace v4.2")
    expect(html).toContain('class="io-grid"')
    expect(html).toContain('class="io-input"')
    expect(html).toContain('class="io-output"')
    expect(html).toContain("repo_fact")
    expect(html).toContain("pricing-owner")
    expect(html).toContain("final_answer")
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
    expect(html).toContain("Trace Provenance")
    expect(html).toContain("Artifacts")
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
        `CaseTrace.decision({ span_id: span?.id, component: "llm", decision_type: "tool_call", intent: "run tests", chosen_action: "bash", rationale: "Need verification", source_refs: ["ctx_1"] })`,
        `CaseTrace.verification({ span_id: span?.id, tool_call_id: "call_1", command: "node test.js", cwd: "/tmp/project", purpose: "Run unit tests", stage: "baseline", exit_code: 1, status: "failed", stdout: "Error: expected 170, got 30", stderr: "" })`,
        `CaseTrace.change({ span_id: span?.id, tool_call_id: "call_2", files: ["src/pricing.mjs"], intent: "Fix discount formula", diff: "- old\\\\n+ new", source_refs: ["ver_1"] })`,
        `CaseTrace.constraint({ source: "user", constraint: "do not modify files", status: "observed_satisfied", source_refs: ["span_1"] })`,
        `CaseTrace.responseOutput({ response_artifact: "artifact_final", text: "The formula returned discount amount instead of discounted price.", source_refs: ["ver_1", "chg_1"] })`,
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
    const traceText = await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")
    const trace = JSON.parse(traceText) as any

    expect(trace.trace_version).toBe("1.3")
    expect(trace.context_snapshots).toHaveLength(1)
    expect(trace.semantic_decisions).toHaveLength(1)
    expect(trace.verification_records).toHaveLength(1)
    expect(trace.change_records).toHaveLength(1)
    expect(trace.constraint_records).toHaveLength(1)
    expect(trace.response_segments).toHaveLength(1)
    expect(trace.dataflow_edges.length).toBeGreaterThanOrEqual(1)
    expect(trace.dataflow_edges.some((edge: any) => edge.relation === "failure_to_change")).toBe(true)
    expect(trace.context_snapshots[0].messages.artifact_id).toBeTruthy()
    expect(trace.verification_records[0].parsed_failures[0]).toMatchObject({ expected: "170", actual: "30" })
    expect(traceText).not.toContain(secret)
    expect(traceText).not.toContain("Bearer abc")
    expect(traceText).not.toContain("final_response_evidence")
    expect(traceText).not.toContain("evidence_refs")

    const artifactText = await fs.readFile(path.join(caseDir, trace.artifacts[0].path), "utf8")
    expect(artifactText).toContain("semantic model message")
    expect(artifactText).not.toContain(secret)
    expect(artifactText).not.toContain("Bearer abc")

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    expect(html).toContain("Trace Provenance")
    expect(html).toContain("Component Dataflow")
    expect(html).toContain("IO Inspector")
    expect(html).toContain("Context Ledger")
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

    const traceText = await fs.readFile(path.join(dir, "redaction-case", "legacy-trace.json"), "utf8")
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

    const trace = JSON.parse(await fs.readFile(path.join(dir, "failure-parse-case", "legacy-trace.json"), "utf8")) as any
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

    const ok = JSON.parse(await fs.readFile(path.join(dir, "readonly-ok", "legacy-trace.json"), "utf8")) as any
    const violated = JSON.parse(await fs.readFile(path.join(dir, "readonly-violated", "legacy-trace.json"), "utf8")) as any

    expect(ok.constraint_records[0]).toMatchObject({
      constraint: "Do not modify repository files",
      status: "observed_satisfied",
    })
    expect(violated.constraint_records[0]).toMatchObject({
      constraint: "Do not modify repository files",
      status: "observed_violated",
    })
  })

  test("uses concrete source refs for response output segments", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-case-trace-source-refs-"))
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
        `CaseTrace.responseOutput({ text: "Fixed discount calculation.", source_refs: ["recent_tool_results", "recent_verification_records", "recent_change_records"] })`,
        `CaseTrace.finish({ status: "success" })`,
      ].join("\n"),
    )

    const proc = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
      OPENCODE_CASE_ID: "source-ref-case",
        OPENCODE_CASE_TRACE_DIR: dir,
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const code = await proc.exited
    const stderr = await new Response(proc.stderr).text()

    expect(stderr).toBe("")
    expect(code).toBe(0)

    const trace = JSON.parse(await fs.readFile(path.join(dir, "source-ref-case", "legacy-trace.json"), "utf8")) as any
    const refs = trace.response_segments[0].source_refs

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
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "legacy-trace.json"), "utf8")) as any
    expect(trace.design_records).toHaveLength(1)
    expect(trace.design_records[0].selected_solution.artifact_id).toBeTruthy()

    const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
    expect(html).toContain("Trace Provenance")
    expect(html).toContain("Artifacts")
  })
})
