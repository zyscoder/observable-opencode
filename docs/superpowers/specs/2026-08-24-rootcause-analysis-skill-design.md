# Root-Cause Analysis Skill Design

## Goal

Create a portable `rootcause-analysis` Skill that guides capable Agents such
as Claude Code and OpenCode to answer a user's defect question from a finalized
semantic Trace. The Agent, rather than a fixed attribution state machine, owns
semantic judgment. Deterministic tooling only retrieves and verifies Trace
facts.

## Scope

The Skill accepts two required inputs:

1. an absolute path to a finalized `trace.json`; and
2. a natural-language question describing the behavior that did not meet the
   user's expectation.

It produces both a structured JSON report and a human-readable Markdown
report. The reports identify the observed discrepancy, root-cause candidates,
the confirmed or most likely root cause, the defect propagation path, the
final impact, rejected hypotheses, evidence gaps, and actionable improvements.

The Skill is analysis-only. It may recommend changes, but it must never apply
changes to the observed Agent, Harness, Skill, MCP server, tool, model
configuration, source repository, build environment, Trace bundle, or session.
It must not execute a proposed repair or feed a recommendation back into the
observed system.

The current Python attribution module is paused. This change does not delete,
extend, or call its recursive Judge workflow. Existing Trace capture remains
passive and unchanged.

## Placement And Portability

The canonical Skill is stored at:

```text
.claude/skills/rootcause-analysis/
```

Claude Code discovers project Skills from this location. The current OpenCode
Skill scanner also discovers `.claude/skills/**/SKILL.md`, so one canonical
copy serves both runtimes and avoids duplicate same-name Skills with uncertain
precedence.

The Skill package contains:

```text
.claude/skills/rootcause-analysis/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── trace-structure.md
│   ├── backward-semantic-taint.md
│   ├── causal-analysis-method.md
│   ├── node-semantics.md
│   └── report-schema.md
└── scripts/
    └── trace_query.py
```

`SKILL.md` contains the required workflow and guardrails. Detailed schemas and
node semantics are loaded from `references/` only when needed. The Python
helper uses only the standard library and can execute without loading its
source into the Agent context.

## Responsibility Boundary

### Agent Responsibilities

The Agent must:

- interpret the user's expectation and questioned behavior;
- distinguish an actual defect from a reasonable but unexpected alternative;
- choose analysis starts from the final effect or questioned action;
- inspect candidate nodes and their causal neighborhoods;
- judge whether each node contains a semantic defect;
- judge whether the defect was introduced locally or inherited from upstream;
- maintain and compare multiple hypotheses;
- confirm a root with evidence and a counterfactual test;
- explain how the defect propagated and affected the final result;
- propose evidence-backed optimization options without applying them;
- report uncertainty instead of inventing a root.

The Agent must not:

- edit source files, prompts, Skills, Harness configuration, MCP configuration,
  tool definitions, model settings, or build scripts in the analyzed project;
- invoke tools to repair, rebuild, retest, or otherwise change the analyzed
  execution state;
- modify, annotate in place, or append analysis results to the source Trace;
- resume the observed session or send attribution conclusions to any Agent;
- present a recommendation as an already completed modification.

Read-only commands used to inspect the Trace bundle and referenced source
artifacts are allowed. Any command that can change the analyzed project or its
runtime state is outside this Skill's scope.

### Query Helper Responsibilities

`trace_query.py` must only perform deterministic, read-only operations:

- validate and summarize a finalized Causal IR Trace;
- search records by ID, type, component, status, title, scope, or text through
  resumable pages containing `nodes`, `matched_count`, `returned_count`,
  `offset`, `limit`, `truncated`, and `next_offset`;
- show a hydrated node with canonical `aliases`, `input_refs`, `output_refs`,
  preserved `source_refs` and `artifact_refs`, and all recorded incoming and
  outgoing edges with eligibility metadata; traversal alone filters to
  eligible edges;
- list bounded upstream and downstream neighborhoods;
- find bounded backward paths over attribution-eligible recorded edges;
- read referenced artifacts relative to the Trace bundle only after validating
  a valid declared SHA-256 from `content_hash` or compatible `hash`, and fail
  closed when it is absent, malformed, conflicting, or mismatched;
