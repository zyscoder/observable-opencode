# Root Confirmation Task 2 Fix Wave 34 Brief

Base commit: `676391224`

## Scope

Close the completed-replay atomicity finding and its coverage gap from the
Fix33 review. Preserve all Fix33 authority-binding behavior. Keep attribution
offline, passive, read-only, and unable to feed results back to the Agent. Do
not implement Task 3, add dependencies, or touch unrelated untracked files.

Use strict TDD. Record focused RED and GREEN, then run affected and full
attribution suites, `compileall`, and `git diff --check 676391224..HEAD`.

## Required Fix

### Prevalidate complete replay payload before any state mutation

For a `confirmation_completed` replay, validate and canonicalize all of the
following before modifying any analysis or Provider state:

- action record and action projection;
- request/projection/outer-state/ledger/frontier binding;
- terminal confirmation and evidence disposition;
- physical request accounting fields;
- persisted Provider payload, including cache identity, accounting shape, and
  circuit fields.

Cache the canonical Provider payload from this preflight. Only after every
replay validation succeeds may the analyzer:

1. apply physical request accounting and uncertainty counters;
2. apply canonical Provider state and circuit fields;
3. record the terminal confirmation.

Do not call a validator capable of raising after accounting has changed. A
failed replay must leave `judge_requests`, logical counters, uncertainty
counter, `state.provider_state`, and transport circuit fields exactly as they
were before the replay attempt.

Successful replay must preserve the existing nonzero physical delta accounting
and persisted circuit restoration.

### Strengthen controls

Add an invalid Provider payload test with a nonzero physical delta and assert
all analysis counters and circuit/provider state are unchanged after failure.
Add a valid nonzero-delta completed replay control that asserts exact counters
and circuit state. Convert the Fix33 pending control into a real
checkpoint/report JSON serialization round trip.

## Tests

Create `tools/trace_attribution/tests/test_root_confirmation_fix34.py` or
extend the Fix33 focused module with clearly isolated Fix34 cases. At minimum:

1. invalid provider threshold/cache/accounting payload with nonzero delta fails
   before any state mutation;
2. invalid terminal binding still fails before any Provider/accounting
   mutation;
3. valid completed replay with nonzero delta restores exact accounting,
   provider state, and circuit fields once;
4. valid pending confirmation survives actual checkpoint/report JSON
   serialization and restore;
5. rejected replay remains Provider-free and all Fix33 authority controls
   remain green.

## Verification

At minimum:

1. New Fix34 focused tests.
2. Fix17-Fix34 plus confirmation, provider-accounting, checkpoint,
   report/evaluator/replay/acceptance regressions.
3. Directly affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check 676391224..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix34-report.md` with root-cause
  validation, RED/GREEN evidence, exact commands/counts, changed files,
  version decisions, self-review, and residual risks.
- One commit containing only Fix34 files, this brief, and report.
