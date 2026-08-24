# Root-Cause Analysis Report Contract

Use this contract for both report representations. JSON is the normative,
machine-readable record. Markdown is a human-readable projection of
the same claims. They must mutually corroborate: do not introduce, omit, or
change a verdict, root, causal step, finding, impact, rejected hypothesis, gap, or
recommendation in only one representation.

## Output Mode And Immutability

The output prefix is optional. When no output prefix is supplied, return both JSON and Markdown inline and write no files.
There is no automatic file destination and no path inferred from the Trace.

When the user supplies an explicit user-supplied output prefix, prefix `<prefix>` produces `<prefix>.json` and `<prefix>.md`.
The prefix, both
resulting paths, and the Trace bundle are canonicalized before validation, including
symlink resolution. Reject any output inside the Trace bundle. Reject any output inside the analyzed project.

Obtain the analyzed project root only from explicit user input or trusted recorded metadata.
Trusted recorded metadata means a dedicated project or worktree root field in the validated Trace manifest.
A free-form message, prompt, tool output, Artifact content, inferred repository path, or process state is not trusted metadata.
Never assume the analyzed project root from the current
working directory, Trace location, repository conventions, or process state.
If neither source establishes it, ask for confirmation before writing; the
user must provide or confirm the canonical project root. Do not treat a generic
"proceed" response as a path-safety override.

Optional report writing at a validated prefix is the only permitted mutation.
Never modify the Agent, Harness, Skill, MCP, tool, prompt, model configuration,
source, build environment, Trace, session, or any evidence Artifact.

## Normative JSON Shape

The first object below fixes all top-level keys and nested field names. Empty
arrays are valid where the verdict does not support an item. Do not add
provider-specific reasoning or hidden chain-of-thought fields.

