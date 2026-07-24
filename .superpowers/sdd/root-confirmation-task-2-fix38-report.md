# Root Confirmation Task 2 Fix38 Report

## Result

Fix38 makes canonical counterfactual semantics an unconditional persisted
`RootConfirmation` invariant. A response cannot bypass the contract by
coherently recomputing its `response_identity`, including when terminal
artifact evidence is restored as a `rejected_snapshot`.

The rejected-snapshot exception remains narrow: restore may avoid re-reading
artifact content that already failed terminal preflight, but it still validates
the persisted response semantics, exact synthetic unknown disposition,
request projection binding, and outer response-identity bijection.

## TDD Evidence

`test_root_confirmation_fix38.py` was added before production code changed.
The first focused run produced the expected RED:

```text
Ran 8 tests in 0.376s
FAILED (failures=3)
```

The three failures proved that a coherently re-signed
`counterfactual="x"` was accepted by:

- `RootConfirmation.from_dict()` for a normal confirmed response;
- `RecursiveAttributionReport.from_dict()` for the same response;
- `RootConfirmation.from_dict()` for a real content-drift
  `rejected_snapshot`.

The valid confirmed and rejected-snapshot controls, exact synthetic unknown
checks, request binding, and outer response-identity checks already passed.

After the minimal production change:

```text
Ran 8 tests in 0.392s
OK
```

## Implementation

`RootConfirmation.from_dict()` now calls the existing shared substantive
validator with `require_canonical_counterfactual=True`.

This reuses the already-versioned `counterfactual/v1` contract and rejects:

- missing or non-JSON counterfactuals;
- incomplete or extra structured fields;
- non-canonical JSON encoding;
- wrong intervention candidates;
- status/prediction/effect contradictions;
- coordinated response re-signing around any of those mutations.

No new validation framework was introduced. The existing
`_validated_terminal_evidence_disposition()` remains authoritative for the
rejected-snapshot synthetic outcome:

- status `unknown`;
- exact disposition-derived failure reason;
- empty evidence;
- operation `confirmation_failed`.

The ordinary `RootConfirmation` invariant supplies canonical unknown
counterfactual, `counterfactual_status=unknown`, and `factor_role=unknown`.
The existing queue projection validator continues to bind candidate, owner,
hypothesis, defect, path, seed, request identity, and response identity.
Only live artifact re-reading is skipped for a rejected snapshot.

Historical fixtures that persisted free-text counterfactuals were changed to
construct canonical facts. Candidate-forging tests now also update the
candidate-bound counterfactual and its duplicate published-root projection so
they continue to exercise their intended ownership and publication checks.
The Fix37 malformed-report assertion was updated to expect rejection at
`from_dict()` rather than at later graph validation.

## Verification

```text
Focused Fix38:
Ran 8 tests in 0.392s
OK

Fix17-Fix38:
Ran 270 tests in 10.462s
OK

Affected persistence/judge/state/analyzer/evaluator/seed/acceptance suites:
Ran 868 tests in 22.751s
OK

Full discovery:
Ran 1003 tests in 23.258s
OK
```

Static verification:

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-fix38-pycache \
  python3 -m compileall -q tools/trace_attribution
exit 0

git diff --check
exit 0
```

## Scope

The change is limited to the persisted root-confirmation counterfactual
boundary, Fix38 regression coverage, and historical contract fixtures. It
adds no dependency, Task 3 behavior, Agent feedback, graph mutation, or
retrieval-score causal use. Existing unrelated untracked files were not
modified.
