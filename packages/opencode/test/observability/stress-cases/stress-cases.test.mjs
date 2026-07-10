import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { analyzeTraceDirectory } from "./analyze-trace-sufficiency.mjs"
import { loadCases, reviewTraceSufficiency, summarizeReviews } from "./lib/stress-review.mjs"

const rootDir = path.dirname(fileURLToPath(import.meta.url))

test("stress cases define eight grounded root-cause scenarios with fixtures", () => {
  const cases = loadCases(rootDir)

  assert.equal(cases.length, 8)
  assert.deepEqual(
    cases.map((item) => item.case_id),
    [
      "wrong-implementation-target",
      "conflicting-evidence",
      "ignored-mcp-fact",
      "compaction-lost-constraint",
      "subagent-misleading-summary",
      "insufficient-verification",
      "tool-failure-hallucination",
      "design-quality-regression",
    ],
  )

  for (const item of cases) {
    assert.equal(typeof item.title, "string", item.case_id)
    assert.equal(typeof item.prompt, "string", item.case_id)
    assert.equal(typeof item.fixture_dir, "string", item.case_id)
    assert.equal(typeof item.expected_good_result, "string", item.case_id)
    assert.equal(typeof item.designed_failure_mode, "string", item.case_id)
    assert.equal(typeof item.ground_truth_root_cause.component, "string", item.case_id)
    assert.equal(typeof item.ground_truth_root_cause.failure_type, "string", item.case_id)
    assert.ok(item.required_trace_evidence.length >= 4, item.case_id)
    assert.ok(item.sufficiency_questions.length >= 3, item.case_id)
    assert.ok(fs.existsSync(path.join(rootDir, item.fixture_dir, "package.json")), item.case_id)
    assert.ok(fs.existsSync(path.join(rootDir, item.fixture_dir, "opencode.json")), item.case_id)
  }
})

test("trace sufficiency review marks missing evidence as insufficient", () => {
  const [caseDefinition] = loadCases(rootDir)
  const trace = {
    trace_version: "5.5",
    manifest: { case_id: caseDefinition.case_id },
    records: [
      {
        record_id: "resp_1",
        event_type: "response.output",
        component: "result",
        data: { text: "I changed the implementation." },
        source_refs: [],
      },
    ],
    dataflow_edges: [],
    metrics: { trace_health: { broad_response_refs: 0 } },
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })

  assert.equal(review.case_id, caseDefinition.case_id)
  assert.equal(review.trace_sufficiency, "insufficient")
  assert.equal(review.can_offline_module_identify_root_cause, false)
  assert.ok(review.missing_semantics.includes(caseDefinition.required_trace_evidence[0]))
  assert.ok(review.evidence_found.every((item) => item.status === "missing"))
})

test("trace summary handles missing trace reviews and subset analysis", () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-stress-subset-"))
  const traces = path.join(temp, "traces")
  const reports = path.join(temp, "reports")

  const reviews = analyzeTraceDirectory({
    casesFile: path.join(rootDir, "cases.json"),
    tracesDir: traces,
    outDir: reports,
    caseIDs: ["ignored-mcp-fact"],
  })
  const summary = summarizeReviews(reviews)

  assert.equal(reviews.length, 1)
  assert.equal(reviews[0].case_id, "ignored-mcp-fact")
  assert.equal(reviews[0].case_effectiveness, "ineffective")
  assert.deepEqual(reviews[0].missing_mechanisms, ["mcp.call"])
  assert.ok(summary.includes("| ignored-mcp-fact |"))
  assert.ok(!summary.includes("| wrong-implementation-target |"))
})

