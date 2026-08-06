# Causal Role Closure Design

## 1. Goal

Extend the retrieval-global attribution path from independently confirming only
necessary roots to independently confirming and publishing the bounded non-root
causal roles already identified by the Global Judge.

The change must preserve:

- passive, read-only analysis after Agent execution;
- the existing confirmed-root path and its Top-3 root limit;
- blind independent confirmation without exposing the Global Judge verdict;
- exact checkpoint replay and report validation;
- no promotion of retrieval rank or Global Judge output into a final fact.

## 2. Current Gap

The Global Judge produces a complete comparison matrix whose rows may classify a
candidate as `root_candidate`, `contributing_condition`, `amplifying_factor`,
`outcome_evidence`, `exculpatory_evidence`, `unrelated`, or `unknown`.

Only `selected_candidate_refs` are currently converted into hypotheses and
queued for independent confirmation. Therefore a non-root assessment remains in
`seed_results[].global_judgment.assessments` and is never independently checked
or projected into:

- `contributing_conditions`;
- `amplifying_factors`;
- `rejected_candidates`.

The report model and `RootConfirmation` verifier already support these
publications. The missing boundary is candidate scheduling.

## 3. Options Considered

### Option A: Publish Global roles directly

This is cheap but violates the independent-confirmation boundary. A mistaken
Global role would become a final causal fact without falsification.

### Option B: Reuse the independent verifier with separate quotas

Root and non-root candidates share the same factual request and confirmation
contract. The Global role is used only to select bounded review candidates and
is not included in the Judge-visible request. Root and factor candidates have
separate quotas.

This is the selected design because it preserves one confirmation truth model,
checkpoint lifecycle, evidence validation, and report projection.

### Option C: Add a dedicated Factor Judge

This gives a conceptually separate API but duplicates provider calls, schemas,
cache identity, replay logic, and grounding validation. It is not justified
until factor-specific evidence requirements materially diverge.

## 4. Candidate Selection

For a successful `candidate_roots` Global judgment:

1. Queue up to three selected root candidates exactly as today.
2. Build a non-root review pool from authored, active-revision candidates whose
   Global role is:
   - `contributing_condition`;
   - `amplifying_factor`;
   - `unrelated`.
3. Exclude selected roots, outcome evidence, exculpatory evidence, unknown rows,
   unresolved candidates, and candidates without a grounded path to the seed.
4. Rank the pool deterministically:
   - contributing condition;
   - amplifying factor;
   - unrelated;
   - higher confidence;
   - shorter causal path;
   - lexical candidate ref.
5. Queue at most three non-root candidates per seed.

The `unrelated` role remains reviewable because an independent verifier may
correct a Global false negative and classify the candidate as a condition or
amplifier.

## 5. Blind Confirmation

Every queued candidate receives the existing `RootConfirmationRequest`.

The request contains candidate facts, recursive path, evidence, obligations,
and competing hypotheses. It does not contain:

- the Global causal role;
- whether the candidate was selected as a root or factor;
- the first-pass verdict wording.

The verifier may return:

- `confirmed + necessary_cause`;
- `rejected + contributing_condition`;
- `rejected + amplifying_factor`;
- `rejected + unrelated`;
- `unknown + unknown`.

A candidate queued for factor review may be promoted to a confirmed root only
when it and every already published root for the same seed mutually classify
each other as independently verified `co_root` candidates. Partial pairwise
support is preserved as a role conflict and cannot alter the closed root set.
A one-sided necessary-cause claim is likewise preserved as a role conflict for
audit. Conversely, a selected root may be demoted to a non-root role.

Root confirmation compares only the Global pass's open authored-root set.
Non-root confirmation compares only against already confirmed roots. Sibling
conditions, amplifiers, and unrelated candidates do not compete with one
another.

## 6. Queue And Provenance Contract

Each queue entry records a non-Judge-visible `review_scope`:

- `root`;
- `non_root`.

