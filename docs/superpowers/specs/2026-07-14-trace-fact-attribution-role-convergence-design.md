# Trace Fact And Attribution Role Convergence Design

## Background

The FeatureBench Seaborn case
`mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1` completed through the
same HTTP session path used by benchmark execution. Host-side hidden tests
reported 26 passed, 30 failed, and 5 skipped tests, while the protected
point-to-point regression set reported 220 passed tests.

Manual backward analysis identified two primary defect introductions:

- `decisionnode_dec_86_a21bcba3` materialized an over-simplified regression
  implementation;
- `decisionnode_dec_325_693609d2` observed an `lmplot` failure but incorrectly
  excluded it from the task scope.

`decisionnode_dec_330_861d440b` propagated those defects into a premature
completion decision. The offline analyzer found useful upstream planning and
verification defects, but it also exposed four systematic problems:

1. a pytest run with `3 passed, 14 warnings` was classified as failed because
   warning source locations were parsed as failures;
2. later code changes were linked to an old failed verification merely because
   it was the most recent recognized verification;
3. the scope decision and the user-visible response were not linked, so the
   answer surface was selected as a root;
4. episode collapse hid the concrete implementation point, while broad
   evidence candidates caused fragmented and occasionally incorrect roots.

This design converges the passive trace fact layer and the offline attribution
model without changing the observed agent's prompts, tools, control flow, or
outputs.

## Goals

1. Make verification outcomes authoritative, mixed-result aware, and resistant
   to warning text and shell-pipeline ambiguity.
2. Link repository changes only to proximal, observable evidence.
3. Reconstruct reasoning-to-response provenance within one assistant message.
4. Detect contradictions between completion claims and known execution facts.
5. Preserve strategic, materialization, verification, scope, and propagation
   roles instead of collapsing every defective node into a flat root list.
6. Reduce judge input noise while retaining complete evidence in artifacts.
7. Make the offline analyzer's own progress, model use, repair attempts, and
   failures observable.
8. Rebuild and reanalyze the existing Seaborn trace before running a new case.

## Non-Goals

- No runtime diagnosis or root-cause decision is added to observable-opencode.
- No derived trace fact or attribution result is fed back to the observed agent.
- No prompt, tool result, model request, action selection, or task-loop behavior
  is modified.
- No causal relation is inferred from temporal proximity alone.
- No complete causal intermediate-representation rewrite is attempted in this
  iteration.
- No raw stream delta is promoted into a standalone semantic record.

## Architecture

The implementation keeps two explicit boundaries:

1. **Trace fact derivation** runs during trace finalization and converts raw
   observed events into conservative semantic facts and provenance edges.
2. **Offline attribution** consumes immutable trace facts, reconstructs the
   backward graph, invokes the judge, and assigns causal roles per observed
   defect branch.

Both layers are post-hoc observers. Their outputs may be inspected by a person
or a later optimization system, but they cannot influence the recorded run.

## Trace Fact Derivation

### Verification Outcome Classifier

Verification classification uses the following precedence:

1. a recognized framework final summary is authoritative;
2. explicit failure and error sections contribute structured failures;
3. mixed semantic signals in a generic script produce `contradictory` or
   `partial`, even when the process exit code is zero;
4. the process exit code is a fallback only when no authoritative semantic
   result is present;
5. an unparseable result remains `unknown`.

For pytest, failure locations count only inside a recognized `FAILURES` or
`ERRORS` section. Warning sections, warning summaries, deprecation messages,
and source locations inside warnings cannot create failed tests.

Generic command output is scanned for structured negative outcomes such as
uncaught exceptions, explicit `[FAIL]` records, or component-specific failure
messages. A later blanket line such as `All tests passed` does not erase an
earlier explicit failure. The record retains both positive and negative facts
and exposes their source artifact ranges.

The normalized outcome is one of:

- `passed`;
- `failed`;
- `partial`;
- `contradictory`;
- `cancelled`;
- `unknown`.

### Proximal Evidence-To-Change Provenance

The current `recent_failed_verification` fallback is removed. A repository
change receives `motivated_by_evidence` only when the relation is supported by
one of these observable mechanisms:

1. an explicit source, decision, action, call, span, or artifact reference;
2. a same-message reasoning/action chain that consumes the evidence before the
   edit;