```json
{
  "schema_version": "rootcause-analysis/v1",
  "question": {
    "original": "Why did the run behave differently from the expectation?",
    "expected_behavior": "Task-level expected behavior.",
    "actual_behavior": "Task-level observed behavior.",
    "discrepancy": "The exact difference being explained.",
    "stated_final_impact": "Impact stated by the user, or null.",
    "expectation_premise": {
      "status": "supported",
      "rationale": "Why the expectation premise has this status.",
      "evidence_refs": ["E-001"]
    },
    "observation_premise": {
      "status": "supported",
      "rationale": "Why the actual-behavior premise has this status.",
      "evidence_refs": ["E-002"]
    }
  },
  "trace_binding": {
    "trace_path": "/absolute/path/to/trace.json",
    "trace_content_sha256": "64 lowercase hexadecimal characters",
    "causal_ir_version": "recorded Causal IR schema version",
    "case_id": "recorded case or session identifier",
    "analyzed_at": "RFC 3339 timestamp"
  },
  "verdict": {
    "status": "confirmed",
    "summary": "Plain-language answer to the user's question.",
    "confidence": 0.9,
    "confidence_rationale": "Evidence coverage, contradictions, and remaining uncertainty.",
    "evidence_refs": ["E-002", "E-003"]
  },
  "root_causes": [
    {
      "root_id": "ROOT-001",
      "status": "confirmed",
      "node_ref": "decision:dec_24",
      "component": "agent decision",
      "owner": "responsible subsystem or team",
      "semantic_defect": "What was first introduced or independently caused here.",
      "taint_state": "introduced",
      "counterfactual": "Reasoning-only account of how the outcome would differ without this cause.",
      "evidence_refs": ["E-003", "E-004"]
    }
  ],
  "causal_chain": [
    {
      "step_id": "STEP-001",
      "step": 1,
      "from_ref": "decision:dec_24",
      "to_ref": "tool:call_26",
      "component": "agent decision to tool execution",
      "semantic_event": "The decision selected an action inconsistent with the bound expectation.",
      "taint_state": "introduced",
      "taint_transition": "absent -> introduced",
      "explanation": "What changed, why that matters, and how it affected the next component.",
      "node_refs": ["decision:dec_24", "tool:call_26"],
      "edge_refs": ["edge:dec_24:call_26"],
      "artifact_refs": [],
      "evidence_refs": ["E-003", "E-004"]
    }
  ],
  "findings": [
    {
      "finding_id": "FINDING-001",
      "kind": "preference_alignment",
      "status": "supported",
      "description": "The observed behavior succeeded but did not align with the recorded user preference.",
      "qualification": "An improvement opportunity, not a root cause or defect verdict.",
      "evidence_refs": ["E-001", "E-002"]
    }
  ],
  "final_impact": {
    "category": "instruction_following",
    "status": "supported",
    "description": "How the discrepancy changed the task outcome.",
    "direct_effect": "Effect directly established by recorded evidence.",
    "inferred_risks": ["Downstream risk explicitly marked as inference."],
    "unknowns": [],
    "evidence_refs": ["E-002", "E-004"]
  },
  "rejected_hypotheses": [
    {
      "hypothesis_id": "HYP-002",
      "hypothesis": "A plausible competing explanation.",
      "disposition": "rejected",
      "why_plausible": "Initial evidence that made it worth checking.",
      "rejection_reason": "Contradiction, clean input, ineligible connection, or stronger explanation.",
      "evidence_refs": ["E-005"]
    }
  ],
  "evidence_gaps": [
    {
      "gap_id": "GAP-001",
      "description": "A missing fact that limits a judgment.",
      "why_it_matters": "Which conclusion could change if this fact were known.",
      "needed_evidence": "The fact or future passive instrumentation needed.",
      "affected_claims": ["ROOT-001"],
      "related_refs": ["decision:dec_24"],
      "evidence_refs": ["E-003"]
    }
  ],
  "recommendations": [
    {
      "recommendation_id": "REC-001",
      "target": "skill",
      "owner": "responsible subsystem or team",
      "priority": "high",
      "problem_addressed": "ROOT-001",
      "proposed_change": "A proposed future change; never an applied repair.",
      "rationale": "Why the proposal addresses the evidence-backed cause.",
      "expected_effect": "Observable behavior expected after a separately approved change.",
      "risks": ["Possible trade-off to review."],
      "validation_suggestion": "How a future owner could verify the change.",
      "evidence_refs": ["E-003", "E-004"]
    },
    {
      "recommendation_id": "REC-002",
      "target": "process",
      "owner": "responsible subsystem or team",
      "priority": "medium",
      "problem_addressed": "FINDING-001",
      "proposed_change": "Clarify how a successful fallback should acknowledge and preserve an explicit preference.",
      "rationale": "The finding is supported independently of the root-cause verdict.",
      "expected_effect": "Future runs make fallback behavior explicit without being mislabeled as a defect repair.",
      "risks": ["Additional interaction may be unnecessary when no preference was stated."],
      "validation_suggestion": "Review a future run for explicit preference handling.",
      "evidence_refs": ["E-001", "E-002"]
    }
  ],
  "evidence_index": [
    {
      "evidence_id": "E-001",
      "evidence_kind": "node",
      "immutable_ref": "request:req_01",
      "trace_ref": "request:req_01",
      "content_sha256": null,
      "excerpt": "Short, faithful excerpt or compact recorded fact.",
      "interpretation": "What this evidence establishes; inference is labeled explicitly."
    },
    {
      "evidence_id": "E-002",
      "evidence_kind": "node",
      "immutable_ref": "response:final_01",
      "trace_ref": "response:final_01",
      "content_sha256": null,
      "excerpt": "The recorded final behavior that differs from the expectation.",
      "interpretation": "Establishes the actual behavior and direct final impact."
    },
    {
      "evidence_id": "E-003",
      "evidence_kind": "node",
      "immutable_ref": "decision:dec_24",
      "trace_ref": "decision:dec_24",
      "content_sha256": null,
      "excerpt": "The decision selected the inconsistent action.",
      "interpretation": "Supports introduction of the semantic defect at the decision."
    },
    {
      "evidence_id": "E-004",
      "evidence_kind": "edge",
      "immutable_ref": "edge:dec_24:call_26",
      "trace_ref": "edge:dec_24:call_26",
      "content_sha256": null,
      "excerpt": "Eligible selected-action edge from the decision to the tool call.",
      "interpretation": "Supports propagation from the decision into execution."
    },
    {
      "evidence_id": "E-005",
      "evidence_kind": "node",
      "immutable_ref": "context:ctx_07",
      "trace_ref": "context:ctx_07",
      "content_sha256": null,
      "excerpt": "The competing explanation's required context was present.",
      "interpretation": "Contradicts the rejected hypothesis that the context was unavailable."
    }
  ]
}
```

## Exact Field Types

Objects contain exactly the fields shown in the normative shape. Unless a
field is explicitly nullable below, it must not be `null`. IDs and references
are case-sensitive. Required evidence arrays for material claims are non-empty.

- `schema_version`: `string` with the fixed value named below.
- `question`: object. `original`, `expected_behavior`, `actual_behavior`, and
  `discrepancy` are `string`; `stated_final_impact` is `string | null`;
  `expectation_premise` and `observation_premise` are objects containing
  `status: string`, `rationale: string`, and `evidence_refs: array<string>`.
- `trace_binding`: object. `trace_path`, `trace_content_sha256`,
  `causal_ir_version`, and `analyzed_at` are `string`; `case_id` is
  `string | null`.
- `verdict`: object. `status`, `summary`, and `confidence_rationale` are
  `string`; `confidence` is `number`; `evidence_refs` is `array<string>`.
