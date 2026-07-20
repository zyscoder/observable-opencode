# Trace Attribution

Offline causal-chain attribution for observable-opencode semantic traces.

The module reads an existing `trace.json`, builds a reverse dependency graph from
`source_refs` and `dataflow_edges`, then uses Claude through the Anthropic Python SDK
to perform backward semantic taint analysis:

1. Decide whether the current node/component contains a semantic defect.
2. If defective, decide whether the defect was caused or propagated by upstream nodes.
3. Continue backward until a defective node has no defective upstream cause; that node is
   reported as a root-cause candidate.

Each node is classified as `present`, `absent`, or `unknown`. Judge failures, incomplete
responses, unresolved refs, and traversal limits remain `unknown`/`inconclusive`; they are
never promoted to root causes. Every observed defect is traversed independently and emitted
under `defect_branches`, with its own judgments, visited order, limits, paths, and roots.
Reports expose `analysis_outcome` as `root_found`, `partial_root_found`, `no_defect`, or
`inconclusive`, plus a separate `termination_reason`. `partial_root_found` means at least one
observed defect reached a root while at least one other defect branch did not.

Root candidates are grouped into confirmed causal episodes before reporting. An episode may
contain same-message reasoning and action decisions, records sharing an explicit tool call ID,
and tool/change records sharing an execution span. Temporal proximity and cumulative context
provenance never merge episodes. When multiple records in one episode are judged as the same
defect introduction, the earliest confirmed defective introduction is reported as the episode
representative; `episode_member_refs` preserves the complete auditable group.

Before episode collapse, every proposed introduction root receives an independent node-local
confirmation. The confirmer must try to falsify the first judgment, ground its answer in text or
code identifiers present in the current node, and apply a counterfactual: executing the node
exactly must itself cause the active defect. If confirmation rejects an earlier alleged root, a
downstream propagation whose only defect predecessor was that rejected node is reclassified and
confirmed as the new introduction boundary.

Each `influenced_by` edge has one of three relations:

- `defect_propagated_from`: the upstream node already contains the active branch defect; only
  this relation continues backward defect traversal.
- `motivated_by_evidence`: truthful evidence influenced a later decision but did not itself
  contain or transmit that decision's defect.
- `derived_from`: ordinary non-defect provenance, such as an evaluation assertion citing
  outcome evidence.

`branch_relation` states whether the current node has the same active defect, is a causal
precursor, is outcome evidence, is unrelated, or cannot be classified. Code complexity or a
possible failure mechanism is not sufficient to establish a root.

The judge prompt also labels the current node's semantic role. An Agent-authored tool command
or self-test script is `authored_agent_action`: its arguments are action semantics, not outcome
evidence. A defective action is introduction or propagation, while the subsequent `tool.result`
remains truthful outcome evidence. A generic plan to run a test is judged separately and is not
defective merely because the later executable test command is defective.

The module is offline with respect to opencode execution. It never writes back to trace
files and never feeds attribution results back into the agent.

Each attribution JSON also includes `trace_improvement_report`. This report describes
where backward taint analysis became weak or blocked, which trace facts were missing,
and which component should emit richer semantics in the next trace iteration.
Unknown judgments on unresolved branches remain in `blocking_gaps`. Unknown sibling records on a
branch that already reached a confirmed root are retained in `advisory_gaps`, so completeness
limitations stay auditable without incorrectly reporting that root attribution failed.

The analyzer also reconstructs agent turns and message-context lineage from existing trace
records and artifact payloads. Reconstruction is an offline passive sidecar: it does not
replay requests, modify trace input, or feed findings back into the agent.

The same offline pass reconstructs `progress.episode` nodes from task-semantic turn members.
Episodes are ordered by decision timestamps, linked to the previous episode, and projected onto
later evaluation/final-response nodes. Each episode preserves every member for audit while exposing
a smaller `candidate_member_refs` set for LLM judgment. The candidate projection prioritizes
reasoning, authored actions, mutations, verification, delegation, MCP/skill activity, and errors;
excluded members remain in the report and source Trace unchanged. Progress episodes are navigation
aggregates and can never be reported as root causes.

