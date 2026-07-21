# Agentic Recursive Attribution Results

## Scope And Decision

This report freezes locally available legacy evidence, records the original Task 9 offline
acceptance-harness result, and appends the Fusion C real-Provider validation performed on
2026-07-20. The original frozen code baseline is
`44b72341e516ac9af24a29c336e59ddcb659139d` (`2026-07-21T05:23:06+08:00`).

The CLI default remains `legacy`. Deterministic fixture results are regression evidence, not
real-LLM attribution-quality acceptance. The historical Task 9 section made no network or
model call. The later Fusion C validation used the authorized Anthropic-compatible DeepSeek
Provider, but it is behavioral validation rather than an audited benchmark-quality gate.

## Frozen Legacy Evidence

The historical attribution reports below were inspected without rerunning a model. Their
original trace paths are absent. A different, current raw Trace for the same Pydantic case is
available and is inventoried below; it has not yet been used for a new model comparison.
Historical reports contain no auditable physical/logical Judge request counts, so request
reduction is unavailable rather than inferred.

| Case | Frozen attribution artifact | SHA-256 | Outcome | Roots | Audited recall | Top-1 | Requests |
| --- | --- | --- | --- | ---: | --- | --- | --- |
| Sphinx FeatureBench Lite `sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1` | `docs/superpowers/reports/2026-07-14-loop5-sphinx-baseline-before-fix.json` | `d50326bac7c882dd3cc827b54c5b05a89ef804a2416b60eb7dc6e2ec420e9c11` | `root_found` | 4 | 0.25 (1/4) | false | unavailable |
| Pydantic FeatureBench `pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1` | `docs/superpowers/reports/2026-07-13-loop3-featurebench-pydantic-attribution-improved-v2.json` | `a541ab6dfc7302c86e446006afccb9b0e9afe3dc6346ff41837bc294513c52ba` | `root_found` | 1 | unavailable | unavailable | unavailable |
| Seaborn FeatureBench Lite `mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1` | local untracked `docs/superpowers/reports/2026-07-14-loop6-featurebench-seaborn-attribution.json` | `c6637958e06d5ad01a1e24543b49bd0f8e5839fcd138551ff66e866549249e6c` | `root_found` | 7 | 0.50 (1/2) | false | unavailable |
| Successful real control | no historical attribution plus auditable no-defect labels found | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |

Review provenance:

| Review artifact | SHA-256 |
| --- | --- |
| `docs/superpowers/reports/2026-07-13-loop4-featurebench-sphinx-review.json` | `e11f9c3df75c4dee8d74c352beb624bb357ecdcd3693e3e9cc0d767a05f45f90` |
| `docs/superpowers/reports/2026-07-13-loop3-featurebench-pydantic-review.json` | `81f8bc540ed63204504fa19c196ebfedda05666afa6096ed85897812a087920d` |
| local untracked `docs/superpowers/reports/2026-07-14-loop6-featurebench-seaborn-review.json` | `7b01080c8579fc5f43e1fd0c15be58a6898b804c5d5d45c25e21f24d47826526` |

The frozen Sphinx report ranks `record:decisionnode_dec_160_d3011460` first and recalls only
`record:decisionnode_dec_162_d4c07ef3` from the four-root audited set. The Pydantic review has
two observed defects but no complete audited expected-root set; its one reported root
`record:responseclaim_claim_5911_b398e6a9` is descriptive, not ground truth. The Seaborn
report recalls `record:decisionnode_dec_325_693609d2`, misses
`record:decisionnode_dec_86_a21bcba3`, and ranks `record:decisionnode_dec_74_7d47a41d` first.

## Available Real Trace Inventory

The review's full `/private/tmp` scan found this parseable Pydantic source Trace:

```text
/private/tmp/observable-opencode-multibench/runs/featurebench/traces/pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1/partial/latest.json
bytes: 39631728
sha256: 254adff67139ea1f8001a6da7904b337a7802723c9d36515ea0fb667b7dff423
TraceGraph case_id: pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1
TraceGraph nodes: 1927
default_start_refs:
  record:missing_semantic_final_test_result
  record:observed_defect_missing_verification_after_change
```

