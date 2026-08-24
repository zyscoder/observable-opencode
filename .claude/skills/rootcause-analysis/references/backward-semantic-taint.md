# Backward Semantic Taint

Use this protocol to locate where a question-specific semantic defect first entered a recorded execution and how it reached the observed effect. Taint is a revisable evidence-backed judgment, not a property implied by a node kind.

## Taint States

- `absent`: Neither the relevant input nor output contains the questioned defect.
- `inherited`: The node forwards a defect already present upstream without material semantic change.
- `transformed`: The node changes the form or meaning of an upstream defect.
- `amplified`: The node increases an upstream defect's scope, severity, or downstream consequence.
- `introduced`: Relevant inputs are not defective, but this node first produces the defective content, decision, action, or omission.
- `blocked`: The node prevents an upstream defect from propagating further.
- `unknown`: Available evidence cannot support a reliable classification.

Classify the precise defect bound from the user's question. The same node can be clean for one question and defective for another.

## Blocked Transition

When a node is `blocked` for the current effect:

1. terminate that upstream propagation branch for this defect and effect;
2. record the blocker, its output, and the evidence that propagation stopped;
3. do not recurse farther upstream on that branch as an explanation of the current effect;
4. inspect downstream from the blocker for an independent reintroduction of the same or a materially related defect.

If downstream evidence reintroduces the defect, open a separate hypothesis at that introduction point. The blocker remains chain evidence; it is not evidence that every later output is clean.

## Per-Node Semantic Judgment

Answer all eight questions for every node retained in a candidate chain:

1. Which component acted, and what does this node mean in the task?
2. Which question-relevant inputs did that component receive?
3. Which semantic output, action, or omission did it produce?
4. Did that result violate the bound expectation, task constraint, or upstream contract?
5. Was the same defect already present in an eligible upstream source?
6. Which taint state applies, and which facts support that judgment?
7. How did this output reach or influence the next downstream node?
8. Would correcting this node plausibly block or materially reduce the final effect?

Use task-language answers. A raw kind such as `tool.result` is an evidence locator, not an explanation.

## Hypothesis Ledger

Maintain one entry per live or rejected explanation:

| Field | Required content |
|---|---|
| Hypothesis | Proposed semantic introducer or independent cause |
| Current node and downstream effect | The branch position and effect it is meant to explain |
| Proposed taint state | Current classification, explicitly provisional until confirmed |
| Support | Facts and resolvable node, edge, or Artifact refs |
| Contradiction | Facts that weaken or disprove the branch |
| Open evidence | Missing facts or unresolved semantic questions |
| Next candidates | Eligible upstream refs or alternative branches to inspect |
| Status | `open`, `rejected`, `superseded`, `probable`, or `confirmed` |

Never erase a rejected hypothesis. Preserve why it lost so the final report can distinguish considered alternatives from ignored ones.

## Recursive Worklist And Backtracking

1. Seed the worklist with evidence-backed analysis starts and initial semantic hypotheses.
2. Inspect the current node with the eight questions.
3. For `inherited`, `transformed`, or `amplified`, enqueue plausible eligible upstream sources that could contain or introduce the same defect.
4. For `absent` or contradictory evidence, reject or revise that branch and backtrack to the next ledger candidate.
5. For `blocked`, apply the blocked transition and do not continue upstream for the current effect.
6. For `unknown`, retrieve another bounded neighborhood or record the exact evidence gap.
7. For `introduced`, test downstream reachability, stronger upstream explanations, the counterfactual, and competing hypotheses before confirmation.
8. Continue while any unexplored item could change the verdict.

There is **no fixed semantic depth** and **no fixed candidate budget**. Query depth and result limits bound one retrieval batch only. When output is truncated, preserve `remaining_frontier_refs` in the ledger and continue if that frontier could change the conclusion. Do not stop at the first plausible cause.

## Missing-Action Analysis

An omitted Skill, MCP call, tool, Subagent, build system, or verification has no invocation node. Do not fabricate one.

1. Establish the expected action from the user request, governing contract, exposed catalog, context, or loaded Skill description.
2. Establish the execution window in which the action could have occurred.
3. Record the **before anchor**: the last node where the expectation and capability were still available.
4. Record the **after anchor**: the first decision, selection, action-generation, or exit node that failed to preserve or realize the expectation.
5. Inspect upstream evidence to distinguish missing context, unavailable capability, weak discovery trigger, selection error, action-generation error, execution failure, and an unconsumed result.
6. Cite both anchors as evidence for the absence; absence alone does not assign blame.

If the action's trigger existed only in an unexposed body, investigate discoverability or description design. If a clear requirement and trigger were exposed but no call was produced, inspect the first unsupported post-exposure decision.

## Root Confirmation

Confirm a root only when all conditions hold:

- an eligible evidence-backed path connects it to the observed effect;
- it introduces the defect or is an independent root that causes or materially aggravates the effect;
- no stronger recorded upstream introducer explains the same defect;
- the correction counterfactual is plausible: correcting this node while holding relevant upstream facts constant would prevent or materially reduce the final effect;
- competing hypotheses were inspected and contradictory evidence was addressed.

The counterfactual is analysis only; never execute the correction. Missing evidence permits `probable` or `inconclusive`, never a fabricated confirmation.

Keep independent roots separate when each introduces a distinct necessary or materially aggravating defect. A later node that only carries, transforms, or amplifies an earlier defect is propagated chain evidence, not a duplicate root. A later node is an additional root only when its independent contribution satisfies every confirmation condition.