- emit compact JSON suitable for Agent context.

The helper must not classify defects, rank root causes, infer an unrecorded
edge, call an LLM, modify Trace files, or feed any result into the observed
Agent session.

`references/backward-semantic-taint.md` is normative guidance for Agent
reasoning. It describes how to investigate and confirm a root cause, but it is
not executable policy and does not impose fixed candidate counts, traversal
depths, or defect taxonomies.

## Trace Input Contract

The preferred input is the logical case root `trace.json` produced by
`observable-trace finalize`. The Skill rejects HTML and physical segment
journals as canonical inputs and explains how to finalize them.

The helper supports the current canonical and compatibility fields:

- `nodes[]` and `edges[]` from Causal IR;
- `records[]` and `dataflow_edges[]` from the formal compatibility projection;
- top-level `artifacts[]` and record-level Artifact references;
- typed `aliases`, `input_refs`, `output_refs`, `source_refs`, `artifact_refs`,
  legacy aliases, and record IDs; compatibility fields may be empty when the
  source projection does not contain them;
- `manifest`, lifecycle state, diagnostics, and metrics.

Every returned fact preserves its source node, edge, Artifact, or manifest
reference. Temporal adjacency alone is never treated as a causal edge.

## Analysis Method

### 1. Bind The Question

Extract four separate statements:

- the user's expected behavior;
- the actual observed behavior;
- the discrepancy being questioned;
- the final impact, if the user states one.

Do not silently replace a behavioral question with a generic failure such as
"verification was missing." For example, a question about omitted Yocto usage
must remain about tool or Skill selection even if GCC compilation succeeded.

### 2. Establish The Premise

Verify that the expectation and actual behavior have Trace evidence. Classify
the premise as `supported`, `contradicted`, or `unknown`. A reasonable fallback
can still violate an explicit user instruction; conversely, an unexpected
action is not automatically a defect.

### 3. Select Analysis Starts

Start from nodes that directly represent the questioned action or final
effect, such as the final answer, failed verification, changed file, omitted
expected action lifecycle, tool result, or exit decision. Record why each
start was selected.

### 4. Perform Recursive Backward Semantic Taint Analysis

For each current node:

1. describe what the node means in the task, not only its event type;
2. decide whether its semantic output is defective relative to the bound
   expectation;
3. identify the exact defective information, decision, action, or omission;
4. inspect explicit upstream sources and eligible incoming edges;
5. decide whether the defect was inherited, amplified, transformed, or
   introduced locally;
6. recurse into plausible upstream introducers;
7. backtrack when evidence contradicts the current hypothesis.

An event type is never a root cause by itself. A root candidate needs a node,
the faulty semantic content or omission, the governing expectation, and an
explanation of why upstream evidence does not already contain the same defect.

#### Semantic Taint States

The Agent classifies the defect's relationship to each inspected node as one
of these states:

- `absent`: the node's semantic input and output do not contain the questioned
  defect;
- `inherited`: the node forwards a defect already present upstream without a
  material semantic change;
- `transformed`: the node changes the form or meaning of an upstream defect;
- `amplified`: the node increases the defect's scope, severity, or downstream
  consequence;
- `introduced`: the relevant inputs are not defective, but this node first
  produces the defective decision, content, action, or omission;
- `blocked`: the node prevents an upstream defect from propagating further;
- `unknown`: the available Trace does not support a reliable judgment.

These states describe a hypothesis about semantic propagation. They must cite
facts and may change when later evidence contradicts the current branch.

For `blocked`, terminate that upstream propagation branch for the current
effect, record the blocker and its evidence, then inspect downstream for an
independent reintroduction. A blocker does not prove all later outputs clean.

#### Per-Node Judgment

For every node inspected as part of a candidate chain, the Agent records:

1. which component acted and what the node means in the task;
2. which question-relevant inputs the component received;
3. which semantic output, action, or omission it produced;
4. whether that result violated a user expectation, task constraint, or
   upstream contract;
5. whether the same defect was already present in an eligible upstream source;
6. the semantic taint state and reasoning;
7. how the output reached or influenced the next downstream node;
8. whether correcting this node would block or materially reduce the final
   effect.

