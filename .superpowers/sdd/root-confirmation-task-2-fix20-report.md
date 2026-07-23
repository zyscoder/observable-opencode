# Root Confirmation Task 2 Fix Wave 20 Report

Base commit: `e2bb6db8b`

## Status

DONE

Fix20 remains limited to Root Confirmation Task 2. The implementation is
offline, passive, read-only, does not feed results back to the Agent, does not
implement Task 3 behavior, and adds no dependency.

## Findings Closed

1. Judge-visible typed mappings now classify equivalent identities by mapping
   shape. `ref`, `raw_ref`, `resolved_ref`, `canonical_ref`, `citation_ref`,
   `node_ref`, artifact identity, and shape-specific scalar aliases must all
   resolve to one active canonical entity. A stale, malformed, unresolved, or
   conflicting identity removes the complete typed fact and its sibling
   content. Role-distinct edge endpoints such as `from_ref` and `to_ref` remain
   independently validated and are not compared as aliases.
2. Artifact `owner_reference` values require a complete resolved envelope with
   eligible active identities. Owner aliases must agree with one another and
   the owner must match the containing hydration manifest `node_ref`. Failure
   removes the parent artifact fact atomically, including owner-bearing
   artifact forms without inline content.
3. Judge-visible verified artifacts may carry exact UTF-8 byte subranges,
   including a valid empty range. The sanitizer verifies exact non-boolean
   integer bounds against the complete verified artifact, both UTF-8
   boundaries, exact range bytes, canonical SHA-256, byte count, artifact
   identity, availability, and owner binding.
4. Confirmation fact grounding accepts both the existing full-content/internal
   range representation and the Fix20 global-offset/exact-slice representation.
   Hash, byte count, range shape, UTF-8, owner, and canonical artifact controls
   remain enforced.

## Persisted Identities

Checkpoint, action-state, report, policy, and Judge prompt schema identities
remain unchanged. Fix20 changes graph-bound final payload eligibility and
confirmation validation only; it does not change a persisted serialized shape
or the semantic meaning of existing persisted fields.

## TDD Record

- The clean identity RED had 5 test methods. Valid alias/single-identity and
  role-distinct edge controls were already GREEN, while conflicting
  `ref`/`citation_ref`/`node_ref`, conflicting owner aliases, and mismatched
  hydration owners produced 15 failing stage subtests across global,
  recursive-step, and confirmation final JSON.
- The identity implementation made all 5 focused tests GREEN and retained all
  12 Fix19 tests.
- The artifact subrange RED kept all 14 rejection controls GREEN while valid
  nonzero UTF-8 and empty subranges failed in all three final stages. A separate
  confirmation-grounding RED failed with the legacy full-range assumption.
- The subrange implementation made all 4 artifact tests GREEN. The affected
  Judge run exposed one legacy full-content/internal-range fixture, which was
  retained through an explicit dual structural representation rather than
  weakening graph-backed verification.
- Self-review added an owner-bearing artifact without content. It failed in all
  three final stages before the owner check was extended, then passed together
  with the explicit shape-specific `candidate_ref` control.

## Verification

- Focused reviewer suite:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix20-focused-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix20.py -v`
  - 11 tests, PASS
- Sanitizer/artifact/Judge affected suites:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix20-affected-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix17.py tools/trace_attribution/tests/test_root_confirmation_fix18.py tools/trace_attribution/tests/test_root_confirmation_fix19.py tools/trace_attribution/tests/test_root_confirmation_fix20.py tools/trace_attribution/tests/test_artifact_hydration.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_confirmation_path.py tools/trace_attribution/tests/test_recursive_analyzer.py`
  - 335 tests, PASS
- Full attribution suite:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix20-full-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
  - 779 tests, PASS
- Compile:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix20-compileall-final python3 -m compileall -q tools/trace_attribution`
  - PASS
- Worktree whitespace check:
  `git diff --check`
  - PASS
- Exact committed baseline check:
  `git diff --check e2bb6db8b..HEAD`
  - PASS

## Self-Review

- Identity comparison is mapping-shape aware and does not conflate role
  endpoints.
- Owner validation occurs before artifact sibling content can be projected and
  propagates the authoritative hydration owner through nested lists and encoded
  JSON.
- Artifact bytes come only from the verified reader. Empty ranges are accepted
  only at real UTF-8 boundaries; boolean offsets, reversed/out-of-bounds ranges,
  valid-boundary wrong offsets, misaligned offsets, byte/hash/content
  mismatches, unresolved or unavailable declarations, and wrong artifact
  identities remain audit-only.
- No persisted contract identity changed because no persisted shape or meaning
  changed.
- Existing unrelated untracked files were not modified or staged.

## Concerns

None.
