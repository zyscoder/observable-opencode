# Root Confirmation Task 2 Fix Wave 17 Report

Base: `63b1f6056`

Branch: `codex/message-context-lineage`

## Result

Closed all three Critical and two Important findings in the Fix Wave 17 brief.
The implementation remains offline, passive, read-only, dependency-free, and
inside Task 2. Retrieval rank remains navigation-only.

## RED Evidence

All reviewer reproductions failed before their corresponding implementation:

- `PYTHONPATH=tools/trace_attribution python3 -m unittest -v tools.trace_attribution.tests.test_root_confirmation_fix17.EventSpecificRevisionEligibilityTest`
  failed because `change.revision_after=0` was accepted under active generation
  1 and a stale change could build a capsule.
- `PYTHONPATH=tools/trace_attribution python3 -m unittest -v tools.trace_attribution.tests.test_root_confirmation_fix17.StaleAnalysisStartTest`
  failed because stale explicit/default starts entered the frontier.
- `PYTHONPATH=tools/trace_attribution python3 -m unittest -v tools.trace_attribution.tests.test_root_confirmation_fix17.ActiveEpisodeContextTest`
  failed because stale episode members affected member refs, summaries, and
  counts.
- `PYTHONPATH=tools/trace_attribution python3 -m unittest -v tools.trace_attribution.tests.test_root_confirmation_fix17.EligibleBudgetPrefilterTest`
  failed because stale adjacency and semantic nodes consumed `scan_limit=1`.
- `PYTHONPATH=tools/trace_attribution python3 -m unittest -v tools.trace_attribution.tests.test_root_confirmation_fix17.JudgePayloadGroundingTest`
  failed because unresolved candidate evidence reached capsule/Judge-facing
  collections.
- `PYTHONPATH=tools/trace_attribution python3 -m unittest -v tools.trace_attribution.tests.test_root_confirmation_fix17.EligibilityNamingTest`
  failed because the generic candidate eligibility alias still existed.
- The focused checkpoint RED
  `test_resume_quarantines_a_stale_start_without_invoking_judge` failed in
  restored frontier evidence eligibility instead of producing a per-seed gap.
- The completed checkpoint RED
  `test_completed_restore_removes_publication_owned_by_a_stale_start` failed
  on the stale confirmation queue before seed quarantine.
- The direct global RED
  `test_direct_global_validation_rejects_a_stale_seed_and_start` reported
  `ValueError not raised`.
- The diagnostic-grounding RED showed that a forged resolved envelope inside
  `unresolved_references` could ground a bare candidate evidence ref.

## Implementation

- Active generation eligibility is event-specific: response claims and
  effective verifications use `repository_revision`; changes use
  `revision_after`. Exact numeric zero is preserved, while booleans, negatives,
  malformed values, missing required fields under authority, and mismatches
  fail closed.
- Stale explicit/default starts are rejected before global or recursive Judge
  work. Restored stale seeds are quarantined with
  `start_ref_active_revision_ineligible`; their live frontier, confirmation
  queue, confirmation ownership, and publications are removed without
  affecting other seeds.
- Causal and progress episodes, navigation windows, summaries, counts, and
  delivery signals are rebuilt from active members. Stale members remain only
  in excluded audit diagnostics.
- Adjacency and semantic scans remove stale endpoints before scan budgets,
  ranking, slicing, and truncation calculations.
- Capsule, global, recursive-step, and confirmation payload paths expose only
  resolved active evidence. Unresolved/stale refs are retained only in
  diagnostics excluded from exact Judge payloads. Capsule restore reconstructs
  those diagnostics from graph edges and authoritative routes without trusting
  persisted missing-ref claims.
- Ordinary evidence paths use `TraceGraph.active_revision_evidence_eligible`.
  Authored root selection uses the additional
  `authored_root_candidate_eligible` gate; the ambiguous compatibility alias
  was removed.

## GREEN Evidence

- Finding-focused:
  `PYTHONPYCACHEPREFIX=/tmp/fix17-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_root_confirmation_fix17`
  -> `12` tests passed.
- Affected attribution suites:
  artifact hydration, backward taint, checkpoint, causal Judge/retrieval,
  episodes, evaluation facts, capsules, global Judge, investigation, recursive
  acceptance/analyzer/benchmarks/CLI, Fix 17, and seed attribution
  -> `680` tests passed.
- Complete attribution suite:
  `PYTHONPYCACHEPREFIX=/tmp/fix17-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
  -> `745` tests passed.
- `PYTHONPYCACHEPREFIX=/tmp/fix17-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/tests`
  -> passed.
- Submission checks:
  `git diff --check` and `git diff --check 63b1f6056..HEAD`
  -> passed before commit. The exact base-to-commit check is repeated after
  commit.

## Modified Files

- `.superpowers/sdd/root-confirmation-task-2-fix17-report.md`
- `tools/trace_attribution/trace_attribution/causal_judge.py`
- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/episodes.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/investigation.py`
- `tools/trace_attribution/trace_attribution/judgment_context.py`
- `tools/trace_attribution/trace_attribution/progress.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_causal_retrieval.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix17.py`

The Fix Wave 17 brief was read and followed but did not require content changes.
No unrelated untracked file is included.

## Risks

- Restore quarantine has the highest state-management risk. It is constrained
  by exact seed and hypothesis ownership identities; stale non-start paths and
  publications continue to fail closed. Partial and completed restore tests
  cover both quarantine and publication removal.
- Capsule restoration now distinguishes recorded route identity from synthetic
  route identity while rebuilding missing diagnostics. Exact collection hashes
  and active reconstruction tests cover route substitution and drift.
- Episode recomputation intentionally ignores persisted aggregate counts.
  Mixed-member, all-active legacy, navigation, and full-suite regressions cover
  the compatibility surface.

## Persistence Identity

No serialized identity was bumped. The existing capsule v5 and Judge contracts
already define Judge evidence as grounded evidence. Stale and unresolved values
were invalid instances of that existing meaning, not a new valid shape or field
meaning. This wave enforces the existing contract and uses the existing
missing/unresolved diagnostic fields; valid serialized shapes and identities
remain unchanged.
