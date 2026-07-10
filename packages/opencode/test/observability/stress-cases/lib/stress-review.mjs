import fs from "node:fs"
import path from "node:path"

export function loadCases(rootDir) {
  const file = path.join(rootDir, "cases.json")
  const cases = JSON.parse(fs.readFileSync(file, "utf8"))
  if (!Array.isArray(cases)) throw new Error(`cases.json must be an array: ${file}`)
  return cases
}

export function reviewTraceSufficiency({ caseDefinition, trace }) {
  const found = caseDefinition.required_trace_evidence.map((name) => {
    const refs = detectEvidence(name, trace)
    return {
      required: name,
      status: refs.length ? "found" : "missing",
      record_refs: refs,
    }
  })
  const foundCount = found.filter((item) => item.status === "found").length
  const total = found.length || 1
  const ratio = foundCount / total
  const traceSufficiency = ratio === 1 ? "sufficient" : ratio >= 0.5 ? "partial" : "insufficient"
  const missingSemantics = found.filter((item) => item.status === "missing").map((item) => item.required)
  const mechanismFound = requiredTraceMechanisms(caseDefinition).map((name) => {
    const refs = detectMechanism(name, trace)
    return {
      required: name,
      status: refs.length ? "found" : "missing",
      record_refs: refs,
    }
  })
  const missingMechanisms = mechanismFound.filter((item) => item.status === "missing").map((item) => item.required)
  const caseEffectiveness = missingMechanisms.length ? "ineffective" : "effective"
  const noisy = detectNoisySemantics(trace)

  return {
    case_id: caseDefinition.case_id,
    title: caseDefinition.title,
    category: caseDefinition.category,
    ground_truth_root_cause: caseDefinition.ground_truth_root_cause,
    trace_sufficiency: traceSufficiency,
    case_effectiveness: caseEffectiveness,
    evidence_found: found,
    mechanism_evidence_found: mechanismFound,
    can_offline_module_identify_root_cause: traceSufficiency === "sufficient" && caseEffectiveness === "effective",
    missing_semantics: missingSemantics,
    missing_mechanisms: missingMechanisms,
    redundant_or_noisy_semantics: noisy,
    recommended_trace_changes: recommendTraceChanges(missingSemantics, missingMechanisms),
  }
}

export function summarizeReviews(reviews) {
  const counts = { sufficient: 0, partial: 0, insufficient: 0 }
  for (const review of reviews) counts[review.trace_sufficiency] = (counts[review.trace_sufficiency] ?? 0) + 1
  const effectivenessCounts = { effective: 0, ineffective: 0 }
  for (const review of reviews)
    effectivenessCounts[review.case_effectiveness] = (effectivenessCounts[review.case_effectiveness] ?? 0) + 1
  const lines = [
    "# Trace Stress Case Sufficiency Summary",
    "",
    `- sufficient: ${counts.sufficient}`,
    `- partial: ${counts.partial}`,
    `- insufficient: ${counts.insufficient}`,
    `- effective cases: ${effectivenessCounts.effective}`,
    `- ineffective cases: ${effectivenessCounts.ineffective}`,
    "",
    "| Case | Root Cause | Sufficiency | Effectiveness | Missing Semantics | Missing Mechanisms |",
    "|---|---|---|---|---|---|",
  ]
  for (const review of reviews) {
    lines.push(
      `| ${review.case_id} | ${review.ground_truth_root_cause.component}/${review.ground_truth_root_cause.failure_type} | ${review.trace_sufficiency} | ${review.case_effectiveness} | ${review.missing_semantics.join(", ") || "-"} | ${review.missing_mechanisms.join(", ") || "-"} |`,
    )
  }
  lines.push("")
  return lines.join("\n")
}