3. exact normalized evidence content retained in the edit-producing model
   request;
4. a confirmed tool-result-to-edit execution episode.

Eligible evidence includes verification records, generic tool results,
diagnostics, MCP or skill outputs, and prior repository observations. The edge
stores the mechanism, confidence tier, source ref, target ref, and supporting
artifact range.

If several candidates remain, the derivation ranks explicit identity above
content matching and content matching above bounded same-message ordering. A
purely temporal candidate is displayed as advisory context but is not emitted
as an attribution-eligible causal edge. If no candidate is reliable, the
change remains unlinked.

### Reasoning-To-Response Provenance

Within one assistant message, the derivation orders reasoning blocks, actions,
tool observations, and response segments by their observed part/event order.
It emits:

- `reasoning_generated_response` when a response follows a reasoning decision
  in the same message with no conflicting message identity;
- `reasoning_expressed_as_response` when exact or normalized content matching
  confirms that the response expresses the decision;
- `completion_decision_generated_claim` when a completion decision produces a
  user-visible completion claim.

Same-message identity and deterministic part ordering are required. Temporal
coincidence across messages is insufficient. These edges describe provenance;
they do not assert that the reasoning or response is correct.

### Completion Claim Conflict Detection

A completion claim is evaluated against facts available before the claim:

- authoritative verification outcomes;
- mixed or contradictory generic tool results;
- explicit exceptions and component failures;
- unresolved required behavior identified in reasoning;
- scope exclusions that contradict the task statement;
- coverage boundaries of the supporting verification.

Exit status alone cannot directly support universal claims such as "all
implementations are complete and passing." A narrow smoke test may support the
tested behavior only. The claim record distinguishes:

- `direct_support_refs`;
- `scope_limited_support_refs`;
- `conflicting_evidence_refs`;
- `unresolved_requirement_refs`.

The claim is `supported` only when its scope is covered and no conflict exists.
Otherwise it is `partially_supported`, `conflicted`, or `unknown`.

### Semantic Noise Policy

Raw stream deltas, repeated message envelopes, and large candidate arrays stay
in raw events or artifacts. Semantic records contain compact summaries and
artifact references. Full candidate evidence remains inspectable in a
sidecar artifact, while only the highest-ranked proximal candidates are
included in semantic summaries.

## Offline Attribution

### Root Role Model

Every defect-introduction result receives one causal role:

- `strategic_root`: an incorrect plan or task decomposition;
- `materialization_root`: the action or change where the defect entered the
  executable implementation;
- `verification_root`: a defective verification design or assertion;
- `scope_root`: an incorrect inclusion or exclusion of required behavior;
- `completion_propagation`: a completion decision that carries an existing
  defect but does not introduce it.

The report exposes:

- `primary_root` for the earliest actionable introduction relevant to the
  observed defect;
- `contributing_roots` for independent causes;
- `strategic_precursors` for upstream planning defects;
- `materialization_refs` for the concrete implementation or verification
  points;
- `propagation_refs` for downstream decisions and claims.

A causal episode may retain several role-bearing members. Episode collapse can
select a display representative, but it cannot discard a materialization root
or convert propagation into an independent root.

### Boundary Reclassification

After branch traversal, the analyzer deterministically checks every proposed
root:

1. if it has an eligible defective upstream, it becomes propagation;
2. if it is an answer surface with an eligible reasoning producer, traversal
   continues to the producer;
3. if it is truthful evidence, it cannot be a root;
4. if its defect entered executable behavior at a later episode member, it is a
   strategic precursor and the later member is retained as materialization;
5. if the required relation is unavailable, the result remains `unknown`
   instead of fabricating a root.

### Candidate Selection

Judge prompts receive at most 12 ranked upstream candidates per node. Ranking
uses:

1. explicit defect-propagation edges;
2. explicit source and execution identity;
3. same-message reasoning/action provenance;
4. exact content retention;
5. bounded advisory context.

The complete candidate set and ranking explanation are written to an artifact.
Candidate trimming changes only offline analysis input; it does not alter the
original trace or agent execution.

### Analyzer Telemetry And Checkpoints

The analyzer writes an append-only checkpoint JSONL next to the final report.
Each entry contains:

