# Root Confirmation Stability Task 2 Fix Wave 9 Report

Base commit: `111383138`

## Scope

Closed all three Important findings from the ninth whole-Task-2 review. The
analysis remains offline, passive, read-only, dependency-free, and isolated
from agent behavior. No Task 3 behavior was added.

## Root Causes

1. The shared authored-root policy enumerated several evidence-only records but
   omitted canonical observation and outcome siblings, including
   `execution.observation`, `observation`, and `case.completed`.
2. Candidate capsule v2 bound candidate nodes and downstream references to the
   active graph, but never reconciled persisted `causal_path_edges`,
   `incoming_edges`, or `outgoing_edges` with normalized active edges. Recorded
   edge metadata was also discarded, making revision drift invisible.
3. Confirmation ownership validation proved bidirectional identity links but
   did not require an unresolved confirmation's owner seed to retain a
   conservative outcome and concrete unresolved fact.

## RED

Added nine finding-level regressions before production changes and ran them as
one exact set.

Result: `Ran 9 tests` / `FAILED (failures=26)`.

The failures proved:

- `case.completed`, `observation`, and `execution.observation` passed direct
  authored-root policy, recursive introduction, global selection/envelope
  restore, and live/restored queue guards;
- forged entries in all three persisted capsule edge collections passed active
  graph validation;
- removed, changed, temporalized, and metadata-revision-drifted active edges
  did not invalidate a restored capsule; and
- direct report construction, report deserialization, partial checkpoint
  restore, and completed checkpoint restore accepted an `unknown` confirmation
  owned by `no_defect` or unblocked `inconclusive` seed state.

The end-to-end analyzer publication case was already suppressed by a divergent
local candidate filter. The shared-policy regressions exposed that this was not
protection at root introduction, global request, queue, or direct publication
boundaries.

## Changes

- Split the authoritative authored-root exclusion taxonomy into shared
  observation/outcome and evidence-fact families. The formal taxonomy test
  derives matching canonical event names from
  `trace-semantic-contract.ts`, while repository extensions such as
  `case.quality_gap` and `mcp.result` are explicitly covered.
- Preserved recorded edge metadata in normalized graph edge facts and included
  it in stable edge identity, retaining revision and other confirmation-relevant
  metadata without changing runtime behavior.
- Rebuilt each capsule's causal-path, incoming, and outgoing edge collections
  from the active graph and required exact normalized equality before judgment,
  restore, or publication can consume the envelope.
- Extended the shared confirmation ownership validator so every status other
  than `confirmed` or definitive `rejected` requires an owning seed with
  `evidence_gap` or `inconclusive` outcome and concrete blocking or missing
  evidence. Existing live `record_confirmation()` behavior already creates
  those facts; construction and restore now enforce them.

## Verification

1. Exact targeted regression set after implementation:
   - `Ran 9 tests in 0.064s` / `OK`.
2. Task 2-focused attribution set:
   - final rerun: `Ran 512 tests in 2.447s` / `OK`.
3. Full Python attribution suite:
   - final rerun: `Ran 688 tests in 4.503s` / `OK`.
4. Compile check:
   - `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix9-pycache python3 -m compileall -q`
     over attribution source, scripts, and tests; exit status `0`.
5. Working-tree and required base-range diff checks:
   - `git diff --check`; exit status `0`.
   - `git diff --check 111383138 --`; exit status `0`.
6. The committed base-range diff check is rerun after commit and recorded in
   the completion response.

## Files

- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_causal_judge.py`
- `tools/trace_attribution/tests/test_causal_retrieval.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `.superpowers/sdd/root-confirmation-task-2-fix9-report.md`

## Residual Risk

- Exact edge reconciliation intentionally invalidates a persisted capsule when
  any bounded normalized edge fact changes, including non-causal incoming or
  outgoing context. This conservative policy favors rejudgment over consuming
  stale evidence.
- Canonical event families with names outside the existing observation,
  result, error, case, verification, or fact conventions still require an
  explicit taxonomy review.
