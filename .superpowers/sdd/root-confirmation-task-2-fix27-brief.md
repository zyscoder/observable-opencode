# Root Confirmation Task 2 Fix Wave 27 Brief

Base commit: `b035e652c`

## Scope

Close all four Important findings from the second cumulative Task 2 review of
`bd8f1ab27..b035e652c`. Preserve the offline, passive, read-only analysis
boundary and never feed attribution conclusions back to the Agent. Do not
implement Task 3 or add dependencies.

Use strict TDD for each finding. Record focused RED evidence before production
changes and GREEN evidence afterwards. Finish with affected suites, full
attribution discovery, `compileall`, and
`git diff --check b035e652c..HEAD`.

## Required Fixes

### 1. Preserve real Global Judge failures as typed failures

`ClaudeCausalJudge.judge_candidates_bounded()` must not convert provider
failure, exhausted request budget, invalid output, or failed repair into a
successful `GlobalCandidateJudgment(outcome="inconclusive")`.

When the validated request outcome has no payload:

- raise `BoundedJudgeCallError` (or an equivalent typed failure already
  consumed by the analyzer);
- preserve the exact physical request count and useful error kind/detail;
- let the analyzer's canonical `fail_seed()` path create the failed action,
  unresolved episode, blocker, and missing evidence;
- do not record a completed global pass or continue that seed's recursive
  traversal/confirmation.

Add tests using the real `ClaudeCausalJudge` adapter for provider error, zero
remaining budget, invalid payload after repair, and successful valid output.
Assert action, episode, request count, seed outcome, and independent progress
of another seed.

### 2. Use strict artifact owner envelopes for every Judge-visible hydration

`TraceGraph.artifact_hydration_manifest()` and every Global Candidate capsule
path that exposes artifact content to a Judge must use the same strict
`artifact_evidence_envelope(..., expected_owner_ref=...)` contract as root
confirmation.

Requirements:

- the candidate/node owner must be active-start/revision/provenance eligible
  under a formal manifest;
- the owner must explicitly reference the artifact;
- zero, stale-only, unbound, mismatched, or ambiguous owners must not expose
  content;
- valid hydration includes the complete owner/provenance/path/hash/range
  envelope and `owner_binding_identity`;
- rejected hydration contributes a concrete missing/ineligible evidence fact
  so the Global Judge cannot treat silently omitted content as complete;
- validation envelope, report/checkpoint restore, and evaluator rebuild the
  same strict result.

Do not duplicate envelope construction. Use one canonical graph function.
Version evidence capsule/graph policy/report metadata identities if serialized
or Judge-visible meaning changes; test exact old-state rejection or the
documented migration.

Add tests for formal unbound owner, stale-only owner, ambiguous owner, owner
substitution, valid explicit owner, and capsule/report/evaluator round-trip.

### 3. Make failed global action, episode, and seed gap a strict bijection

Define one canonical failure projection containing at least:

- global pass identity and seed binding identity;
- blocker/reason;
- detail/missing evidence;
- physical request delta;
- canonical owner;
- terminal failure status.

Require exact content and cardinality reconciliation across:

- the failed global action;
- exactly one unresolved branch/episode;
- the owning seed builder's blocking reason and missing evidence;
- investigation journal and report/checkpoint/evaluator projection.

Reject mutated diagnostic content, duplicate or missing episodes, substituted
owners, extra seed gaps, and mismatched request counts. Two independent seeds
may each have one failure without interfering.

Version persisted policy/schema identities if the projection shape changes.

### 4. Derive confirmation semantic identity canonically

For every pending confirmation entry, recompute the expected semantic identity
with the same canonical confirmation request identity function used to create
the provider action key. The stored value must match exactly.

The canonical identity must govern:

- enqueue;
- live queue validation;
- queue-key projection;
- confirmation action key and replay lookup;
- checkpoint/report pending restore;
- evaluator validation.

Reject arbitrary substitutions, semantically equivalent but non-canonical
encodings, missing/extra keys, duplicate request identities, and action/queue
identity mismatch. A valid replay must converge without another provider call.

## Verification

At minimum run and report:

1. New Fix27 focused tests.
2. Fix17-Fix27 plus seed/global/causal Judge/checkpoint core regressions.
3. Graph, artifact hydration, evidence capsule, recursive analyzer,
   acceptance, and evaluator affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check b035e652c..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix27-report.md` with root-cause
  validation, RED/GREEN evidence, schema/policy decisions, exact commands and
  test counts, changed files, and residual risks.
- One commit containing only Fix27 files, this brief, and the report. Do not
  modify, remove, or stage unrelated untracked files.
