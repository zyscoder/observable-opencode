# Root Confirmation Task 2 Fix Wave 28 Brief

Base commit: `b831bcd42`

## Scope

Close all three Important findings from the third cumulative Task 2 review.
This wave must consolidate identity and terminal persistence at their canonical
sources rather than add branch-specific checks. Keep analysis offline, passive,
read-only, and unable to feed conclusions back to the Agent. Do not implement
Task 3 or add dependencies.

Use strict TDD. Record RED before production edits, GREEN after each coherent
fix, then run affected and full attribution suites, `compileall`, and
`git diff --check b831bcd42..HEAD`.

## Required Fixes

### 1. Bind confirmation replay to the full factual request

The confirmation request identity must cover the complete canonical
`RootConfirmationRequest.factual_dict()` semantics, not only hypothesis,
candidate, defect, and seed identifiers.

The versioned identity/projection must include all facts that can affect a
confirmation verdict, including:

- candidate and seed/hypothesis bindings;
- defect state and hypothesis semantic hash;
- recursive path and path reference envelopes;
- supporting and opposing evidence;
- checked/decisive artifact owner envelopes;
- competing hypotheses;
- task obligations;
- analysis perspective and every other Judge-visible factual field.

Requirements:

- define one canonical request projection and one versioned hash function;
- store the canonical projection or its exact hash in the pending queue and
  every started/completed/failed action;
- use the same value for action keys, replay lookup, checkpoint/report restore,
  journal/action projections, and evaluator validation;
- immediately before provider call or replay, rebuild the
  `RootConfirmationRequest`, recompute its canonical projection/hash, and
  require exact equality with persisted queue/action facts;
- any path, evidence, obligation, competitor, artifact, semantic hash, or
  factual-field mutation must reject replay and fail closed rather than reuse
  an old result;
- a bit-identical restored request must replay without another provider call.

Remove the narrow four-field identity as an authorization boundary. A separate
queue locator may remain only for in-memory lookup and must never authorize
provider-result replay.

Version all affected action/report/checkpoint/evaluator policy identities and
test old-state rejection or documented conservative migration.

### 2. Classify every global pass/failure episode before bijection

Before computing terminal global pass/failure cardinality, validate every
investigation journal or unresolved-branch record that carries any global
pass/failure marker.

Requirements:

- define exact allowed schemas for completed pass, failed pass, and failed
  unresolved episode;
- records with `kind="global_candidate_pass"`, `global_pass_identity`,
  `pass_identity`, or `failure_projection` must be classified exactly;
- reject unknown/non-terminal status, missing projection, extra or contradictory
  identity fields, malformed owner, and extra fields that alter meaning;
- do not filter malformed records out before validation;
- then require a strict multiset bijection:
  one failed action <-> one failed journal event <-> one unresolved episode
  <-> one owning seed blocker/missing-evidence set;
- reject an otherwise identical extra episode even if its projection is
  omitted, and reject extra unknown-status pass records.

Apply the same validator to live state, checkpoint/report restore, stale-seed
quarantine output, and evaluator validation.

### 3. Persist a typed global failure immediately

After any enabled global-pass failure mutates request accounting, failed action,
failure event/episode, and seed gap, write an immediate terminal checkpoint
before control flow continues.

Requirements:

- one helper should atomically finalize the canonical failure facts in memory
  and invoke `_checkpoint_state` with a versioned
  `global:failed:<pass_identity>` semantic key;
- use it for missing capability, capsule/request validation failure, typed
  `BoundedJudgeCallError`, generic provider failure, and invalid output;
- physical request deltas and uncertainty/exactness must be persisted before
  the checkpoint;
- restoration from that checkpoint treats the pass as terminal and cannot call
  the provider again;
- a simulated crash immediately after the failure checkpoint must restore the
  exact action/episode/seed gap and request counts;
- failure of one seed must not prevent another seed from completing.

Avoid a second failure representation. The immediate checkpoint must serialize
the same canonical projection validated in requirement 2.

## Verification

At minimum:

1. New Fix28 focused tests.
2. Fix17-Fix28 plus confirmation, global Judge, checkpoint, seed, and recursive
   core regressions.
3. Report/evaluator/replay/acceptance affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check b831bcd42..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix28-report.md` documenting
  root-cause validation, RED/GREEN evidence, identity/schema/policy decisions,
  exact test commands/counts, changed files, and residual risks.
- One commit containing only Fix28 files, this brief, and report. Do not touch
  or stage unrelated untracked files.