function requiredTraceMechanisms(caseDefinition) {
  if (Array.isArray(caseDefinition.required_trace_mechanisms)) {
    return caseDefinition.required_trace_mechanisms.map(String).filter(Boolean)
  }
  const byCategory = {
    context_compaction: ["context.compaction"],
    subagent_coordination: ["subagent.call"],
    mcp_usage: ["mcp.call"],
    tool_failure: ["tool.error"],
  }
  return byCategory[caseDefinition.category] ?? []
}

function detectMechanism(name, trace) {
  const records = Array.isArray(trace?.records) ? trace.records : []
  return records.filter((record) => record.event_type === name).map((record) => recordRef(record))
}

function detectEvidence(name, trace) {
  const detectors = {
    search_query_and_results: (records) =>
      records.filter(
        (record) =>
          hasAny(record, ["grep", "rg", "search", "glob"]) &&
          (hasAny(record, ["pattern", "query", "matches", "Found "]) || record.event_type === "tool.call"),
      ),
    candidate_files_considered: (records) =>
      records.filter((record) => hasAny(record, ["src/pricing.mjs", "legacy", "current-requirement", "old-design"])),
    target_selection_rationale: (records) =>
      records.filter(
        (record) =>
          record.event_type === "decision" &&
          hasAny(record, ["rationale", "chosen_action", "recent_reasoning", "Need source evidence"]),
      ),
    edited_file_paths: (records) =>
      records.filter(
        (record) =>
          record.event_type === "change" ||
          ((record.event_type === "tool.call" || record.event_type === "execution.observation") &&
            hasAny(record, ["edit", "write", "patch", "src/", ".mjs"])),
      ),
    final_test_result: (records) =>
      records.filter(
        (record) =>
          record.event_type === "verification" ||
          (record.event_type === "evidence.semantic_fact" && hasAny(record, ["verification_output", "npm test"])) ||
          hasAny(record, ["pricing tests passed", "AssertionError", "exit_code"]),
      ),
    semantic_fact_values: (records) =>
      records.filter(
        (record) =>
          record.event_type === "evidence.semantic_fact" &&
          hasAny(record, ["structured_claim", "subject", "predicate", "value"]),
      ),
    conflict_fact_group: (records) => {
      const text = JSON.stringify(records)
      return text.includes("15") && text.includes("20")
        ? records.filter((record) => record.event_type === "evidence.semantic_fact" || hasAny(record, ["15", "20"]))
        : []
    },
    claim_direct_evidence_refs: (records) =>
      records.filter(
        (record) =>
          record.event_type === "response.claim" &&
          (Array.isArray(record.data?.direct_evidence_refs) ? record.data.direct_evidence_refs.length > 0 : false),
      ),
    mcp_call_output: (records) =>
      records.filter(
        (record) =>
          record.event_type === "mcp.call" ||
          (record.event_type === "evidence.semantic_fact" && hasAny(record, ["mcp", "syntheticFacts"])),
      ),
    fact_in_context_package: (records) =>
      records.filter(
        (record) =>
          (record.event_type === "context.pack" || record.event_type === "context.transform") &&
          hasAny(record, ["discount_cap", "billing-platform", "evidence:"]),
      ),
    compaction_ledger: (records) =>
      records.filter(
        (record) =>
          record.event_type === "context.compaction" &&
          hasAny(record, ["context_ledger", "retained", "dropped", "summary_artifact"]),
      ),
    post_compaction_llm_context: (records) =>
      records.filter(
        (record) =>
          (record.event_type === "context.transform" || record.event_type === "llm.call") &&
          hasAny(record, ["model_messages", "llm_request_ready", "after_context_refs"]),
      ),
    subagent_trace: (records) =>
      records.filter(
        (record) =>
          record.event_type === "subagent.call" &&
          hasAny(record, ["child_trace", "child_session_id", "child_key_evidence_refs"]),
      ),
    subagent_parent_consumption: (records) =>
      records.filter((record) => record.event_type === "subagent.call" && hasAny(record, ["reported_to", "output"])),
    verification_commands: (records) =>
      records.filter((record) => record.event_type === "verification" || hasAny(record, ["npm test", "test:full"])),
    test_scope_metadata: (records) =>
      records.filter((record) => hasAny(record, ["test:full", "test:pricing", "package.json", "owner.test"])),
    tool_error_observation: (records) =>
      records.filter(
        (record) =>
          record.event_type === "tool.error" &&
          (record.status === "error" ||
            record.data?.status === "error" ||
            hasAny(record, ["ENOENT", "not found", "isError", "ERR", "No such file"])),
      ),
    claim_support_assessment: (records) =>
      records.filter(
        (record) =>
          record.event_type === "claim.support_assessment" &&
          (record.data?.support_level || record.data?.tool_failure_dependency_refs || record.data?.quality_flags),
      ),
    tool_failure_claim_dependency: (records) =>
      records.filter(
        (record) =>
          record.event_type === "claim.support_assessment" &&
          Array.isArray(record.data?.tool_failure_dependency_refs) &&
          record.data.tool_failure_dependency_refs.some((ref) => String(ref).startsWith("tool_error:")),
      ),
    tool_failure_handled_or_unsupported: (records, fullTrace) => {
      const unsupported = detectors.unsupported_claims(records, fullTrace)
      if (unsupported.length) return unsupported
      return records.filter(
        (record) =>
          record.event_type === "claim.support_assessment" &&
          Array.isArray(record.data?.tool_failure_dependency_refs) &&
          record.data.tool_failure_dependency_refs.some((ref) => String(ref).startsWith("tool_error:")) &&
          (!Array.isArray(record.data?.missing_evidence_types) || record.data.missing_evidence_types.length === 0),
      )
    },
    required_action_obligations: (records) =>
      records.filter(
        (record) =>
          record.event_type === "task.obligation" &&
          hasAny(record, ["obligation_type", "status"]) &&
          (record.data?.obligation_type || record.data?.status),
      ),
    unsupported_claims: (records, fullTrace) => {
      const health = fullTrace?.metrics?.trace_health ?? {}
      if ((health.unsupported_response_claims ?? 0) > 0 || (health.context_only_response_claims ?? 0) > 0) {
        return records.filter((record) => record.event_type === "response.claim")
      }
      return records.filter(
        (record) =>
          record.event_type === "response.claim" &&
          (!Array.isArray(record.data?.direct_evidence_refs) || record.data.direct_evidence_refs.length === 0),
      )
    },
    design_constraint_facts: (records) =>
      records.filter(
        (record) =>
          (record.event_type === "evidence.semantic_fact" || record.event_type === "execution.observation") &&
          hasAny(record, ["architecture", "design", "constraint", "hardcode", "public API"]),
      ),
    change_diff_semantics: (records) =>
      records.filter(
        (record) =>
          record.event_type === "change" &&
          (record.data?.change_semantics ||
            hasAny(record, ["numeric_constant_update", "hardcode_candidate", "conditional_logic_change"])),
      ),
    architecture_violation_signal: (records) =>
      records.filter(
        (record) =>
          semanticRiskFlags(record).some((flag) =>
            ["hardcode_candidate", "test_fixture_value_added", "test_coupling_candidate"].includes(flag),
          ) || hasAny(record, ["hardcode", "bypass", "violat", "architecture", "constraint"]),
      ),
  }
  const records = Array.isArray(trace?.records) ? trace.records : []
  const detector = detectors[name] ?? (() => [])
  return detector(records, trace).map((record) => recordRef(record))
}

