# Causal Analysis Method

This reference defines how to bind the user's question to Trace evidence before and after backward semantic taint analysis.

## Bind The Question

Record four distinct statements:

1. **Expected behavior:** What the user required or reasonably expected.
2. **Actual behavior:** What the Trace records the run doing or producing.
3. **Discrepancy:** The exact difference the user asks to explain.
4. **Final impact:** The consequence named by the user, if any.

Keep the original task semantics. Do not replace a question about an omitted Skill, wrong build system, ignored correction, or incorrect answer with a generic statement such as "verification was missing." A successful fallback can still violate an explicit instruction; unexpected behavior is not automatically defective.

## Establish The Premise

Assess the expectation and actual behavior separately, citing evidence:

- `supported`: recorded facts substantiate the statement;
- `contradicted`: recorded facts conflict with the statement;
- `unknown`: the Trace lacks enough evidence to decide.

If the central expectation or actual behavior is contradicted, explain the mismatch before pursuing causation. If a missing premise could change the verdict, keep the result inconclusive rather than assuming it.

## Select Each Analysis Start

Choose the narrowest recorded node or omission boundary that directly represents the questioned behavior or final effect. Common starts include a final response, decision, mutation, failed verification, tool result, exit, or the after anchor of an omitted action. Record for every analysis start:

- the node or before/after refs;
- the question clause it represents;
- why it is closer to the observed effect than nearby alternatives;
- any final effect not covered by that start.

Use multiple starts when the question contains distinct effects. Do not choose a node merely because it is last in time.

## Evidence Discipline

Label material claims as one of:

- **Fact:** Directly recorded content, metadata, explicit references, eligible edges, or a hash-verified Artifact. Cite its resolvable ref.
- **Inference:** A semantic conclusion derived from cited facts. State the reasoning and plausible alternatives.
- **Unknown:** A fact needed for a judgment but absent, unresolved, truncated, or unavailable because Artifact integrity failed.

Temporal adjacency is context, not causal proof. Event type, failure status, and component ownership are also insufficient by themselves. Never infer an unrecorded edge or quote Artifact content that was not successfully hydrated and verified.

## Competing And Rejected Hypotheses

Generate explanations from the bound task semantics and recorded facts, not from a fixed defect taxonomy. Keep alternatives live while they could explain the effect. A rejected hypothesis records:

- the proposed cause;
- why it was plausible;
- supporting refs;
- the contradiction, ineligible connection, clean upstream input, or stronger explanation that rejected it;
- any residual uncertainty.

Do not label an unexplored hypothesis rejected. Use `superseded` when a stronger explanation absorbs it without contradiction.

## Final-Impact Binding

After confirming or qualifying roots, reconnect every causal chain to the discrepancy in the user's question. State the impact category in task terms, such as functional behavior, instruction following, architecture, safety, completeness, or answer quality. Runtime success does not erase a question-specific defect.

Separate:

- direct impact proven by the Trace;
- inferred downstream risk;
- impact that remains unknown.

If a proposed root cannot explain a stated final impact through eligible evidence, it is not sufficient for that impact. Report a partial explanation or evidence gap instead.

Use `confirmed`, `probable`, `inconclusive`, or `no_defect` for the overall conclusion. The report language follows the user's question unless another language is requested.
