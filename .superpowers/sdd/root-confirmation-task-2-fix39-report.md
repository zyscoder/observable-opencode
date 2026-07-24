# Root Confirmation Task 2 Fix39 Report

## Result

Fix39 publishes primary and co-root roles per seed instead of ranking every
confirmed root globally. Every seed whose outcome is `confirmed_root` now has
exactly one primary root; additional independently confirmed reciprocal roots
for that same seed are co-roots. A lower-ranked root owned by another seed
remains that seed's primary, including when both seeds confirm the same
physical trace node.

Root, factor, and rejected-candidate publications now use one canonical,
pure projection contract shared by live construction, report restore,
checkpoint restore, graph validation, and evaluator safety validation.

## TDD Evidence

`test_root_confirmation_fix39.py` was added before production code changed.
The final pre-production focused run produced the expected RED:

```text
Ran 10 tests
FAILED (failures=8, errors=2)
```

The failures demonstrated that:

- global ranking demoted an independent seed's root to a cross-seed co-root;
- report restore accepted coordinated root-field and provenance mutations;
- factor labels/provenance and rejected provenance were not canonical;
- persistence versions had not changed.

The two errors showed that evaluator duplicate detection treated two
seed-distinct publications of the same physical occurrence as duplicates.

After the production implementation:

```text
Ran 10 tests in 1.142s
OK
```

The focused tests use real analyzer output and cover independent shared-node
seeds, same-seed reciprocal roots, interrupted resume, report/checkpoint
controls, evaluator controls, coordinated modern/legacy mutations, and graph
truth validation.

## Implementation

### Per-Seed Publication

`_rank_confirmed_roots()` now:

- merges restored primary and co-root collections;
- groups roots by authoritative
  `RootConfirmation.seed_binding_identity`;
- deduplicates each seed by confirmation identity;
- applies the existing deterministic rank only inside a seed;
- emits one primary per seed and the remaining same-seed roots as co-roots;
- flattens seed groups deterministically.

Report validation requires every confirmed seed to own exactly one primary,
permits only same-seed co-roots, requires
`observed_defect_refs == (seed.start_ref,)`, and preserves the existing
reciprocal competitor checks inside each seed.

Evaluator semantic duplicate and role-conflict checks now identify a
publication by semantic occurrence plus seed binding. This permits the same
physical node to be independently published for two seeds without weakening
same-seed duplicate rejection.

### Canonical Publication Contract

The new pure helpers in `causal_state.py` derive:

- immutable provenance from publication contract, confirmation identity,
  response identity, defect fingerprint, and seed binding identity;
- root response fields from the authoritative confirmation;
- root component/event type from the canonical candidate node;
- root defect type from the authoritative defect state;
- exact owning-seed observation refs;
- `causal_role="defect_introduction"`;
- empty episode fields until an authoritative episode projection exists;
- factor labels from factor role and analysis perspective;
- exact factor/rejected confirmation provenance.

Live analyzer publication and restore/evaluator validation call these same
helpers. Graph-aware validation additionally rebuilds each root from the
active graph node, so coordinated candidate and root forgery cannot turn a
report-local mutation into graph truth.

Serialized embedded confirmations are normalized through
`RootConfirmation.from_dict().to_dict()` before publication comparison. This
keeps evaluator-only semantic annotations outside the authoritative persisted
confirmation while preserving exact response identity.

Historical state tests were migrated from hand-built, noncanonical
publications to the shared helpers. Tests that intentionally exercise invalid
roots or field drift remain hand-built mutations.

## Persistence

Changed semantic boundaries are explicitly versioned:

```text
causal-publication/v3
published-root-projection/v3
perspective-binding/v1
recursive-attribution-report/v18
recursive-attribution-checkpoint/v17
recursive-attribution-output/v6
recursive-analysis-actions/v15
```

The root-confirmation persistence contract now includes
`published-root-projection/v3`, `causal-publication/v3`, and
`perspective-binding/v1`. Older envelopes are not silently accepted as
current publications.

## Verification

```text
Focused Fix39 after review closure:
Ran 15 tests in 1.512s
OK

Fix17-Fix39:
Ran 285 tests
OK

Affected seed/report/evaluator/checkpoint/analyzer/benchmark/CLI suites:
Ran 259 tests
OK

Canonical state regression suite:
Ran 34 tests in 0.021s
OK

Full discovery:
Ran 1018 tests in 25.093s
OK
```

Static verification:

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-fix39-pycache \
  PYTHONPATH=tools/trace_attribution \
  python3 -m compileall -q \
  tools/trace_attribution/trace_attribution \
  tools/trace_attribution/scripts \
  tools/trace_attribution/tests
exit 0

obsolete Fix38 persistence version scan
no matches

git diff --check
exit 0
```

## Scope

The change is limited to per-seed root publication, canonical publication
projection/provenance, persistence versions, evaluator seed-aware occurrence
identity, and directly affected fixtures. It adds no Task 3 behavior,
dependency, Agent feedback, graph mutation, or behavioral feedback into the
analyzed agent. Existing unrelated untracked files were not modified.

## Independent Review Closure

The first cumulative review found two Important gaps:

- report restore accepted a same-seed primary/co-root swap and cross-seed
  primary order drift;
- checkpoint restore accepted cross-seed co-root roles and forged root
  component/provenance fields.

The first follow-up introduced one canonical per-seed rank shared by live
publication, report restore, graph/evaluator validation, and checkpoint
restore. It also reconstructs each checkpoint root from its exact
confirmation, seed, defect state, and active graph node before accepting it.

The second review found that a mutable top-level analysis perspective and raw
candidate/hydrated data could still influence the rank. The final design:

- binds `analysis_perspective` into every `RootConfirmation` response
  identity and confirmation request/action projection;
- requires report and checkpoint perspective to equal the bound confirmation
  perspective;
- ranks only on the bound perspective and canonical root response fields
  (`component`, `event_type`, `excerpt`, `reason`, confidence, path, and
  identity);
- excludes candidate `data`, hydrated artifacts, and revision snapshots from
  primary/co-root ranking.

Negative tests now cover role swaps, ordering drift, coordinated perspective
drift, hydrated artifact ranking influence, and checkpoint role/field/
provenance tampering. A fresh final reviewer reported no Critical or Important
findings. The remaining trust boundary is explicit: these are recomputable
content-integrity hashes, not external digital signatures.
