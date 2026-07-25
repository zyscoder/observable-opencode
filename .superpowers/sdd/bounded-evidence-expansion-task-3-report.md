# Bounded Evidence Expansion Task 3 Report

## Status

Complete locally. The Global Judge can request one of six bounded,
graph-grounded evidence contexts, receive immutable factual results, and
re-evaluate the same seed without falling back to ordinary recursive traversal.
Attribution remains offline, passive, read-only, and unable to feed conclusions
back to the analyzed Agent.

## Delivered

- Added `EvidenceExpansionRequest`, `EvidenceExpansionResult`, and
  `ExpansionLimits`.
- Added `upstream`, `downstream`, `artifact`, `action_group`,
  `message_transform`, and `full_node` expansion contexts.
- Restricted expansion to bounded TraceGraph adjacency, verified artifact
  hydration, recorded action identity, message lineage, or the requested node.
- Excluded temporal-only, progress/navigation, and retrieval-routing edges from
  causal expansion.
- Added stable request identities, duplicate suppression, graph-canonical
  reconstruction, structured rejection, exact serialized-byte accounting, and
  recursively immutable result payloads.
- Added a Global Judge re-evaluation loop with at most three expansion rounds,
  eight resolved nodes, and 32768 serialized bytes per round.
- Persisted the initial request, final validation envelope, expansion history,
  terminal blocker, and exact physical request count in checkpoint actions and
  seed results.
- Changed an unresolved Global expansion into a concrete `evidence_gap`; it no
  longer creates an ordinary recursive frontier as an implicit fallback.
- Bumped the Global judgment, validation envelope, action-state, and persistence
  contract versions.

## TDD And Verification

The initial expansion test failed before the module existed. A later immutability
regression also failed because nested result dictionaries could still be
mutated after validation.

Final verification:

```text
Focused expansion/global/analyzer/seed suites: 208 tests passed
Full attribution suite:                       1066 tests passed
Isolated-pycache compileall:                  passed
git diff --check:                             passed
```

The recovery regression exposed and fixed one production defect: the restored
seed judgment contains the expanded `final_validation_envelope`, but checkpoint
reconciliation compared it with the initial envelope. The validator now compares
the seed projection with the final envelope, and interrupted/resumed output is
byte-equivalent to uninterrupted output.

## Real DeepSeek Validation

A sanitized, answer-free Sphinx FeatureBench Causal IR fixture was analyzed with
`deepseek-v4-pro` through the Anthropic-compatible DeepSeek endpoint. The API key
was supplied only through a non-echoing child-process environment variable and
was not persisted.

The run remained conservative but did not reach evidence expansion:

| Metric | Result |
| --- | ---: |
| Analysis outcome | `inconclusive` |
| Logical Global Judge calls | 1 |
| Physical requests | 3 |
| Confirmed roots | 0 |
| Expansion rounds | 0 |
| Provider circuit | closed |

The initial response and both repairs failed the Global comparison contract:

1. A root candidate with unknown input defect claimed certain confidence.
2. The focused repair omitted a complete comparison for every open authored
   root-eligible candidate.
3. The full retry repeated the incomplete comparison.

The fact layer did not invent a result. It persisted an exact
`global_judge_bounded_failure`, rejected the affected hypotheses, and returned
`inconclusive`.

## Critical Findings

This run identifies an upstream bottleneck rather than an expansion defect:

- The source Trace contains six records and about 1591 JSON bytes.
- Retrieval produced five candidate capsules totaling about 38966 bytes.
- Capsule bytes were roughly 24.5 times the source Trace bytes.
- Four candidates were treated as open authored roots: interruption, change,
  prompt, and decision.
- The model had to complete a dense four-candidate status, role, path, and
  counterfactual matrix before it could select a root or request more evidence.

The deterministic tests prove bounded expansion, persistence, deduplication,
budget handling, and resume behavior. The real run does not yet prove that a
production model can reliably enter and complete the expansion protocol because
the preceding Global comparison contract failed first.

## Next Iteration

1. Tighten root-candidate eligibility so lifecycle/outcome aggregates and
   execution-result records are not treated as authored roots merely because
   they are upstream.
2. Add per-candidate repair constraints containing the exact required path,
   eligibility, open-candidate set, and valid completion rules.
3. Add a fusion payload guard: when capsules enlarge a small Trace instead of
   compressing it, use a smaller non-duplicated matrix or the recursive path.
4. Persist privacy-safe invalid-output hashes and field-level validation codes
   so protocol failures can be compared without storing raw model responses.
5. Rerun Sphinx after those changes and require at least one valid real-model
   Global judgment; then exercise a genuine `needs_expansion -> rejudge`
   transition before declaring live expansion acceptance.

## Runtime Artifact

The local validation output is:

```text
/private/tmp/observable-opencode-task3-deepseek-20260725/sphinx-minimal/
```

It contains the sanitized Trace, attribution report, message lineage, and
checkpoint action journal. It contains no API key.
