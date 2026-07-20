# Agentic Recursive Attribution Results

## Scope And Decision

This report freezes the locally available legacy evidence before Task 9 changes and records
the offline acceptance result for the recursive investigator. The code baseline is
`44b72341e516ac9af24a29c336e59ddcb659139d` (`2026-07-21T05:23:06+08:00`).

The CLI default remains `legacy`. The scripted matrix passes, but it is deterministic unit
evidence rather than an authorized real-Provider benchmark. Sphinx, Pydantic,
requirements-understanding, context-compaction, and a real successful negative control have
not all passed the real acceptance gate.

## Frozen Legacy Evidence

The historical reports below were inspected without rerunning a model. Their artifact-root
paths no longer exist on this machine, so the current legacy analyzer could not be rerun on
the original traces. None of the three attribution JSON files records a physical or logical
Judge request count; request reduction is therefore unavailable rather than inferred.

| Case | Frozen attribution artifact | SHA-256 | Outcome | Roots in artifact | Human-label recall | Top-1 | Requests | Unresolved refs |
| --- | --- | --- | --- | ---: | ---: | --- | --- | ---: |
| Sphinx FeatureBench Lite `sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1` | `docs/superpowers/reports/2026-07-14-loop5-sphinx-baseline-before-fix.json` | `d50326bac7c882dd3cc827b54c5b05a89ef804a2416b60eb7dc6e2ec420e9c11` | `root_found` | 4 | 0.25 (1/4) | false | unavailable | 0 |
| Pydantic FeatureBench `pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1` | `docs/superpowers/reports/2026-07-13-loop3-featurebench-pydantic-attribution-improved-v2.json` | `a541ab6dfc7302c86e446006afccb9b0e9afe3dc6346ff41837bc294513c52ba` | `root_found` | 1 | unavailable | unavailable | unavailable | 0 |
| Seaborn FeatureBench Lite `mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1` | local untracked `docs/superpowers/reports/2026-07-14-loop6-featurebench-seaborn-attribution.json` | `c6637958e06d5ad01a1e24543b49bd0f8e5839fcd138551ff66e866549249e6c` | `root_found` | 7 | 0.50 (1/2) | false | unavailable | 0 |
| Successful real control | no historical attribution plus auditable no-defect labels found | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |

Sphinx's corrected manual root set is:

- `record:decisionnode_dec_162_d4c07ef3`: parser-contract implementation decision;
- `record:decisionnode_dec_362_abbf250d`: inadequate self-test authoring action;
- `record:decisionnode_dec_306_45a8106a`: first out-of-scope host-compatibility decision;
- `record:decisionnode_dec_346_dac72e2f`: later independent host-compatibility decision.

The frozen baseline ranks `record:decisionnode_dec_160_d3011460` first and recalls only
`dec_162`. The corrected labels come from the audited comparison report; they are not copied
into any Judge context.

The exact frozen Sphinx root output, in rank order, is:

1. `record:decisionnode_dec_160_d3011460` (`incorrect_declaration_parser_contract`)
2. `record:decisionnode_dec_162_d4c07ef3` (`incorrect_declaration_parser_contract`)
3. `record:chgnode_chg_2_1ee08ab1` (`incorrect_declaration_parser_contract`)
4. `record:chgnode_chg_5_75e72f77` (`c_domain_symbol_resolution_defect`)

Seaborn's review explicitly labels
`record:decisionnode_dec_86_a21bcba3` and
`record:decisionnode_dec_325_693609d2` as expected roots, with
`record:decisionnode_dec_330_861d440b` as a contributing root. The frozen attribution recalls
`dec_325`, misses `dec_86`, ranks `dec_74` first, and promotes `dec_330` as a root rather than
retaining its contributing role.

Pydantic's review records two observed defects but does not provide a complete audited
`expected_root_refs` set. The one reported root,
`record:responseclaim_claim_5911_b398e6a9`, is therefore preserved as an observed legacy
result, not treated as sufficient ground truth for recall or Top-1.
Its exact frozen root output is
`record:responseclaim_claim_5911_b398e6a9` (`false_completion_claim`).

The exact frozen Seaborn root output, in rank order, is:

