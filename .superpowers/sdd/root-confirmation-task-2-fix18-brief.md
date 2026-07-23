# Root Confirmation Stability Task 2 Critical Fix Wave 18

Base commit: `bd60e0509`

Close all Critical and Important findings from the second independent
whole-Task-2 review.

## 1. Quarantine every publication owned by a stale seed

Partial and completed restore must remove or quarantine all state owned by a
stale start seed before ownership validation. Apply exact seed/hypothesis
ownership filtering consistently to:

- confirmations and confirmation queue;
- confirmed roots and co-roots;
- contributing conditions;
- amplifying factors;
- rejected candidates;
- global/local judgments, decisive evidence, and expansion state.

The stale seed must converge to a per-seed `evidence_gap`/`inconclusive` result
without blocking active seeds. Add partial, completed, multi-seed, contributing
condition, rejected candidate, and mixed-publication RED tests.

## 2. Deep-sanitize every Judge-visible fact before serialization

Introduce one graph-aware structural sanitizer for global, recursive-step, and
confirmation Judge payloads. It must run before compacting or stringifying
node data.

- Resolve aliases and validate every nested ref/ref-list/evidence object.
- Remove an entire associated evidence object when its identity is stale,
  unresolved, malformed, ambiguous, or artifact-ineligible; deleting only its
  ref field is insufficient.
- Never serialize stale/unresolved nested facts into opaque `content`.
- Missing/truncated/unresolved artifact identities and excerpts remain in an
  audit-only collection excluded from every Judge prompt and grounded ref set.
- Prompt-visible payloads contain only active resolved facts and verified
  artifact content.

Inspect exact final JSON for global, recursive-step, and confirmation requests.
Cover bare aliases, nested evidence objects, sibling content, missing/truncated
artifacts, competing hypotheses, restored capsules, and valid active controls.

## 3. Build one authoritative active progress-episode projection

Create one active episode projection used by judgment context, investigation,
navigation windows, active path reconstruction, and summaries.

- Drop episodes with zero active members before scan/window limits.
- Recompute per-episode summaries, member counts, delivery/change/verification
  counts, first/last positions, and semantic flags from active members.
- Recompute cumulative counts and consecutive no-delivery values along the
  filtered episode chain; never retain persisted aggregate values.
- Compute truncation and window metadata from eligible projected episodes.
- Stale persisted aggregates and all-stale episodes may appear only in
  audit-only diagnostics excluded from Judge payloads.

Cover mixed/all-stale episodes, one active change against stale persisted
`cumulative_change_count=2`, `limit=1`, chain recomputation, investigation, and
judgment-context consumers.

## 4. Version the active evidence policy and compatibility surfaces

Bump `graph-external-evidence-eligibility/v1` to a new identity reflecting
event-specific active-generation semantics. Incorporate the new identity into
checkpoint, cache, capsule, global judgment, and other persisted compatibility
identities that can reuse conclusions produced under the older policy.

Old policy state must never be silently accepted. Either reject it and force
reanalysis or conservatively migrate each seed to `inconclusive` with an
explicit policy-migration blocker. Add tests for old/new identities, cache
misses, partial/completed checkpoints, and current-state round trips.

## Constraints And Verification

- Task 2 only; no Task 3 evidence expansion.
- Offline, passive, read-only; no Agent feedback or behavior change.
- Retrieval rank/score remains navigation-only.
- Missing evidence is `evidence_gap`/`inconclusive`, never a forced root.
- Preserve per-seed isolation.
- No new dependency.
- Strict TDD: capture each reviewer reproduction as RED before implementation.
- Run focused reviewer tests, all affected attribution suites, the full
  attribution suite, compileall, and `git diff --check bd60e0509..HEAD`.
- Commit only this wave and write
  `.superpowers/sdd/root-confirmation-task-2-fix18-report.md`.
