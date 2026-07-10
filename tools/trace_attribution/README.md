# Trace Attribution

Offline causal-chain attribution for observable-opencode semantic traces.

The module reads an existing `trace.json`, builds a reverse dependency graph from
`source_refs` and `dataflow_edges`, then uses Claude through the Anthropic Python SDK
to perform backward semantic taint analysis:

1. Decide whether the current node/component contains a semantic defect.
2. If defective, decide whether the defect was caused or propagated by upstream nodes.
3. Continue backward until a defective node has no defective upstream cause; that node is
   reported as a root-cause candidate.

The module is offline with respect to opencode execution. It never writes back to trace
files and never feeds attribution results back into the agent.

Each attribution JSON also includes `trace_improvement_report`. This report describes
where backward taint analysis became weak or blocked, which trace facts were missing,
and which component should emit richer semantics in the next trace iteration.

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
--judge-max-tokens 4096
--judge-timeout-sec 60
--max-depth 8
--max-nodes 48
--model claude-sonnet-4-5
```

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
backward semantic taint analysis.

Stress-case reviews that report `missing_semantics` also inject offline-only
`case.missing_semantic` nodes. Runtime traces may additionally contain passive
`case.observed_defect` / `case.missing_semantic` records for health findings such as
"repository changed but no final test result was observed". These records are derived
after the agent run and are never fed back into the agent.

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
- `judge_error`: the attribution judge timed out or failed on a node; the analyzer keeps
  the node as a low-confidence boundary candidate and still writes a partial report.

Use this report as the feedback loop between the reasoning module and semantic tracing:
when attribution can only say "the defect is somewhere around LLM generation", the report
spells out which LLM/context/message fields need to be added to future traces.

## Test

```bash
PYTHONPATH=tools/trace_attribution \
python3 -m unittest discover -s tools/trace_attribution/tests
```