function recordRef(record) {
  if (record.event_type === "verification" && record.data?.verification_id)
    return `verification:${record.data.verification_id}`
  if (record.event_type === "change" && record.data?.change_id) return `change:${record.data.change_id}`
  if (record.event_type === "evidence.semantic_fact") return `evidence:${record.record_id}`
  return `record:${record.record_id ?? record.event_type ?? "unknown"}`
}

function hasAny(input, needles) {
  const text = JSON.stringify(input).toLowerCase()
  return needles.some((needle) => text.includes(String(needle).toLowerCase()))
}

function semanticRiskFlags(record) {
  const flags = record?.data?.change_semantics?.risk_flags
  return Array.isArray(flags) ? flags.map(String) : []
}

function detectNoisySemantics(trace) {
  const noisy = []
  const health = trace?.metrics?.trace_health ?? {}
  if ((health.broad_response_refs ?? 0) > 0) noisy.push("broad_response_refs")
  if ((health.duplicate_semantic_facts ?? 0) > 0) noisy.push("duplicate_semantic_facts")
  if ((health.generic_semantic_facts ?? 0) > 0) noisy.push("generic_semantic_facts")
  if ((health.path_only_evidence_facts ?? 0) > 0) noisy.push("path_only_evidence_facts")
  if ((health.broad_legacy_context_refs ?? 0) > 0 || hasBroadLegacyContextRefs(trace)) {
    noisy.push("broad_legacy_context_refs")
  }
  if (hasCancelledAfterCompletedCase(trace)) noisy.push("cancelled_after_case_completion")
  return noisy
}

