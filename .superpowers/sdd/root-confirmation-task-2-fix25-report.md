# Root Confirmation Task 2 Fix Wave 25 Report

Base commit: `54625368a`

## Status

DONE

Fix25 remains limited to Root Confirmation Task 2. The implementation is
offline, passive, read-only, does not feed results back to the Agent, adds no
dependency, and does not implement Task 3 behavior.

## Findings Closed

1. A shared, event-independent subject/provenance validator now governs
   active-revision evidence. Any explicit `revision_provenance_status` other
   than `valid` is ineligible. Under a formally provenanced manifest, every
   revision-bearing record requires matching subject revision and valid record
   provenance. Every default-start branch uses the stricter start validator,
   including `response.output`. Legacy omission remains compatible only where
   no formal binding is required.
2. Confirmation evidence validation now distinguishes graph paths from
   evidence references. `checked_evidence_refs` accepts active trace nodes and
   complete verified manifest artifacts. Artifact confirmation requests carry
   a full content, hash, byte range, owner, provenance, and availability
   envelope. Queueing, live confirmation, partial/pending/completed restore,
   and evaluator validation accept verified artifacts and reject
   missing/truncated/unresolved artifacts. Failed enqueue attempts create the
   explicit per-seed `confirmation_enqueue_failed` blocker.
3. Local state validation rejects duplicate step, predecessor, and causal
   relation occurrence identities. Nested judgment predecessor projections
   and persisted causal relations are reconciled as a canonical bidirectional
   bijection, rejecting missing, extra, substituted, duplicate, and
   contradictory relation occurrences in live state, every restore surface,
   and evaluator validation.
4. Partial, pending, and completed restoration now share one nested
   investigation owner extractor and quarantine predicate. Ownership is found
   through direct owners, active visits, directive arguments, hypotheses, seed
   bindings, referenced state, and frontier visit keys. Ledger snapshots are
   excluded from ownership inference to avoid cross-seed false positives.
   Investigation round and byte counters are rebuilt from retained journal
   entries whenever quarantine removes state.

## Version Judgment

Checkpoint, action-state, report, evidence-policy, and Judge prompt identities
remain unchanged. Fix25 adds no persisted field and changes no serialized
shape. It enforces the already-declared formal provenance contract, restores
the existing evidence-reference meaning consistently for verified artifacts,
and validates existing owner/relation projections more strictly. Existing
checkpoint and report data that violates those contracts is intentionally
rejected rather than migrated.

## TDD Record

- RED: formal revision-bearing and default-start records could omit matching
  subject/provenance; `response.output` was among the escaping start records.
- GREEN: one shared validator covers generic evidence, revision authorities,
  and every default-start branch while retaining non-formal legacy behavior.
- RED: verified artifacts could not reliably enter confirmation requests or
  survive all restore/evaluator surfaces; enqueue failure could silently drop
  a selected candidate.
- GREEN: complete artifact envelopes work across live and restored
  confirmation, invalid artifact states fail closed, and enqueue failure
  blocks the owning seed explicitly.
- RED: duplicate local occurrences and missing or contradictory causal
  relation projections survived local-state validation.
- GREEN: occurrence uniqueness and canonical predecessor/relation bijection
  are enforced on partial, pending, completed, and evaluator paths.
- RED: stale ownership hidden in nested investigation state escaped report
  quarantine, while broad recursive scans could also remove active-seed
  journals through mixed ledger snapshots.
- GREEN: shared structural extraction covers nested ownership and actual
  frontier checkpoint sections, preserves active mixed-seed entries, and
  rebuilds derived counters after quarantine.

## Files

- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix25.py`
- `tools/trace_attribution/tests/test_causal_retrieval.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `.superpowers/sdd/root-confirmation-task-2-fix25-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix25-report.md`

## Verification

- Focused Fix25:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix25-final-focused2-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix25.py`
  - 13 tests, PASS
- Core affected Fix17-Fix25, seed, recursive, checkpoint, and evaluator:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix25-final-core-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix17.py tools/trace_attribution/tests/test_root_confirmation_fix18.py tools/trace_attribution/tests/test_root_confirmation_fix19.py tools/trace_attribution/tests/test_root_confirmation_fix20.py tools/trace_attribution/tests/test_root_confirmation_fix21.py tools/trace_attribution/tests/test_root_confirmation_fix22.py tools/trace_attribution/tests/test_root_confirmation_fix23.py tools/trace_attribution/tests/test_root_confirmation_fix24.py tools/trace_attribution/tests/test_root_confirmation_fix25.py tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_recursive_benchmarks.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_causal_checkpoint.py`
  - 348 tests, PASS
- Graph/artifact/recursive/checkpoint/evaluator affected:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix25-final-graph-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_backward_taint.py tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_investigation.py tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_confirmation_path.py tools/trace_attribution/tests/test_recursive_cli.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_benchmarks.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_causal_checkpoint.py tools/trace_attribution/tests/test_root_confirmation_fix25.py`
  - 517 tests, PASS
- Evidence capsule regression:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix25-final-capsule-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evidence_capsule.py`
  - 26 tests, PASS
- Full attribution suite:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix25-final-full2-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
  - 833 tests, PASS
- Compile:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix25-final-compileall2 python3 -m compileall -q tools/trace_attribution`
  - PASS
- Exact baseline whitespace check:
  `git diff --check 54625368a..HEAD`
  - PASS

## Self-Review

- Formal and malformed manifests, explicit invalid provenance, revision
  generation zero, legacy omission, and every default-start branch are covered.
- Artifact evidence is never accepted as a graph path. Only verified,
  available, non-truncated artifacts can enter evidence envelopes.
- Relation reconciliation compares complete canonical projections while
  retaining the intentional judgment-predecessor/relation shared occurrence.
- Nested quarantine follows ownership-bearing state and ignores whole-ledger
  snapshots, preventing stale cross-seed context from deleting active entries.
- No dependency, Agent feedback, Task 3 behavior, or unrelated untracked file
  was added or modified.

## Risks

The shared provenance validator deliberately rejects previously tolerated
formal records that omit record-level provenance. Legacy traces without a
formal manifest retain their prior compatibility. No other known concern
remains.
