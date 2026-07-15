# Loop 1: Causal IR FeatureBench Attribution Comparison

## Execution

- Benchmark: FeatureBench Lite
- Case: `mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1`
- Agent transport: `opencode serve -> HTTP session -> HTTP message`
- Model: `deepseek-v4-pro`
- Trace: Causal IR `1.0`, trace schema `6.0`
- Case duration: 1,153,846 ms
- Agent result: completed
- Host F2P result: 37 passed, 19 failed, 5 skipped
- Host P2P result: 220 passed, 0 failed

The official Docker harness was unavailable, so the hidden FeatureBench test patch was restored only after the agent completed and was executed in an isolated host virtual environment.

## Manual Backward Taint Result

The implementation is partially correct. The main backward chain is:

1. Nineteen hidden behavioral contracts fail.
2. The final response nevertheless claims that all implementation is complete.
3. The completion decision relies on handwritten happy-path scripts and method-presence checks.
4. Repeated numerical warnings are explicitly accepted as normal.
5. Earlier broad edit actions materialize simplified implementations in `regression.py` and `axisgrid.py`.

Expected implementation roots are the authored edit episodes containing `dec_147` and `dec_157`. Expected verification roots are the narrow self-test episode around `dec_523`/`dec_525` and the warning-dismissal decision `dec_528`. The method-presence strategy is visible around `dec_533` through `dec_553`.

## Offline Attribution Result

The analyzer used `max_depth=24`, `max_nodes=128` per defect branch, a 3,600-second judge timeout, and disabled DeepSeek thinking for structured output.

- Judge errors: 0
- Search-limit truncations: 0
- Explicit manual-review defect branches: 3/3 reached `root_found`
- Primary expected implementation roots covered by episode membership: 2/2
- Expected verification roots covered: 2/2
- Answer-surface false roots: 0
- Truthful tool-result false roots: 0
- Message-lineage gaps: 0
- Artifact loads: 396 loaded, 0 missing, 17 truncated

The analyzer reported five semantically plausible root episodes:

1. The `_LinearPlotter` edit episode, represented by the concrete edit tool call.
2. The `bin_predictor` / `estimate_data` / regression helper edit episode.
3. The handwritten 18-case smoke-test episode.
4. The decision that dismisses numerical warnings and accepts narrow verification.
5. The method/property-presence verification episode.

This is materially better than the previous Seaborn attribution: concrete authored edits now outrank answer text, and the root report retains complete episode membership for audit.

## Remaining Attribution Defects

### Trace-health branches pollute task attribution

The trace contains passive `case.missing_semantic` and `case.observed_defect` records stating that no formal final test result was observed. They are useful observability-health facts, but the analyzer treats them as peer task-defect branches. Both remain inconclusive and downgrade the overall outcome from `root_found` to `partial_root_found`, even though all three explicit task-quality defects reached roots.

Trace-health attribution and task-quality attribution need separate outcome domains. A trace-health branch may remain unresolved without reducing task-root coverage.

### Root confirmation can contradict its own judgment

For `dec_528` and `dec_543`, the judge explanation says the node propagates an already-existing defect, while the final root record labels it `defect_introduction`. This happens when confirmation has no eligible upstream predecessor and promotes the current node to an introduction boundary.

Absence of an upstream edge is not proof of introduction. Such a node should remain `first_observed_propagation` or inconclusive unless the counterfactual confirms that executing the node itself introduces the active defect.

### Generic plans can represent concrete edit episodes

One implementation episode is represented by `dec_155`, a generic plan to implement several methods, even though the same episode contains a concrete edit tool call with the full defective code. For function-level diagnosis, the representative should prefer the most causally specific authored action over the earliest generic plan while retaining all episode members.

### Schema repair remains brittle

Two decision judgments exhausted schema repair because `defect_propagation` did not include a `defect_propagated_from` influence. The analyzer correctly kept these unknown, but the 2/24 unknown rate is avoidable. Repair prompts should enumerate valid predecessor refs and state that propagation without one must be returned as `unknown` or `defect_evidence`.

## Remaining Trace Defects

### Canonical aliases are incomplete

The Causal IR contains 10,247 `unresolved_ref` diagnostics for only 405 unique legacy references:

- `tool_span`: 5,748 occurrences
- `context_snapshot`: 4,074 occurrences
- `span`: 379 occurrences
- `processor_text`: 42 occurrences
- `session`: 3 occurrences
- `message`: 1 occurrence

`context.pack` nodes do not register `context_snapshot:<snapshot_id>`, and `tool.call` nodes do not register `tool_span:<span_id>` / `span:<span_id>`. Session, message, and processor-text identities are intentional external boundaries but are currently treated as unresolved internal nodes.

### Diagnostics and artifacts dominate storage

The trace has 2,622 nodes and 26,654 edges, but its directory is 1.1 GB. Diagnostics repeat the same unresolved reference up to 90 times. Full message-transform artifacts account for about 472 MB, partial snapshots for 81 MB, and `records.jsonl` for 262 MB.

Large text remains valuable and must stay viewable, but unresolved-reference diagnostics should be aggregated, and artifact storage should support compressed content-addressed payloads without removing semantic content.

### Decision provenance is missing

Reasoning decisions such as `dec_528` have empty `input_refs` and `source_refs`. The trace preserves the preceding self-test result and the later decision text, but does not connect the tool result through the prepared context/LLM turn to the decision. This forces the offline analyzer to treat downstream reasoning as having no upstream predecessor.

### Subagent consumption is missing

The explore subagent includes its full prompt, output, 269 child records, and child timeline, but `parent_consumption_refs` is empty. The trace proves execution but not which parent context, LLM turn, or decision consumed the child output.

### Verification scope is under-modeled

The handwritten Python scripts are retained as tool calls/results but not promoted to verification attempts because they are not standard `pytest` commands. Their assertion count, tested behavior list, warning summary, and relationship to the final completion claim are therefore absent from the semantic fact layer.

## Loop 2 Recommendation

Implement a focused `Causal IR v1.1` convergence pass:

1. Add canonical aliases for context snapshots and tool spans; classify intentional session/message/processor-text boundaries as external.
2. Backfill confirmed `tool.result -> context/LLM -> reasoning decision` provenance using message identity and content-matched lineage.
3. Add content-matched subagent-result consumption refs to the first parent context/LLM/decision that consumes the child output.
4. Add passive `verification.attempt` semantics for assertion-bearing or explicitly test-described scripts, including scope and warning summaries.
5. Separate task-quality and trace-health branch outcomes in the attribution report.
6. Prevent propagation-to-introduction promotion without a successful node-local counterfactual; prefer concrete authored actions as episode representatives.
7. Aggregate unresolved diagnostics and add compressed content-addressed artifact storage while keeping HTML inspection lossless.

After implementation, rerun the same Seaborn case as a regression and add a different FeatureBench repository case to test cross-repository generality.
