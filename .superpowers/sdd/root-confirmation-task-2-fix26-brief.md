# Root Confirmation Task 2 Fix Wave 26 Brief

Base commit: `bc7d3ed2c`

## Scope

Close every Critical/Important finding, plus the queue/key Minor finding, from
the cumulative Task 2 review of `bd8f1ab27..bc7d3ed2c`. Keep the attribution
pipeline offline, passive, read-only, and unable to feed results back to the
Agent. Do not implement Task 3 evidence expansion and do not add dependencies.

Use strict TDD for each behavior: add a focused failing test, run it and record
the expected RED, implement the smallest coherent fix, then run the focused
test GREEN. Finish with affected regression suites, the full attribution
suite, `compileall`, and `git diff --check bc7d3ed2c..HEAD`.

## Required Fixes

### 1. Apply strict active-start provenance at every seed boundary

Formal manifests must not admit an explicit seed/start that default start
selection would reject. Use the same strict active-start eligibility contract
for:

- explicit analyzer start refs and seed creation;
- Global Candidate Judge request seed/start refs;
- checkpoint/report restoration;
- stale-seed quarantine and all other seed-binding reconstruction boundaries.

Under formal provenance, revision-bearing starts require matching
`subject_revision` and valid record-level provenance. Preserve legacy omission
compatibility only where the graph has no formal binding requirement.

Add tests proving a formal unbound explicit start cannot enter `state.start_refs`,
cannot build/restore a global request, and cannot survive checkpoint/report
restore, while a correctly bound start and a non-formal legacy trace remain
valid.

### 2. Fail closed when explicitly enabled retrieval-global cannot complete

When `retrieval-global` is explicitly enabled, lack of Judge capability,
candidate capsule failure, request validation failure, bounded Judge failure,
or invalid Judge output must terminate that seed's global pass as an explicit
unresolved result:

- record the concrete blocker and missing evidence;
- produce `evidence_gap` or `inconclusive` as required by existing aggregation
  rules;
- prevent confirmation or publication of a root for that seed;
- persist and restore the failure action/episode bijectively.

Only an explicitly disabled/off mode may retain compatibility behavior that
does not require a global pass. Do not silently label an enabled-mode failure
`fallback_recursive` and continue to root publication.

Add tests for every failure branch and for checkpoint/report restore after
failure. Prove another seed can still complete independently.

### 3. Bind verified artifact evidence to one valid active owner

A verified artifact is eligible evidence only when:

- the manifest content is resolved, complete, available, non-truncated, and
  hash/byte-range valid;
- at least one record owner explicitly references it;
- the selected owner is active-revision eligible and formally bound where
  required;
- candidate-local use identifies the expected owner/path without ambiguity.

Zero active owners, only stale/unbound owners, or multiple unresolved active
owners must fail closed. Never select the lexicographically first owner.
Persist a complete owner/provenance envelope in confirmation actions, report
metadata, checkpoint state, and evaluator-facing projections. Revalidate the
same envelope on live use and every restore/evaluator path.

Add focused tests for orphan artifacts, stale-only owners, mixed stale/active
owners, ambiguous active owners, explicit owner selection, owner substitution,
and successful round-trip of a valid artifact envelope.

If this changes a persisted schema or policy identity, make the version change
explicit and test migration/rejection behavior. Do not silently reinterpret old
state.

### 4. Unify global-pass runtime and persisted identity

Use one canonical per-seed pass identity based on the seed binding identity.
The same identity must govern:

- whether a pass has already run;
- action keys and journal ownership;
- the single persisted seed `global_judgment`;
- checkpoint/report restoration;
- final/evaluator bijection validation.

Do not allow a new frontier hypothesis for the same seed to run a second
unsuperseded pass that overwrites the first judgment. If the implementation
needs multiple passes, model an explicit ordered supersession sequence and
validate it everywhere; otherwise enforce exactly one pass per seed.

Add tests for interrupted restore, multiple frontier occurrences for one seed,
duplicate completed actions, substituted owners, and one valid pass for each of
two independent seeds.

### 5. Reconcile step judgments with authoritative provider actions

Project every completed `provider_call_completed` action with
`call_kind="step"` into a canonical owner-bound step-judgment occurrence.
Require a strict multiset/bijection between that projection and:

- `step_judgments`;
- nested predecessor judgments;
- flat causal relation occurrences;
- report/checkpoint/evaluator projections.

Reject missing, extra, duplicate, substituted, or content-mutated judgments and
relations even when owner and occurrence identifiers are syntactically valid.
Failed/incomplete provider actions must never authorize a completed judgment.

Persist the projection in the report/evaluator contract. Version any changed
serialized schema or action/evaluator policy identity explicitly.

Add tamper tests covering content replacement, coordinated replacement of the
step judgment plus relation, duplicate actions, missing actions, failed action,
and valid replay/round-trip.

### 6. Make pending confirmation queue and key projection bijective

Derive canonical queue keys from queue entries and require exact equality with
the persisted/restored key set. Reject duplicate candidate entries for the same
seed, duplicate semantic keys, missing keys, and extra keys. Apply the same
validator to:

- live queue validation;
- checkpoint restoration;
- pending report restoration;
- evaluator validation.

Do not let an inconsistent key set suppress a later legitimate enqueue.

## Verification

At minimum run and report:

1. The new Fix26 focused test module(s).
2. Root Confirmation Fix17-Fix26 plus seed, recursive, checkpoint, global
   Judge, graph/artifact, and evaluator affected suites.
3. Evidence capsule regressions.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check bc7d3ed2c..HEAD`.

## Deliverables

- Code and focused regression tests.
- `.superpowers/sdd/root-confirmation-task-2-fix26-report.md` containing:
  root-cause validation, RED/GREEN evidence for each finding, schema/policy
  version decisions, exact test commands/counts, changed files, residual risks,
  and confirmation that unrelated untracked files were untouched.
- One commit containing only Fix26 files, this brief, and the report.
