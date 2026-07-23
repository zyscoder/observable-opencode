# Root Confirmation Task 2 Fix Wave 18 Report

## Status

DONE

- Branch: `codex/message-context-lineage`
- Baseline: `bd60e0509a70ccb054ac4a8ec91b8958db523d9b`
- Scope: Root Confirmation Task 2 only
- Behavior: offline, passive, read-only attribution analysis
- Dependencies added: none
- Agent behavior changes: none

## Findings Closed

### 1. Stale seed publication quarantine

RED reproductions covered partial and completed restore, multiple seeds,
contributing conditions, rejected candidates, and mixed publications. Restore
either failed ownership validation or retained stale-seed conclusions.

The restore path now derives exact stale seed-binding and hypothesis ownership
before validation. It quarantines confirmations, queues, roots, co-roots,
contributing/amplifying factors, rejected candidates, local judgments,
introduction bindings, investigation journals, paths, decisive evidence,
global judgments, and expansion state. A stale seed becomes an explicit
`evidence_gap` while active seeds retain their independently owned roots.

### 2. One graph-aware deep Judge sanitizer

RED reproductions showed nested evidence/artifact objects and JSON encoded in
strings reaching global, recursive-step, and confirmation prompts after only
their ref field had been removed.

`judge_payload.py` now provides the single graph-aware structural sanitizer.
It resolves aliases, validates nested scalar/list references, removes an
associated fact as a unit, recursively sanitizes encoded JSON before
serialization, rejects stale or unresolved identities, and accepts unindexed
embedded artifacts only when a canonical SHA-256 matches the exact UTF-8
content. Missing, truncated, unresolved, and integrity diagnostics remain
available in persisted audit state but are removed from Judge-visible views.

### 3. One active progress-episode projection

RED reproductions showed all-stale episodes consuming windows and persisted
cumulative/change/consecutive values surviving active-revision filtering.

The authoritative projection now drops episodes with zero active members and
recomputes member summaries, candidate members, semantic counts, phase,
delivery state, positions, timestamps, previous eligible episode, cumulative
change/verification counts, and consecutive no-delivery counts. Judgment
context, investigation, retrieval, navigation, active paths, and summaries all
consume this projection. Persisted aggregates and stale members are audit-only.

The 64-slot investigation tests now use episodes with real active members.
A separate regression proves an adjacent all-stale episode returns zero
episode results and does not consume the output budget.

### 4. Evidence-policy compatibility identities

RED reproductions showed policy-v1 checkpoints, cache identities, and capsules
could still reuse conclusions.

Current compatibility identities are:

- Evidence policy: `graph-external-evidence-eligibility/v2`
- Judgment cache: `2.0`, with evidence policy included in the key
- Causal step prompt: `recursive-causal-step-v9`
- Root confirmation prompt: `recursive-root-confirmation-v8`
- Attribution report: `recursive-attribution-report/v6`
- Evidence capsule: `candidate-evidence-capsule/v6`
- Global judgment: `global-candidate-judgment/v5`
- Global validation envelope: `global-candidate-validation-envelope/v5`
- Global persistence contract: global v5 + envelope v5 + capsule v6 +
  evidence-policy v2
- Root persistence contract: confirmation v8 + resolution v2 +
  evidence-policy v2

Old policy/config state is rejected rather than silently accepted. Stale
start-seed evidence inside otherwise compatible current state is conservatively
quarantined per seed.

## TDD Evidence

The initial Fix18 finding suite was run by finding group before implementation.
All four groups failed for their intended reason:

- stale publication ownership/evidence validation failures;
- stale and missing artifact sentinels present in final prompt JSON;
- all-stale episodes and persisted aggregates present in active windows;
- v1 policy checkpoint/cache/capsule identities accepted or reused.

After implementation the original finding suite passed. Self-review then added
two further RED boundaries: stale-owned local state in partial restore and
unverified embedded artifact/arbitrary namespaced refs in the sanitizer. Both
failed before their fixes and passed afterward.

Final focused finding result:

```text
Ran 11 tests in 0.181s
OK
```

## Verification

Affected attribution suites:

```text
Ran 691 tests in 5.770s
OK
```

Full attribution suite:

```text
Ran 756 tests in 5.851s
OK
```

Additional verification:

```text
PYTHONPYCACHEPREFIX=/tmp/fix18-compile-final \
  python3 -m compileall -q tools/trace_attribution
# exit 0

git diff --check
# exit 0

git diff --check bd60e0509..HEAD
# exit 0 after the Fix18 commit
```

## Files

Production changes:

- `tools/trace_attribution/trace_attribution/judge_payload.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/progress.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/investigation.py`
- `tools/trace_attribution/trace_attribution/judgment_context.py`
- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/analyzer.py`
- `tools/trace_attribution/trace_attribution/cache.py`
- `tools/trace_attribution/trace_attribution/causal_judge.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`

Tests:

- `tools/trace_attribution/tests/test_root_confirmation_fix18.py`
- affected checkpoint, judge, capsule, global-judge, investigation,
  recursive-analyzer, and seed-attribution contract tests

## Self-review

- No duplicate prompt-local deep sanitizer was introduced.
- No consumer retains the legacy progress projection for active attribution.
- Retrieval ranking remains navigation-only.
- Missing or stale evidence produces `evidence_gap`/inconclusive state, never a
  forced root.
- Per-seed publication ownership remains isolated.
- No Task 3 evidence expansion or Agent feedback path was added.
- Existing unrelated untracked files were not modified or staged.

## Concerns

None.
