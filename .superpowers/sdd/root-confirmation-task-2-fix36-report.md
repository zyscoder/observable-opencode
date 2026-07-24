# Root Confirmation Task 2 Fix36 Report

## Result

Fix36 closes the cumulative-review gap that allowed an offline or restored
`confirmed` root to carry no substantive grounded evidence. It preserves
`confirmation_identity` as the occurrence identity and adds a distinct,
persisted `response_identity` over the occurrence plus every substantive
response field.

## TDD Evidence

The production implementation was unchanged when
`test_root_confirmation_fix36.py` first ran.

After correcting two adversarial-test fixtures that were being rejected by
the existing dataclass constructor before reaching the binder, the canonical
RED run was:

```text
Ran 9 tests in 0.144s
FAILED (failures=10, errors=6)
```

The failures demonstrated the intended missing behavior:

- `RootConfirmation` had no `response_identity`;
- serialized confirmations omitted the response identity;
- offline binding accepted empty evidence, empty reason, zero confidence,
  and an empty counterfactual;
- completed replay accepted response tampering;
- the seed builder published an evidence-free confirmed root;
- report, checkpoint, output, action-state, root-confirmation, and action
  projection contracts still used their prior versions.

The final focused GREEN run was:

```text
Ran 9 tests in 0.209s
OK
```

## Implementation

### Response and occurrence identity

- Kept `confirmation_identity` unchanged as the occurrence/binding identity.
- Added `confirmation_response_identity_for()` and
  `RootConfirmation.response_identity`.
- The response identity hashes the occurrence identity together with status,
  excerpt, reason, counterfactual, confidence, evidence refs,
  counterfactual status, factor role, competitor comparisons, and factor
  mechanism.
- `RootConfirmation.to_dict()` persists both identities.
- `RootConfirmation.from_dict()` rejects a missing or mismatched response
  identity before accepting the payload.
- Confirmation action projections now bind `response_identity` to the
  response hash while retaining the existing request and semantic-key
  meanings.

### Shared substantive invariant

`validate_root_confirmation_substantive_invariants()` is shared by:

- live Provider JSON validation;
- offline result binding;
- persisted confirmation restoration;
- terminal evidence validation;
- seed-result publication.

It requires a nonempty reason and counterfactual for every status, positive
confidence for non-unknown results, consistent status/counterfactual/factor
semantics, and nonempty evidence plus excerpt for a confirmed necessary
cause. Request-aware live/offline paths continue to verify exact fact-tree
grounding, competitor comparisons, and factor mechanisms.

Internal unknown terminal outcomes now persist an explicit unknown
counterfactual rather than an empty field. `FrozenMapping` competitor
comparisons are also restored as generic mappings so canonical response
identity survives immutable report projections.

### Persistence versions

- report: `recursive-attribution-report/v15`
- checkpoint: `recursive-attribution-checkpoint/v14`
- output: `recursive-attribution-output/v3`
- action state: `recursive-analysis-actions/v12`
- root confirmation: `recursive-root-confirmation/v16`
- confirmation action projection: `action-projection/v6`
- response identity: `response-identity/v1`

The evaluator now consumes report v15. Older payloads are not silently
accepted under the new contract.

## Serialization And Atomicity Coverage

The Fix36 tests exercise:

- live and offline acceptance/rejection parity;
- every substantive response field changing the response identity while the
  occurrence identity remains stable;
- missing and forged response identities;
- action projection rejection when the occurrence identity is forged into
  the response slot;
- JSON-round-tripped completed replay rejection for response tampering and
  coordinated evidence-free responses, with state and Provider counters
  unchanged;
- real checkpoint restore rejection for an evidence-free confirmed response;
- valid modern report JSON restore, graph validation, and evaluator control;
- report/evaluator rejection for response tampering and coordinated empty
  evidence;
- seed publication rejection before an evidence-free confirmed root mutates
  the result.

## Verification

```text
Focused Fix36:
Ran 9 tests in 0.209s
OK

Fix17-Fix36:
Ran 254 tests in 8.469s
OK

Affected suites:
Ran 291 tests in 9.721s
OK

Full discovery:
Ran 987 tests in 19.089s
OK
```

Static verification:

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-fix36-pycache \
  python3 -m compileall -q tools/trace_attribution
exit 0

git diff --check
exit 0
```

The brief's literal `compileall` command initially encountered only sandboxed
macOS bytecode-cache `PermissionError` failures under
`~/Library/Caches/com.apple.python`. Re-running with a temporary bytecode
cache prefix completed successfully without changing source behavior.

## Scope

The change is limited to attribution state, Judge binding, recursive
confirmation persistence/replay/publication, evaluator version consumption,
Fix36 regression coverage, and updates to existing contract/tamper fixtures.
It adds no dependency, Task 3 behavior, graph mutation, Agent feedback, or
online behavior. Existing unrelated untracked files were not modified.
