# Root Confirmation Task 2 Fix Wave 26 Report

Base commit: `bc7d3ed2c4255ecde8ea0941027457074b91fabe`

## Scope And Constraints

Fix26 closes all six findings in
`root-confirmation-task-2-fix26-brief.md`. The attribution path remains
offline, passive, read-only, and unable to feed conclusions back to the Agent.
No Task 3 evidence-expansion behavior and no dependency were added.

## Root-Cause Validation And TDD Evidence

### 1. Strict active-start provenance

Root cause: explicit starts and Global Candidate Judge validation used the
general evidence-eligibility boundary. Under a formal manifest, a record could
therefore remain evidence-eligible while lacking the record-level binding
required of an analysis seed. Restore and stale-seed reconstruction also used
the weaker boundary inconsistently.

RED: the new strict-start tests showed that a formal unbound explicit seed
entered `state.start_refs`, could reach global request construction, and could
survive checkpoint/report reconstruction. The correctly bound and non-formal
legacy controls already passed.

GREEN: explicit creation and global request validation now use
`active_revision_start_eligible`. Checkpoint and report restore reject formal
unbound starts. Stale historical starts are retained only for conservative
quarantine and cannot publish conclusions.

### 2. Enabled retrieval-global failures

Root cause: an explicitly enabled global pass treated missing capability and
runtime/validation failures as recursive fallback opportunities. That allowed
a seed to proceed toward confirmation without the requested global pass and
did not persist a terminal per-seed failure episode.

RED: focused tests reproduced missing capability, capsule construction,
request validation, bounded Judge failure, invalid output, and restore without
a bijective failure episode.

GREEN: every enabled-mode failure records one canonical failed global-pass
event, a concrete blocker, missing evidence, and one unresolved episode. All
frontier hypotheses owned by that seed are rejected, so the seed is
`evidence_gap`/`inconclusive` and cannot publish a root. Other seeds continue
independently. Only `fusion_mode="off"` retains no-global-pass compatibility.

### 3. Verified artifact owner binding

Root cause: artifact eligibility proved content availability and integrity but
did not require an active record owner. When owners existed, the graph selected
the lexicographically first owner without candidate-path disambiguation, and
the selected owner/provenance facts were not durably projected.

RED: orphan and stale-only artifacts were accepted, ambiguous owners were
silently selected, no explicit-owner API existed, and owner substitution could
survive persisted projections.

GREEN: artifact evidence now requires complete verified bytes plus one active,
strictly bound owner. Candidate-local use selects the candidate owner or the
only active owner on its path; all other ambiguity fails closed. The complete
owner/provenance/path/hash/range envelope and
`owner_binding_identity` are persisted in queue, confirmation action,
checkpoint, report, and evaluator-facing projection, then rebuilt and compared
exactly on every validation path.

### 4. Canonical global-pass identity

Root cause: runtime suppression and persisted judgment ownership depended on a
frontier occurrence. A second hypothesis for the same seed could run another
unsuperseded global pass and replace the seed judgment.

RED: two frontier occurrences for one seed produced two calls, and persisted
passes lacked one canonical seed-level identity.

GREEN: `global-pass-identity/v1` derives only from
`seed_binding_identity`. Runtime suppression, journal ownership, restored seed
judgment reconciliation, failure episodes, and evaluator derivations all use
that identity. Duplicate identities and substituted owners are rejected.

### 5. Authoritative completed step actions

Root cause: `step_judgments` and flat/nested predecessor relations were trusted
as state snapshots independently of durable `provider_call_completed` actions.
Coordinated content replacement could therefore remain internally consistent
without matching the provider result.

RED: content mutation, coordinated judgment/relation replacement, duplicate or
missing completed actions, and conversion of a completed action to failed were
accepted. Investigation/rejudge flows also revealed that one visit may have
multiple valid provider occurrences.

GREEN: each completed step action creates a canonical occurrence keyed by its
provider semantic key. Its owner-bound judgment and predecessor relations are
projected before any control/investigation transition. Strict multiset
bijections now cover provider actions, action projections, step judgments,
nested predecessors, flat relations, checkpoints, reports, and evaluator
validation. Failed/incomplete actions authorize no judgment. Rejudge
occurrences are all retained while business state transitions still happen
once.

### 6. Confirmation queue/key bijection

Root cause: the queue and cached key set could drift. A stale extra key could
silently suppress a legitimate enqueue, while missing/extra persisted keys,
duplicate candidates, and duplicate semantic identities were not rejected
consistently.

RED: live missing/extra keys, duplicate entries, duplicate semantic identities,
and checkpoint/report key mutations were accepted or silently suppressed.

