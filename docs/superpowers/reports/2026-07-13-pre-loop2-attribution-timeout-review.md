# Pre-Loop 2 Attribution Timeout Review

## Objective

Re-run the same four Loop 1 traces after increasing the per-request attribution timeout from 15 seconds to 3600 seconds. Compare the model-based attribution with manual analysis, then carry attribution-module improvements into Loop 2-10 alongside semantic-trace improvements.

The trace inputs, objective, model, and prompt construction were unchanged. Two 3600-second runs were performed:

- Controlled comparison: `max_depth=2`, `max_nodes=8`.
- Full search bounds: `max_depth=8`, `max_nodes=48`.

## Result Summary

| Case | Manual result | 15-second result | 3600-second controlled result | 3600-second full result |
|---|---|---|---|---|
| `compaction-lost-constraint` | No defect; the summary retained the billing-only constraint | 8 judge errors, 7 false roots | 0 judge errors, no root | 0 judge errors, no root |
| `ignored-mcp-fact` | No defect; MCP fact was used in the change and final answer | 7 judge errors, 6 false roots | 1 malformed-JSON error, 1 false evaluation root | 0 judge errors, no root |
| `tool-failure-hallucination` | No defect; the error was reported and replacement evidence was used | 8 judge errors, 7 false roots | 0 judge errors, no root | 0 judge errors, no root |
| `semantic-architecture-boundary` | No defect; the answer explicitly included risk and verification planning | 4 judge errors, 3 false answer-surface roots | 0 judge errors, 1 false evaluation root | 0 judge errors, 1 false evaluation root |

Artifacts:

- Controlled reports: `/tmp/observable-opencode-loop1-v75/attribution-timeout3600/`
- Full reports: `/tmp/observable-opencode-loop1-v75/attribution-timeout3600-deep/`

## Critical Findings

### 1. The one-hour timeout fixes transport-level false roots

The old run produced 27 judge errors in 28 node judgments. The controlled 3600-second run produced one judge error, caused by malformed JSON after repair rather than timeout. The full run produced no judge errors.

The timeout change is necessary, but it does not fix attribution semantics.

### 2. Ground truth is incorrectly treated as an observed defect

When a stress review has no quality gap or missing trace semantic, `ground_truth_root_cause` is injected as `case.observed_defect`. Ground truth describes the case's designed failure mode, not proof that the failure occurred in this execution.

This creates a self-confirming attribution start node even when the agent completed the case correctly.

### 3. Evaluation nodes have ambiguous semantics

The judge is asked whether the current node contains a defect. For `case.observed_defect`, the model often answers that the evaluation node is only a meta-observation and is not itself defective. The analyzer then stops before checking the response, tool result, context, or change that the observation refers to.

Evaluation nodes must be treated as assertions to validate, not ordinary causal components.

### 4. The architecture quality gap is a false positive

The response explicitly contains a risk section, but the deterministic quality detector excludes `response.output` from `risk_assessment`. It therefore injects a missing-risk quality gap. The prompt then instructs the model to treat every `case.quality_gap` as a defect to explain.

Both 3600-second runs judged all three answer nodes non-defective, yet the analyzer preserved the evaluation node as the root cause. This is an evaluator defect, not an agent defect or a trace-provenance defect.

### 5. Boolean defect status loses uncertainty

`judgment_from_dict` maps a missing `has_defect` field to `False`. Several successful API responses produced empty reason, zero confidence, or `unknown` defect type and were silently accepted as non-defective.

The model output needs a tri-state status:

- `present`
- `absent`
- `unknown`

Incomplete JSON, judge failure, and low-confidence empty judgments must become `unknown`, not `absent` or a root cause.

### 6. Prompt compaction can remove the decisive semantic text

Long nodes are compacted by serializing the whole node and retaining only the leading characters. The architecture judge explicitly reported that the response was truncated. The missing risk text existed in the trace but was not reliably included in the judge prompt.