Raw event names such as `tool.result` are insufficient descriptions. Reports
translate them into task-level language, for example: "the build command
returned a GCC-only success result, which the next decision treated as adequate
verification even though the user required Yocto."

#### Recursive Worklist And Backtracking

The Agent maintains an investigation ledger rather than one irreversible
path. Each work item contains the current node, downstream effect, proposed
taint state, candidate upstream refs, supporting evidence, contradictory
evidence, and open questions.

The Agent may generate multiple hypotheses from task semantics and Trace
facts. It explores the most explanatory evidence-backed branch first, but
retains alternatives until they are disproved or made redundant by a stronger
root. When a branch encounters contradictory evidence, an ineligible edge, or
an upstream node without the proposed defect, it backtracks and inspects the
next candidate. Retrieval limits are operational batches, not analysis limits:
the Agent can request another bounded neighborhood until the hypothesis is
resolved or the Trace evidence is exhausted.

The method must not use fixed `max_depth`, fixed root-candidate budgets, or
first-match stopping rules as semantic completion criteria. It may use bounded
query batches to protect context, provided the ledger records unexplored
candidates and the Agent continues when they could change the verdict.

#### Missing-Action Taint

An omitted Skill, MCP, tool, Subagent, build system, or verification action has
no invocation node. The Agent represents it as an evidence-backed semantic
break, not as a fabricated runtime node:

1. establish the expected action from the user request, governing contract,
   catalog, context, or loaded Skill description;
2. establish the execution window in which the action could have occurred;
3. identify the last recorded stage where the expectation remained available;
4. identify the first decision, selection, action-generation, or exit stage
   that failed to preserve or realize it;
5. inspect upstream evidence to distinguish missing context, unavailable
   capability, weak trigger, selection error, action-generation error,
   execution failure, and unconsumed result;
6. cite the before and after anchors that prove the absence.

The absence of an invocation is not itself enough to blame the Agent. If the
trigger existed only in an unexposed Skill body, the evidence instead points
to Skill discoverability or description design. If the requirement and a clear
trigger were exposed but no call was produced, the first unsupported
post-exposure decision becomes a root candidate.

#### Algorithm Outline

```text
bind question -> establish premise -> choose observed-effect starts

worklist = candidate starts
hypotheses = initial explanations grounded in task semantics

while a potentially verdict-changing work item remains:
    describe the current node in task language
    judge semantic defect against the bound expectation

    if defect is absent or branch evidence is contradicted:
        reject or revise this hypothesis; backtrack

    if defect is inherited, transformed, or amplified:
        inspect eligible upstream sources
        enqueue plausible upstream introducers

    if defect is introduced:
        test evidence-backed reachability to the final effect
        test whether stronger upstream introducers exist
        test the correction counterfactual
        compare competing hypotheses
        confirm only if all root criteria hold

    if evidence is unknown:
        retrieve another bounded neighborhood or record an evidence gap

emit confirmed/probable/inconclusive/no_defect
```

The Agent controls hypothesis generation, semantic judgments, retrieval order,
and explanation. The helper only supplies immutable facts requested during the
loop.

### 5. Confirm The Root

Confirm a root only when all of these hold:

- the node is on an evidence-backed path to the observed effect;
- the node introduces or independently causes the defect;
- no stronger recorded upstream introducer explains it;
- a counterfactual correction at this node would plausibly prevent or
  materially reduce the downstream defect;
- competing hypotheses have been considered.

Use `confirmed`, `probable`, `inconclusive`, or `no_defect`. Never turn missing
evidence into a confirmed root.

Independent causes remain separate roots. A later node that merely propagates
an earlier defect is part of the causal chain, not a duplicate root. A later
node may be an additional root only when it independently introduces a defect
that is necessary or materially aggravates the final effect.

### 6. Explain Propagation And Impact

Describe each transition in plain language:

- what the upstream component produced;
- what the downstream component consumed or omitted;
- what changed semantically across the transition;
- why that caused the next defect;
- which node and edge prove the transition.

The final impact must connect to the user's question. Runtime success does not
erase instruction-following, architecture, safety, or completeness defects.

