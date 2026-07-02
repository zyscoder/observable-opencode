import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { loadCases, reviewTraceSufficiency } from "./lib/stress-review.mjs"

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
    trace_version: "5.2",
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
