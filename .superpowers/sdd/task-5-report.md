# Task 5 Report: Necessary-Cause Escalation Without Direct Promotion

Status: **DONE**

No git commit was created. The pre-existing dirty worktree was preserved.

## Scope Delivered

- A completed non-root `FactorRoleJudgment` with
  `necessity_status="necessary"` now enqueues exactly one root-scope
  confirmation. Recording the FactorRole itself never edits primary roots or
  co-roots.
- The queue origin has the exact contract:

  ```python
  {
      "kind": "factor_role_escalation",
      "factor_action_identity": "...",
      "factor_judgment_identity": "...",
      "factor_request_identity": "...",
  }
  ```

- The root request is rebuilt by the existing authoritative
  graph/ledger/frontier request builder. No FactorRole provider reason,
  evidence, role claim, or other response field is copied into root request
  facts.
- Replay and repeated application of the same completed FactorRole action
  reuse the exact escalation binding and cannot enqueue another root action.
- Escalations are excluded from the ordinary per-seed Global root Top-3 count,
  while queue closure permits only the exact non-root source plus one
  escalation lifecycle for a candidate.
- A confirmed escalation publishes only when the existing reciprocal
  independent co-root checks pass, then uses the existing deterministic
  primary/co-root ranking. A confirmed response without mutual support is not
  published and does not remove an existing root.
- Rejected and unknown escalation results produce exact
  `factor_role_escalation_gaps`. Unknown is non-blocking for the owning seed;
  both statuses leave existing roots unchanged.
- Report and restore closure reject non-necessary or non-completed sources,
  cross-seed/hypothesis/defect/perspective bindings, foreign request or
  judgment identities, duplicate consumption, and root publication not
  authorized by a confirmed escalation.
- Escalation origins are supported across queue, journal, action projection,
  checkpoint restore, stale quarantine, report reconstruction, graph
  validation, and response-identity validation without weakening ordinary
  confirmation validation.
- No Agent, Trace, step-Judge, or Global-Judge input was changed. The only new
  provider operation is the required root confirmation for a completed
  necessary FactorRole.

## TDD Record

Baseline before Task 5:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection -v

Ran 87 tests in 6.596s
OK
```

Initial Task 5 RED:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection.FactorRoleEscalationTest -v

Ran 7 tests
FAILED (failures=5, errors=5)
```

The failures were the intended missing behaviors: no escalation queue,
origin, terminal escalation action, independent gap, or report binding
closure existed.

Final Task 5 GREEN:

```text
Ran 7 tests in 1.136s
OK
```

The seven tests cover exact enqueue and origin binding, authoritative request
facts, idempotent replay, reciprocal confirmed ranking, rejected/unknown gap
closure, ordinary Top-3 exclusion, and report rejection of non-necessary,
non-completed, foreign, or multiply consumed sources.

## Regression Verification

Task 5 brief four-module command:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection \
  tools.trace_attribution.tests.test_causal_role_closure \
  tools.trace_attribution.tests.test_root_confirmation_fix39 \
  tools.trace_attribution.tests.test_seed_attribution -v

Ran 159 tests in 12.787s
OK
```

Complete attribution discovery:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest discover \
  -s tools/trace_attribution/tests -v

Ran 1200 tests in 44.622s
OK
```

The total is the reviewed Task 4 baseline of 1193 plus seven Task 5 tests.

Static compilation:

```text
PYTHONPYCACHEPREFIX=/tmp/task5-pycache python3 -m py_compile \
  tools/trace_attribution/trace_attribution/recursive_analyzer.py \
  tools/trace_attribution/trace_attribution/causal_state.py \
  tools/trace_attribution/tests/test_factor_role_projection.py \
  tools/trace_attribution/tests/test_causal_role_closure.py

passed with no output
```

Whitespace validation:

```text
git diff --check

passed with no output
```

## Necessary Test Migration

`tools/trace_attribution/tests/test_causal_role_closure.py` is outside the
brief's three primary files but required one migration. Its Task 4 assertion
required a necessary non-root factor to remain absent from roots even after a
reciprocal confirmed root result. Task 5 explicitly changes that terminal
behavior. The migrated assertion now verifies that the factor first remains
non-root, then enters the root set only through a
`factor_role_escalation` root queue. The adjacent outperformed case still
verifies that a non-publishable escalation cannot erase the existing root.

