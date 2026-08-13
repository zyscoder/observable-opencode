# Attribution Execution Reliability Design

## Background

An attribution run over the `stupid-build` trace selected two automatic trace-health
seeds and failed before processing any recursive frontier item. Each Global Judge page
contained at most eight candidates, but the physical request still contained between
231,282 and 254,400 input tokens. The configured model accepts 202,752 total tokens.
The provider rejected all three attempts, the transport classified the HTTP 400 as
retryable, and the final report projected the execution failure as an `evidence_gap`.

The result was therefore not an inconclusive semantic judgment. No Global Judge
judgment completed, no candidate set was produced, and backward traversal never began.

## Goals

1. Prevent an oversized Judge request before any physical provider call.
2. Fit Global Judge pages to a configured input budget instead of only a candidate-count
   limit.
3. Preserve the complete Causal IR and validation envelope while sending a smaller,
   auditable evidence projection to the Judge.
4. Treat stable context-window HTTP 400 failures as non-retryable.
5. Distinguish attribution execution failure from semantic evidence insufficiency in the
   report.
6. Record enough payload-budget diagnostics to explain what was retained, omitted, or
   deferred to evidence expansion.

## Non-Goals

1. This change does not implement `expectation_gap` or automatically create a Yocto
   deviation seed from `--question`.
2. It does not modify observable-opencode Agent behavior or feed attribution results back
   to the Agent.
3. It does not delete facts from `trace.json`, Causal IR, checkpoints, or validation
   envelopes.
4. It does not use a lower fixed candidate limit as the sole overflow mitigation.

## Selected Design

### 1. Explicit Judge Context Budget

`AttributionOptions` gains a model context-window budget and a reserved safety margin.
The effective maximum input tokens are:

```text
context_window_tokens - judge_max_tokens - context_safety_margin_tokens
```

All values are positive integers. The effective input budget must remain positive. The
CLI exposes the context window and safety margin so compatible Anthropic endpoints with
different model limits remain supported.

The budget implementation uses a deterministic conservative token estimate. It must not
depend on a provider call or model-specific tokenizer. The estimator records UTF-8 bytes,
characters, estimated tokens, configured output tokens, safety margin, and total context
window. A provider-specific tokenizer can be introduced later without changing the
budget interface.

### 2. Judge Projection Is Separate From Canonical Evidence

`GlobalCandidateJudgeRequest.validation_envelope()` remains complete and authoritative.
`GlobalCandidateJudgeRequest.to_dict()` remains the canonical factual request used for
validation and replay.

A new prompt projection builds a bounded view for the Judge. It retains:

- case, objective, perspective, seed and active defect bindings;
- exact offered candidate identities and eligibility facts;
- candidate semantic summaries and source refs;
- exact candidate-to-seed path identities and causal edge summaries;
- restoration obligations;
- concise action-group, evidence-reference and artifact facts;
- explicit omission manifests with hashes and original byte counts.

Large repeated node snapshots, duplicate artifact content, repeated path snapshots and
validation-only collections are removed from the prompt projection. Their refs remain
available to evidence expansion. The projection is deterministic, schema-versioned and
hash-bound to the canonical request.

### 3. Token-Aware Dynamic Paging

Candidate ordering remains deterministic. Eight remains an upper bound, not a guaranteed
page size. Before executing a page, the analyzer builds its projected request and checks
the estimated input budget.

If a page is too large:

1. split the ordered candidate page in half;
2. repeat until every page fits or a page contains one candidate;
3. for a single oversized candidate, apply the bounded prompt projection;
4. if the minimal projection still does not fit, fail locally with a typed context-budget
   error and zero physical requests.

The generated page plan records candidate refs, projection identity, estimated tokens,
budget, split reason and parent page identity. Checkpoint replay validates those fields so
the same plan is reproduced.

### 4. Provider Preflight and Failure Classification

Every physical Judge call performs a final local context-budget check. This is a safety
net for repair, retry, recursive and confirmation prompts that do not use Global Judge
paging.

An over-budget preflight raises a typed `JudgeContextBudgetExceeded` through
`TransportCallError` with `physical_requests=0`.

Provider failures with HTTP 400 are non-retryable unless their structured code clearly
identifies a transient condition. If the SDK loses the structured code, known context
length markers in the provider message are classified as `context_window_exceeded`,
non-retryable. The circuit opens immediately and does not send duplicate requests.

### 5. Execution Failure Semantics

The recursive report adds structured execution failures. A context-budget failure uses:

```json
{
  "kind": "analysis_execution_failed",
  "stage": "global_candidate_judgment",
  "reason": "context_window_exceeded",
  "retryable": false,
  "physical_requests": 0,
  "budget": {},
  "affected_start_refs": []
}
```

When no semantic judgment completed because of such a failure:

- `analysis_outcome` is `execution_failed`;
- seed outcome is `execution_failed`, not `evidence_gap`;
- `termination_reason` is `analysis_execution_failed`;
- the user projection states that no root-cause conclusion was attempted;
- `missing_evidence` is reserved for missing semantic facts, not transport failures.

Partial runs may retain valid semantic results from completed seeds while separately
listing execution failures for unfinished seeds.

### 6. Diagnostics

Each Judge request records:

- projection schema and identity;
- canonical and projected byte counts;
- projected estimated input tokens;
- context window, output reserve and safety margin;
- candidate and evidence-context counts;
- retained and omitted sections;
- dynamic page split history;
- physical-request count and exactness.

No secret, API key or provider authorization header is recorded.

## Error Handling

1. Invalid budget configuration fails at request construction with a concise CLI error.
2. Local overflow is deterministic and consumes zero physical requests.
3. Provider-reported context overflow opens the circuit immediately as non-retryable.
4. Checkpoint replay rejects projection-policy or budget-policy drift.
5. A failed page cannot be interpreted as `no_defect`, `no_root_candidates`, or
   `evidence_gap`.

## Testing Strategy

1. Unit tests cover token estimation, budget validation and prompt preflight.
2. Provider tests cover context-window HTTP 400 classification and exactly one physical
   attempt for provider-reported overflow.
3. Global Judge tests build oversized capsules and verify deterministic dynamic splitting.
4. Report tests verify `execution_failed` remains distinct from `evidence_gap`.
5. Existing causal Judge, Global Judge and pagination suites must remain green.
6. A synthetic large request reproduces the former 200k-token class of failure and proves
   no over-budget physical request can be emitted.

## Acceptance Criteria

1. The same class of large trace never sends a request estimated above the configured input
   budget.
2. A local overflow reports zero physical requests.
3. A provider context-window HTTP 400 is attempted once and is non-retryable.
4. Dynamic pages preserve every candidate exactly once per comparison round.
5. The complete validation envelope and Causal IR remain unchanged and auditable.
6. `processed_frontier_items` can advance after successful bounded Global Judge pages.
7. An execution failure is visible as `analysis_outcome=execution_failed`, never as a bare
   `evidence_gap`.

