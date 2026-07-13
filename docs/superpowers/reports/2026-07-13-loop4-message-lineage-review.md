# Loop 4 Message Context Lineage Review

## Objective

Re-run the prior open-source FeatureBench Pydantic trace before executing a new case and verify whether the attribution module still:

1. promotes truthful pytest failure evidence to a root cause;
2. fails to reach `decisionnode_dec_1093_ee253ed8`;
3. propagates trace observability gaps into code-change root causes.

The source agent run was not replayed. Message lineage and attribution were produced offline from the existing trace and artifacts.

## Reconstruction Result

- Agent turns: 212
- Message/context snapshots: 1,682
- Reconstructed edges: 1,256
- Confirmed same-message edges: 362
- Exact content-matched context-retention edges: 653
- Temporal-only observational edges: 241
- Attribution-eligible edges: 1,015
- Reconstruction gaps: 0

The reconstructor recovered both decisive facts:

- `dec_1093 -> dec_1095` through identical `sessionID` and `messageID` plus record order;
- `dec_1093 -> node_5041/node_5062` because the exact normalized rationale text is present in each LLM request artifact.

Temporal-only edges remain in the lineage output but are not merged into attribution traversal.

## Before And After

### Before Scheme C

- The analyzer visited 22 nodes.
- It did not visit `dec_1093`.
- A truthful pytest call was reported as a root boundary.
- Missing-verification trace gaps produced code-change roots.
- Final answer and LLM generation surfaces were reported as roots.

### After Scheme C

- The analyzer visited 39 nodes with no search-limit termination.
- `dec_1093` was visited and classified as `defect_introduction` with 0.95 confidence.
- The recovered taint path is:

  `observed premature completion -> final response claim -> final LLM call -> dec_1093`

- No pytest tool call or result is a root.
- No code-change node is a root through missing-verification evidence.
- No final LLM or response surface remains a root after upstream decisions are judged.

The original three regression failures are therefore resolved.

## Residual Attribution Risk

The result still contains eight additional decision roots. Several are false positives: for example, a decision that says “run a broader test” was interpreted as knowingly proceeding despite failures that had not yet been produced. This is no longer a trace reachability problem. It is an offline semantic-judgment precision problem caused by exposing a broad set of retained historical decisions with the downstream defect description.

The next attribution refinement should:

1. distinguish context availability from actual decision use;
2. prioritize reasoning connected to tool results consumed by the final claim;
3. instruct the judge that downstream outcomes do not prove what an earlier node knew;
4. treat continued investigation as non-defective unless the decision explicitly dismisses evidence, narrows scope incorrectly, or declares completion;
5. rank or cluster sibling decision candidates instead of presenting every retained decision as an independent final root.

## Acceptance

The Scheme C old-trace gate passes. The message lineage is sufficient to reach the manually identified decision and eliminates the previous evidence/root and observability-gap/root category errors. Root-set precision remains the primary optimization target for the next loop and must be evaluated on a different open-source FeatureBench case.
