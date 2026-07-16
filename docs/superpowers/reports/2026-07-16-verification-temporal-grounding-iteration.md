# Verification Temporal Grounding Iteration

## Scope

- Trace core: Causal IR 1.0 / semantic trace 6.0
- Agent path: `opencode serve -> POST /session -> POST /session/:id/message`
- Model: DeepSeek `deepseek-v4-pro`
- Cases: successful requirement repair and contradictory immutable test oracle
- Attribution: backward semantic taint, depth 8, 48 nodes per branch, one-hour judge timeout
- Collection: passive sidecar with `behavior_impact=none`

## Defect Fixed

Canonical verification records already contained repository revision, phase,
status, final-state effectiveness, and supersession refs. Their derived
observations and `verification_output` facts did not. Claim grounding therefore
ranked a baseline failure and a post-change pass only by text and could use both
as direct support for a current pass Claim.

The implementation now propagates and synchronizes:

- `verification_refs`
- `verification_repository_revision`
- `verification_phase`
- `verification_status`
- `verification_effective_for_final_state`
- `verification_temporal_role`
- supersedes and superseded-by refs

Current verification Claims reject superseded, wrong-revision, status-mismatch,
and unscoped shell candidates before semantic ranking. Historical Claims may
still use matching baseline evidence. Rejected historical evidence remains
visible through `temporal_advisory` context edges with
`eligible_for_attribution=false`.

## Additional Parser Fix

The first successful stress run exposed `48000 !== 51000` being split at `!`
into two Claims. Comparison expressions using `!==`, `===`, `!=`, `==`, `<=`,
`>=`, `<`, and `>` are now protected during Claim sentence splitting.

## Automated Verification

- CaseTrace: 130 passed
- Causal IR: 53 passed
- Offline attribution: 98 passed
- TypeScript typecheck: passed
- Native macOS build and smoke test: passed

## Successful HTTP Case

`verification-temporal-success-v3` completed in 90.4 seconds and produced 226
semantic records, 535 edges, and a 14 MiB trace directory.

The final response contained four Claims. The baseline failure remained one
historical Claim. The current verification Claim had exactly the post-change
verification fact and `verification:ver_2_de0fdbb0` as direct support. The
baseline failure and generic successful shell commands did not directly support
the pass Claim. There were no unsupported Claims.

Manual result: no Agent defect. Offline result: `no_defect` for all four
branches, zero judge errors, zero unresolved refs, and no search-limit hit.

## Contradictory Oracle HTTP Case

The immutable test file asserted both 51000 and 48000 for the same function and
input. The Agent correctly implemented the current 15% requirement, preserved
the test file, reran the test, and honestly reported the remaining line-5
failure.

The case produced 237 semantic records, 571 edges, eight Claims, and a 15 MiB
trace directory. The current failure Claim was supported by the revision-1
failed verification fact; the revision-0 baseline failure remained superseded
context.

Manual root boundary: the synthetic benchmark oracle contains mutually
exclusive assertions for an identical input. This is a benchmark-input defect,
not an Agent action defect.

Offline result: seven Claim branches were `no_defect`. The derived statement
"any implementation cannot satisfy both" was marked `unsupported_claim`, so the
overall result was `inconclusive`. There were zero judge errors and zero
unresolved refs, but the module could not reach the contradictory assertion pair
as one formal defect-introduction episode.

## Quality Assessment

Temporal verification provenance is now reliable enough to distinguish:

- baseline failure from current failure;
- current pass from superseded failure;
- real test results from unrelated successful shell commands; and
- historical context from attribution-bearing direct support.

The remaining causal gap is not temporal. The Trace records both assertions and
the resulting failure, but does not derive a typed contradiction or
unsatisfiable-constraint fact. The offline module therefore cannot prove the
impossibility Claim or identify the benchmark oracle as the root boundary.

## Next Iteration

1. Introduce a deterministic `constraint.contradiction` or
   `verification.oracle_conflict` fact for identical inputs with incompatible
   expected values.
2. Record both assertion source locations, normalized input identity, expected
   values, derivation method, and an attribution-eligible edge from the oracle
   conflict to the failed task outcome.
3. Seed attribution from an external evaluation outcome rather than treating an
   honest response Claim as the task defect.
4. Verify that the module locates the benchmark oracle and classifies the Agent
   response as a correct diagnosis rather than a root cause.
5. After contradiction semantics stabilize, reduce repeated superseded advisory
   edges by grouping them into one historical verification context set per
   Claim.