Compaction must preserve objective-relevant fields such as response text, missing-evidence terms, support status, and direct provenance refs before metadata previews.

### 7. Larger search bounds do not fix an incorrect start judgment

Changing from `max_depth=2`, `max_nodes=8` to `max_depth=8`, `max_nodes=48` did not materially change the four results. Three cases stopped at the start node, and the architecture case visited four nodes in both runs.

The full bounds should remain the default for high-quality analysis, but traversal policy and start-node validation are higher-priority fixes.

### 8. Report status is misleading when no root is found

Successful no-defect cases currently produce `analysis_confidence=blocked`. The report cannot distinguish:

- No observed defect.
- Defect existence is unknown.
- A defect exists but attribution is blocked.
- A root cause was found.
- Search ended because of depth or node limits.

## Loop 2 Implementation Scope

### Offline attribution module

1. Add `defect_status=present|absent|unknown` and an explicit report outcome.
2. Add a defect-presence gate for `case.observed_defect` and `case.quality_gap` before backward traversal.
3. Stop injecting `ground_truth_root_cause` as an observed defect without execution evidence.
4. Invalidate an evaluation gap when its cited upstream nodes are non-defective; do not preserve the evaluator as the root.
5. Validate every judge response against the required schema. Missing required fields trigger repair, then become `unknown` if still incomplete.
6. Record judge stage, raw-response artifact reference, parse/repair status, latency, token usage, and termination reason.
7. Build objective-aware node prompts that preserve full relevant response text and structured evidence before generic metadata.
8. Resolve embedded response provenance refs and rank graph edges as direct causal, consumed, selected context, candidate, or legacy.
9. Report traversal termination as `root_found`, `no_defect`, `inconclusive`, `depth_limit`, `node_limit`, or `queue_exhausted`.
10. Keep the one-hour per-request timeout and add durable per-node checkpoints so long analysis can resume.

### Semantic trace and stress review

1. Include `response.output` in risk, tradeoff, requirement, and verification quality detection.
2. Derive structured answer semantic units passively after execution; never feed them back to the agent.
3. Complete the compaction ledger with algorithm version, input/output message counts, and retained/dropped obligation refs.
4. Separate direct provenance from candidate and legacy context in canonical trace views.
5. Retain raw trace data for inspection while excluding broad legacy edges from default attribution traversal.

## Loop 2 Acceptance Gates

- The four successful Loop 1 traces produce `no_defect` and zero root causes.
- The architecture response is recognized as containing risk assessment.
- At least one genuinely failed stress case reaches a concrete introduction component below the evaluation layer.
- Missing or malformed judge fields produce `unknown`, never implicit `False` or a fabricated root.
- Every report records the effective timeout, judge stages, termination reason, and whether search bounds were reached.
- Controlled and full-bound regressions produce explainable, stable differences.

## Loop 3-10 Joint Optimization Rules

Every loop must include both trace and attribution-module evaluation:

1. Run at least one known-success case that must not produce a root cause.
2. Run at least one genuine failure or quality-degradation case that must produce a causal path.
3. Compare manual backward semantic taint analysis with the offline module.
4. Classify every disagreement as trace missing semantics, graph/provenance error, judge/prompt error, traversal error, or evaluator false positive.
5. Implement trace and attribution fixes independently with separate regression assertions.
6. Re-run previously fixed cases before advancing to the next loop.

Suggested rotating focus:

- Loop 3: response, LLM generation, and message-transform provenance.
- Loop 4: compaction and obligation retention.
- Loop 5: tool, MCP, and skill result consumption.
- Loop 6: subagent orchestration and parent-child evidence flow.
- Loop 7: code discovery and implementation-target selection.
- Loop 8: requirement understanding and solution-design quality.
- Loop 9: judge reliability, latency, checkpointing, and resume behavior.
- Loop 10: full diverse regression and release validation.
