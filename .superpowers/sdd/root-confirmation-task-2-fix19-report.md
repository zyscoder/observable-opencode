# Root Confirmation Task 2 Fix Wave 19 Report

Base commit: `3e5d8b878`

## Status

DONE

Fix19 remains limited to Root Confirmation Task 2. The implementation is
offline, passive, read-only, does not feed results back to the Agent, does not
implement Task 3, and adds no dependency.

## Findings Closed

1. Typed evidence/reference/artifact mappings are classified before
   projection. A stale, unresolved, malformed, ambiguous, or ineligible
   identity removes the whole typed mapping, including sibling content.
   Ordinary business fields such as `config_ref` and `customer_ref` are not
   treated as graph references merely because of their suffix.
2. Judge-visible artifacts require indexed graph provenance and verified
   bytes. Self-hashed unindexed artifacts, missing artifacts, integrity
   failures, and semantic-slice-only truncated artifacts are excluded as whole
   facts. Truncated semantic slices now report `availability=truncated`, never
   `available`.
3. Persisted local analysis state carries an exact owner envelope:
   `seed_binding_identity`, `hypothesis_id`, `visit_key`, and
   `occurrence_identity`. Restore filters state by exact active owner, preserves
   active sibling state sharing the same node ref, and rejects ownerless or
   malformed current state.
4. Episode investigation projects the authoritative active episode set before
   adjacency scanning, scan/output budgets, inspected counts, window
   construction, and truncation metadata.
5. Legacy v2 report migration is fail-closed. It records source and target
   evidence policy identities and uses only
   `evidence_policy_migration_required` for migrated seed and unresolved-branch
   blockers. Legacy local state is quarantined rather than assigned to an
   active seed.
6. A capsule with no explicit downstream path scopes outgoing edges to the
   active owner seed's reverse-reachable graph closure. It preserves valid
   provenance routes to that seed while excluding sibling-seed outgoing edges.

## Persisted Identities

- Checkpoint: `recursive-attribution-checkpoint/v6`
- Action state: `recursive-analysis-actions/v4`
- Report: `recursive-attribution-report/v7`
- Local state owner occurrence:
  `local_state_occurrence:v1:<sha256>`
- Global candidate and root-confirmation persistence identities include
  `local-state-owner/v1`.

Current report/action/checkpoint round trips and evaluator compatibility checks
were updated together. `LocalStateOwner.from_dict` requires the exact four-key
schema and rejects missing, extra, non-string, empty, and invalid occurrence
identity values.

## TDD Record

Reviewer reproductions were added before their production fixes.

- The initial Fix19 finding suite reproduced atomic sanitizer, artifact
  provenance, shared-node owner, episode projection, and legacy migration
  failures.
- Before owner/version/policy implementation, the 9-test focused suite had
  6 GREEN and 3 RED, covering missing owner persistence, stale schema versions,
  and missing explicit legacy policy migration.
- The four closeout tests were then run against the prior implementation.
  The 12-test focused run produced four behavioral REDs: generic v2 migration
  reasons remained; non-string owner fields were coerced; a semantic-slice
  fallback was reported as available; and an unscoped capsule exposed both
  sibling outgoing edges. The owner type test expanded to four failing
  subtests, so unittest reported seven failure records.
- The first full-suite run after making no-path outgoing empty found one
  additional regression: a valid owner-route provenance edge was removed from
  `test_provenance_envelope_limit_keeps_all_routes_for_selected_resolved_refs`.
  A refined RED established the exact rule: preserve owner-reachable routes,
  exclude sibling-owner routes. The implementation and restore validation now
  share that rule.

## Verification

- Fix19 focused:
  `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix19.py -v`
  - 12 tests, PASS
- Directly affected capsule/analyzer suites:
  `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix19.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_recursive_analyzer.py`
  - 140 tests, PASS
- Full attribution suite:
  `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
  - 768 tests, PASS
- Compile:
  `PYTHONPYCACHEPREFIX=/tmp/fix19-compileall python3 -m compileall -q tools/trace_attribution`
  - PASS
- Worktree whitespace check:
  `git diff --check`
  - PASS
- Exact committed baseline check:
  `git diff --check 3e5d8b878..HEAD`
  - PASS

## Self-Review

- Sanitization is graph-aware and atomic at typed mapping boundaries; no
  duplicate suffix-based final sanitizer remains.
- Artifact availability and Judge visibility use verified reader state, not
  self-declared hashes.
- Owner persistence is exact and conservative across live creation, partial
  checkpoint restore, completed report restore, evaluator validation, and
  aggregate rebuilding.
- Capsule owner scoping preserves same-owner provenance routes and excludes
  sibling-owner routes.
- Retrieval rank/score remains navigation-only.
- Existing unrelated untracked files were not modified or staged.

## Concerns

None.
