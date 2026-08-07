# Task 3A Report

## Scope

Changed only the four authorized observability source/test files, plus this required report. Existing unrelated untracked report fixtures were left untouched. No runtime queue, runtime, run, MCP, prompt, TUI worker, or README files were modified.

## RED / GREEN Evidence

### 1. Independent process and root ordinals; idempotent values/finalization

- RED: The preserved router draft failed `keeps process and root ordinals independent`: process creation advanced root ordinals from expected `0, 1` to `1, 2`.
- GREEN: `SessionTraceRegistry` now maintains `rootOrdinal` and `processOrdinal` independently. Existing `values()` deduplication and successful-finalization `WeakSet` behavior remain intact; failed finalizers remain retryable.
- Verification: router suite passes the process-first ordinal, finish-twice, failed-finish retry, orphan, and failed-reset tests.

### 2. Complete route hints and cyclic-array guard

- RED: Router tests failed for missing raw span IDs, all requested semantic ID classes, `evidence_refs`, `aliases`, arbitrary `{ ref_type, ref_id }`, and self-referential arrays (stack overflow).
- GREEN: Route identity extraction is table-driven and emits raw plus typed identities for edge/check/constraint/design/gate/segment/claim/lifecycle IDs, including compaction/exit/response aliases. `source_refs`, `evidence_refs`, and `aliases` are collected. Arrays and records share one `WeakSet` traversal guard.
- Verification: all route-hint tests pass, including the cyclic array reproduction.

### 3. Compatibility claim identity, multi-root `get()`, and empty `finish()`

- RED: `configure -> set A -> prompt A -> set B -> prompt B` returned the A compatibility object from unscoped `get()` and reused it for B. `finish()` before any trace existed created `empty-finish-case`.
- GREEN: Compatibility traces are explicit registry entries and can be claimed once. A second `setSessionID` resolves an independent root. Once multiple roots exist, `get()` resolves the isolated process trace instead of guessing a root. `finish()` returns without constructing a registry or trace.
- Verification: the integration test observes separate A, B, and process case directories and distinct A/B contents; empty finish leaves no directory.

### 4. Process/root directories and long routed IDs

- RED: A rootless record occupied the base path and advanced the first root to a suffixed path. Long base/session combinations collapsed three roots into one directory because the first 160 characters were identical.
- GREEN: Trace creation has explicit `root`, `process`, and `compatibility` roles. Process and root ordinals are independent, process routing uses a distinct `--process` directory, and the first root retains the base directory. Overlong subsequent routes preserve a sanitized session tail and include a stable SHA-256 fragment before the 160-character boundary.
- Verification: root A remains at the base, process data stays in `--process`, all long IDs are at most 160 characters, long A/B suffixes remain visible, and names are stable across runs.

### 5. Unknown and owner reference routing

- RED: Coverage did not prove behavior after multiple roots; the legacy single-root fallback could mask ownership errors.
- GREEN: After two roots, unknown refs use the isolated process trace. Registered owner refs are checked first and return to their root without process/root overwrite.
- Verification: both the registry-level identity test and raw-event integration test assert unknown/process isolation and owner/root routing.

### 6. Reconfigure/finalization failure behavior

- RED: A throwing reset finalizer escaped `configure`, skipped `beginLifecycle`, and left the next trace on the old case ID. Public `finishAll` also propagated the injected finalization error.
- GREEN: Reconfigure performs best-effort reset and always calls `beginLifecycle` in `finally`. Public finish APIs suppress trace-side finalization failures while registry finalization still retries failed traces on a later call.
- Verification: the reproduction observes no caller exception, a fresh object with `reconfigure-new`, a new routed prompt in that trace, and a successful retry after restoring the finalizer.

### 7. Legacy fixture cleanup

- Removed the artificial `ses_repeat -> ses_temporal` alias and used the real `ses_temporal` root for repeated records.
- Removed the artificial common `ses_fixture` parent for `ses_owner` and `ses_other`; assertions now read and validate each independent trace.
- Removed the artificial `ses_other -> ses_parent` alias in the subagent boundary fixture; the parent and other root traces are read separately without weakening the original parent-consumption assertion.
- Retained only genuine child-parent aliases and the disabled-API no-op fixture.

## Regression Found During GREEN

The first process-isolation implementation routed every zero-root unhinted call to `--process`, causing 98 legacy single-case fixtures to lose their base compatibility trace. This exposed the required compatibility boundary: zero-root unhinted calls retain the base compatibility trace, while unhinted/unknown calls after multiple roots use the isolated process trace. Representative regressions were rerun first, followed by the complete suite.

## Self-Review

- Checked the final source diff for duplicate mutable identity state; removed the redundant CaseTrace compatibility trace pointer.
- Kept the full sanitized base ID until routed hashing so the stable digest is computed before the final 160-character boundary.
- Confirmed failed registry finalizers are not marked finalized, allowing retry; successful traces remain idempotent.
- Confirmed `values()` deduplicates compatibility/root aliases and orphan/process entries by object identity.
- Confirmed all remaining fixture aliases represent real child-parent relationships.
- Confirmed no unrelated tracked or untracked files are staged or modified by this task.

## Final Verification

- `bun --cwd packages/opencode test test/observability/case-trace-session.test.ts test/observability/case-trace.test.ts`: 175 pass, 0 fail, 2160 assertions.
- `bun --cwd packages/opencode typecheck`: pass (`tsgo --noEmit`).
- `git diff --check`: clean.

## Concerns / Handoffs

- Zero-root unhinted calls intentionally retain compatibility semantics for existing single-case callers. Production no-hint entry policy remains Task 3C scope.
- Independent root terminal-state policy remains Task 3B scope.
- Public finalization is deliberately best-effort and silent to callers; registry-level tests retain retry visibility for internal finalization failures.
