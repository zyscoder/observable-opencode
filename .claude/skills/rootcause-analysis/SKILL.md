---
name: rootcause-analysis
description: Use when a user provides a finalized semantic trace and asks why an Agent run failed, behaved unexpectedly, violated an instruction, omitted an expected action, or produced a lower-quality result.
---

# Root-Cause Analysis

## Purpose

Diagnose unmet expectations from Trace evidence. Judge semantics; retrieve facts.

## Required Inputs

- Absolute path to a finalized `trace.json`.
- The user's question about the behavior to explain.

Ask for missing inputs.

## Analysis-Only Boundary

This Skill is **analysis-only**:

- must not modify the observed Agent, Harness, Skill, MCP, tool, prompt, model configuration, repository, build environment, Trace bundle, or session;
- must not run builds or tests against the analyzed system, repair it, or change configuration;
- must not mutate the Trace, annotate it in place, or append analysis to it;
- must not feed conclusions or recommendations back to the observed Agent or resume its session.

Only write reports outside the analyzed system and Trace bundle; recommendations remain proposals.

## Required Workflow

1. **Validate input.** Run `python3 scripts/trace_query.py validate --trace <absolute-trace-path>`; stop with finalize guidance on invalid input.
2. **Bind the question.** Record expectation, actual behavior, exact discrepancy, and stated final impact without substituting a generic failure.
3. **Establish the premise.** Classify expectation and observation as `supported`, `contradicted`, or `unknown` from cited facts.
4. **Select starts.** Choose direct evidence of the questioned action, omission boundary, or final effect; record why.
5. **Create a hypothesis ledger.** Track hypotheses, support, contradiction, open evidence, and next candidates.
6. **Inspect eligible upstream evidence recursively.** Query explicit sources and eligible edges in bounded batches. For search, advance `next_offset` until `truncated` is `false`; otherwise record unexplored candidates.
7. **Classify semantic taint.** Judge each node `absent`, `inherited`, `transformed`, `amplified`, `introduced`, `blocked`, or `unknown` for this defect.
8. **Backtrack or expand.** Reject contradicted branches, revisit alternatives, and retrieve evidence while unresolved candidates could change the verdict.
9. **Confirm each root independently.** Require an evidence-backed path, introduction or independent causation, no stronger upstream introducer, reasoning-only counterfactual, and compared alternatives.
10. **Explain propagation and impact.** State what each component produced or omitted, what followed, and how the task outcome changed.
11. **Emit JSON and Markdown.** Write `rootcause-analysis/v1` JSON and evidence-mirrored Markdown outside the analyzed system and Trace bundle. Every conclusion, causal step, root cause, and recommendation must cite IDs that resolve through `evidence_index`; the two reports must mutually corroborate. Every recommendation must cite its motivating evidence refs. End Markdown by stating that no proposed change was applied and all recommendations require separate review and execution.
12. **Stop without repair.** State that no proposed change was applied and that implementation requires a separate task.

Use plain task language, not unexplained event-type labels. Explain what evidence established and how it was used; an event kind is not a cause by itself. Final reports follow the language of the user's question unless requested otherwise.

## References

- **Read when validating or querying a Trace:** [Trace structure and query protocol](references/trace-structure.md).
- **Read when choosing starts, binding evidence, or assessing impact:** [Causal analysis method](references/causal-analysis-method.md).
- **Read when tracing a defect, omission, or competing root hypothesis:** [Backward semantic taint](references/backward-semantic-taint.md).
- **Read when translating recorded node kinds into task-level questions:** [Node semantics](references/node-semantics.md).
- **Read when writing or validating the final JSON and Markdown reports:** [Report schema](references/report-schema.md).
