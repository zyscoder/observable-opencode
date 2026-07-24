# Root Confirmation Task 2 Fix Wave 30 Brief

Base commit: `46d5021ac`

## Scope

Close both Important findings from the fifth cumulative Task 2 review. Keep the
analysis offline, passive, read-only, and unable to feed results back to the
Agent. Do not implement Task 3 or add dependencies.

Use strict TDD. Record focused RED and GREEN for each finding, then run affected
and full attribution suites, `compileall`, and
`git diff --check 46d5021ac..HEAD`.

## Required Fixes

### 1. Detect marker-stripped global terminal records before filtering

Deleting explicit global marker fields must not allow a formerly global pass or
failure episode to masquerade as ordinary journal/unresolved state.

Create one ledger-aware residual-signature classifier used before stale
quarantine, terminal-set derivation, checkpoint/report restore, evaluator
validation, and any filtering. It must recognize:

- explicit global marker fields and `kind="global_candidate_pass"`;
- partial subsets of the completed/failed pass exact schemas, including the
  seed/ref/fingerprint/owner/status/compression/action shape;
- partial subsets of the failed episode schema, including node/defect/owner/
  reason/details/depth;
- a `LocalStateOwner` that matches or partially claims the canonical
  seed-projection/global-pass owner for a known seed, even after the explicit
  pass/projection fields are removed.

Requirements:

- if a record has a residual global signature but is not one exact valid
  completed pass, failed pass, or failed episode, reject it;
- reconstruct canonical seed binding, pass identity, and complete owner from
  authoritative seed/ref/fingerprint/defect state where available;
- never silently delete a residual record during stale-seed quarantine;
- do not misclassify ordinary investigation or unresolved records as global;
- use the same classifier on live state, checkpoint, report, evaluator, and
  stale-quarantine paths.

Add tamper tests that remove all explicit markers from failed and completed
passes and episodes, substitute an owner, and then exercise live terminal-set,
checkpoint, report/evaluator, and stale quarantine. Add controls proving
ordinary unresolved and investigation records remain valid.

If classification requires a seed/defect authority map, make that input
explicit rather than guessing from untrusted record fields.

### 2. Persist a round-trippable seed-local terminal artifact gap

If artifact evidence becomes invalid between enqueue and confirmation
preflight, the analyzer must terminate only that seed without revalidating the
same invalid envelope as supporting evidence.

Define one canonical terminal evidence disposition with states such as
`validated` and `rejected_snapshot`:

- preserve the original queue/action artifact envelope bytes exactly for audit;
- for a rejected snapshot, persist the rejection reason and current graph
  comparison facts separately;
- an `unknown` confirmation created by preflight failure must cite no rejected
  artifact as supporting/decisive evidence;
- terminal action/report/checkpoint/evaluator validation must require the
  rejected snapshot and disposition to match exactly, but must not demand that
  the rejected snapshot is currently graph-eligible;
- a confirmed/rejected substantive verdict may use only `validated` envelopes;
- validate and construct the terminal disposition before writing the action;
- write exactly one canonical failed/unknown action and seed evidence gap;
- restore must round-trip it without rebuilding a normal confirmation request
  from invalid evidence or invoking the provider again;
- other seeds continue normally.

Use the existing persisted artifact envelope as the audit snapshot. Do not
invent another artifact ownership truth or treat invalid content as evidence.
Version action/report/checkpoint/root-confirmation persistence explicitly.

Add tests for owner substitution, stale owner, hash/path/content drift, missing
artifact, checkpoint restore, report/evaluator round-trip, no provider replay,
and independent second-seed completion. Retain a valid multi-owner explicit
owner control.

## Verification

At minimum:

1. New Fix30 focused tests.
2. Fix17-Fix30 plus global terminal, confirmation, artifact, seed, checkpoint,
   and recursive core regressions.
3. Report/evaluator/replay/acceptance affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check 46d5021ac..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix30-report.md` with root-cause
  validation, RED/GREEN evidence, schema/policy decisions, exact test
  commands/counts, changed files, and residual risks.
- One commit containing only Fix30 files, this brief, and report. Do not touch
  or stage unrelated untracked files.