The SHA-256 was unchanged before/after local parsing. No model analysis was run.
The Trace contains 998 legacy artifacts whose `hash` values are 16-hex truncated identities
rather than canonical `sha256:<64-hex>` content hashes. The strict evaluator therefore rejects
this file for acceptance until the trace/artifact bundle is regenerated or migrated with
full content hashes. It remains valid input for descriptive analyzer runs and provenance
inventory; the command below deliberately exposes, rather than bypasses, this gate.

Existing requirement/architecture/verification traces include:

```text
/private/tmp/observable-opencode-final-claim-grounding-stress/traces/semantic-architecture-boundary/partial/latest.json
/private/tmp/observable-opencode-final-claim-grounding-stress/traces/semantic-requirement-priority/partial/latest.json
/private/tmp/observable-opencode-final-claim-grounding-stress/traces/semantic-verification-depth/partial/latest.json
/private/tmp/observable-opencode-final-claim-grounding-stress-v2/traces/semantic-architecture-boundary/partial/latest.json
/private/tmp/observable-opencode-final-claim-grounding-stress-v2/traces/semantic-requirement-priority/partial/latest.json
/private/tmp/observable-opencode-final-claim-grounding-stress-v2/traces/semantic-verification-depth/partial/latest.json
/private/tmp/observable-opencode-final-claim-grounding-stress-v3/traces/semantic-requirement-priority/partial/latest.json
/private/tmp/observable-opencode-final-claim-grounding-stress-v4/traces/semantic-requirement-priority/partial/latest.json
/private/tmp/observable-opencode-final-claim-grounding-stress-v5/traces/semantic-requirement-priority/partial/latest.json
/private/tmp/observable-opencode-phase2-stress-context-set/traces/semantic-requirement-priority/partial/latest.json
/private/tmp/observable-opencode-verification-temporal-v6/traces/verification-temporal-contradictory-oracle/partial/latest.json
/private/tmp/observable-opencode-verification-temporal-v6/traces/verification-temporal-success/partial/latest.json
/private/tmp/observable-opencode-verification-temporal-v6/traces/verification-temporal-success-v2/partial/latest.json
/private/tmp/observable-opencode-verification-temporal-v6/traces/verification-temporal-success-v3/partial/latest.json
```

The three historical Sphinx/Pydantic/Seaborn paths named by old metadata remain absent. The
current inventory therefore corrects the former broad absence claim without pretending that
the available Pydantic Trace is the exact historical input or that a real context-compaction
quality gate has already passed.

## Deterministic Plumbing Matrix

The nine fixtures exercise the real recursive analyzer, frontier, hypothesis ledger,
transformations, independent confirmation, ranking, report invariants, v2 projection, and
strict evaluator through a zero-transport scripted Judge. They do **not** count toward
attribution-quality acceptance because `scripted_analysis` is an answer table.

| Fixture | Outcome | Primary root | Co-root | Condition | Amplifier |
| --- | --- | --- | --- | --- | --- |
| `sphinx_recursive_minimal.json` | `root_found` | `record:decision` | none | `record:prompt` | `record:timeout` |
| `prompt_wrong_agent_faithful.json` | `root_found` | `record:prompt` | none | none | none |
| `context_compaction_loss.json` | `root_found` | `record:compaction` | none | none | none |
| `tool_error_ignored.json` | `root_found` | `record:decision` | none | `record:tool_error` | none |
| `subagent_warning_ignored.json` | `root_found` | `record:main_agent_decision` | none | `record:subagent_warning` | none |
| `multi_root_failure.json` | `root_found` | `record:encryption_decision` | `record:audit_decision` | none | none |
| `inferred_missing_edge.json` | `root_found` | `record:decision` | none | none | none |
| `timeout_amplifier.json` | `root_found` | `record:decision` | none | none | `record:timeout` |
| `success_negative_control.json` | `no_defect` | none | none | none | none |

All nine have confirmed-root recall 1.0. The eight positive cases have Top-1 true. The empty
root control has Top-1 `null` and `negative_control_correct=true`. These numbers prove
deterministic plumbing only. Physical request reduction remains unavailable.