function hasBroadLegacyContextRefs(trace) {
  const records = Array.isArray(trace?.records) ? trace.records : []
  return records.some(
    (record) =>
      Array.isArray(record.data?.legacy_context_refs) &&
      record.data.legacy_context_refs.length > 20,
  )
}

function hasCancelledAfterCompletedCase(trace) {
  const records = Array.isArray(trace?.records) ? trace.records : []
  const completed = records.some((record) => record.event_type === "case.completed" && record.status === "success")
  if (!completed) return false
  return records.some(
    (record) =>
      record.status === "cancelled" &&
      record.data?.finalized_status === "finalized_without_close" &&
      record.data?.finalized_reason === "trace_cancelled",
  )
}

function recommendTraceChanges(missing, missingMechanisms = []) {
  const recommendations = new Set()
  for (const item of missingMechanisms) {
    recommendations.add(
      `Stress case did not trigger ${item}; redesign the case or runner so this mechanism occurs before judging trace sufficiency.`,
    )
  }
  for (const item of missing) {
    if (item.includes("candidate") || item.includes("search") || item.includes("target")) {
      recommendations.add("Record search result candidate sets, rankings, and selected target rationale.")
    }
    if (item.includes("conflict") || item.includes("semantic_fact")) {
      recommendations.add("Canonicalize semantic facts and group conflicting subject/predicate values.")
    }
    if (item.includes("mcp") || item.includes("context")) {
      recommendations.add("Record whether MCP facts enter the context package and final claim attribution.")
    }
    if (item.includes("compaction")) {
      recommendations.add("Expose compaction ledgers with retained and dropped semantic facts.")
    }
    if (item.includes("subagent")) {
      recommendations.add("Attach child trace summaries and child evidence refs to parent subagent records.")
    }
    if (item.includes("verification") || item.includes("test")) {
      recommendations.add("Structure verification scope, command, assertion failures, and claim-to-test links.")
    }
    if (item.includes("tool_error") || item.includes("unsupported")) {
      recommendations.add("Link tool failures to later unsupported or context-only response claims.")
    }
    if (item.includes("obligation")) {
      recommendations.add("Emit task.obligation records for user-required actions and path constraints.")
    }
    if (item.includes("claim_support")) {
      recommendations.add("Emit claim.support_assessment records with support level, quality flags, and failure refs.")
    }
    if (item.includes("design") || item.includes("change") || item.includes("architecture")) {
      recommendations.add("Extract semantic diff summaries and compare changes with design constraints.")
    }
  }
  if (!recommendations.size) recommendations.add("No trace schema change recommended by this review.")
  return [...recommendations]
}
