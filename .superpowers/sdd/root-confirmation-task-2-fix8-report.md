# Root Confirmation Stability Task 2 Fix Wave 8 Report

Base commit: `906bf4007`

## Scope

Closed all three Important findings from the eighth whole-Task-2 review. The
analysis remains offline, passive, read-only, dependency-free, and isolated
from agent behavior. No Task 3 evidence-expansion behavior was added.

## RED

Added the targeted regressions before production changes and ran the six
finding-level tests together.

Result: `Ran 6 tests` / `FAILED (failures=16, errors=3)`.

The failures proved:

- all five named evidence-only event types passed the direct authored-root
  predicate, recursive introduction validation, and live/restored queue guard;
- global selection passed only because it used a divergent local exclusion
  list rather than the shared predicate;
- capsule restore validated only the first downstream path reference;
- no active-graph capsule validator existed for changed event type, stale
  revision, or fabricated eligibility; and
- top-level `confirmed`, `rejected`, and `unknown` confirmations could be
  orphaned from the per-seed ledger.

A separate partial-checkpoint reproduction ran one test and failed once,
proving that restored state accepted an orphan `unknown` confirmation before
the completed-restore assertion was reached.

## Design Decisions

- `root_candidate_eligible()` is the authoritative authored-root policy.
  Repository-defined evidence/outcome types, including `tool.result`,
  `tool.error`, `verification`, `evidence.fact`, `evidence.semantic_fact`,
  `claim.support_assessment`, and `external.evaluation_fact`, are never roots.
  They remain available as evidence and do not poison a valid authored-root
  branch merely because a scripted judgment attempts to introduce them.
- Candidate capsule v2 persists exact active-graph facts: canonical identity,
  component, event type/kind, subject/repository revision fields, navigation
  eligibility fields, evidence eligibility, and authored-root eligibility.
  One shared validator recomputes these facts and compares the embedded node
  and every raw/resolved/canonical downstream path reference at its exact path
  position.
- Validation-envelope persistence moved to v2. Live construction, partial
  checkpoint restore, completed/pending report restore, and final publication
  all use the same active-graph capsule validator. Older envelopes are rejected
  for migration or rejudgment instead of being interpreted under the stronger
  contract.
- `validate_confirmation_ownership()` enforces exact two-way ownership for
  every confirmation status. Each top-level identity maps to exactly one
  `(seed_ref, defect_fingerprint)` entry and appears in that entry; each listed
  seed identity resolves to exactly one matching top-level confirmation.
  Builders retain all attempted confirmation identities even when unresolved
  evidence prevents root publication.
- Conservative v2 report migration moves legacy confirmations into migration
  metadata rather than retaining orphan top-level audit records.

## Verification

1. Exact targeted regression set:
   - `Ran 7 tests in 0.031s` / `OK` after implementation.
2. Task 2-focused attribution set:
   - `Ran 320 tests in 1.859s` / `OK`.
3. Full Python attribution suite:
   - `Ran 682 tests in 3.680s` / `OK`.
4. Compile check:
   - `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix8-pycache python3 -m compileall -q` over attribution source, scripts, and tests; exit status `0`.
5. Working-tree diff check:
   - `git diff --check`; exit status `0`.
6. Required committed base-range diff check:
   - `git diff --check 906bf4007..HEAD`; exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_backward_taint.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_causal_judge.py`
- `tools/trace_attribution/tests/test_causal_state.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `tools/trace_attribution/tests/fixtures/recursive_cases/tool_error_ignored.json`
- `.superpowers/sdd/root-confirmation-task-2-fix8-report.md`

## Residual Risk

- Capsule v2 intentionally invalidates older persisted validation envelopes;
  affected checkpoints require conservative migration or global rejudgment.
- Exact embedded-node comparison is deliberately strict. Any active trace fact
  or revision change invalidates the restored capsule instead of attempting a
  partial compatibility interpretation.
- Evidence-only introduction judgments are retained as evidence but are not
  independently confirmed or published as factors; authored candidates on the
  same seed remain eligible for normal confirmation.