## Self-Review

- Verified that escalation creation consumes only
  `factor_role_completed` actions whose canonical judgment is exactly
  necessary/unknown-role and whose queue is completed non-root.
- Verified exact action/judgment/request identity equality and exact
  candidate, seed, hypothesis, semantic hash, defect, perspective, path,
  owner, request projection, response, and terminal action binding.
- Verified root request identity and projection are recomputed from
  authoritative state and checked again at dispatch, replay, report, and
  restore boundaries.
- Verified ordinary root queue keys retain their four-part identity and only
  escalation keys append the source FactorRole action identity.
- Verified escalation entries do not count toward ordinary root scope or
  total Global candidate quotas, and no second escalation lifecycle can be
  created for the same source candidate.
- Verified necessary signals cannot use the ordinary factor gap/publication
  paths and cannot directly authorize a root.
- Verified rejected/unknown gaps are reconstructed from terminal actions;
  deletion, identity rewrite, foreign binding, and duplication are rejected.
- Verified confirmed escalation publication uses the existing mutual
  competitor graph and canonical ranking, including the legal confirmed but
  unpublished case.
- Verified Task 3/4 physical accounting, lifecycle reachability, publication
  bijections, queue-key closure, provider-free replay, stale quarantine, and
  response-identity checks remain green in full discovery.
- Verified no additional runtime feedback or Agent/Trace/Judge factual input
  path was introduced.

## Modified Files

- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/tests/test_factor_role_projection.py`
- `tools/trace_attribution/tests/test_causal_role_closure.py`
  (necessary Task 4 expectation migration)
- `.superpowers/sdd/task-5-report.md`

## Concerns

None.

## R1 Review Closure

Status: **DONE**

No git commit was created. The pre-existing dirty worktree was preserved.

### Changes

- A confirmed `factor_role_escalation` now uses the root-scope publication
  path when its seed has no published root. It enters the existing
  deterministic primary-root ranking and can become the primary root.
- Reciprocal `co_root` comparisons remain mandatory when an escalation
  competes with an already published root. Ordinary non-root confirmations
  retain their Task 4 mutual-support gate.
- When a newly published escalation explicitly and independently marks an
  ordinary unresolved root candidate `outperformed` or `rejected`, that
  candidate's seed blocker is resolved without changing its persisted
  confirmation response or authoritative request facts. The exact relation
  is recomputed at live, checkpoint, report, and `from_dict` boundaries.
- Escalation closure now requires exact canonical `LocalStateOwner` equality
  from the completed FactorRole action to the escalation queue and terminal
  root action. This includes `visit_key`, `occurrence_identity`, hypothesis,
  seed, defect, and candidate occurrence bindings. Escalation gaps continue
  to be derived from that terminal action, so their owner must match exactly.
- Added a coordinated foreign-occurrence regression that rewrites queue,
  journal, root action, and gap together while leaving the completed
  FactorRole owner unchanged. `RecursiveAttributionReport.from_dict` fails
  closed.
- Added field-closure regressions for coordinated seed, hypothesis, defect,
  and candidate mutations.
- Added completed necessary FactorRole plus terminal escalation checkpoint
  replay coverage. Replay performs zero FactorRole/root provider calls and
  retains exactly one escalation queue/action and one consumed source action.

### R1 TDD Evidence

Initial focused RED command exercised four new or migrated methods:

```text
PYTHONPATH=tools/trace_attribution:tools/trace_attribution/tests \
python3 -m unittest \
  test_causal_role_closure.GlobalNonRootSchedulingTest.test_non_root_necessity_without_published_root_becomes_primary \
  test_factor_role_projection.FactorRoleEscalationTest.test_report_rejects_coordinated_foreign_escalation_owner \
  test_factor_role_projection.FactorRoleEscalationTest.test_report_rejects_escalation_binding_field_mutations \
  test_factor_role_projection.FactorRoleEscalationTest.test_completed_necessary_escalation_checkpoint_replays_once -v