- run, branch, node, and attempt identifiers;
- traversal depth and queue size;
- judge start/end timestamps and elapsed time;
- transport outcome, timeout, schema validation, and repair count;
- model and provider identifiers;
- input, output, cache, and reasoning token counts when reported;
- selected candidate refs and artifact hydration counts;
- final node status and causal role.

The final report summarizes calls, repairs, unknown judgments, token usage,
elapsed time, and termination reason. A partial checkpoint remains useful when
the analyzer is interrupted before its final report is written.

## Error Handling

- Verification text that cannot be classified remains `unknown`.
- Missing evidence identity never falls back to an eligible temporal edge.
- Missing response provenance creates an observability gap, not an answer-level
  root.
- Judge timeout, transport failure, or irreparable schema output creates a
  branch-local `unknown` judgment and a checkpoint record.
- Artifact truncation is reported with original and loaded sizes.
- Interrupted attribution preserves completed checkpoints and marks unfinished
  branches as inconclusive.
- A claim conflict parser failure cannot silently turn a claim into supported.

## Test Strategy

Implementation follows red-green-refactor and keeps TypeScript trace tests and
Python attribution tests independent.

### Trace Unit Tests

1. `3 passed, 14 warnings` is `passed`, and warning source lines do not become
   failures.
2. Output containing `lmplot failed` followed by `All integration tests passed`
   is `contradictory`.
3. A piped pytest command uses the framework summary when present and remains
   conservative when absent.
4. A change links to a proximal generic smoke-test failure instead of an old
   failed verification.
5. A change with no supported provenance remains unlinked.
6. Same-message reasoning links to its response output.
7. A universal completion claim conflicts with a known component failure.
8. Narrow test success is recorded as scope-limited support.

### Attribution Unit Tests

1. A response surface with a reasoning producer is not selected as a root.
2. A scope decision becomes `scope_root`.
3. A later completion decision with defective upstream becomes
   `completion_propagation`.
4. An early plan and later edit remain `strategic_root` and
   `materialization_root` in the same episode.
5. Candidate prompts contain at most 12 records while the artifact preserves
   the full set.
6. Checkpoints record successful, repaired, timed-out, and interrupted calls.
7. An unavailable causal link yields `unknown`, not a temporal root.

### Regression Suites

- All existing `packages/opencode` trace tests pass.
- Type checking passes from `packages/opencode`.
- All existing `tools/trace_attribution/tests` tests pass.
- No test or production hook sends derived attribution data to the observed
  agent.

## Seaborn Replay Acceptance Gate

The existing Seaborn raw trace is rebuilt and the same observed-defect review is
reanalyzed before a new benchmark case is executed. Acceptance requires:

1. `vernode_ver_3_cd35c91a` is `passed`, not `failed`;
2. the integration result containing `lmplot failed` is `contradictory`;
3. `chg_7`, `chg_8`, and `chg_9` are not linked to stale `ver_2` through
   `recent_failed_verification`;
4. `decisionnode_dec_325_693609d2` has an attribution-eligible provenance path
   to `responsenode_segment_22_237890ad`;
5. the final all-passing claim contains the `lmplot` failure as conflicting
   evidence;
6. the scope branch selects `dec_325` as its primary root and does not select
   the response surface;
7. the incomplete-contract branch retains `dec_86` as a materialization root
   while earlier plan decisions remain strategic precursors;
8. `dec_330` is propagation rather than an independent primary root;
9. all three defect branches report `root_found`, with no unresolved reference
   and no judge transport error;
10. primary manual roots match 2 of 2, and the total independent root set is no
    larger than five;
11. judge prompts contain no more than 12 candidates per node, with full
    candidates available in artifacts;
12. attribution checkpoint and summary telemetry are complete.

Only after this acceptance gate passes does the next optimization loop execute
a new open-source benchmark case.

## Delivery Sequence

1. Add failing trace fact and provenance tests.
2. Implement verification classification and proximal evidence derivation.
3. Implement reasoning/response lineage and claim conflict derivation.
4. Add failing attribution role, boundary, candidate, and checkpoint tests.
5. Implement role-preserving attribution and telemetry.
6. Run TypeScript and Python regression suites.
7. Rebuild the existing Seaborn trace without rerunning the observed agent.
8. Rerun offline attribution against the unchanged manual review.
9. Compare acceptance metrics and document remaining gaps.
10. Commit, push, release, and begin the next stress-case loop only after the
    replay gate passes.
