# Root Confirmation Task 2 Fix Wave 35 Brief

Base commit: `b59431810`

## Scope

Close both Important Provider canonicalization findings from the Fix34 review.
Preserve Fix34 replay ordering and Fix33 authority binding. Keep attribution
offline, passive, read-only, and unable to feed results back to the Agent. Do
not implement Task 3, add dependencies, or touch unrelated untracked files.

Use strict TDD. Record focused RED and GREEN, then run affected and full
attribution suites, `compileall`, and `git diff --check b59431810..HEAD`.

## Required Fixes

### 1. Bind the complete Provider accounting snapshot

Completed replay preflight must compare all canonical Provider accounting
fields against the projected post-replay state:

- `judge_requests`;
- `judge_request_uncertainty_count`;
- `logical_judge_calls`;
- `logical_confirmation_calls`;
- `investigation_rounds`;
- `artifact_bytes`.

Use the existing projected state and require complete accounting equality.
Remove any partial two-field comparison. A type-valid, nonnegative,
identity-resigned drift in any accounting field must fail before live
accounting, Provider/circuit state, queue, journal, or terminal state changes.

### 2. Canonicalize Provider cache statistics

Define and validate the exact cache-stat forms produced by this codebase:

- minimal disabled form: exactly `{"enabled": false}`;
- full `JudgmentCache.stats()` form: exact keys
  `enabled`, `path`, `loaded_entries`, `hits`, `misses`, `writes`,
  `invalid_entries`, `corrupt_entries`, `write_error_count`, `write_errors`.

For the full form:

- `enabled` is bool and `path` is string;
- every count is a nonnegative exact int;
- `write_errors` is a list of strings;
- `write_error_count == len(write_errors)`;
- enabled cache requires a nonempty path; disabled full form requires an empty
  path.

Reject missing keys, extra keys, wrong types, inconsistent counts, and
noncanonical enabled/path combinations even if the Provider identity is
recomputed. Return one canonical deep copy.

Apply this validator to all Provider state validation paths, not only completed
replay, so checkpoint restore and replay share the exact schema.

## Tests

Create `tools/trace_attribution/tests/test_root_confirmation_fix35.py` with:

1. identity-resigned drift for each of the four nonphysical accounting fields
   (`logical_judge_calls`, `logical_confirmation_calls`,
   `investigation_rounds`, `artifact_bytes`) rejected atomically;
2. existing physical and uncertainty accounting mismatch controls;
3. minimal disabled cache stats accepted;
4. valid full enabled and full disabled cache stats accepted;
5. malformed full cache stats rejected for missing/extra keys, wrong bool/
   string/int/list types, inconsistent write-error count, and invalid
   enabled/path combinations;
6. malformed cache stats rejected by checkpoint restore and completed replay
   before mutation;
7. valid nonzero completed replay and checkpoint restore controls remain green.

Use real Provider identity recomputation in tamper tests.

## Verification

At minimum:

1. New Fix35 focused tests.
2. Fix17-Fix35 plus Provider/accounting/cache/checkpoint/confirmation/
   report/evaluator/replay/acceptance regressions.
3. Directly affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check b59431810..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix35-report.md` with root-cause
  validation, RED/GREEN evidence, exact commands/counts, changed files,
  version decisions, self-review, and residual risks.
- One commit containing only Fix35 files, this brief, and report.