Ran 4 tests in 0.904s
FAILED (failures=6)
```

The two intended R1 failures were:

- the confirmed escalation produced `inconclusive` with no primary root;
- coordinated foreign occurrence owners were accepted by `from_dict`.

Four additional subtest failures were test-assertion mismatches: the existing
queue-key closure rejected coordinated field mutations before the new
escalation-specific regex matched. After synchronizing the queue-key fixture,
the four mutation cases already failed closed:

```text
Ran 1 test in 0.467s
OK
```

The terminal escalation checkpoint replay case was already behaviorally green,
matching the R1 review probe; the new test makes that behavior durable.

Final focused GREEN:

```text
PYTHONPATH=tools/trace_attribution:tools/trace_attribution/tests \
python3 -m unittest \
  test_causal_role_closure.GlobalNonRootSchedulingTest.test_non_root_necessity_without_published_root_becomes_primary \
  test_factor_role_projection.FactorRoleEscalationTest -v

Ran 11 tests in 2.046s
OK
```

### R1 Verification

Task 5 brief four-module command:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection \
  tools.trace_attribution.tests.test_causal_role_closure \
  tools.trace_attribution.tests.test_root_confirmation_fix39 \
  tools.trace_attribution.tests.test_seed_attribution -v

Ran 162 tests in 13.536s
OK
```

Complete attribution suite:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest discover \
  -s tools/trace_attribution/tests -v

Ran 1203 tests in 45.208s
OK
```

Static compilation:

```text
PYTHONPYCACHEPREFIX=/tmp/task5-r1-pycache python3 -m py_compile \
  tools/trace_attribution/trace_attribution/recursive_analyzer.py \
  tools/trace_attribution/trace_attribution/causal_state.py \
  tools/trace_attribution/tests/test_factor_role_projection.py \
  tools/trace_attribution/tests/test_causal_role_closure.py

passed with no output
```

The first unprefixed compile attempt could not write macOS's default Python
cache directory and raised `PermissionError`; redirecting bytecode cache to
`/tmp` produced the successful command above.

Whitespace validation:

```text
git diff --check

passed with no output
```

### R1 Self-Review

- Confirmed that a necessary FactorRole still never edits roots directly; only
  its independently completed root-scope escalation can publish.
- Confirmed that no-root escalation publication and existing-root reciprocal
  co-root publication take separate gates, then share canonical ranking.
- Confirmed that an ordinary unknown root remains blocking unless a published
  escalation on the same seed has one exact independent
  `outperformed`/`rejected` comparison to it.
- Confirmed that resolving an outperformed blocker does not rewrite provider
  output, root request projection, confirmation identity, queue identity, or
  lifecycle accounting.
- Confirmed exact owner equality includes the complete canonical owner object,
  not only hypothesis and seed.
- Confirmed journal/queue/action/gap closure, queue-key closure, duplicate
  consumption rejection, and provider-free replay remain intact.
- Confirmed no additional provider request beyond the necessary
  RootConfirmation and no Agent/Trace/Judge input feedback were introduced.

### R1 Modified Files

- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/tests/test_factor_role_projection.py`
- `tools/trace_attribution/tests/test_causal_role_closure.py`
- `.superpowers/sdd/task-5-report.md`

### R1 Concerns

None.

---

# Task 5: Question Binding, User Projection, and Read-Only Trace Proof

## Status

Completed. No files were staged or committed; the existing dirty worktree was
preserved.

## Delivery

- Question mode now emits `analysis_question` with the v1 schema version,
  original and normalized question, deterministic question ID, SHA-256 binding
  of `stable_json(graph.raw_trace)`, selected starts, and
  `offline_read_only` analysis mode.
- The projection is constructed before legacy output writes and before every
  recursive completed-replay/output transaction operation. Published report,
  completed checkpoint report, and returned result payload therefore bind the
  same final JSON object.
- Only question mode adds `conclusion`, `causal_chain`,
  `supporting_evidence_refs`, `rejected_hypotheses`, `confidence`, and
  `unresolved_gaps`. Objective-mode reports retain their existing shape.
- Projection is deterministic and report-bound: confirmed-root reasons and
  confidence, existing taint paths, rejected candidates/rejected hypotheses,
  unresolved refs, and recorded trace-improvement gaps are reused directly.
  Evidence refs are retained only when they occur in confirmed-root evidence
  and resolve in the graph. Question text is never promoted to evidence.
