# Root Confirmation Task 2 Fix Wave 21 Report

Base commit: `7a096f455`

## Status

DONE

Fix21 remains limited to Root Confirmation Task 2. The implementation is
offline, passive, read-only, does not feed results back to the Agent, does not
implement Task 3 behavior, and adds no dependency.

## Findings Closed

1. Judge-visible mappings now classify themselves as typed when they contain a
   supported shape identity key such as `candidate_ref`, `citation_ref`,
   `node_ref`, `seed_ref`, or another supported scalar alias. When exactly one
   shape role is present, that alias must resolve to the same active identity as
   the mapping's generic identity envelope. Invalid or conflicting identities
   remove the complete fact and sibling semantic content.
2. Plain mappings, nested lists, plural `citations`, `facts`, `references`, and
   `artifacts` containers, plus JSON encoded inside strings, use the same atomic
   classification in global, recursive-step, and confirmation final payloads.
   Multiple role endpoints such as `source_ref` and `target_ref`, and ordinary
   business fields, remain distinct and visible when valid.
3. Recursive Judge context now admits downstream judgments only when their
   `LocalStateOwner` resolves to an exact ledger hypothesis and frontier visit,
   carries the current seed binding, matches the judged path node, and has the
   expected occurrence identity. Two active seeds sharing a node cannot consume
   each other's contradictory judgment, while valid same-seed multi-visit
   downstream judgments remain available.
4. Local investigation evidence must bind to the current visit. Live evidence
   with an explicit owner is checked exactly; persisted evidence must also
   match a journal result for the same frontier action. Foreign, unowned, and
   action-orphaned local evidence is excluded or rejected.
5. Checkpoint restore validates step judgments, predecessor relations, visits,
   confirmation queue/journal entries, global-pass events, seed-ledger facts,
   decisive evidence, expansion history, visit-indexed state, and local
   investigation evidence against ledger, frontier, seed routing, and exact
   occurrence identities. Partial restore rejects valid-looking owners moved
   across active seeds.
6. Pending and completed report restore reconstruct the authoritative ledger
   and frontier from report snapshots. Derived global judgments, compression
   facts, expansion reasons, and pass counts must match completed global-pass
   actions bidirectionally; missing, extra, forged, orphaned, or cross-seed
   owners are rejected.

## Persisted Identities

Checkpoint, action-state, report, policy, and Judge prompt schema identities
remain unchanged. Fix21 does not add or remove persisted fields. It strengthens
validation of existing owner, ledger, frontier, journal, and report facts. The
visit-key migration fixture was updated to carry the already-required exact
confirmation occurrence and complete local action facts.

## TDD Record

- Mapping RED: the conflicting mapping marker survived all three final JSON
  stages, while role-distinct and ordinary-business controls were already
  GREEN.
- Mapping GREEN: Fix21 and Fix20 focused identity tests passed together.
- Owner RED: the second active seed consumed the first seed's judgment at a
  shared node; foreign and unowned local evidence reached the prompt; partial
  restore accepted owner transplants in judgments, relations, visits, and
  confirmation state; global pass and pending report owners were not
  cross-checked.
- Owner GREEN: all live, local, partial, pending, and completed restore
  scenarios passed, including contradictory shared-node judgments and valid
  same-seed downstream visits.
- Self-review RED/GREEN: persisted local evidence with an exact owner but no
  action entry was rejected only after adding journal-result validation.
  Deleting a derived global judgment was rejected only after adding
  bidirectional global-pass coverage checks.

## Verification

- Focused Fix21 suite:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix21-focused-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix21.py -v`
  - 7 tests, PASS
- Sanitizer/recursive/checkpoint/Judge affected suites:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix21-affected-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix17.py tools/trace_attribution/tests/test_root_confirmation_fix18.py tools/trace_attribution/tests/test_root_confirmation_fix19.py tools/trace_attribution/tests/test_root_confirmation_fix20.py tools/trace_attribution/tests/test_root_confirmation_fix21.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_causal_checkpoint.py tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_confirmation_path.py tools/trace_attribution/tests/test_investigation.py`
  - 409 tests, PASS
- Full attribution suite:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix21-full-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
  - 786 tests, PASS
- Compile:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix21-compileall-final python3 -m compileall -q tools/trace_attribution`
  - PASS
- Worktree whitespace check:
  `git diff --check`
  - PASS
- Exact committed baseline check:
  `git diff --check 7a096f455..HEAD`
  - PASS

## Self-Review

- Mapping-owned shape inference compares a single semantic alias with generic
  envelope identities without conflating multi-role endpoints.
- Live context filtering is seed-local but permits verified downstream owners
  from different hypotheses and visits on the same seed path.
- Restore validation is stricter than live quarantine: persisted facts must
  resolve bidirectionally to ledger/frontier/action state and exact occurrence
  identities.
- Pending and completed report fast paths run the same owner validation before
  returning reusable conclusions.
- No unrelated refactor, dependency, Agent feedback, or Task 3 behavior was
  introduced. Existing unrelated untracked files were not modified or staged.

## Concerns

None.
