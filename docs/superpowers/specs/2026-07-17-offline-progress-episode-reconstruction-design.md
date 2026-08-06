# Offline Progress Episode Reconstruction Design

## Status

Approved by the user's instruction to implement the next iteration from the 2026-07-16 multi-benchmark analysis.

## Problem

The current Causal IR retains enough decision semantics for a human reviewer to identify the TerminalBench, Pydantic, and Sphinx root causes. The offline analyzer nevertheless misses all three roots because the observed outcome reaches lifecycle and context-transform records rather than the decisions that governed task progress.

Two distinct defects are in scope:

1. The graph lacks a selective chain from an observed defect or final response to meaningful cross-turn progress.
2. Production attribution deterministically marks evaluation assertions as present instead of asking the LLM Judge to validate them against supplied evidence.

Runtime Agent behavior, prompt contents, tool execution, and repository changes must remain untouched.

## Considered Approaches

### A. Link terminal records to the most recent decisions

This is small, but it creates broad temporal edges without explaining why the decisions matter. It would also make failure attribution sensitive to an arbitrary recent-node limit.

### B. Reconstruct offline progress episodes

Group semantic records by observed Agent turn, derive bounded progress facts, chain episodes in execution order, and project final/evaluation records onto the most recent eligible episode. This is the selected approach because it is passive, auditable, and provides the LLM with both a compact progress summary and the exact member decisions.

### C. Add runtime task planning and progress instrumentation

This can provide stronger task-obligation identity, but it changes the runtime trace producer and has a larger blast radius. It remains a later phase after the existing Trace has been used as fully as possible.

## Architecture

### `progress.py`

A new focused module reconstructs progress from existing `TraceNode` records and message-lineage turns.

It produces one `progress.episode` node for each turn containing at least one semantic member:

- reasoning or action decisions;
- tool calls, results, or errors;
- changes;
- execution observations;
- verifications.

Lifecycle, context transforms, prompt assembly, and provider transport records are excluded from episode membership.

Each episode records:

- member refs and concise member summaries;
- candidate member refs selected for LLM judgment, while excluded members remain available for audit;
- the deterministic candidate-selection method and excluded member refs;
- deterministic phase: `exploration`, `implementation`, `verification`, `delegation`, or `reasoning`;
- read/search, mutation, verification, delegation, and error counts;
- whether the turn made delivery progress;
- cumulative change and verification counts;
- consecutive no-delivery-progress episode count;
- previous episode ref;
- `offline_only=true`, `collection_mode=offline_passive_reconstruction`, and `behavior_impact=none`.

### Graph integration

`TraceGraph.from_trace()` adds reconstructed episode nodes after native records and message lineage have been built.

Edges have these meanings:

```text
member decision/action/result -> progress.episode
previous progress.episode     -> current progress.episode
latest progress.episode       -> offline evaluation assertion
latest progress.episode       -> final response output/claim
```

The projection uses only episodes that occur before the target record. It does not change the input Trace JSON.

Turn order is anchored to the first task decision timestamp, not raw record insertion order or the
oldest administrative record attached to a message. This handles partial traces where a late message
reuses an earlier `context.compaction_check` record.

### Backward traversal

Upstream selection prioritizes:

1. direct response support;
2. progress episodes;
3. decisions, changes, and verifications;
4. execution outcomes;
5. LLM calls;
6. lifecycle and generic context records.

A `progress.episode` is an offline aggregate and cannot be a root cause. The analyzer expands its
candidate decision/action members and deterministically visits the previous episode as a navigation
edge. The previous episode is not automatically defective: each concrete candidate still requires an
independent LLM judgment. This separation prevents a non-defective aggregate judgment from cutting off
older decisions while avoiding temporal adjacency being treated as defect propagation.

Candidate projection keeps reasoning decisions, the first authored action, mutations, verifications,
delegations, MCP/skill records, and errors. All other members remain in `member_refs` and
`member_summaries` but do not require an individual model call by default.

### Evaluation assertion validation

`ClaudeJudgeClient` validates `case.observed_defect` and `case.quality_gap` nodes with the same LLM judgment schema used for ordinary nodes. This allows false Trace-health assertions such as the Axios missing-verification warning to be rejected.

`case.missing_semantic` remains a deterministic Trace-health boundary because it asserts unavailable information rather than task behavior.

Test doubles that do not implement evaluation validation retain the current deterministic boundary behavior. Production `ClaudeJudgeClient` always implements it.

### Judge telemetry

Attribution metadata adds:

- `judge_model`;
- `judge_request_count`.

This proves whether an attribution report used an LLM without storing credentials or prompts.

## Invariants

- Reconstruction is offline and deterministic.
- No reconstructed fact is written back to the source Trace.
- Reconstructed facts are never exposed to the Agent.
- Temporal adjacency alone never makes a decision defective.
- A progress episode cannot be emitted as a root-cause candidate.
- Episode chronology follows task decisions rather than administrative record creation time.
- Candidate projection may reduce LLM calls but never deletes source members or source artifacts.
- Only refs present in the reconstructed graph may be returned by the Judge.
- Existing raw events and Causal IR records remain unchanged.

## Testing

Unit tests must first demonstrate these current failures:

1. an observed empty-patch defect cannot reach a prior root decision;
2. a final response cannot reach an earlier action episode through administrative context;
3. a production-style Judge cannot reject a false evaluation assertion;
4. attribution metadata does not expose model/request telemetry.

After implementation, tests verify episode membership, previous-episode chaining, target projection, upstream priority, non-root aggregation behavior, evaluation validation, and Judge telemetry.

The existing Pydantic, Sphinx, and TerminalBench traces are then re-analyzed. Success is measured in two layers:

- graph reachability: all three manually labeled root decision sets become reachable within `max_depth=24` and `max_nodes=128`;
- LLM attribution: the Judge visits all three root decision sets, produces an episode-equivalent or exact root for at least two cases, and introduces no Agent root in the three successful controls.

Failure to meet the LLM target is reported as the next iteration input rather than hidden by heuristic root promotion.
