# Causal Episode And Per-Defect Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make offline backward attribution branch-local, causally consistent, and capable of returning one auditable root episode for every observed defect.

**Architecture:** Extend judgment semantics with branch and influence relations, construct confirmed causal episodes from trace identity fields, then run backward traversal independently per observed defect. Aggregate branch results only after episode-level root selection and expose `partial_root_found` when coverage is incomplete.

**Tech Stack:** Python 3.9+, dataclasses, unittest, Anthropic-compatible judge client, observable-opencode semantic Trace JSON.

## Global Constraints

- Attribution remains an offline passive sidecar and never writes into the source Trace.
- Attribution results are never fed to the observed agent.
- Temporal-only and context-retention edges cannot merge causal episodes.
- A root must be grounded in current-node semantics, not code complexity or possibility language.
- Existing report fields remain readable while new branch-local structures are added.

---

### Task 1: Causal Judgment Invariants

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/models.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `TaintInfluence.relation: str`, `NodeJudgment.branch_relation: str`.
- Produces: `validate_judgment_payload()` rejection of invalid propagation and speculative roots.

- [ ] **Step 1: Write failing schema tests**

Add tests that require propagation to include a `defect_propagated_from` influence, require `branch_relation`, and reject a root reason based only on “could/may/might introduce” speculation.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_backward_taint.ClaudeJudgeClientTest.test_rejects_propagation_without_defective_upstream \
  tools.trace_attribution.tests.test_backward_taint.ClaudeJudgeClientTest.test_rejects_speculative_root_reason
```

Expected: failures because the current validator accepts both payloads.

- [ ] **Step 3: Implement the minimal model and validator changes**

Add relation constants and fields:

```python
INFLUENCE_RELATIONS = {
    "defect_propagated_from",
    "motivated_by_evidence",
    "derived_from",
}

BRANCH_RELATIONS = {
    "same_defect",
    "causal_precursor",
    "outcome_evidence",
    "unrelated",
    "unknown",
}

@dataclass(frozen=True)
class TaintInfluence:
    upstream_ref: str
    reason: str
    confidence: float = 0.0
    relation: str = "defect_propagated_from"
```

Require at least one `defect_propagated_from` relation for propagation. Require introduction judgments to use `same_defect` or `causal_precursor`. Reject root reasons whose only causal assertion is speculative language.

- [ ] **Step 4: Run focused and full Python tests**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/trace_attribution/trace_attribution/models.py \
  tools/trace_attribution/trace_attribution/claude.py \
  tools/trace_attribution/tests/test_backward_taint.py
git commit -m "feat: enforce causal judgment invariants"
```

### Task 2: Confirmed Causal Episode Index

**Files:**
- Create: `tools/trace_attribution/trace_attribution/episodes.py`
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Test: `tools/trace_attribution/tests/test_episodes.py`

**Interfaces:**
- Produces: `CausalEpisodeIndex.from_graph(graph: TraceGraph) -> CausalEpisodeIndex`.
- Produces: `episode_for(ref: str) -> CausalEpisode` and `representative(candidates, judgments) -> str`.

- [ ] **Step 1: Write failing episode tests**

Construct a trace containing a reasoning decision, LLM tool-call decision, tool call, change, and result. Assert that confirmed same-message, call-ID, and span identities form one episode while temporal-only records remain separate.

- [ ] **Step 2: Run episode tests and verify RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_episodes
```

Expected: import failure because `episodes.py` does not exist.

- [ ] **Step 3: Implement union-find episode construction**

Create immutable episode records:

```python
@dataclass(frozen=True)
class CausalEpisode:
    episode_id: str
    member_refs: List[str]

class CausalEpisodeIndex:
    @classmethod
    def from_graph(cls, graph: TraceGraph) -> "CausalEpisodeIndex":
        index = cls(graph.nodes)
        for left_ref, right_ref in confirmed_episode_pairs(graph):
            index.union(left_ref, right_ref)
        return index.freeze()

    def episode_for(self, ref: str) -> CausalEpisode:
        return self.episodes_by_ref.get(ref, CausalEpisode(stable_episode_id([ref]), [ref]))
```

Union records only through `reasoning_selected_action`, identical `callID`, identical execution span involving tool/change records, or explicit tool/change source refs.

- [ ] **Step 4: Add representative-selection tests**

Assert that the earliest member judged `defect_introduction` is selected, while an earlier `non_defective` reasoning node does not displace a later defective tool-authoring decision.

- [ ] **Step 5: Run episode and full tests**

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tools/trace_attribution/trace_attribution/episodes.py \
  tools/trace_attribution/trace_attribution/graph.py \
  tools/trace_attribution/tests/test_episodes.py
git commit -m "feat: group trace records into causal episodes"
```