test("stress runner supports explicit multi-step HTTP flows for compaction scenarios", async () => {
  const runner = await import("./run-stress-cases.mjs")
  assert.equal(typeof runner.planCaseActions, "function")

  const defaultActions = runner.planCaseActions({
    case_id: "single-turn",
    prompt: "Fix the bug and run npm test.",
  })

  assert.deepEqual(defaultActions, [
    {
      type: "prompt",
      text: "Fix the bug and run npm test.",
    },
  ])

  const compactionCase = loadCases(rootDir).find((item) => item.case_id === "compaction-lost-constraint")
  const flowActions = runner.planCaseActions(compactionCase)

  assert.deepEqual(
    flowActions.map((item) => item.type),
    ["prompt", "summarize", "prompt"],
  )
  assert.match(flowActions[0].text, /docs\/large-context\.md/)
  assert.match(flowActions[0].text, /src\/payment/)
  assert.equal(flowActions[1].auto, false)
  assert.match(flowActions[2].text, /上一轮|前一轮/)
  assert.doesNotMatch(flowActions[2].text, /src\/payment/)
})

test("trace sufficiency review marks mechanism-missing cases as ineffective", () => {
  const caseDefinition = {
    ...loadCases(rootDir).find((item) => item.case_id === "compaction-lost-constraint"),
    required_trace_evidence: ["semantic_fact_values"],
    required_trace_mechanisms: ["context.compaction"],
  }
  const trace = {
    trace_version: "5.5",
    manifest: { case_id: caseDefinition.case_id },
    records: [
      {
        record_id: "fact_constraint",
        event_type: "evidence.semantic_fact",
        component: "processor",
        data: {
          structured_claim: {
            subject: "src/payment",
            predicate: "must_not_modify",
            value: true,
          },
        },
      },
    ],
    dataflow_edges: [],
    metrics: { trace_health: {} },
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })

  assert.equal(review.trace_sufficiency, "sufficient")
  assert.equal(review.case_effectiveness, "ineffective")
  assert.equal(review.can_offline_module_identify_root_cause, false)
  assert.deepEqual(review.missing_mechanisms, ["context.compaction"])
  assert.deepEqual(review.mechanism_evidence_found, [
    {
      required: "context.compaction",
      status: "missing",
      record_refs: [],
    },
  ])
})

test("trace sufficiency review requires formal tool error and claim support facts", () => {
  const caseDefinition = {
    ...loadCases(rootDir).find((item) => item.case_id === "tool-failure-hallucination"),
    required_trace_evidence: ["tool_error_observation", "claim_support_assessment", "tool_failure_claim_dependency"],
  }
  const trace = {
    trace_version: "5.5",
    manifest: { case_id: caseDefinition.case_id, case_status: "success", server_status: "cancelled" },
    records: [
      {
        record_id: "case_completed",
        event_type: "case.completed",
        component: "run",
        status: "success",
        data: { case_status: "success", server_status: "cancelled" },
      },
      {
        record_id: "life_1",
        event_type: "agent.lifecycle",
        component: "processor",
        status: "cancelled",
        data: { finalized_status: "finalized_without_close", finalized_reason: "trace_cancelled" },
      },
      {
        record_id: "claim_1",
        event_type: "response.claim",
        component: "result",
        status: "success",
        data: {
          text: "docs/current-requirement.md 文件不存在（工具失败）。",
          direct_evidence_refs: ["response_segment:seg_1"],
        },
      },
    ],
    dataflow_edges: [],
    metrics: { trace_health: { broad_response_refs: 0 } },
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })

  assert.equal(review.trace_sufficiency, "insufficient")
  assert.equal(review.can_offline_module_identify_root_cause, false)
  assert.ok(review.missing_semantics.includes("tool_error_observation"))
  assert.ok(review.missing_semantics.includes("claim_support_assessment"))
  assert.ok(review.missing_semantics.includes("tool_failure_claim_dependency"))
  assert.ok(review.redundant_or_noisy_semantics.includes("cancelled_after_case_completion"))
})