Conclusion-leaking `observed_defect.mechanism` wording was removed. Human labels remain in a
separate top-level section and are removed before graph construction, prompting, caching, or
checkpointing.

## Deterministic Rule-Based Semantic Smoke Test

A second deterministic suite runs six cases without reading `scripted_analysis` by ref. Its
fixture-specific phrase classifier recognizes tokens such as `constant initialization`,
`skip audit`, `despite the warning`, and `without investigating`. Randomized refs, component
aliases, and insertion order preserve the expected plumbing output, but this does not prove
general semantic understanding or answer-table independence beyond those structural fields.

The explicit paraphrase probe changes `Use a constant initialization vector` to the equivalent
`Reuse one fixed IV`. The rule suite then drops `record:encryption_decision` and retains only
`record:audit_decision`, instead of preserving both roots. This known limitation is recorded
as a passing characterization test, not hidden as a quality success. Scripted and rule-smoke
metrics are excluded from attribution-quality/default gates. Only the real LLM Provider suite
is semantic acceptance evidence.

## Independent Acceptance Contract

The evaluator requires an immutable `--trace` source. Report-only acceptance is invalid. It:

- strictly parses exact report/label schemas and recomputes the report outcome state machine;
- rebuilds a `TraceGraph`, recomputes content-only `semantic-anchor/v2`, relation-aware
  `semantic-occurrence/v1`, and both full index/collision sets;
- resolves candidate snapshots, judgments, hypotheses, confirmations, roots, factors, and
  compatibility projections against source facts;
- validates every confirmed/factor path as directed, attribution-eligible, and non-temporal;
- recomputes confirmation identities and rejects duplicate causal-occurrence identities or role reuse;
- verifies artifacts by Trace membership, owner, contained path, canonical SHA-256, actual
  bytes, optional range/content, and envelope metadata;
- rejects fabricated/unresolved confirmed facts, forbidden roots, disallowed unresolved
  outcomes, contradictory `no_defect`, and checkpoint reuse above logical calls.

The v3 human-label schema binds every scored role to both content semantics and causal
occurrence; `node_ref` is optional navigation. The v4 comparison schema scores roots,
factors, forbidden matches, Top-1, disagreements, and counts by occurrence identity. It also
reports content-anchor recall/precision as diagnostics, never as a replacement quality gate.
Thus two same-anchor roots in distinct neighborhoods contribute two denominator entries and
finding one yields recall `0.5`. Multi-root metrics are fractions. Empty expected roots use explicit
`negative_control_correct`; Top-1 is `null`. Recall/precision/rate metrics are bounded.
Request performance separately reports signed `(legacy-current)/legacy`,
`request_ratio=current/legacy`, and `request_delta=legacy-current`, so regressions remain
visible. A historical report may be described separately, but cannot ground its own facts.

## Exact Real Commands

Run the available Pydantic Trace once cold and once resumed. Human labels must stay outside
the analyzer inputs:

```bash
mkdir -p /private/tmp/observable-opencode-task9-real/pydantic
PYTHONPATH=tools/trace_attribution python3 -m trace_attribution.cli \
  --engine recursive-agentic \
  --trace /private/tmp/observable-opencode-multibench/runs/featurebench/traces/pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1/partial/latest.json \
  --review docs/superpowers/reports/2026-07-15-loop3-causal-ir-featurebench-pydantic-review.json \
  --out /private/tmp/observable-opencode-task9-real/pydantic/recursive.attribution.json \
  --analysis-perspective "Improve Agent repository reasoning and implementation quality." \
  --model deepseek-v4-pro \
  --judge-timeout-sec 3600
```

Evaluate only after producing a separately audited v3 dual-identity label file:

```bash
PYTHONPATH=tools/trace_attribution python3 \
  tools/trace_attribution/scripts/evaluate_recursive_attribution.py \
  --trace /private/tmp/observable-opencode-multibench/runs/featurebench/traces/pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1/partial/latest.json \
  --report /private/tmp/observable-opencode-task9-real/pydantic/recursive.attribution.json \
  --labels /private/tmp/observable-opencode-task9-real/pydantic/human-labels.v3.json \
  --legacy-report docs/superpowers/reports/2026-07-13-loop3-featurebench-pydantic-attribution-improved-v2.json \
  --out /private/tmp/observable-opencode-task9-real/pydantic/comparison.v4.json
```

