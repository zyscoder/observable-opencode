# Fusion C Open-Source Benchmark Evaluation

## Scope

This evaluation runs the current recursive-agentic attribution engine at commit
`954e69fac5ad8a4f68f8d01f827e46f09aeb09bd` against five existing Trace v6.0
bundles produced from open-source benchmark cases. The analyzer uses the authorized
DeepSeek Anthropic-compatible Provider. Attribution remains offline and does not
modify the Agent, Trace, repository, or benchmark result.

The Trace bundles predate the new explicit `process.signal` node. Therefore Gin and
Pydantic are compatibility tests for the current analyzer, not evidence that the
current collector still omits signal facts.

## Cases And Ground Truth

| Case | Benchmark | External/manual result | Attribution objective |
| --- | --- | --- | --- |
| `gin-gonic__gin-2121` | SWE-bench Multilingual | official resolved | reject Agent root for external interruption |
| `axios__axios-5892` | SWE-bench Multilingual | official resolved | reject false missing-verification diagnostic |
| `astropy__astropy-12907` | SWE-bench Verified | official resolved | classify supported final claims as non-defective |
| `cancel-async-tasks` | TerminalBench | stable process-SIGINT reproduction exposes partial semantic failure | find the implementation assumption and inadequate self-test design |
| `pydantic...test_deprecated_fields...lv1` | FeatureBench Lite | interrupted Trace; no matching audited final evaluation for this exact snapshot | classify lifecycle evidence without inventing a task-quality root |

The TerminalBench review injects only the external defect description, the final
`All 7 tests pass` claim, and its self-test result. It does not provide the manual
root refs to the analyzer. Manual expected roots remain outside the analyzer input:
`record:decisionnode_dec_2_ba104d23` and
`record:decisionnode_dec_18_fe917370` (with the semantically richer test-design
rationale in `record:decisionnode_dec_17_c574dc0e`).

## Results

| Case | Outcome | Visited | Physical/logical calls | Confirmed roots | Main result |
| --- | --- | ---: | ---: | ---: | --- |
| Gin | `inconclusive` | 0 | 0/0 | 0 | correctly reports `process_signal_node_missing` |
| Axios | `inconclusive` | 3 | 6/4 | 0 | does not reject the false missing-verification diagnostic |
| Astropy | `inconclusive` | 9 | 17/10 | 0 | one claim is absent, three correct/explanatory claims are treated as defective or unresolved |
| TerminalBench | `inconclusive` | 3 | 9/4 | 0 | expected root decisions are not visited; a progress episode is incorrectly proposed as a root and confirmation remains unknown |
| Pydantic | `inconclusive` | 0 | 0/0 | 0 | correctly reports `process_signal_node_missing`; task quality remains unanalyzed |

Aggregate results:

- 5/5 cases are `inconclusive`; decisive-result rate is 0%.
- 32 physical Provider requests serve 18 logical calls. Fourteen requests are
  repair/retry overhead: 43.8% of physical traffic, or 77.8% overhead relative to
  logical calls.
- 15 semantic nodes are visited, 12 refs remain unresolved, and no root is confirmed.
- Three branches exhaust all JSON repair attempts with a final validation error.
- Three generated `judge_retry` diagnostics are incorrectly routed through the
  investigation-tool validator and become additional `investigation_rejected` noise.
- The three successful negative controls produce no confirmed Agent root, so root
  safety is 3/3. However, none reaches the correct `no_defect` outcome.
- TerminalBench manual root recall is 0/2 and neither expected root is visited.

## Manual Versus Automatic Analysis

### Axios

The raw Trace contains a successful command:

```text
npx mocha test/unit/adapters/http.js --timeout 10000 --grep "decompression|content-encoding"
```

It is classified as `code_inspection`, not a verification attempt. Because the
command is piped through `head`, its exit status is not a reliable Mocha status, but
the correct Trace statement is `verification attempted; result status masked by
pipeline`, not `no verification command observed`. The analyzer sees the false
diagnostic and nearby change, marks them defective, and then stops at a protocol
error. This is primarily a derived-Trace classification defect, amplified by Judge
state inconsistency and candidate overload.

### Astropy

The full pytest artifact reports 11 passed tests with exit code 0. The Trace failure
parser treats a normal file/line location as a parsed failure, sets
`failure_output_masked_by_exit_code`, and derives `verification_status=failed`.
Claim grounding then selects this false failed-verification fact as direct support.

The final explanation is also split inside an unmatched parenthetical expression:
one claim ends with `(i.e.` and the next starts with a comma. The analyzer therefore
judges a correct description of the pre-existing repository defect as an Agent
defect and traverses the successful fix. The raw artifact is sufficient for a human
to reject the false failure; the derived semantic layer is wrong.

### TerminalBench

The raw Trace contains the relevant reasoning, authored test script, changes, first
failing self-test, weakened replacement test, and final success claim. Human backward
analysis can recover the defect:

1. The implementation assumes re-cancelling and re-gathering preserves already
   started asynchronous cleanup.
