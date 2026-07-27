# Global Judge Convergence Design

## Background

The current recursive attribution pipeline can retrieve useful candidates, but
the Global Judge is less reliable at confirming the actual root. A live Sphinx
fixture exposed three related problems:

1. lifecycle and materialized-result records can remain open authored root
   candidates even when a more direct authored decision/action exists;
2. the Judge receives generic rules instead of a deterministic per-candidate
   comparison contract, so malformed comparison matrices survive into repair
   attempts;
3. a small trace can expand into candidate capsules many times larger than the
   source trace, increasing cost and reducing judgment focus.

All changes in this design are offline-only. They must not change the observed
Agent, tool, model, MCP, skill, or benchmark execution behavior.

## Goals

- Keep lifecycle/outcome records out of root confirmation.
- Treat a materialized `change` as a fallback root only when no direct authored
  cause is recorded upstream.
- Give both the initial Global Judge request and every repair request an exact
  per-candidate comparison contract.
- Prevent oversized negative-compression payloads from entering global fusion.
- Preserve the complete evidence capsule and graph validation envelope as the
  factual authority.
- Fall back to the existing recursive backward-taint path when global fusion is
  bypassed.

## Non-goals

- No online diagnosis or feedback to the Agent.
- No modification to prompt assembly, tool selection, model calls, or benchmark
  execution.
- No replacement of the recursive analyzer.
- No lossy deletion of persisted evidence.
- No change to the final root-confirmation schema.

## Design

### 1. Graph-aware root eligibility

`root_candidate_eligible(node)` remains the node-local taxonomy check used by
recursive traversal. Lifecycle events such as `process.signal` and interruption
records remain eligible there because they can be confirmed as contributing or
amplifying factors.

`global_authored_root_candidate_eligible(graph, ref)` becomes the authoritative
graph-aware check for Global Judge root selection. Lifecycle events are
ineligible only at this root-selection boundary. A `change` remains eligible
when it is the earliest authored fact available, but becomes ineligible when an
active, causal predecessor records the decision or action that produced it.
This preserves factor attribution and sparse-trace fallback coverage while
preventing downstream artifacts from competing with their producers.

Candidate evidence capsules persist this graph-aware result. Validation
recomputes it from the active graph, so persisted eligibility cannot drift.

### 2. Candidate comparison contract

The Global Judge request derives a deterministic contract containing:

- every offered candidate;
- whether it is open and root eligible;
- its exact candidate-to-seed causal path;
- the complete set of open authored candidates it must compare against;
- required input/output defect-state relationships;
- valid causal roles and counterfactual requirements.

The same contract is embedded in the initial prompt and in repair constraints.
This keeps retries bound to the same facts instead of replacing one malformed
free-form answer with another.

### 3. Negative-compression fusion gate

Candidate compression metrics also report:

- capsule-to-trace byte expansion ratio;
- whether the payload is eligible for global fusion;
- the deterministic bypass reason.

The gate bypasses global fusion only when the capsule payload is larger than the
source trace, above the bounded absolute payload budget, and the open authored
root matrix is dense. A small open-root set remains eligible because its
comparison contract is bounded even when supporting evidence is verbose. The
existing recursive analyzer processes the untouched frontier after a bypass.
Bypass is a normal offline routing decision, not an attribution failure.

The full capsules remain available for validation and later inspection. The
gate only decides whether sending all of them to one model request is useful.

## Data And Control Flow

1. Build the candidate pool and full evidence capsules.
2. Recompute graph-aware root eligibility and validate capsules.
3. Compute compression and fusion-gate metrics.
4. If eligible, issue the Global Judge request with the candidate contract.
5. If ineligible, record a non-terminal offline gate event and continue through
   recursive backward-taint analysis.
6. On invalid model output, retry with the same candidate contract.
7. Confirm roots only through the existing independent confirmation boundary.

## Acceptance Criteria

- `process.signal` and interruption nodes remain recursive factor candidates
  but are never open Global Judge roots.
- A `change` with a direct authored causal predecessor is not an open root; an
  otherwise identical sparse `change` remains eligible.
- Capsule validation rejects eligibility values that contradict the active
  graph.
- Initial and repair prompts contain one exact contract row per candidate.
- Oversized negative-compression payloads make zero Global Judge calls and do
  not mark the seed unresolved.
- The recursive path still reaches a terminal result after a fusion bypass.
- Existing attribution tests remain green.
- A live sanitized Sphinx rerun shows fewer false open roots and either a valid
  Global judgment or an explicit payload-gate fallback.