For semantic requirement/architecture/verification runs, substitute one exact inventory path
above and write to `/private/tmp/observable-opencode-task9-real/semantic/`. The unavailable
real Sphinx and successful no-defect inputs remain pending. No model result is claimed here;
the parent task will execute authorized comparisons after review.

## Fusion C Real-Provider Validation

Fusion C combines deterministic graph reconstruction and bounded retrieval with LLM-driven
recursive semantic judgment, multi-hypothesis backtracking, and independent root
confirmation. Four real Provider runs exercise both positive and negative paths:

| Case | Outcome | Visited | Physical/logical requests | Result |
| --- | --- | ---: | ---: | --- |
| New interrupted trace with explicit `process.signal` | `root_found` | 1 | 2/2 | `record:process_signal_bcea91f9` independently confirmed at 0.90 |
| Legacy interrupted Pydantic trace | `inconclusive` | 0 | 0/0 | `process_signal_node_missing`; no fabricated root |
| Successful case negative control | `no_defect` | 1 | 1/1 | no root and no unresolved branch |
| Architecture-boundary negative control | `no_defect` | 1 | 1/1 | no root and no unresolved branch |

The new signal trace is at
`/private/var/folders/sp/z3z2zy2110v1bnxszvqjkfqh0000gn/T/opencode-case-trace-interrupted-diagnostics-rhNmKH/interrupted-diagnostics-case/trace.json`;
its report is
`/private/tmp/observable-opencode-task9-real/signal-node-v10-20260720/recursive.attribution.json`.
The analyzer starts from `case.failed`, traverses only the recorded `process.signal`, and then
independently confirms that signal as the earliest trace-visible defect introduction. The
unknown external sender remains an explicit scope boundary rather than invented missing
evidence.

The legacy Pydantic report is
`/private/tmp/observable-opencode-task9-real/pydantic-current-v6-20260720/recursive.attribution.json`.
Because that old trace records interrupted shutdown but has no distinct signal occurrence,
the analyzer performs no LLM call, returns `inconclusive`, and recommends adding
`process_signal_node`, `signal_observation_source`, and `signal_to_failure_edge`. This replaces
the earlier unsafe behavior that could confirm `case.failed` as its own cause.

The successful and architecture reports are respectively
`/private/tmp/observable-opencode-task9-real/success-final-20260720/recursive.attribution.json`
and
`/private/tmp/observable-opencode-task9-real/architecture-final-v2-20260720/recursive.attribution.json`.
Both return clean `no_defect` results. Candidate roots now require an exact, independently
validated `request_root_confirmation` action; exhausted confirmation budget is exposed as
`unknown`, never silently promoted to a confirmed root.

## Blocking Gates

The default cannot switch until real Sphinx, Pydantic, requirement understanding, context
compaction, and successful no-defect cases have audited v3 labels and pass source-backed
evaluation; request reduction and cold/resumed checkpoint reuse must also be measured from
real Provider runs. Scripted and rule-based deterministic suites can never satisfy these gates.

## Historical Offline Verification

Task 9 focused tests: 41 passed. Dedicated fixture/metrics tests: 12 passed. The historical
complete offline suite had 433 passing tests. Production modules compiled with an isolated
Python bytecode cache. No model, network, Agent mutation, or Trace mutation was performed in
that historical run.

## Fusion C Verification

- Python attribution suite: 447 passed.
- Bun case-trace suite: 131 passed with repository-pinned Bun 1.3.13.
- `git diff --check`: clean.
- Real Provider positive signal path: confirmed root with no unresolved state.
- Real Provider negative controls: both `no_defect` with no root and no unresolved state.
- Legacy interrupted trace: explicit instrumentation gap, zero model requests, no false root.

Trace collection remains a passive sidecar: the new signal fact records handler-observed
lifecycle data and declares `recording_mode=passive_posthoc` and `agent_feedback=none`; none
of the attribution output is fed back into the Agent.