## Install

```bash
python3 -m pip install anthropic
```

## Run

```bash
export ANTHROPIC_API_KEY="..."
export CLAUDE_MODEL="claude-sonnet-4-5"

PYTHONPATH=tools/trace_attribution \
python3 -m trace_attribution \
  --trace /tmp/observable-opencode-loop-auto/stress-v66-loop2/traces/tool-failure-hallucination/trace.json \
  --out /tmp/observable-opencode-attribution/tool-failure.attribution.json \
  --objective "Find why the final answer quality was poor."
```

For DeepSeek's Anthropic-compatible endpoint:

```bash
export ANTHROPIC_API_KEY="$DEEPSEEK_API_KEY"
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export CLAUDE_MODEL="deepseek-v4-pro"
```

Optional:

```bash
--review /tmp/observable-opencode-loop-auto/reports/tool-failure-hallucination.trace-review.json
--start-ref record:responseclaim_claim_147_e7906af6
--base-url https://api.deepseek.com/anthropic
--judge-cache /tmp/observable-opencode-attribution/tool-failure.judge-cache.jsonl
--judge-max-tokens 4096
--judge-timeout-sec 3600
--provider-error-threshold 3
--thinking-mode auto
--lineage-out /tmp/observable-opencode-attribution/message-lineage.json
--max-depth 8
--max-nodes 48
--model claude-sonnet-4-5
```

Each judge or JSON-repair request waits up to 3600 seconds by default. Override it with
`--judge-timeout-sec` or `CLAUDE_TIMEOUT_SECONDS` when a different per-request budget is required.
`--thinking-mode auto` disables thinking on DeepSeek's Anthropic-compatible endpoint so the output
budget is spent on the structured judgment; use `enabled` explicitly when deeper online reasoning is
worth the additional latency and token cost.

`--max-depth` and `--max-nodes` apply independently to every defect branch. This prevents a
large or difficult branch from consuming the search budget needed to analyze other observed
defects.

## Resumable Judge Runs

Every CLI run durably checkpoints validated node judgments and root confirmations. When
`--judge-cache` is omitted, the cache is written next to `--out` as
`<attribution-output-stem>.judge-cache.jsonl`. Re-run the same command after a timeout, provider
disconnect, or process restart: exact request matches are loaded from the checkpoint, while only
failed or not-yet-visited judgments call the provider again.

The cache key covers the stage, model, complete system/user messages, token budget, thinking
configuration, and prompt-schema version. A prompt, model, context, or configuration change cannot
silently reuse a stale judgment. Only schema-valid judgments are persisted; prompts, API keys, and
provider responses that failed validation are not stored in the checkpoint.

Consecutive connection or timeout failures open a provider circuit after
`--provider-error-threshold` failures (default `3`). The current branch then terminates as
`inconclusive` with `termination_reason=provider_unavailable` instead of manufacturing many
fallback `unknown` judgments. `metadata.judge_cache` reports loaded entries, hits, misses, and
writes; `metadata.provider_circuit` reports provider failures and circuit state. Delete or point
`--judge-cache` to a new path only when a complete re-evaluation is intended.

When `--lineage-out` is omitted, the CLI writes `<attribution-output-stem>.message-lineage.json`
next to the attribution report. The lineage output contains normalized agent turns, prompt/context/
compaction/LLM snapshots, and reconstructed edges. Edge evidence is classified as:

- `confirmed` for explicit IDs or same-message deterministic relationships;
- `content_matched` for exact normalized decision text retained in an LLM request artifact;
- `temporal_inferred` for same-session ordering without direct identity or content proof.

Only confirmed and content-matched edges are eligible for backward attribution. Temporal-only
edges remain visible for review but cannot establish a root cause.

## Structured Judge Context