It also records `origin`, preserving that the scheduler acted on a
Global matrix row without treating that row as final evidence. These fields are
included in started actions, terminal action projections, journals,
checkpoints, and report audit projections, but excluded from
`RootConfirmationRequest.factual_dict()`. The action state schema is v19 and
the confirmation action projection contract is v8. The report, checkpoint,
and output transaction schemas are v21, v21, and v10; older persistence
artifacts fail closed instead of being interpreted under the changed facts.

Per seed:

- root queue cardinality must not exceed 3;
- non-root queue cardinality must not exceed 3;
- total queue cardinality must not exceed 6;
- the same candidate may appear only once.

## 7. Publication Rules

Only definitive independent confirmations are published:

- root-scope `confirmed + necessary_cause` -> confirmed root;
- non-root `confirmed + necessary_cause` with reciprocal `co_root` evidence ->
  co-root;
- non-root one-sided necessary-cause claim -> auditable
  `factor_confirmation_conflicts` entry;
- `rejected + contributing_condition` -> contributing condition;
- `rejected + amplifying_factor` -> amplifying factor;
- `rejected + unrelated` -> rejected candidate;
- root-scope `unknown` -> unresolved branch;
- non-root `unknown` -> non-blocking `factor_confirmation_gaps` entry.

Trigger-like semantics remain represented as
`contributing_condition.factor_mechanism.mechanism_type=enabling_condition`.
No new top-level trigger collection is introduced in this phase.

Every factor publication must retain:

- confirmation identity and response identity;
- seed binding and defect fingerprint;
- grounded recursive path;
- evidence refs;
- factor mechanism for conditions and amplifiers;
- analysis perspective.

## 8. Failure Handling

- A factor candidate that cannot be queued is recorded as a bounded factor
  coverage gap in `factor_confirmation_enqueue_gaps`, not as a
  root-confirmation failure. The gap binds the candidate, defect, seed, Global
  role, scheduler origin, and rejection reason.
- Provider failure or insufficient evidence yields an unknown confirmation and
  a non-blocking factor gap for non-root review.
- Root outcome calculation remains controlled by root confirmations. Failure to
  classify an optional non-root factor does not erase a confirmed root.
- A root's `outperformed` or `rejected` comparison against an independently
  unknown non-root candidate closes that competitor for root publication.
- Checkpoint replay must reproduce queue scope, ordering, and final
  publications without repeating completed calls.
- Every factor gap or role conflict must bind exactly one completed
  `review_scope=non_root` queue item from
  `global_candidate_factor_assessment`. A factor enqueue gap must instead bind
  one reviewable Global non-root row and prove that no matching queue item
  exists.

## 9. Verification

Focused tests must prove:

1. Global conditions and unrelated rows are independently confirmed.
2. Global role text is absent from Judge-visible requests.
3. A Global unrelated row may become an independently confirmed amplifier.
4. Root and non-root quotas are enforced separately.
5. Outcome evidence is never queued as a causal factor.
6. Non-root confirmations do not appear in `introduction_candidates`.
7. Checkpoint serialization and report validation preserve exact ownership.
8. Existing confirmed-root tests remain unchanged and green.
9. A non-root necessary-cause claim remains auditable even when no root was
   confirmed, instead of crashing report construction.
10. Unknown or rejected root candidates are excluded from later non-root
    comparison requests.
11. Queue scope/origin and enqueue failures survive checkpoint replay and
    reject coordinated report forgery.
12. A selected-root unknown cannot become a non-blocking factor gap through
    coordinated queue, journal, and action edits.
13. Partial co-root support across a multi-root seed never promotes the
    non-root candidate or invalidates the existing roots.

The Sphinx regression must evaluate the prompt, change, and timeout rows
independently while retaining `record:decision` as the primary root. Publishing
the expected prompt condition and timeout amplifier remains a measured
calibration target rather than a prerequisite for root closure.
