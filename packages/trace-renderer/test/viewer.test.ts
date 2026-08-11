import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { renderCaseTraceHtml } from "../src/html"
import { renderProvenanceTraceHtml } from "../src/viewer"
import type { ProvenanceTraceSummary, TraceSummary } from "opencode/observability/case-trace"

describe("trace renderer visual contracts", () => {
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
  test("renders v6.0 provenance report with flow-style sections and scrollable IO panes", () => {
    const trace: ProvenanceTraceSummary = {
      trace_version: "6.0",
      manifest: {
        trace_version: "6.0",
        case_id: "viewer-v46-case",
        run_id: "run_viewer_v46",
        started_at: "2026-06-30T00:00:00.000Z",
        ended_at: "2026-06-30T00:00:01.000Z",
        duration_ms: 1000,
        status: "success",
        collection_mode: "passive_sidecar",
        behavior_impact: "none",
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
        {
          record_id: "evidence_1",
          component: "mcp",
          event_type: "evidence.semantic_fact",
          timestamp: "2026-06-30T00:00:00.820Z",
          time_ms: 820,
          title: "repo_fact",
          status: "success",
          source_refs: ["mcp:mcp_1"],
          source_locations: [{ path: "src/pricing.mjs", line_start: 5, line_end: 5 }],
          data: {
            fact_kind: "mcp_fact",
            canonical_subject: "pricing",
            claim: "pricing bug is line 5",
            structured_claim: {
              subject: "pricing",
              predicate: "bug_location",
              value: "line 5",
              extraction_method: "mcp_json_text",
              source_span: { path: "src/pricing.mjs", line_start: 5, line_end: 5 },
            },
            quality_flags: [],
          },
        },
        {
          record_id: "claim_1",
          component: "result",
          event_type: "response.claim",
          timestamp: "2026-06-30T00:00:00.920Z",
          time_ms: 920,
          title: "Response claim 1",
          status: "success",
          source_refs: ["evidence:evidence_1", "tool_span:span_1"],
          data: {
            text: "pricing bug is line 5",
            response_segment_id: "segment_1",
            claim_index: 1,
            direct_evidence_refs: ["evidence:evidence_1"],
            context_refs: [],
            execution_refs: ["tool_span:span_1"],
            support_level: "direct",
            quality_flags: [],
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
        {
          edge_id: "edge_2",
          from: { type: "evidence", id: "evidence_1" },
          to: { type: "response_claim", id: "claim_1" },
          relation: "supports_claim",
          label: "Evidence supports response claim",
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
        records: 5,
        dataflow_edges: 2,
        artifacts: 1,
        token_usage: { input: 10, output: 5, total: 15 },
        trace_health: {
          circular_reference_markers: 0,
          open_records: 1,
          finalized_open_records: 1,
          expected_lifecycle_finalized_records: 1,
          unexpected_missing_close_records: 0,
          llm_turns_missing_token_usage: 0,
          llm_turns_missing_finish_reason: 0,
          compaction_quality_flags: {},
          empty_subagent_results: 0,
          broad_response_refs: 0,
          duplicate_evidence_facts: 0,
          duplicate_semantic_facts: 0,
          generic_evidence_facts: 0,
          generic_semantic_facts: 0,
          unsupported_response_claims: 0,
          context_only_response_claims: 0,
          execution_observations: 0,
          task_plan_states: 0,
          payload_duplication_groups: 0,
          compaction_check_missing: 0,
          issues: [],
        },
      },
    }

    const html = renderProvenanceTraceHtml(trace)

    for (const id of [
      "overview",
      "trace-health",
      "semantic-pipeline",
      "llm-turns",
      "lifecycle",
      "subagents",
      "claim-evidence-matrix",
      "evidence-facts",
      "execution-observations",
      "agent-flow",
      "component-dataflow",
      "io-inspector",
      "semantic-facts",
      "context-compaction",
      "artifacts",
    ]) {
      expect(html).toContain(`id="${id}"`)
    }
    expect(html).toContain("Trace v6.0")
    expect(html).toContain("Trace Health")
    expect(html).toContain("Claim Evidence Matrix")
    expect(html).toContain("structured_claim")
    expect(html).toContain('class="io-grid"')
    expect(html).toContain('class="io-input"')
    expect(html).toContain('class="io-output"')
    expect(html).toContain("repo_fact")
    expect(html).toContain("pricing-owner")
    expect(html).toContain("final_answer")
  })
  test("renders authorized artifact-backed summaries with expandable full content", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-authorized-artifact-html-"))
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

    try {
      await fs.mkdir(path.join(dir, "artifacts"), { recursive: true })
      await fs.writeFile(path.join(dir, "artifacts/artifact_1.txt"), "authoritative artifact body")
      const html = renderCaseTraceHtml(trace, {
        artifactDir: dir,
        artifactContents: new Map([["artifact_1", "full semantic model messages payload"]]),
      } as any)

      expect(html).toContain("Artifacts")
      expect(html).toContain("查看完整内容")
      expect(html).toContain("full semantic model messages payload")
      const snapshotDigest = createHash("sha256").update("authoritative artifact body").digest("hex")
      expect(html).toContain(`href="artifacts/render-snapshots/sha256/${snapshotDigest}"`)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })
})
