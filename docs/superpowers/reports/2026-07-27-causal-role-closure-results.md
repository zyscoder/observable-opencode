# Causal Role Closure Results

## Scope

This iteration closes the first stage of the four-stage attribution roadmap:
independent causal-role confirmation on top of the existing retrieval-global
root analysis. It does not change Agent execution, Trace production, prompts
sent by the Harness, or tool behavior.

Validation used the sanitized Sphinx fixture
`sphinx-recursive-minimal` with DeepSeek `deepseek-v4-pro` through the
Anthropic-compatible endpoint. The API key was injected through a non-echoing
environment variable and was not persisted.

## Implemented Contract

- Root and non-root confirmation queues have independent Top-3 quotas.
- Global non-root rows are scheduling hints only; their roles are not exposed
  to the independent verifier.
- Root confirmation compares only open authored-root candidates.
- Non-root confirmation compares only already confirmed roots.
- Conditions, amplifiers, and unrelated candidates do not form a sibling
  root-election matrix.
- A non-root necessary-cause result becomes a co-root only with reciprocal
  `co_root` evidence.
- One-sided necessary-cause claims are retained in
  `factor_confirmation_conflicts` without invalidating the primary root.
- Unknown non-root results are retained in `factor_confirmation_gaps` and are
  non-blocking for root publication.
- Non-root enqueue rejection is retained in
  `factor_confirmation_enqueue_gaps`; it cannot silently reduce role coverage.
- Every factor gap/conflict binds exactly one completed non-root queue owner
  and exact Global-factor scheduler origin.
- Started and terminal confirmation actions carry `review_scope` and `origin`;
  action state is v19 and action projection is v8.
- Report, checkpoint, and output transaction schemas are v21, v21, and v10.
- A non-root necessary-cause result must have mutual `co_root` support from
  every published same-seed root before promotion.
- Definitive confirmations require grounded evidence references.
- Focused repair requests now identify exact invalid confidence fields and
  missing competitor identities.

## Real Sphinx Result

| Measure | Result |
| --- | --- |
| Analysis outcome | `confirmed_root` |
| Primary root | `record:decision` |
| Root recall / precision | `1.0 / 1.0` |
| Semantic root recall / precision | `1.0 / 1.0` |
| Top-1 match | `true` |
| Introduction recall / precision | `1.0 / 1.0` |
| Safety checks | passed |
| Fabricated / unresolved confirmed facts | `0 / 0` |
| Root confirmations | 1 |
| Non-root confirmations | 3 |
| Physical Judge requests | 13 |
| Logical Judge / confirmation calls | `5 / 4` |

The Global pass classified:

- `record:decision` as root candidate;
- `record:prompt` and `record:change` as contributing conditions;
- `record:timeout` as unrelated and `record:verification` as outcome evidence.

Independent confirmation produced:

- `record:decision`: confirmed necessary cause;
- `record:change`: independently claimed necessary cause but lacked reciprocal
  co-root support, so it was retained as a conflict;
- `record:prompt`: unknown because candidate-local evidence did not settle its
  factor role;
- `record:timeout`: unknown after competitor-matrix coverage repair exhaustion.

The expected prompt condition and timeout amplifier were therefore not
recovered. Factor-role precision remains `0.0`, unknown rate is `0.5`, and
human/LLM role disagreement is `0.666667`. These are role-calibration failures,
not root-grounding or Trace-reference failures.

The rerun also verified that every terminal action carried the expected
scope/origin pair: the root as `root/global_candidate_judgment`, and all three
factor candidates as `non_root/global_candidate_factor_assessment`. No enqueue
gap was fabricated; the change conflict is backed by the exact terminal
non-root action and confirmed root.

## Failure-Driven Fixes

Real-provider runs exposed four integration defects that unit-only validation
did not reveal:

1. DeepSeek repeatedly emitted certainty `1.0` with unknown input defect.
   Validation errors and repair constraints now identify the exact candidate
   and require confidence `0.99`.
2. A definitive unrelated confirmation omitted evidence refs and failed only
   during report publication. The confirmation boundary now rejects this
   earlier and requests grounded evidence.
3. Non-root candidates inherited the complete root-election competitor graph,
   causing missing-comparison failures. Their comparison set is now limited to
   confirmed roots.
4. A one-sided non-root necessary-cause claim erased a valid root. Co-root
   promotion now requires reciprocal support; otherwise the disagreement is
   recorded separately.
5. Report validation accepted factor gaps detached from queue ownership. Gaps
   and conflicts now require one exact completed non-root queue owner.
6. A failed factor enqueue could disappear after the hypothesis was rejected.
   The failed coverage attempt is now persisted and grounded against the
   corresponding Global assessment.
7. Non-root requests included merely queued root candidates even after they
   were rejected or became unknown. They now compare only published confirmed
   roots.
8. Partial pairwise co-root support could promote a candidate in a multi-root
   seed and invalidate request snapshots. Promotion now requires reciprocal
   support from every published same-seed root.
9. Coordinated edits across queue, journal, and action projection could hide a
   selected-root unknown as a factor gap. Factor gaps now also bind back to one
   reviewable, non-selected Global assessment.
10. Report and pending queue persistence could accept the old audit shape.
    Report/checkpoint/output are now v21/v21/v10, and pending origin is
    mandatory at restore time.

## Verification

- Full attribution suite: `1094 tests`, all passed.
- Focused causal-role suite: `12 tests`, all passed.
- Python compilation: passed with `PYTHONPYCACHEPREFIX` under `/private/tmp`.
- `git diff --check`: passed.
- Completed checkpoint replay: returned in `0.55s` with a dummy API key and no
  provider request; report SHA-256 remained
  `c4031305538e3793f3a22ec70c4a17bbee7ae39c6760a0151765366738619274`.

Artifacts:

- Final report:
  `/private/tmp/observable-opencode-causal-role-closure-20260727h/sphinx-minimal/recursive.attribution.json`
- Comparison:
  `/private/tmp/observable-opencode-causal-role-closure-20260727h/sphinx-minimal/recursive.comparison.json`

## Assessment And Next Stage

Root attribution is now structurally closed for this regression: non-root
uncertainty cannot erase a confirmed root, and every terminal fact remains
grounded and replayable.

The factor layer is not ready for cross-Benchmark quality claims. The current
root-confirmation response schema still asks a candidate to answer both
"am I necessary?" and "what non-root role do I have?" through one status model.
This encourages `unknown` when the verifier correctly recognizes a stronger
root but cannot express downstream materialization, enabling conditions, and
amplification independently.

The next stage should introduce a role-classification response projection while
reusing the same provider, evidence tree, cache, checkpoint, and grounding
validators. It should:

1. classify necessity separately from non-root role;
2. distinguish downstream defect materialization from causal contribution;
3. make exact competitor coverage schema-driven rather than prompt-repaired;
4. calibrate prompt conditions and interruption amplifiers across multiple
   open-source Benchmark families;
5. report per-role recall, precision, abstention, and request cost before
   entering cross-Benchmark stability optimization.