1. `record:decisionnode_dec_74_7d47a41d` (`incomplete_regression_contract_implementation`)
2. `record:decisionnode_dec_84_38558af8` (`incomplete_regression_contract_implementation`)
3. `record:decisionnode_dec_302_03f0b677` (`narrow_self_tests_support_false_completion_claim`)
4. `record:decisionnode_dec_322_0f29a17d` (`narrow_self_tests_support_false_completion_claim`)
5. `record:decisionnode_dec_325_693609d2` (`narrow_self_tests_support_false_completion_claim`)
6. `record:responsenode_segment_22_237890ad` (`required_lmplot_integration_explicitly_dismissed`)
7. `record:decisionnode_dec_330_861d440b` (`narrow_self_tests_support_false_completion_claim`)

Historical reports predate `semantic_anchor:v1`, and the raw traces needed to recompute anchors
are absent. Their exact record refs are frozen above; semantic anchor values are intentionally
reported as unavailable rather than fabricated from thin legacy root projections.

Review provenance:

| Review artifact | SHA-256 |
| --- | --- |
| `docs/superpowers/reports/2026-07-13-loop4-featurebench-sphinx-review.json` | `e11f9c3df75c4dee8d74c352beb624bb357ecdcd3693e3e9cc0d767a05f45f90` |
| `docs/superpowers/reports/2026-07-13-loop3-featurebench-pydantic-review.json` | `81f8bc540ed63204504fa19c196ebfedda05666afa6096ed85897812a087920d` |
| local untracked `docs/superpowers/reports/2026-07-14-loop6-featurebench-seaborn-review.json` | `7b01080c8579fc5f43e1fd0c15be58a6898b804c5d5d45c25e21f24d47826526` |

The host evaluations are unofficial: Docker was unavailable. Sphinx ran an independently
runnable 14-test subset (8 passed, 6 failed, 21 environment setup errors excluded), Pydantic
ran 12 fail-to-pass tests (7 passed, 5 failed), and Seaborn ran 56 non-skipped fail-to-pass
tests (26 passed, 30 failed) plus 220 passing pass-to-pass tests.

## Offline Recursive Matrix

All fixtures execute the real `AgenticRecursiveAnalyzer`, frontier, hypothesis ledger,
transformation logic, independent confirmation, ranking, report invariants, semantic-anchor
projection, and strict evaluator. The Judge is an explicit zero-transport
`OfflineJudgeCapability`; human labels are held outside factual requests.

| Fixture | Outcome | Primary root | Co-root | Condition | Amplifier | Logical calls | Physical requests |
| --- | --- | --- | --- | --- | --- | ---: | ---: |
| `sphinx_recursive_minimal.json` | `root_found` | `record:decision` | none | `record:prompt` | `record:timeout` | 8 | 0 |
| `prompt_wrong_agent_faithful.json` | `root_found` | `record:prompt` | none | none | none | 5 | 0 |
| `context_compaction_loss.json` | `root_found` | `record:compaction` | none | none | none | 4 | 0 |
| `tool_error_ignored.json` | `root_found` | `record:decision` | none | `record:tool_error` | none | 5 | 0 |
| `subagent_warning_ignored.json` | `root_found` | `record:main_agent_decision` | none | `record:subagent_warning` | none | 5 | 0 |
| `multi_root_failure.json` | `root_found` | `record:encryption_decision` | `record:audit_decision` | none | none | 5 | 0 |
| `inferred_missing_edge.json` | `root_found` | `record:decision` | none | none | none | 3 | 0 |
| `timeout_amplifier.json` | `root_found` | `record:decision` | none | none | `record:timeout` | 6 | 0 |
| `success_negative_control.json` | `no_defect` | none | none | none | none | 1 | 0 |

Across the nine fixtures:

- candidate recall: 1.0 for every case;
- Top-1 agreement: true for every case, including the empty expected/predicted root set;
- factor-role precision: 1.0 for every case;
- unknown rate and human/LLM disagreement rate: 0.0 for every case;
- unresolved hypotheses and fabricated refs: 0 for every case;
- request reduction: unavailable because both the scripted Judge and historical baselines
  lack comparable measured Provider requests;
- checkpoint reuse and investigation yield: 0.0 because this matrix intentionally performs
  one cold, fully grounded offline pass without investigation.

