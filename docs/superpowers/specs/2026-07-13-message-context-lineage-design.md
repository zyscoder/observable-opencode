# Message Context Lineage Design

## Goal

Reconstruct the complete offline message and turn lineage of an observable-opencode run so backward attribution can distinguish defect evidence, propagation, and introduction without changing or feeding information back into agent execution.

## Scope

The reconstruction consumes only existing passive trace material:

- `trace.json`, `records.jsonl`, and `raw-events.jsonl`;
- artifact payloads under `artifacts/sha256`;
- prompt, context, compaction, LLM, decision, tool, MCP, skill, subagent, verification, change, response, and exit records.

It does not replay an LLM request, modify a prompt, alter a tool result, affect task-loop control flow, or write derived findings back into the agent session.

## Architecture

### Deterministic identity index

Every record is indexed by the identifiers it actually carries: `sessionID`, `messageID`, `partID`, `callID`, `spanID`, `decisionID`, context snapshot ID, artifact ID, payload hash, and timestamp. Direct IDs and explicit trace edges are authoritative.

### Agent turn reconstruction

Records sharing a session and assistant message are grouped into an `AgentTurn`. A turn exposes its LLM calls, reasoning decisions, action decisions, tool calls, tool results, response outputs, and relationship to the previous turn.

Within one message, an earlier `reasoning_block` is linked to a later `llm_tool_call` or execution decision. Existing decision-to-tool and tool-to-result edges remain authoritative.

### Message snapshot lineage

The reconstructor emits normalized snapshots for these stages when observed:

1. prompt assembly;
2. session messages;
3. context selection;
4. compaction input and output;
5. model-message construction;
6. provider-message transformation;
7. final LLM request.

Snapshots store counts, hashes, artifact references, transformation names, and compact structural changes. Large message bodies remain in artifacts.

### Content-backed retention

When an LLM request artifact contains the exact normalized text of an earlier decision, the reconstructor emits `retained_in_context`. Matching uses normalized text fingerprints and is limited to decisions that occurred before the request. It is not a semantic similarity guess.

### Evidence tiers

Each reconstructed edge records one of three evidence tiers:

- `confirmed`: explicit ID, source ref, dataflow edge, call ID, or artifact hash;
- `content_matched`: exact normalized text or stable content fingerprint;
- `temporal_inferred`: same-session event ordering without direct identity/content proof.

Only confirmed and content-matched edges are eligible for automatic backward attribution. Temporal edges are displayed and reported, but cannot establish a root cause.

## Causal Roles

Every model node judgment uses exactly one causal role:

- `defect_introduction`: this node first introduced the relevant defect;
- `defect_propagation`: this node carried an already introduced defect;
- `defect_evidence`: this node truthfully exposed or measured a defect;
- `non_defective`: this node did not contain the relevant defect;
- `unknown`: the trace is insufficient to classify the node.

A root cause must be `defect_introduction`, explicitly marked as a root candidate, and have no earlier defective cause. A failed verification or faithful tool result can never become a root solely because its payload contains a failure.

`case.missing_semantic` is an observability gap, not evidence that every cited code change is defective. It remains an inconclusive analysis boundary and is handled by the trace-improvement report.

## Output

The CLI writes a separate message-lineage JSON next to the attribution report. It contains:

- normalized turns;
- message snapshots;
- reconstructed edges with evidence tier and confidence;
- reconstruction statistics and unresolved/ambiguous associations.

The attribution report records the lineage summary and uses only attribution-eligible reconstructed edges.

## Old-Trace Acceptance Gate

Before a new benchmark case is run, the prior Pydantic trace must satisfy all of the following:

1. the broad pytest call/result is not reported as a root cause;
2. `case.missing_semantic` does not create code-change root causes;
3. `decisionnode_dec_1093_ee253ed8` is reachable from the final LLM request or its accepted narrow verification path;
4. the analyzer visits `dec_1093` when tracing premature completion;
5. temporal-only edges are not sufficient to promote a root cause;
6. no Ground Truth root label is injected into the graph.

## Error Handling

Missing artifacts, malformed snapshots, ambiguous message ownership, and truncated payloads are recorded as reconstruction gaps. They never create a causal edge with confirmed status and never fabricate a root cause.

## Performance

Record and identifier indexes are built once. Full artifact content is read only for final-response LLM requests and other requests selected for attribution. Decision matching uses normalized fingerprints and retains only recent matching decisions per request. The judge receives the active backward subgraph rather than the full session history.

## Compatibility

Existing trace files remain unchanged. Existing `defect_status` and `has_defect` fields remain available, while `causal_role` becomes the authority for root eligibility. Legacy judgments are mapped conservatively: an explicit legacy root becomes `defect_introduction`; a present non-root without upstream cause becomes `unknown`, not a fabricated root.