GREEN: live enqueue validates the existing queue first. Canonical keys are
derived from entries and must equal the persisted set exactly. Candidate
identity is unique per seed, semantic identity is globally unique in the
queue, and checkpoint/report/evaluator restoration invokes the same validator.

## Schema And Policy Decisions

- Report schema: `recursive-attribution-report/v8` to `v9`.
- Checkpoint schema: `recursive-attribution-checkpoint/v7` to `v8`.
- Action state: `recursive-analysis-actions/v5` to `v6`.
- Graph evidence policy: `graph-external-evidence-eligibility/v3` to `v4`.
- Root confirmation persistence:
  `recursive-root-confirmation/v10`, with `artifact-owner/v1` and
  `step-action-projection/v1`.
- Global persistence adds `global-pass-identity/v1` and `failure-action/v1`.

Current reports require queue-key and step-action projections. A v8 report is
rejected rather than silently reinterpreted. Old checkpoint/action schemas are
rejected by their exact schema checks. Existing explicit v1/v2 conservative
report migration remains unchanged and does not publish migrated roots.

## Verification

Focused Fix26:

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix26-focused-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_root_confirmation_fix26
31 tests, PASS
```

Fix17-Fix26 plus seed/global/checkpoint core regression:

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix26-regression-core3-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_root_confirmation_fix17 tools.trace_attribution.tests.test_root_confirmation_fix18 tools.trace_attribution.tests.test_root_confirmation_fix19 tools.trace_attribution.tests.test_root_confirmation_fix20 tools.trace_attribution.tests.test_root_confirmation_fix21 tools.trace_attribution.tests.test_root_confirmation_fix22 tools.trace_attribution.tests.test_root_confirmation_fix23 tools.trace_attribution.tests.test_root_confirmation_fix24 tools.trace_attribution.tests.test_root_confirmation_fix25 tools.trace_attribution.tests.test_root_confirmation_fix26 tools.trace_attribution.tests.test_seed_attribution tools.trace_attribution.tests.test_global_judge tools.trace_attribution.tests.test_causal_checkpoint
279 tests, PASS
```

Affected root/seed/recursive/checkpoint/global/graph/artifact/evaluator suites:

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix26-affected2-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_root_confirmation_fix17 tools.trace_attribution.tests.test_root_confirmation_fix18 tools.trace_attribution.tests.test_root_confirmation_fix19 tools.trace_attribution.tests.test_root_confirmation_fix20 tools.trace_attribution.tests.test_root_confirmation_fix21 tools.trace_attribution.tests.test_root_confirmation_fix22 tools.trace_attribution.tests.test_root_confirmation_fix23 tools.trace_attribution.tests.test_root_confirmation_fix24 tools.trace_attribution.tests.test_root_confirmation_fix25 tools.trace_attribution.tests.test_root_confirmation_fix26 tools.trace_attribution.tests.test_seed_attribution tools.trace_attribution.tests.test_recursive_analyzer tools.trace_attribution.tests.test_causal_checkpoint tools.trace_attribution.tests.test_global_judge tools.trace_attribution.tests.test_causal_state tools.trace_attribution.tests.test_causal_retrieval tools.trace_attribution.tests.test_artifact_hydration tools.trace_attribution.tests.test_recursive_acceptance_review tools.trace_attribution.tests.test_recursive_benchmarks
509 tests, PASS
```

Evidence capsule regression:

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix26-evidence-capsule-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_evidence_capsule
26 tests, PASS
```

Investigation/rejudge regression after occurrence-contract fixture migration:

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix26-investigation-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_investigation
51 tests, PASS
```

Full attribution discovery:

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix26-full2-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'
864 tests, PASS
```

Compilation:

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix26-compile-pycache python3 -m compileall -q tools/trace_attribution
PASS
```

## Changed Files

Production and evaluator:

- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`

Focused and migrated regression tests:

- `tools/trace_attribution/tests/test_root_confirmation_fix26.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix18.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix19.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix23.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix25.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_recursive_acceptance_review.py`
- `tools/trace_attribution/tests/test_investigation.py`

Task artifacts:

- `.superpowers/sdd/root-confirmation-task-2-fix26-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix26-report.md`

## Residual Risks

- The strict envelope includes manifest path and owner provenance, so a valid
  trace regeneration that intentionally changes either fact requires a new
  analysis instead of reusing an old report. This is intentional fail-closed
  behavior.
- Provider-specific network transports were not exercised; the required
  analysis remains offline and uses bounded/offline test doubles.
- No known correctness blocker remains.

Existing unrelated untracked files and `.benchmark-runs/` were not read,
modified, staged, or removed.