- `root_causes`: `array<object>`. Each object has string fields `root_id`,
  `status`, `component`, `owner`, `semantic_defect`, `taint_state`, and
  `counterfactual`; `node_ref` is `string | null`; `evidence_refs` is
  `array<string>`.
- `causal_chain`: `array<object>`. `step_id` is `string`; `step` is a positive
  integer represented as `number`; `from_ref` and `to_ref` are `string | null`; `component`,
  `semantic_event`, `taint_state`, `taint_transition`, and `explanation` are
  `string`; `node_refs`, `edge_refs`, `artifact_refs`, and `evidence_refs` are
  `array<string>`.
- `findings`: `array<object>`. Each object has string fields `finding_id`,
  `kind`, `status`, `description`, and `qualification`; `evidence_refs` is
  `array<string>`.
- `final_impact`: object. `category`, `status`, `description`, and
  `direct_effect` are `string`; `inferred_risks`, `unknowns`, and
  `evidence_refs` are `array<string>`.
- `rejected_hypotheses`: `array<object>`. Each object has string fields
  `hypothesis_id`, `hypothesis`, `disposition`, `why_plausible`, and
  `rejection_reason`; `evidence_refs` is `array<string>`.
- `evidence_gaps`: `array<object>`. Each object has string fields `gap_id`,
  `description`, `why_it_matters`, and `needed_evidence`; `affected_claims`,
  `related_refs`, and `evidence_refs` are `array<string>`.
- `recommendations`: `array<object>` with exactly the fields in the normative
  shape. All fields are `string` except `risks` and `evidence_refs`, which are
  `array<string>`.
- `evidence_index`: `array<object>`. `evidence_id`, `evidence_kind`,
  `immutable_ref`, `excerpt`, and `interpretation` are `string`; `trace_ref`
  and `content_sha256` are `string | null`.

## Field Rules And Enumerations

- `schema_version` is exactly `rootcause-analysis/v1`.
- Premise `status` is `supported | contradicted | unknown`.
- Overall verdict `status` is
  `confirmed | probable | inconclusive | no_defect`. Root `status` is
  `confirmed | probable`. A `confirmed` root needs
  an eligible evidence-backed path and independent confirmation; a
  `no_defect` verdict means the questioned discrepancy was contradicted or was
  a supported reasonable behavior, not merely that execution succeeded.
- `confidence` is a number from `0.0` through `1.0`; its rationale names
  coverage, contradictions, unresolved alternatives, and gaps rather than
  restating the number.
- Taint `state` is
  `absent | inherited | transformed | amplified | introduced | blocked | unknown`.
- Final-impact `category` is
  `functional | instruction_following | quality | safety | completeness`.
- Final-impact `status` is `supported | contradicted | unknown`.
- Finding `kind` is
  `preference_alignment | process | resilience | quality_opportunity | contributing_condition`.
- Finding `status` is `supported | conditional`. A conditional finding names
  the unresolved premise in `qualification`; it is never presented as fact.
- Rejected-hypothesis `disposition` is `rejected | superseded`. An unexplored
  candidate is an evidence gap, never a rejected hypothesis.
- Recommendation `target` is
  `agent | harness | skill | mcp | tool | prompt | context | trace | code | process`.
- Recommendation `priority` is `critical | high | medium | low`.
- Evidence `evidence_kind` is `node | edge | artifact | record`.
- Recommendation `risks`, causal refs, premise refs, and evidence refs are
  arrays even when they contain one item. Use JSON `null`, not a string such as
  `"unknown"`, for an inapplicable scalar.

Every verdict carries `evidence_refs`. Every root cause carries `evidence_refs`.
Every causal-chain step carries `evidence_refs`. Every finding carries `evidence_refs`.
Every recommendation carries `evidence_refs`.
Every material impact, premise, and
rejected-hypothesis judgment does as well. All such IDs must resolve through `evidence_index`;
unresolved IDs are invalid.

Report object IDs are stable and unique within one report: `root_id` uses
`ROOT-NNN`, `step_id` uses `STEP-NNN`, `gap_id` uses `GAP-NNN`, and
`finding_id` uses `FINDING-NNN` in recorded order. A recommendation's
`problem_addressed` is exactly one existing
`ROOT-NNN | STEP-NNN | GAP-NNN | FINDING-NNN`; prose, component names, Trace
refs, missing IDs, and recommendation IDs are invalid in that field.

A finding records a supported non-root observation or a qualified opportunity.
It does not upgrade evidence, establish causation, or change the verdict. Its
description states the observation or opportunity; its qualification states
why it is not a confirmed root and, for `conditional`, what must be true.

