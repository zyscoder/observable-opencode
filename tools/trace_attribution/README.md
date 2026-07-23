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

The compatibility engine remains the default. Select the resumable recursive engine
explicitly when running the LLM-driven recursive hypothesis, investigation, and independent
confirmation flow:

```bash
PYTHONPATH=tools/trace_attribution \
python3 -m trace_attribution \
  --engine recursive-agentic \
  --trace /tmp/case/trace.json \
  --out /tmp/attribution/case.attribution.json \
  --objective "Find why the final answer quality was poor." \
  --analysis-perspective "Improve Agent repository reasoning"
```

The recursive defaults are `--max-frontier-items 96`, `--max-depth 20`,
`--max-hypotheses 24`, `--max-investigation-rounds 12`,
`--max-artifact-bytes 1048576`, and `--max-judge-requests 128`. The analysis perspective
changes deterministic ranking and labels only; it never makes a node eligible or ineligible
as a cause. The legacy defaults remain `--max-depth 8 --max-nodes 48`.

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

### Recursive checkpoint and resume

For `--engine recursive-agentic`, the CLI creates three fsync-backed, append-only
journals plus transactional commit manifests under
`<attribution-output-stem>.checkpoint/`:

```text
case.attribution.checkpoint/
  manifest.json
  commit.json
  frontier.jsonl
  hypotheses.jsonl
  investigation-actions.jsonl
  output-commit.json
```

`frontier.jsonl` preserves queued, in-flight, and completed semantic visits;
`hypotheses.jsonl` preserves candidate-specific hypotheses and transformed defect state;
`investigation-actions.jsonl` preserves Judge, investigation, control, confirmation, budget,
cache identity, and Provider-circuit lifecycle. Every record has an immutable run ID, exact
schema version, journal sequence, globally monotonic transaction sequence, semantic key,
timestamp, previous hash, and record hash. A recursive state checkpoint appends and fsyncs all
three members before atomically publishing `commit.json`, which anchors every committed journal
count and hash. Restore never combines uncommitted members from different snapshots. A deleted
committed tail, mixed run ID, wrong head, interior corruption, reorder, hash-chain break, or stale
configuration stops resume with an explicit error.

A parseable final record without its newline is still treated as incomplete. Initialization
durably repairs its delimiter only when the committed head proves its identity; otherwise it
truncates the crash tail. Complete records beyond the committed heads are also truncated. Repair
events and counts are persisted in `commit.json` and exposed as `metadata.checkpoint_audit`.

Resume by running the exact same command. A custom directory can be selected with
`--checkpoint-dir /path/to/case.checkpoint`. The compatibility fingerprint binds the effective
Trace (including an injected review), objective, start refs, perspective, every recursive budget,
model, effective endpoint, thinking configuration, maximum output tokens, request timeout,
Provider threshold, and Judge cache path. Changing any of those values requires a new checkpoint
directory.

Validated completed Judge and confirmation results, completed investigations, physical/logical
budgets, artifact bytes, and Provider circuit state are replayed without repeating calls. A call
that has only a durable `*_started` record is conservatively closed as unknown: it is never called
again and is never treated as a fabricated success. A bounded Provider allowance is durably
reserved before crossing the Provider boundary. A completed call reconciles that reservation to
the exact physical delta; an in-flight call keeps the reservation consumed and increments
`metadata.judge_request_uncertainty_count`.

SIGINT and SIGTERM use the same graceful behavior. The handler only sets a stop flag and remains
installed through output publication. At the next safe analysis boundary all journals are fsynced
and an explicit inconclusive/partial report is prepared. The attribution and message-lineage files
are both staged and fsynced, then published under the recoverable `output-commit.json` transaction;
only after both files and their directories are durable is `analysis_completed` committed. A crash
between output files or before the completion marker is repaired by the same resume command.
SIGKILL cannot execute a process handler. Recovery is limited to the last journal or output
transaction state already fsynced before the kill; no SIGKILL handler or survival of in-memory work
is claimed.

Recursive outputs are deterministic relative to `--out` unless overridden:

```text
case.attribution.json
case.attribution.message-lineage.json
case.attribution.judge-cache.jsonl
case.attribution.checkpoint/
```

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

## Recursive Attribution Acceptance

Every CLI output projects two independent identities onto recursive facts:

- `semantic_anchor:v2:*` is always content semantics and never depends on graph collision
  count or occurrence position;
- `semantic_occurrence:v1:*` is always relation-aware causal-neighborhood identity.

Both normalize Unicode with NFKC. Human text is case-folded, while POSIX paths and code
identifiers preserve case. Paths are lexically normalized; verified repository roots produce
relative paths, root escapes become explicit unresolved markers, and unverified absolute
paths retain a namespaced full canonical path. Windows paths use a platform-tagged,
case-insensitive form. Full-graph semantic and occurrence collision maps are explicit report
metadata. Adding a duplicate occurrence therefore cannot change an existing semantic anchor.

