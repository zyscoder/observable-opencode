# Causal Episode And Per-Defect Coverage Design

## Background

The current offline attribution module reconstructs message lineage and can reach the decisions that caused poor agent outcomes. Two identical analyses of the same Sphinx FeatureBench trace nevertheless exposed stable and stochastic precision failures:

- one parser mistake was emitted as three roots: reasoning, edit decision, and code change;
- the defective self-test branch had no root because the test-authoring action was classified as evidence;
- the out-of-scope compatibility branch had no root because a decision with no defective upstream was classified as propagation;
- a second run drifted from the scope-management branch through an environment failure into an unrelated symbol change and invented a speculative root;
- the global `root_found` status hid two observed-defect branches with no valid root.

This design improves the offline attribution module only. It does not change runtime tracing, the observed agent, its prompts, its tools, or its behavior.

## Goals

1. Analyze each observed defect as an independent backward-taint branch.
2. Prevent semantic contamination between unrelated defect branches.
3. Collapse multiple records from one agent action into one causal episode.
4. Enforce that propagation has an explicit defective upstream cause.
5. Distinguish evidence that motivates a bad choice from a defect that is propagated into that choice.
6. Report root coverage and analysis outcome for every observed defect.
7. Preserve all original node judgments and episode members for auditability.

## Non-Goals

- No runtime root-cause analysis.
- No attribution result is fed back to the observed agent.
- No new trace instrumentation is added in this iteration.
- No second global reviewer LLM is introduced.
- No root is inferred from temporal proximity alone.

## Architecture

### Branch-Local Backward Analysis

Every `case.observed_defect` and `case.quality_gap` start node creates one `DefectBranch`. Explicit CLI start refs that are not evaluation records each create an equivalent standalone branch.

Traversal state is keyed by `(branch_id, node_ref)` instead of only `node_ref`. A shared trace node may therefore receive different judgments when reached from different observed defects. The judge prompt always contains:

- the active observed-defect ref and failure type;
- the downstream path for that branch;
- the current node and eligible upstream nodes;
- the required relation between the current node and active defect.

Judgments from one branch cannot enqueue nodes or create roots in another branch.

### Branch Relation

Each node judgment adds `branch_relation`:

- `same_defect`: the node contains the active defect;
- `causal_precursor`: the node contains a different local defect that concretely causes the active defect;
- `outcome_evidence`: the node truthfully exposes the active defect;
- `unrelated`: the node is not causally relevant to the active defect;
- `unknown`: available facts are insufficient.

`defect_status` is interpreted relative to the active branch. An unrelated defect is therefore `absent` for that branch. A `causal_precursor` judgment must explain a concrete mechanism; code complexity, changed line count, and phrases such as “could introduce a defect” are insufficient.

### Influence Relations

`TaintInfluence` adds `relation`:

- `defect_propagated_from`: the upstream node already contains the active defect or a concrete causal precursor;
- `motivated_by_evidence`: the upstream node is non-defective evidence that influenced a later choice;
- `derived_from`: provenance for an evidence node without claiming defect propagation.

A `defect_propagation` judgment is valid only when it contains at least one `defect_propagated_from` influence. If a defective decision was merely motivated by non-defective evidence, the decision is a `defect_introduction` root, not propagation.

Payload validation rejects:

- propagation with no influences;
- propagation containing no `defect_propagated_from` influence;
- introduction with upstream defect-propagation influences;
- evidence or unrelated nodes marked as roots;
- speculative root reasons without a concrete defect mechanism.

The existing judge JSON-repair path receives the validation error and can correct the judgment. An irreparable response remains `unknown`; it is never promoted to a root.

## Causal Episodes

The deterministic `CausalEpisodeIndex` groups records only through confirmed identity relationships:

1. `reasoning_selected_action` lineage edges for records in the same message;
2. identical `callID` across LLM tool-call decisions, `tool.call`, and `tool.result`;
3. identical tool-execution span across a tool-execute decision, repository change, and execution observation;
4. explicit source refs between tool calls, results, changes, and observations.

Content-matched context-retention and temporal-only edges do not merge episodes.

Episode examples:

- parser episode: `dec_160 -> dec_161/dec_162 -> tool.call -> chg_2 -> tool.result`;
- self-test episode: `dec_360 -> dec_361/dec_362 -> tool.call -> tool.result`.

Episode root selection is branch-local:

1. keep only members judged `defect_introduction` for the active branch;
2. remove any member with an earlier defect-introduction ancestor in the same confirmed episode;
3. select the earliest remaining semantic introduction by trace order;
4. retain all member refs and their judgments in the episode result.

This selects `dec_160` for the parser defect. For the self-test defect, `dec_360` remains non-defective and `dec_362` becomes the episode root because it authors the incorrect executable test semantics.

## Per-Defect Results

The report adds `defect_branches`, each containing:

- `branch_id` and `start_ref`;
- active `defect_type` and description;
- `analysis_outcome`;
- root episodes and representative root refs;
- branch-local taint paths and visited order;
- unresolved refs, judge errors, and search-limit state.

Branch outcomes are:

- `root_found`: at least one grounded root episode exists and no required path is unresolved;
- `no_defect`: the evaluation assertion is contradicted by trace facts;
- `inconclusive`: the defect remains present or unknown without a valid root, or analysis was limited.

The top-level outcome becomes:

- `root_found` only when every defective branch is `root_found` and non-defective branches are `no_defect`;
- `partial_root_found` when at least one branch has a root and at least one branch is inconclusive;
- `no_defect` when every branch is `no_defect`;
- `inconclusive` otherwise.

Top-level roots are deduplicated by `(branch_id, episode_id)`. Existing `node_judgments` remain as a compatibility summary, while complete branch-local judgments are retained under each branch.

## Error Handling

- Judge transport, timeout, and schema failures produce branch-local `unknown` judgments.
- Invalid propagation never silently becomes a root.
- A branch cannot borrow a root from another branch.
- Search limits are reported independently for each branch.
- Episode construction gaps remain visible but do not prevent an ungrouped, grounded node from being a root.

## Test Strategy

Implementation follows red-green-refactor tests.

### Unit Tests

1. Reject propagation with no `defect_propagated_from` influence.
2. Classify a bad decision motivated by truthful environment evidence as introduction.
3. Keep truthful tool output as evidence while locating a defective test-authoring action.
4. Group reasoning, tool-call decision, tool call, change, and result into one episode.
5. Select the earliest defective member as episode representative.
6. Analyze the same node independently under two observed defects.
7. Reject an unrelated speculative code-change root.
8. Produce `partial_root_found` when only one of multiple defect branches has a root.

### Existing Regression Suite

All existing `tools/trace_attribution/tests` tests must pass. TypeScript trace-generation behavior is unchanged.

## Sphinx Baseline Acceptance Gate

The unchanged Sphinx trace and review are rerun after implementation. Acceptance requires:

1. parser branch: one root episode represented by `decisionnode_dec_160_d3011460`;
2. verification branch: one root episode represented by `decisionnode_dec_362_abbf250d` or the corresponding authored tool-call action, with the tool result retained as evidence;
3. scope branch: one root episode represented by `decisionnode_dec_346_dac72e2f`;
4. `chgnode_chg_5_75e72f77` is not a root for the scope branch;
5. SIGTERM, environment failures, and truthful tool results are not roots;
6. all three observed-defect branches report `root_found`;
7. the top-level outcome is `root_found`, with zero judge errors and no search-limit termination.

Only after this gate passes will the loop execute a new open-source benchmark case.