test("trace sufficiency review recognizes tool failures linked to claim support", () => {
  const caseDefinition = {
    ...loadCases(rootDir).find((item) => item.case_id === "tool-failure-hallucination"),
    required_trace_evidence: ["tool_failure_claim_dependency"],
  }
  const trace = {
    trace_version: "5.5",
    manifest: { case_id: caseDefinition.case_id },
    records: [
      {
        record_id: "toolerror_call_missing",
        event_type: "tool.error",
        component: "tool",
        status: "error",
        data: {
          call_id: "call_missing",
          tool_name: "read",
          error_kind: "file_not_found",
          error_message: "No such file docs/current-requirement.md",
        },
        source_refs: ["tool_error:call_missing"],
      },
      {
        record_id: "claimsupport_1",
        event_type: "claim.support_assessment",
        component: "result",
        status: "success",
        data: {
          claim_id: "claim_1",
          support_level: "direct",
          direct_evidence_refs: ["tool_error:call_missing"],
          tool_failure_dependency_refs: ["tool_error:call_missing"],
        },
      },
    ],
    dataflow_edges: [],
    metrics: { trace_health: { broad_response_refs: 0 } },
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })

  assert.equal(review.trace_sufficiency, "sufficient")
  assert.equal(review.can_offline_module_identify_root_cause, true)
  assert.deepEqual(review.missing_semantics, [])
})

test("trace sufficiency review accepts handled tool failures without unsupported claims", () => {
  const caseDefinition = {
    ...loadCases(rootDir).find((item) => item.case_id === "tool-failure-hallucination"),
    required_trace_evidence: ["tool_failure_handled_or_unsupported"],
  }
  const trace = {
    trace_version: "5.5",
    manifest: { case_id: caseDefinition.case_id },
    records: [
      {
        record_id: "claim_1",
        event_type: "response.claim",
        component: "result",
        status: "success",
        data: {
          claim_id: "claim_1",
          support_level: "direct",
          direct_evidence_refs: ["evidence:fact_architecture", "tool_error:call_missing"],
          quality_flags: [],
        },
      },
      {
        record_id: "claimsupport_1",
        event_type: "claim.support_assessment",
        component: "result",
        status: "success",
        data: {
          claim_id: "claim_1",
          support_level: "direct",
          direct_evidence_refs: ["evidence:fact_architecture", "tool_error:call_missing"],
          tool_failure_dependency_refs: ["tool_error:call_missing"],
          missing_evidence_types: [],
        },
      },
    ],
    dataflow_edges: [],
    metrics: { trace_health: { unsupported_response_claims: 0, context_only_response_claims: 0 } },
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })

  assert.equal(review.trace_sufficiency, "sufficient")
  assert.deepEqual(review.missing_semantics, [])
})

test("trace sufficiency review recognizes task obligations and broad legacy context noise", () => {
  const caseDefinition = {
    ...loadCases(rootDir).find((item) => item.case_id === "insufficient-verification"),
    required_trace_evidence: ["required_action_obligations"],
  }
  const trace = {
    trace_version: "5.5",
    manifest: { case_id: caseDefinition.case_id },
    records: [
      {
        record_id: "obl_test",
        event_type: "task.obligation",
        component: "processor",
        data: {
          obligation_type: "verification_required",
          status: "unmet",
          quality_flags: ["task_obligation_unmet"],
        },
      },
      {
        record_id: "claim_legacy_wide",
        event_type: "response.claim",
        component: "result",
        data: {
          text: "Legacy file left untouched.",
          direct_evidence_refs: [],
          legacy_context_refs: Array.from({ length: 24 }, (_, index) => `evidence:legacy_${index}`),
        },
      },
    ],
    dataflow_edges: [],
    metrics: { trace_health: { broad_response_refs: 0 } },
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })

  assert.equal(review.trace_sufficiency, "sufficient")
  assert.deepEqual(review.missing_semantics, [])
  assert.ok(review.redundant_or_noisy_semantics.includes("broad_legacy_context_refs"))
})
