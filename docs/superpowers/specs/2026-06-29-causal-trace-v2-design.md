# Causal Trace v2 Design

## Goal

Build a causal observability layer for observable-opencode that helps benchmark owners inspect why an end-to-end case produced a poor answer. The trace should expose the causal chain across user intent, context selection, compaction, LLM calls, tool/MCP/skill/subagent execution, code changes, verification, and final claims.

## Non-Goals

- Do not preserve the old trace directory or schema as the primary contract.
- Do not perform automatic root-cause diagnosis or judgement.
- Do not store secrets or credentials in trace artifacts.
- Do not require a server to view a completed trace bundle.

## Trace Bundle

Each case writes a bundle:

```text
<trace_root>/<case_id>/
  manifest.json
  causal-trace.json
  records.jsonl
  raw-events.jsonl
  viewer.html
  partial/latest.json
  artifacts/sha256/<hash>.json|txt
```

`manifest.json` is the case-level entry point. It records case id, run id, status, timestamps, input summary, environment, and output file names.

`causal-trace.json` is the main structured artifact. It contains nodes, causal edges, artifact index, metrics, and diagnostic hints.

`records.jsonl` is the write-ahead semantic stream. It records every node, edge, artifact, metric, and finalization record as it happens.

`raw-events.jsonl` is optional low-level runtime event data. It is useful for detailed debugging but is not the primary analysis surface.

`partial/latest.json` is refreshed during execution and on interruption so long-running or cancelled cases still have a usable trace snapshot.

## Causal Model

The core data model is a graph:

```ts
type CausalTrace = {
  trace_version: "2.0"
  manifest: TraceManifest
  nodes: CausalNode[]
  edges: CausalEdge[]
  artifacts: ArtifactIndex[]
  metrics: TraceMetrics
  diagnostics_hints: DiagnosticHint[]
}
```

Node kinds:

- `run.start`: case prompt, model, config, and process entry.
- `task.loop`: agent loop iteration, goal, selected action, and stop reason.
- `context.pack`: final context sent to a model.
- `context.compaction`: compaction trigger, selection, summary, and auto-continue.
- `llm.call`: provider/model request, response, token usage, tool calls, and errors.
- `tool.call`: read/grep/bash/edit/write tool execution.
- `mcp.call`: MCP server, tool, args, returned content summary.
- `skill.load`: skill instructions, hash, and key rules.
- `subagent.call`: parent prompt, child session id, child trace reference, child result.
- `observation`: meaningful fact extracted from files, tools, MCP, skill, verification, or subagent output.
- `change`: repository modifications and diff.
- `verification`: command/test execution, stage, status, and parsed failures.
- `final.claim`: important final-answer claim.

Edges describe causality:

- `context_to_llm`
- `llm_to_tool`
- `tool_to_observation`
- `mcp_to_observation`
- `skill_to_context`
- `subagent_to_observation`
- `verification_to_change`
- `change_to_verification`
- `observation_to_claim`
- `compaction_to_context`

## Component Semantics

Task orchestration records each loop's current goal, known facts, selected action, and completion or continuation reason.

Context management records the exact context package given to each LLM call, including message count, system/tool count, token estimate, selected artifacts, and context source summary.

Compaction records trigger reason, model budget, selected head/tail counts, hidden prior compactions, previous summary, serialized tail, generated summary, auto-continue decision, processor result, and iteration metadata.

Tool calls record arguments, duration, output summary, artifacts, and semantic observations. Verification-like shell commands produce `verification` nodes. Edit/write tools produce `change` nodes.

MCP calls record actual returned content through redacted artifacts and observation nodes. Counting content items is not enough for attribution.

Skill loads record instruction content through artifact-backed summaries, plus a `skill.load` node that can connect to later context or tool choices.

Subagent calls record child session id, input prompt, output, model, child trace reference when available, and observation nodes for returned conclusions.

Final responses are split into one or more `final.claim` nodes, each linked to current concrete evidence references. The first implementation may keep the existing claim chunking but must move it into causal graph nodes.

## Viewer

`viewer.html` should work as a static file. It has four sections:

- Causal Graph: component/record graph with claim-focused evidence chains.
- Timeline: chronological loop, LLM, tool, MCP, skill, subagent, compaction, change, and verification nodes.
- Evidence Inspector: claim-to-evidence view with artifact-backed content.
- Context Analyzer: model context packages and compaction before/after records.

Low-value noise is collapsed by default: title LLM calls, stream deltas, MCP connect, repeated system/tool schema content, and raw low-level events.

Large content is stored in artifacts and linked from the viewer. The viewer may inline small previews only.

## Stability

Trace finalization must not depend only on `beforeExit` or `exit`. The writer handles `SIGINT`, `SIGTERM`, `SIGHUP`, `uncaughtException`, and `unhandledRejection`. Interrupted traces are finalized as `cancelled` or `error`.

All records are written incrementally to `records.jsonl`. `partial/latest.json` is periodically refreshed and also written during finalization.

## Security

All structured payloads and artifacts pass through existing redaction logic. Sensitive keys and bearer/API key patterns are redacted before disk write. Tests must verify that API-key-like strings do not appear in manifest, causal trace, records, viewer, or artifacts.

## Acceptance Criteria

- A basic LLM case shows prompt, context package, LLM call, and final claim.
- A tool-fix case shows failed verification, code change, passed verification, and final claim links.
- An MCP case stores returned content and links it to observations.
- A skill case stores skill instruction content and links it to trace evidence.
- A subagent case records child session id and returned observations.
- A compaction case creates a `context.compaction` node with selection and summary artifacts.
- A SIGINT case still produces `manifest.json`, `causal-trace.json`, `viewer.html`, and `partial/latest.json`.
- Repeated large content is de-duplicated by hash in `artifacts/sha256`.
- Secret-like strings are redacted in text outputs.