The offline analyzer builds a passive `causal_judgment_context` for every visited node. It preserves
the active-defect fingerprint, normalized incoming edge semantics, the active-path edge, previously
completed downstream judgments, the concrete causal episode, and the containing progress episode.
The same compact context is retained under each defect branch's `metadata.judgment_contexts` so a
reviewer can audit what the model received without replaying the Agent or modifying `trace.json`.

Long runs retain every turn-level `progress.episode`, but backward navigation groups adjacent
no-delivery turns into a delivery-bounded window. The analyzer expands every concrete candidate in
that window and then jumps to the preceding delivery episode. This reduces artificial search depth
without deleting turn facts or promoting an aggregate node to root cause.

## Quality Gap Attribution

Stress-case reviews can include `quality_review.quality_gaps` for semantic-understanding
cases that are not outright failures but still miss important reasoning. Pass the review
file with `--review` to inject offline-only `case.quality_gap` nodes into the attribution
graph:

```bash
node packages/opencode/test/observability/stress-cases/analyze-trace-sufficiency.mjs \
  --traces /tmp/observable-opencode-loop2-quality/semantic-run/traces \
  --out /tmp/observable-opencode-loop2-quality/semantic-run/reports

PYTHONPATH=tools/trace_attribution \
python3 -m trace_attribution \
  --trace /tmp/observable-opencode-loop2-quality/semantic-run/traces/semantic-requirement-priority/trace.json \
  --review /tmp/observable-opencode-loop2-quality/semantic-run/reports/semantic-requirement-priority.trace-review.json \
  --out /tmp/observable-opencode-attribution/semantic-requirement-priority.attribution.json \
  --objective "Find why the semantic quality score missed the target."
```

The injected quality-gap records are not written back to the original trace and are never
fed back into opencode. They only give the offline analyzer a precise starting point for
backward semantic taint analysis. These evaluation records are assertions to validate,
not guaranteed defects: the analyzer rejects them when their cited upstream facts are
non-defective.

Stress-case reviews that report `missing_semantics` also inject offline-only
`case.missing_semantic` nodes. Runtime traces may additionally contain passive
`case.observed_defect` / `case.missing_semantic` records for health findings such as
"repository changed but no final test result was observed". These records are derived
after the agent run and are never fed back into the agent.

Ground Truth root-cause labels are retained for external scoring only. They are not injected
as observed defects or copied into semantic-gap nodes. Only an explicit `observed_defects`
entry can create an offline `case.observed_defect` start node.

## Trace Improvement Feedback

`trace_improvement_report` is generated deterministically from the attribution graph and
judgments. It does not make extra model calls. Typical entries include:

- `llm_call_missing_generation_semantics`: attribution reached an `llm.call`, but the
  trace lacks normalized input messages, message transform stages, selected context refs,
  compaction provenance, or output text.
- `answer_surface_root_cause`: attribution stopped at `response.output` or
  `response.claim`, so the trace needs stronger links from final answer content back to
  LLM calls, tools, context, and claim-support checks.
- `defective_node_points_to_nondefective_upstream`: a provenance edge was too broad or
  missing an intermediate semantic node, causing the taint path to point at an upstream
  node that was not itself defective.
- `unresolved_trace_refs`: a source ref or dataflow endpoint did not resolve to a trace
  node.
- `judge_error`: the attribution judge timed out, failed, or returned an irreparable
  incomplete judgment; the affected node remains `unknown` and the report is inconclusive.
- `analysis_search_limit`: `max_depth` or `max_nodes` stopped traversal before all cited
  upstream nodes were evaluated.
- `unresolved_defect_branch`: one observed defect did not reach a confirmed root episode,
  even if other branches succeeded.

Use this report as the feedback loop between the reasoning module and semantic tracing:
when attribution can only say "the defect is somewhere around LLM generation", the report
spells out which LLM/context/message fields need to be added to future traces.

## Test

```bash
PYTHONPATH=tools/trace_attribution \
python3 -m unittest discover -s tools/trace_attribution/tests
```