The successful control emits no introduction candidate, confirmation, or root. Unknown,
Provider, fabricated-reference, unresolved-evidence, missing-confirmation, semantic-identity
collision, and budget-promoted-root probes all fail closed in the evaluator.

## Semantic Anchor And Label Contract

`semantic_anchor:v1` removes run-local refs, absolute repository prefixes, timestamps, PIDs,
ports, session/request IDs, Provider IDs, and model transport identity. It retains case ID,
component/event semantics, normalized rationale/action, repository-relative paths, and artifact
hashes. Tests verify stability across changed checkout roots and transport metadata, and
separation when rationale, path, artifact hash, or case changes.

Each fixture's `human_labels` and `scripted_analysis` sections are stripped before
`TraceGraph.from_trace`. The loader rejects label-shaped keys nested in `records` or
`dataflow_edges`. Tests additionally assert that no human semantic anchor appears in any
captured Judge request. CLI outputs add anchors after analysis, so anchors do not influence
traversal, Provider prompts, cache keys, checkpoints, or Agent behavior.

## Real Acceptance Commands

The historical metadata names these original trace roots, but they are currently absent:

```text
/private/tmp/observable-opencode-featurebench-loop4-sphinx/traces/sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1/trace.json
/private/tmp/observable-opencode-featurebench-loop3d/traces/pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1/trace.json
/private/tmp/observable-opencode-featurebench-loop6-seaborn/traces/mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1/trace.json
```

After restoring a trace directory, run a cold recursive comparison into a new directory:

```bash
mkdir -p /private/tmp/observable-opencode-task9-real/sphinx
PYTHONPATH=tools/trace_attribution python3 -m trace_attribution.cli \
  --engine recursive-agentic \
  --trace /private/tmp/observable-opencode-featurebench-loop4-sphinx/traces/sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1/trace.json \
  --review docs/superpowers/reports/2026-07-15-loop2-causal-ir-featurebench-sphinx-review.json \
  --out /private/tmp/observable-opencode-task9-real/sphinx/recursive.attribution.json \
  --analysis-perspective "Improve Agent repository reasoning and implementation quality." \
  --model deepseek-v4-pro \
  --judge-timeout-sec 3600
```

Repeat the identical command to test cache/checkpoint reuse. Use the corresponding Pydantic
trace with `docs/superpowers/reports/2026-07-15-loop3-causal-ir-featurebench-pydantic-review.json`.
The requirements-understanding, context-compaction, and real successful-control trace inputs
are not present locally and remain pending artifacts; only their synthetic fixtures exist.

For each real case, preserve a separate reviewed label JSON and run:

```bash
python3 tools/trace_attribution/scripts/evaluate_recursive_attribution.py \
  --report /private/tmp/observable-opencode-task9-real/sphinx/recursive.attribution.json \
  --labels /private/tmp/observable-opencode-task9-real/sphinx/human-labels.json \
  --legacy-report docs/superpowers/reports/2026-07-14-loop5-sphinx-baseline-before-fix.json \
  --out /private/tmp/observable-opencode-task9-real/sphinx/comparison.json
```

Before accepting a real result, hash the input trace before and after analysis and require
identical SHA-256 values. Also require zero fabricated refs, zero unresolved confirmed facts,
successful cold and resumed runs, and the real Provider request gate. No API or network call was
made while implementing this report.

## Blocking Gates

The engine default cannot switch until all of the following are measured on real traces:

1. Sphinx and Pydantic candidate recall and Top-1 meet or exceed their frozen baselines.
2. A requirements-understanding case and context-compaction case have audited labels and pass.
3. A real successful negative control emits `no_defect` with no root.
4. Sphinx physical Provider requests are measured and no more than 98 against the historical
   196-request target; the current frozen JSON cannot prove either number.
5. Cold/resumed runs report cache/checkpoint reuse without semantic drift.
6. Every confirmed root resolves its node, full directed path, evidence, confirmation, and
   semantic identity; every unknown or exhausted branch remains inconclusive.

## Offline Verification

Task 9 focused tests: 15 passed. Complete offline suite: 407 passed. Production modules compile
under Python 3.9 with an isolated bytecode cache. `git diff --check` and the hard-coded component
exclusion scan pass. No model, network, shell investigation, Agent mutation, or Trace mutation
was performed during implementation.
