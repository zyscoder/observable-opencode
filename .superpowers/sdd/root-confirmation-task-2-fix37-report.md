# Root Confirmation Task 2 Fix37 Report

## Result

Fix37 closes all four Fix36 review findings across live validation, offline
binding, checkpoint/report restore, evaluator validation, journal replay, and
published-root projection. External confirmation semantics are never inferred
during validation. Internal synthetic unknown outcomes construct their
canonical facts explicitly before persistence.

## TDD Evidence

`test_root_confirmation_fix37.py` was created and run before production code
changed. Its real serialized checkpoint, report, and evaluator mutations
produced this canonical RED:

```text
Ran 7 tests
FAILED (failures=16, errors=0)
```

The failures showed that:

- offline binding accepted nonempty but incomplete counterfactual text;
- restored confirmations were not rebound to their current factual request;
- terminal queue response identity was absent and journal/queue/action values
  were not bijective;
- published roots could drift from the response fields in their embedded
  confirmation;
- persistence versions still described the Fix36 contracts.

The final focused suite includes an additional atomic completed-replay check:

```text
Ran 8 tests in 0.386s
OK
```

## Implementation

### Canonical counterfactual

`confirmation_counterfactual_for()` now emits stable JSON under
`root-confirmation-counterfactual/v1` with exactly:

- `intervention_ref`;
- `intervention_kind`;
- `predicted_defect_status`;
- `causal_effect`;
- the counterfactual schema.

The status and `counterfactual_status` determine the only valid prediction and
effect pair. Live Provider JSON is normalized through this helper. Offline
binding requires the same exact representation and rejects incomplete text,
wrong candidates, wrong predictions, wrong effects, extra fields, and
non-canonical encoding. Missing external semantics are not repaired.

### Request-aware restore

For each non-rejected terminal confirmation queue entry, restore now:

1. validates the persisted factual request projection against the current
   graph, ledger, frontier, defect, owner, and artifact state;
2. rebuilds the current `RootConfirmationRequest`;
3. re-runs `bind_root_confirmation()` against that request;
4. requires exact equality with the persisted confirmation before mutation.

Checkpoint restore and `validate_recursive_report_against_graph()` share this
queue-bound validation path. Completed replay performs the same bind before
Provider-state prevalidation or terminal state mutation. A coherently
re-signed but ungrounded excerpt therefore fails atomically.

### Response identity bijection

Terminal queue records now require `response_identity`. The queue validator
binds it to the parsed response, while local-state validation requires the
same value in the queue, confirmation journal, action projection, and embedded
confirmation. Substituting occurrence `confirmation_identity` in any outer
location is rejected by checkpoint restore, report validation, and evaluator
validation.

### Published root projection

A modern `ConfirmedRoot` must now copy these embedded confirmation fields
exactly:

- `reason`;
- `counterfactual`;
- `confidence`;
- `evidence_refs`;
- `excerpt`.

Modern `root_causes` must also exactly equal the deduplicated legacy projection
of `confirmed_roots` followed by `co_roots`, apart from generated semantic
anchor identifiers. Coordinated root/legacy tampering cannot bypass the
embedded response.

### Persistence versions

- report: `recursive-attribution-report/v16`
- checkpoint: `recursive-attribution-checkpoint/v15`
- output: `recursive-attribution-output/v4`
- action state: `recursive-analysis-actions/v13`
- root confirmation: `recursive-root-confirmation/v17`
- confirmation action projection: `action-projection/v7`
- counterfactual: `counterfactual/v1`
- terminal queue response identity: `queue-response-identity/v1`
- published root projection: `published-root-projection/v1`
- root confirmation prompt: `recursive-root-confirmation-v10`

Affected historical fixtures were upgraded to construct canonical
counterfactuals and exact response projections. Older payloads are not
silently accepted under the new contracts.

## Verification

```text
Focused Fix37:
Ran 8 tests in 0.386s
OK

Fix17-Fix37:
Ran 262 tests in 9.753s
OK

Affected checkpoint/judge/state/analyzer/evaluator/seed/acceptance suites:
Ran 369 tests in 11.042s
OK

Full discovery:
Ran 995 tests in 21.838s
OK
```

Static verification:

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-fix37-pycache \
  python3 -m compileall -q tools/trace_attribution
exit 0

git diff --check
exit 0
```

## Scope

The change is limited to root-confirmation semantics, their persistence and
restore boundaries, evaluator contract consumption, Fix37 regression
coverage, and historical contract fixtures. It adds no dependency, Task 3
behavior, graph mutation, Agent feedback, or retrieval-score causal use.
Unrelated untracked files were not modified.