2. The replacement test models direct parent-task cancellation, not process-level
   SIGINT timing.
3. The final passing self-test therefore cannot falsify the implementation assumption.

The offline progress window stops before the previous delivery episode. It offers
the final test-run decisions but omits the preceding test-design decision and write
action. The recursive prompt also lacks the invariant that `progress.episode` is a
navigation aggregate and cannot be a root. The confirmer receives outcome-evidence
hypotheses as competing roots and rejects the response because not every irrelevant
competitor is covered. This is primarily graph reconstruction and Judge protocol,
not missing raw Trace content.

### Gin And Pydantic

Both old interrupted traces lack a distinct `process.signal` node. The current
analyzer performs zero model calls, refuses to confirm `case.failed` as its own
cause, and emits an exact instrumentation recommendation. This is a correct
compatibility behavior. A current-collector rerun is still required to verify that
the new signal node closes the gap on real benchmark execution.

## Current Quality Assessment

### Semantic Trace

Raw forensic completeness is moderate to strong: decisions, authored tool inputs,
changes, self-tests, final claims, full artifacts, and timings are sufficient for
manual analysis in the three semantically inspected cases. The weak point is the
derived fact layer:

- verification command recognition is incomplete;
- failure parsing can contradict exit status and the actual test summary;
- claim segmentation breaks semantic units;
- external benchmark outcomes are not first-class causal nodes;
- progress windows omit the previous delivery boundary needed for root search.

Assessment: useful for human investigation, not yet reliable as an automatic causal
fact source.

### Attribution Module

The module has materially improved safety: it does not fabricate a root from an
interrupted outcome and independently confirms candidates before reporting them.
Its current effectiveness is nevertheless low:

- decisive-result rate: 0/5;
- failure-root recall on the auditable TerminalBench case: 0/2;
- successful-case root false-positive rate: 0/3;
- successful-case `no_defect` accuracy: 0/3;
- final schema/protocol failures: three branches;
- Provider repair overhead: 43.8% of physical requests.

Assessment: conservative and auditable, but not yet operationally useful for
automatic root-cause localization.

## Recommended Next Iteration

### P0: Correct Derived Trace Facts

1. A location-only parsed stack/file reference must not imply verification failure.
   Add a consistency invariant: exit 0 plus an explicit passed summary cannot become
   failed without an explicit assertion/error marker.
2. Recognize `npx mocha`, npm/pnpm/yarn test scripts, and similar executable forms as
   verification attempts. A pipeline that masks status should yield
   `verification_result_status=unknown`, not `verification_missing`.
3. Make claim splitting parenthesis-aware and merge punctuation-leading continuation
   fragments before evidence grounding.
4. Add an immutable external-evaluation sidecar node with official score, failing
   assertion, revision, and provenance.

### P0: Repair Causal Reconstruction

1. Include the previous delivery episode as a causal boundary candidate, then page
   backward through earlier delivery episodes when the active defect remains.
2. Deterministically forbid `progress.episode`, lifecycle, and context packaging
   aggregates from becoming defect-introduction roots.
3. Prioritize `evaluation -> claim/verification -> authored decision/tool input ->
   change` paths. Offer context transforms only when the active defect concerns
   context transformation.
4. Limit each LLM step to roughly 6-8 structurally strongest candidates. Page to a
   second group only when the first group cannot explain the defect.

### P0: Simplify The Judge Protocol

1. Separate current-node defect judgment from predecessor selection. Let the LLM
   return a sparse ranked predecessor set while deterministic code records which
   candidates were not selected; do not require 20 low-value `unrelated` objects.
2. Do not route analyzer-generated `judge_retry` diagnostics through the tool
   investigation schema.
3. Confirm roots only against other independently root-eligible introduction
   hypotheses, not every active outcome-evidence hypothesis.
4. Reject status/reason contradictions such as `status=present` together with
   `the node itself is not the defect` before they enter the causal state.

## Next Acceptance Gate

Rerun the same cases after the P0 changes, then add at least one freshly generated
current-collector benchmark Trace. Acceptance requires:

- TerminalBench visits the test-design/implementation decisions and confirms at
  least one actionable root; no progress aggregate can be a root.
- Axios returns `no_defect` or an explicit `trace_health_false_positive` and records
  the Mocha verification attempt.
- Astropy returns `no_defect`; the parenthetical explanation is one claim and the
  11-pass pytest run is not marked failed.
- Gin or another signal-terminated current run contains `process.signal` and has no
  `process_signal_node_missing` gap.
- Final validation-error branches are zero and repair traffic is below 20% of
  physical requests.

## Artifacts

Results are stored under:

```text
/private/tmp/observable-opencode-benchmark-eval-20260721/
  gin/recursive.attribution.json
  axios/recursive.attribution.json
  astropy/recursive.attribution.json
  terminalbench/recursive.attribution.json
  pydantic/recursive.attribution.json
  terminalbench-cancel-review.json
```
