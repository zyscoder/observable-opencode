# Root Confirmation Task 2 Fix Wave 22 Report

Base commit: `118fd29e2`

## Status

DONE

Fix22 remains limited to Root Confirmation Task 2. The implementation is
offline, passive, read-only, does not feed results back to the Agent, adds no
dependency, and does not implement Task 3 behavior.

## Findings Closed

1. Completed global-pass actions now have unique exact owners. Canonical
   owner/payload multisets for global judgments, candidate compression,
   recursive expansion reasons, pass counts, fusion mode, and physical request
   totals must match in both directions. Duplicate, omitted, substituted,
   cross-seed, and rewritten derivations fail pending and completed restore.
2. A shared state reconciler binds each seed-ledger global judgment to its
   unique completed pass action, including the complete judgment payload,
   validation envelope, candidate capsules, compression facts, expansion
   history, selected candidates, candidate refs, and decisive evidence.
3. Confirmation queue and journal entries now reconcile exact confirmation
   payloads with top-level confirmations, seed decisive evidence, and completed
   checkpoint actions visible at the restored snapshot transaction. Stale-seed
   quarantine removes the corresponding introduction bindings before
   reconciliation. Legacy non-completed journal bookkeeping remains accepted,
   but any entry claiming completion requires the full payload.
4. `active_repository_revision()` now filters candidate producers before
   taking the maximum. Explicit revision status and provenance status,
   manifest-bound subject revision, event-specific numeric revision, evidence
   eligibility, and audit/offline behavior are checked without consulting the
   active numeric revision. A stale high revision cannot displace a valid lower
   revision; valid ties and zero remain authoritative.
5. Evaluator validation calls the analyzer's exact graph/report validator
   before scoring. Cross-seed owner transplants, forged hypothesis/visit/
   occurrence identities, missing or duplicate global derivations, same-owner
   payload rewrites, and extra candidate facts are rejected identically.

## Shared Validation

The implementation uses one canonical multiset comparator, one exact
global-pass owner index, one state action-payload reconciler, and one public
`validate_recursive_report_against_graph()` entry point. Live checkpoint
serialization, partial restore, pending/completed report restore, and evaluator
validation all reuse these rules; no parallel evaluator approximation was
added.

## Persisted Identities

Checkpoint, action-state, report, evidence-policy, and Judge prompt schema
identities remain unchanged. Fix22 adds no serialized fields and changes no
persisted field meaning. It strengthens validation and reuses the existing
journal, metadata, owner, ledger, frontier, and manifest facts. The exported
manifest revision validator is an in-process helper only.

## TDD Record

- RED: pending and completed restore accepted duplicate derived judgments that
  omitted another authoritative pass; partial restore accepted same-owner
  rewrites in global judgment, expansion, decisive evidence, candidate refs,
  confirmation queue, and confirmation journal content.
- RED: a stale revision-99 claim displaced a valid revision-2 change; stale
  status/subject, invalid provenance, audit-only, offline-only, and explicitly
  ineligible producers could establish numeric authority.
- RED: evaluator validation accepted shape-valid forged hypothesis and visit
  owners and did not run analyzer action reconciliation.
- GREEN: 14 focused Fix22 tests cover invalid mutations and valid controls.
- Regression refinement: checkpoint action comparison was bounded to the
  restored snapshot transaction and active seeds, preserving crash replay and
  stale-seed quarantine while keeping completed represented actions exact.

## Verification

- Focused Fix22:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix22-focused-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix22.py -v`
  - 14 tests, PASS
- Core affected Fix17-Fix22, seed, recursive, checkpoint, and evaluator:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix22-affected-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix17.py tools/trace_attribution/tests/test_root_confirmation_fix18.py tools/trace_attribution/tests/test_root_confirmation_fix19.py tools/trace_attribution/tests/test_root_confirmation_fix20.py tools/trace_attribution/tests/test_root_confirmation_fix21.py tools/trace_attribution/tests/test_root_confirmation_fix22.py tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_recursive_benchmarks.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_causal_checkpoint.py`
  - 315 tests, PASS
- Graph/recursive/checkpoint/evaluator affected:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix22-graph-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_backward_taint.py tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_investigation.py tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_confirmation_path.py tools/trace_attribution/tests/test_recursive_cli.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_benchmarks.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_causal_checkpoint.py`
  - 504 tests, PASS
- Full attribution suite:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix22-full-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
  - 800 tests, PASS
- Compile:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix22-compileall-final python3 -m compileall -q tools/trace_attribution`
  - PASS
- Exact baseline whitespace check:
  `git diff --check 118fd29e2..HEAD`
  - PASS

## Self-Review

- Canonical comparisons are bidirectional multisets, so duplicate and missing
  occurrence coverage cannot cancel by length.
- Owner uniqueness is checked before deriving report collections.
- Partial state uses only action records committed no later than its snapshot;
  represented completed actions remain exact and future replay actions are not
  mistaken for omissions.
- Revision authority is computed without recursive dependence on the active
  numeric revision and preserves integer zero while rejecting booleans.
- Evaluator grounding checks retain their established diagnostics, then the
  same analyzer owner/action reconciler runs before any score is computed.
- Existing unrelated untracked files were not modified or staged.

## Concerns

None.
