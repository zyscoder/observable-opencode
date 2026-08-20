import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { analyzeTraceDirectory } from "./analyze-trace-sufficiency.mjs"
import { loadCases, reviewTraceSufficiency, scoreTraceQuality, summarizeReviews } from "./lib/stress-review.mjs"

const rootDir = path.dirname(fileURLToPath(import.meta.url))

test("stress cases define grounded root-cause and semantic quality scenarios with fixtures", () => {
  const cases = loadCases(rootDir)

  assert.equal(cases.length, 17)
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
      "context-restart-artifact-contamination",
      "control-flow-preservation",
      "nested-skill-chain",
      "context-restart-precedence-pressure",
      "logging-control-flow-invariant",
      "nested-skill-implicit-dependency",
      "design-quality-regression",
      "semantic-requirement-priority",
      "semantic-architecture-boundary",
      "semantic-verification-depth",
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
    if (item.category === "semantic_quality") {
      assert.ok(Array.isArray(item.quality_rubric), item.case_id)
      assert.ok(item.quality_rubric.length >= 3, item.case_id)
      assert.equal(typeof item.target_score, "number", item.case_id)
      assert.equal(typeof item.minimum_acceptable_score, "number", item.case_id)
    }
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

test("changed_path accepts an absolute traced path for a relative assertion", () => {
  const caseDefinition = {
    case_id: "absolute-changed-path",
    required_trace_evidence: [],
    sufficiency_questions: [],
    acceptance_assertions: [
      {
        id: "pricing_changed",
        type: "changed_path",
        path: "src/pricing.mjs",
        should_change: true,
      },
    ],
  }
  const trace = {
    manifest: { case_id: caseDefinition.case_id },
    records: [
      {
        record_id: "change_1",
        event_type: "change",
        component: "tool",
        data: {
          files: ["/tmp/fixture/src/pricing.mjs"],
        },
      },
    ],
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })

  assert.equal(review.actual_case_outcome.status, "pass")
  assert.deepEqual(review.actual_case_outcome.assertions[0].record_refs, ["record:change_1"])
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

test("quality rubric scores semantic understanding gaps", () => {
  const caseDefinition = {
    case_id: "semantic-unit",
    title: "Semantic unit",
    category: "semantic_quality",
    target_score: 80,
    minimum_acceptable_score: 60,
    quality_rubric: [
      {
        dimension: "requirement_understanding",
        weight: 30,
        evidence: ["semantic_fact_values", "claim_direct_evidence_refs"],
      },
      {
        dimension: "architecture_reasoning",
        weight: 30,
        evidence: ["architecture_boundary_reasoning"],
      },
      {
        dimension: "solution_tradeoff",
        weight: 40,
        evidence: ["alternative_solution_comparison", "risk_assessment"],
      },
    ],
  }
  const trace = {
    records: [
      {
        record_id: "fact_req",
        event_type: "evidence.semantic_fact",
        component: "processor",
        data: { structured_claim: { subject: "discount", predicate: "cap", value: "15%" } },
      },
      {
        record_id: "claim_req",
        event_type: "response.claim",
        component: "result",
        data: { text: "需求为 15% 上限。", direct_evidence_refs: ["evidence:fact_req"] },
      },
      {
        record_id: "claim_arch",
        event_type: "response.claim",
        component: "result",
        data: { text: "架构边界是 billing 负责报价，payment 不应修改。" },
      },
    ],
    metrics: { trace_health: {} },
  }

  const quality = scoreTraceQuality({ caseDefinition, trace })

  assert.equal(quality.total_score, 60)
  assert.equal(quality.status, "meets_minimum")
  assert.deepEqual(
    quality.quality_gaps.map((item) => item.dimension),
    ["solution_tradeoff"],
  )
  assert.deepEqual(quality.quality_gaps[0].gap_context_refs, ["record:claim_req", "record:claim_arch"])
  assert.match(quality.attribution_objective, /solution_tradeoff/)
})

test("quality rubric recognizes risk assessment in response output", () => {
  const caseDefinition = {
    case_id: "response-output-risk",
    category: "semantic_quality",
    target_score: 80,
    minimum_acceptable_score: 60,
    quality_rubric: [
      {
        dimension: "solution_tradeoff",
        weight: 100,
        evidence: ["risk_assessment"],
      },
    ],
  }
  const trace = {
    records: [
      {
        record_id: "response_with_risk",
        event_type: "response.output",
        component: "result",
        data: {
          text: "风险：不能把 billing 的 15% 折扣上限与 payment 的 20% 结算常量混用。",
          response_role: "intermediate_summary",
        },
      },
    ],
    metrics: { trace_health: {} },
  }

  const quality = scoreTraceQuality({ caseDefinition, trace })

  assert.equal(quality.total_score, 100)
  assert.equal(quality.status, "meets_target")
  assert.deepEqual(quality.quality_gaps, [])
  assert.deepEqual(quality.dimensions[0].record_refs, ["record:response_with_risk"])
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

  const contextRecoveryCase = loadCases(rootDir).find((item) => item.case_id === "context-restart-artifact-contamination")
  const contextFlowActions = runner.planCaseActions(contextRecoveryCase)
  assert.deepEqual(contextFlowActions.map((item) => item.type), ["prompt", "new_session", "prompt"])
  assert.match(contextFlowActions[0].text, /stale_run\.md/)
  assert.match(contextFlowActions[2].text, /15%/)
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

test("stress review separates designed failure target from actual case outcome", () => {
  const caseDefinition = {
    case_id: "separated-outcome",
    title: "Separated outcome",
    category: "semantic_quality",
    ground_truth_root_cause: {
      component: "verification",
      failure_type: "insufficient_scope",
      description: "The fixture is designed to tempt a narrow verification.",
    },
    required_trace_evidence: ["final_test_result", "edited_file_paths"],
    required_trace_mechanisms: [],
    acceptance_assertions: [
      { id: "tests", type: "verification_passed", command_contains: "npm test" },
      { id: "target", type: "changed_path", path: "src/owner.mjs", should_change: true },
    ],
  }
  const trace = {
    manifest: { case_status: "success" },
    records: [
      {
        record_id: "change_1",
        event_type: "change",
        component: "tool",
        data: { files: ["src/owner.mjs"] },
      },
      {
        record_id: "verification_1",
        event_type: "verification",
        component: "tool",
        status: "passed",
        data: { command: "npm test", status: "passed", effective_for_final_state: true },
      },
    ],
    metrics: { trace_health: {} },
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })
  const summary = summarizeReviews([review])

  assert.equal(review.designed_failure_target.failure_type, "insufficient_scope")
  assert.equal(review.actual_case_outcome.status, "pass")
  assert.equal(review.mechanism_coverage.status, "sufficient")
  assert.equal(review.ground_truth_root_cause, undefined)
  assert.match(summary, /Designed Failure Target/)
  assert.doesNotMatch(summary, /\| Root Cause \|/)
})

test("stress review reports unknown actual outcome without executable acceptance assertions", () => {
  const caseDefinition = {
    case_id: "unknown-outcome",
    title: "Unknown outcome",
    category: "semantic_quality",
    ground_truth_root_cause: {
      component: "result",
      failure_type: "possible_quality_gap",
      description: "This is only a designed failure target.",
    },
    required_trace_evidence: ["final_test_result"],
    required_trace_mechanisms: [],
  }
  const trace = {
    manifest: { case_status: "success" },
    records: [
      {
        record_id: "verification_1",
        event_type: "verification",
        component: "tool",
        status: "passed",
        data: { command: "npm test", status: "passed", effective_for_final_state: true },
      },
    ],
    metrics: { trace_health: {} },
  }

  const review = reviewTraceSufficiency({ caseDefinition, trace })

  assert.equal(review.mechanism_coverage.status, "sufficient")
  assert.equal(review.actual_case_outcome.status, "unknown")
  assert.equal(review.actual_case_outcome.reason, "no_executable_acceptance_assertions")
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