- When no root is independently confirmed, conclusion explicitly reports the
  existing outcome and insufficient evidence. The checkpoint schema remains
  unchanged; normalized questions continue to occupy the existing `objective`
  field, so different questions produce different config fingerprints.

## TDD Record

Initial RED:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_attribution_request -v

Ran 18 tests in 0.012s
FAILED (errors=4)
```

The expected failures were absent question payload construction, absent
question-mode fields in legacy output, and a recursive output transaction that
did not yet carry the final bound payload.

Focused GREEN:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_attribution_request -v

Ran 18 tests in 0.012s
OK
```

The final focused checkpoint command additionally includes the objective-mode
shape regression:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_attribution_request \
  tools.trace_attribution.tests.test_recursive_cli \
  tools.trace_attribution.tests.test_causal_checkpoint -v

Ran 128 tests in 9.067s
OK
```

## Regression Verification

Task 4 related regression set:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_attribution_request \
  tools.trace_attribution.tests.test_recursive_cli \
  tools.trace_attribution.tests.test_causal_checkpoint \
  tools.trace_attribution.tests.test_benchmark_bundle \
  tools.trace_attribution.tests.test_benchmark_case \
  tools.trace_attribution.tests.test_evaluation_facts \
  tools.trace_attribution.tests.test_recursive_acceptance_review \
  tools.trace_attribution.tests.test_backward_taint \
  tools.trace_attribution.tests.test_recursive_benchmarks -v

Ran 393 tests in 53.796s
OK
```

The original Task 4 set contained 390 tests; this run includes the three new
Task 5 tests. Test transports are mocked/offline and no network call occurred.

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task5-pycache python3 -m py_compile \
  tools/trace_attribution/trace_attribution/request.py \
  tools/trace_attribution/trace_attribution/service.py \
  tools/trace_attribution/tests/test_attribution_request.py \
  tools/trace_attribution/tests/test_recursive_cli.py

exit 0

git diff --check

passed with no output
```

## Modified Files

- `tools/trace_attribution/trace_attribution/service.py`
- `tools/trace_attribution/tests/test_attribution_request.py`
- `tools/trace_attribution/tests/test_recursive_cli.py`
- `.superpowers/sdd/task-5-report.md`

## Concerns

Legacy reports do not contain independently confirmed roots. In question mode,
they therefore intentionally project `inconclusive`/`no_defect` with evidence
gaps instead of promoting legacy `root_causes` into newly confirmed causes.

---

# Task 5 Review Closure: Projection and Replay Integrity

## Status

Completed the Important and Minor review items without staging or committing.

## Changes

- Question projections now combine `confirmed_roots` and `co_roots`, dedupe by
  node and semantic/confirmation identity, include every root in the conclusion,
  take the maximum existing confidence, and aggregate eligible root evidence.
- Evidence eligibility uses the graph's public active/revision eligibility API,
  allowing supported artifact evidence while dropping stale or invalid refs.
- Existing taint paths are retained only when they touch selected starts and,
  when roots exist, confirmed roots; path direction is not inferred or changed.
- Gaps are stable structured dictionaries, and question payload construction
  recursively copies mappings and sequences before projection.
- The completed checkpoint replay test exercises the actual checkpoint bundle,
  output transaction, completion marker, and replay commit against the final
  question-bound payload.

## TDD And Verification

Review RED (42 focused tests) produced six expected failures covering root
coverage, evidence eligibility, path filtering, structured gaps, deep copying,
and trace binding. A subsequent one-test RED exposed iterator consumption for
selected starts; caching the starts before projection made that test pass.

```text
Focused attribution/CLI/checkpoint suite: Ran 131 tests in 8.833s - OK
Task 4 related regression suite: Ran 397 tests in 52.747s - OK
py_compile (service and changed tests): exit 0
git diff --check: passed with no output
```

The trace-binding coverage computes both the immutable source-file byte SHA-256
and `sha256(stable_json(graph.raw_trace))`; it asserts the latter is the bound
value and the former remains unchanged. All tests use offline/mocked transports;
no network call is made.

## Concerns

None known. Existing objective-mode output remains field-compatible while its
returned nested structures are independently copied.