## Report Contract

The Agent writes sibling files to a user-specified output prefix. When no
prefix is supplied, it writes outside the immutable Trace bundle to
`<trace-parent>/rootcause-analysis/<case-dir-name>/analysis.{json,md}`.

The JSON report contains:

```json
{
  "schema_version": "rootcause-analysis/v1",
  "question": {},
  "trace_binding": {},
  "verdict": {},
  "root_causes": [],
  "causal_chain": [],
  "final_impact": {},
  "rejected_hypotheses": [],
  "evidence_gaps": [],
  "recommendations": [],
  "evidence_index": []
}
```

Every recommendation is structured as:

```json
{
  "recommendation_id": "REC-001",
  "target": "agent|harness|skill|mcp|tool|prompt|context|trace|code|process",
  "owner": "responsible subsystem or team",
  "priority": "critical|high|medium|low",
  "problem_addressed": "root cause or contributing factor reference",
  "proposed_change": "what should be changed, without applying it",
  "rationale": "why the change addresses the evidence-backed cause",
  "expected_effect": "observable behavior expected after the change",
  "risks": [],
  "validation_suggestion": "how a future owner could verify the change",
  "evidence_refs": []
}
```

Recommendations must distinguish root-cause corrections from resilience,
observability, and process improvements. They must identify an owner and cite
the evidence that motivates the proposal. The report may suggest a future
validation procedure, but the Skill does not execute that procedure.

Each causal-chain step includes a plain-language explanation and structured
`node_refs`, `edge_refs`, and `artifact_refs`. Each important Markdown claim
includes bracketed evidence IDs that resolve through `evidence_index`, so the
human and machine-readable reports can be checked against one another.

The Markdown report presents:

1. conclusion in plain language;
2. expectation versus actual behavior;
3. root cause and responsibility owner;
4. numbered defect propagation narrative;
5. final impact;
6. rejected alternatives;
7. evidence gaps and confidence;
8. prioritized corrective actions;
9. evidence index.

The Markdown report ends with an explicit statement that no proposed change
was applied and that all recommendations require separate review and execution.

## Failure And Uncertainty Handling

- Missing or stale `trace.json`: stop and provide the exact finalize command.
- Invalid JSON or unsupported shape: report validation errors without editing
  the input.
- Missing Artifact: preserve the reference and mark the semantic evidence as
  unavailable.
- Broken or ineligible edge: do not traverse it as causal evidence.
- Excessively large Trace: use iterative bounded retrieval and keep an
  investigation ledger; do not load the entire document into model context.
- Multiple independent causes: report each root and the chain it explains.
- Insufficient evidence: report `inconclusive` with the exact missing facts and
  suggested future Trace instrumentation.
- A user asks the Skill to apply a proposed fix: keep the current run
  analysis-only, finish the report, and state that implementation requires a
  separate task outside the root-cause analysis Skill.

## Generalization Requirements

The Skill must work across at least these defect families without hard-coded
answers:

- incorrect or incomplete final answer;
- context contamination across turns or resumed sessions;
- code edits that violate an explicit invariant;
- omitted Skill, MCP, tool, Subagent, or verification action;
- incorrect tool or build-system selection;
- ignored user correction or stale Artifact reuse;
- failed compilation, test, or command execution;
- reasonable fallback that still violates an explicit expectation.

## Verification Strategy

Verification has four layers:

1. validate Skill metadata and package structure with the official Skill
   validator;
2. test the query helper against synthetic Causal IR fixtures, including
   compatibility records, eligible and ineligible edges, aliases, Artifacts,
   malformed input, and large bounded traversal;
3. run baseline pressure prompts without the Skill and document failures such
   as generic failure substitution, temporal-causality invention, premature
   root confirmation, and unreadable event-type narration;
4. rerun representative pressure prompts with the Skill and check that the
   reports bind the user's question, preserve evidence references, explain the
   propagation chain, and remain inconclusive when evidence is insufficient.

Forward testing must include both a known-root fixture and a deliberately
ambiguous fixture. Passing deterministic script tests alone does not establish
that the Skill teaches the Agent to reason correctly.