The nine offline regression fixtures live under
`tests/fixtures/recursive_cases/`. Each fixture has three isolated top-level sections:

- `records` and `dataflow_edges`: factual Causal IR supplied to `TraceGraph` and Judge;
- `scripted_analysis`: deterministic, zero-network Judge responses for algorithm tests;
- `human_labels`: external roots, conditions, amplifiers, forbidden roots, and allowed
  unresolved outcomes.

`load_fixture()` strips the latter two sections before constructing a Trace and rejects
label-shaped keys nested inside facts. Human labels therefore do not enter Judge prompts,
cache keys, checkpoints, or Agent inputs. `scripted_analysis` is an answer table used only
by the **deterministic plumbing suite**. That suite validates traversal, persistence,
projection, role wiring, and evaluator safety; it is excluded from attribution-quality
acceptance.

`recursive-attribution-labels/v3` requires both `semantic_anchor_id` and
`semantic_occurrence_id` for every scored root, condition, amplifier, and forbidden root.
`node_ref` is optional navigation metadata. Acceptance recomputes both identities from the
immutable source Trace and requires an exact binding. Occurrence identity is the scoring key;
the content anchor is retained for cross-run grouping and diagnostics. Consequently, two
same-anchor nodes in different causal neighborhoods remain two independently scored facts,
while repeating the same occurrence is rejected.

`tests.test_recursive_acceptance_review.DeterministicRuleSemanticSmokeTest` is a separate
fixture-phrase rule smoke test. It randomizes refs, component aliases, and insertion order to
exercise plumbing without the scripted ref table. Its keyword rules are fixture-specific and
do not establish answer-table independence or paraphrase invariance. A characterization probe
shows that replacing "constant initialization vector" with the equivalent "fixed IV" wording
causes one expected root to disappear. Only an authorized real LLM Provider suite can satisfy
semantic attribution-quality gates.

Run the fixture and metric suite:

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_recursive_benchmarks -v
```

Score a real recursive report against its immutable source Trace and a separately reviewed
label file:

```bash
python3 tools/trace_attribution/scripts/evaluate_recursive_attribution.py \
  --trace /path/to/case/partial/latest.json \
  --report /tmp/case.recursive.attribution.json \
  --labels /tmp/case.human-labels.json \
  --legacy-report /tmp/case.legacy.attribution.json \
  --out /tmp/case.recursive.comparison.json
```

Acceptance requires `--trace`; report-only scoring is invalid. The evaluator strictly loads
the recursive report and source `TraceGraph`, recomputes anchor and occurrence indexes plus
confirmation identities, and resolves every candidate, root, factor, judgment, confirmation, path hop,
evidence ref, and artifact against source facts. Artifact checks include canonical SHA-256,
owner, path containment, byte range, and actual content. Directed causal paths may contain
only attribution-eligible non-temporal hops.

The `recursive-attribution-comparison/v5` schema reports occurrence-level confirmed-root
recall/precision separately from
introduction-candidate recall/precision, Top-1 (`null` for empty expected roots), explicit
negative-control correctness, signed physical Judge request reduction, request ratio and
signed request delta, mean causal-path length,
factor precision, unknown rate, confirmation rejection, investigation yield, checkpoint
reuse, and human/LLM disagreement. Multi-root recall is a fraction, not any-hit success.
Top-1, role membership, forbidden-root checks, disagreements, and counts all compare causal
occurrences. Separate `semantic_*` recall/precision fields expose content-level diagnostics
but never substitute for occurrence-level acceptance quality.
Recall/precision/rate metrics are bounded to `[0,1]`. Request reduction is signed, request
ratio may exceed 1, and missing request measurements remain `null`.

Evaluation exits nonzero and writes no comparison when the source Trace is absent or when it
finds fabricated/unresolved refs, schema/index mismatch, duplicate occurrence identities,
collision-map mismatch, disconnected paths, invalid artifacts, inconsistent outcome state,
disallowed unresolved outcomes, missing/mismatched independent confirmations, forbidden
roots, or a budget/Provider/unknown branch promoted to a root. Historical report-only
metrics may be documented descriptively, but they are never acceptance evidence.

Neither the scripted matrix nor the rule-based smoke suite contributes to quality acceptance
or default selection. `--engine legacy` remains the default until authorized real Sphinx, Pydantic,
requirements-understanding, context-compaction, and successful-control gates all pass.

The current frozen baseline, exact local artifact hashes, unavailable measurements, and
commands for authorized real runs are recorded in
`docs/superpowers/reports/2026-07-20-agentic-recursive-attribution-results.md`.

## Test

```bash
PYTHONPATH=tools/trace_attribution \
python3 -m unittest discover -s tools/trace_attribution/tests
```