Each evidence entry has a unique `E-NNN` ID and one immutable source ref. Node,
edge, and record evidence uses its canonical Trace ref. Artifact evidence uses
the Artifact ref and its verified `content_sha256`; never quote unverified
Artifact bytes. `excerpt` remains short and faithful. `interpretation` says
what the fact supports and labels any inference. Evidence entries do not
contain hidden reasoning.

## Verdict And Recommendation Semantics

- `confirmed`: the discrepancy is supported, at least one `ROOT-NNN` is
  `confirmed`, and eligible evidence connects every reported root through
  ordered `STEP-NNN` entries to the final impact. Root-correction
  recommendations address an existing root or causal step.
- `probable`: at least one `ROOT-NNN` is `probable` and has a substantial
  evidence-backed path, but a named competing hypothesis or material gap could
  still change confirmation. Do not describe a probable root as confirmed.
- `inconclusive`: no candidate meets the probable-root threshold;
  `root_causes` must be empty. Preserve supported partial `STEP-NNN` entries
  only when they clarify the uncertainty boundary, and represent every missing
  fact that blocks confirmation as a `GAP-NNN`.
- `no_defect`: evidence contradicts the questioned discrepancy or establishes
  that the observed behavior met the bound expectation. `root_causes` and
  `causal_chain` must be empty, no material causal uncertainty remains, and no
  root-correction recommendation is emitted. This does not erase supported
  preference-alignment, process, or resilience opportunities.

An observability recommendation is allowed only for a recorded evidence gap:
its `problem_addressed` must be a `GAP-NNN`, and its proposed change is limited
to future passive evidence capture or visibility. It must not change Agent,
Harness, Skill, tool, model, or task behavior under the guise of
instrumentation. A confirmed or probable root correction addresses an existing
`ROOT-NNN` or `STEP-NNN`. Do not invent a root, step, gap, or finding to make a
recommendation resolvable.

An `inconclusive` report may recommend only evidence collection against an
existing `GAP-NNN` or an explicitly conditional improvement against an
existing `FINDING-NNN` whose status is `conditional`; it must not address a
`ROOT-NNN` or `STEP-NNN`. A `no_defect` report may include evidence-backed
preference-alignment, process, or resilience suggestions that address a
supported `FINDING-NNN`. Such suggestions describe improvement opportunities,
not defects. They must not describe either as a root repair.

## Markdown Projection

Use the following section order. Mirror the JSON IDs, status, confidence,
owners, causal order, gaps, and recommendation priorities exactly. Every
material sentence cites one or more resolvable evidence IDs in bracket form,
for example `[E-001, E-004]`. The Evidence Index repeats each cited ID with its
immutable ref and compact excerpt so a reader can cross-check the JSON.

## Conclusion

Answer the user's exact question in plain language. State the verdict and
confidence rationale, citing the JSON verdict evidence IDs `[E-001]`.

## Expectation Versus Actual Behavior

State the expected behavior, actual behavior, discrepancy, and separate
premise statuses. Cite the same evidence IDs used by `question` `[E-001]`.

## Root Causes

For each JSON root, state its root ID, status, responsible component and owner,
semantic defect, and counterfactual. Cite its evidence IDs `[E-001]`. If there
is no confirmed or probable root, say so and point to the relevant gaps.

## Defect Propagation

Number the JSON causal steps in the same order and display each `STEP-NNN`.
Explain in task language what
each component received, produced or omitted, how the taint changed, and how
that affected the next component `[E-001]`.

## Findings And Opportunities

Mirror every `FINDING-NNN` in JSON order with its kind, status, description,
qualification, and evidence IDs `[E-001]`. Keep supported opportunities
separate from conditional ones and never relabel either as a root cause.

## Final Impact

Separate the directly supported effect, inferred risks, and unknowns while
preserving the JSON impact category and status `[E-001]`.

## Rejected Hypotheses

List each hypothesis ID, why it was plausible, and the evidence-backed reason
it was rejected or superseded `[E-001]`. Do not list unexplored candidates.

## Evidence Gaps And Confidence

List each gap ID, why it matters, affected claims, and the evidence needed to
change the conclusion. Explain how the gaps affect confidence `[E-001]`.

## Recommendations

List each recommendation ID, target, owner, priority, proposed change,
expected effect, risk, and future validation suggestion. Preserve the JSON
order and cite exactly the motivating evidence IDs `[E-001]`. Recommendations
are proposals only; do not imply that any was applied or validated.

## Evidence Index

Mirror every JSON evidence entry as `E-NNN - immutable-ref - excerpt`, including
the verified content hash for Artifact evidence. Do not include an uncited
entry merely to inflate apparent coverage.

The Markdown report must end with this exact sentence:

No proposed change was applied. All recommendations require separate review and execution.