### Task 3: Branch-Local Backward Traversal

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/models.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/trace_improvement.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `DefectBranchResult` in `AttributionReport.defect_branches`.
- Produces top-level outcomes `root_found`, `partial_root_found`, `no_defect`, and `inconclusive`.

- [ ] **Step 1: Write failing branch-isolation tests**

Add one trace with two observed defects sharing an upstream node. Make the fake judge return different branch-local judgments based on downstream context. Assert independent visited sets, roots, and outcomes.

- [ ] **Step 2: Write failing per-defect coverage test**

Create one branch with a root and one defective branch with no root. Assert top-level `partial_root_found`, not `root_found`.

- [ ] **Step 3: Run focused tests and verify RED**

Expected: current analyzer globally deduplicates the shared node and reports `root_found`.

- [ ] **Step 4: Implement branch-local traversal state**

Use `(branch_id, ref)` for visited and judgment storage. Add:

```python
@dataclass(frozen=True)
class DefectBranchResult:
    branch_id: str
    start_ref: str
    defect_type: str
    analysis_outcome: str
    root_causes: List[RootCauseCandidate]
    taint_paths: List[List[str]]
    node_judgments: Dict[str, NodeJudgment]
    visited_order: List[str]
    unresolved_refs: List[str]
    metadata: JsonDict = field(default_factory=dict)
```

Run each start branch independently, then aggregate compatibility summaries.

- [ ] **Step 5: Integrate episode-level root selection**

For each branch, group root-eligible judgments by episode and keep one representative. Store `episode_id`, `episode_member_refs`, and `observed_defect_refs` in each root candidate.

- [ ] **Step 6: Run focused and full tests**

Expected: branch isolation and per-defect coverage tests pass; existing tests remain green.

- [ ] **Step 7: Commit**

```bash
git add tools/trace_attribution/trace_attribution/models.py \
  tools/trace_attribution/trace_attribution/analyzer.py \
  tools/trace_attribution/trace_attribution/trace_improvement.py \
  tools/trace_attribution/tests/test_backward_taint.py
git commit -m "feat: analyze attribution per observed defect"
```

### Task 4: Branch-Aware Judge Prompt

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`
- Modify: `tools/trace_attribution/README.md`

**Interfaces:**
- Consumes active branch semantics from `downstream_context`.
- Produces grounded `branch_relation` and influence relations.

- [ ] **Step 1: Write failing prompt-content tests**

Assert the prompt explicitly says that environment evidence can motivate but not propagate an out-of-scope decision, authored test code is action semantics rather than result evidence, and complexity alone cannot establish a root.

- [ ] **Step 2: Verify RED, implement prompt/schema changes, verify GREEN**

Add the new schema fields and rules. Preserve hydrated artifacts and existing truncation behavior.

- [ ] **Step 3: Update README report semantics**

Document causal episodes, influence relations, defect branches, and `partial_root_found`.

- [ ] **Step 4: Run full Python tests and commit**

```bash
git add tools/trace_attribution/trace_attribution/claude.py \
  tools/trace_attribution/tests/test_backward_taint.py \
  tools/trace_attribution/README.md
git commit -m "feat: ground judge decisions in active defect branches"
```

### Task 5: Sphinx Before/After Regression Gate

**Files:**
- Create: `docs/superpowers/reports/2026-07-14-loop5-sphinx-after-fix.json`
- Create: `docs/superpowers/reports/2026-07-14-loop5-sphinx-attribution-comparison.md`

**Interfaces:**
- Consumes the unchanged Sphinx Trace and review hashes recorded in the design.
- Produces a before/after root and branch-coverage comparison.

- [ ] **Step 1: Run the improved analyzer on the unchanged Trace**

Use `deepseek-v4-pro`, a 3600-second judge timeout, `max_depth=24`, and `max_nodes=128`.

- [ ] **Step 2: Check the seven acceptance conditions**

Programmatically assert the parser, verification, and scope representative roots; reject `chg_5`; verify no evidence/signal roots; require three branch roots, zero judge errors, and no search limit.

- [ ] **Step 3: Write the comparison report**

Separate deterministic fixes from remaining judge variability and identify whether the next bottleneck belongs to Trace semantics or attribution logic.

- [ ] **Step 4: Run final verification**

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests
./packages/opencode/node_modules/.bin/tsgo --noEmit -p packages/opencode/tsconfig.json
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/reports/2026-07-14-loop5-sphinx-after-fix.json \
  docs/superpowers/reports/2026-07-14-loop5-sphinx-attribution-comparison.md
git commit -m "test: validate causal episode attribution on sphinx"
```
