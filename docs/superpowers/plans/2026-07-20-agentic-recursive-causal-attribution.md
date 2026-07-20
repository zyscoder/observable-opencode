# Agentic Recursive Causal Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline causal-attribution engine that recursively follows semantic defect propagation and transformation, maintains competing hypotheses, invokes bounded investigation tools when evidence is insufficient, and independently confirms root causes.

**Architecture:** Keep Causal IR and `TraceGraph` as the immutable fact layer. Add a serializable recursive frontier and hypothesis ledger around a new LLM relation Judge; direct semantic predecessors drive normal traversal, while bounded local investigation tools are used only for unknown, conflicting, or missing-evidence states. Reuse the existing Anthropic-compatible transport, durable cache, and circuit breaker, and retain the legacy analyzer behind a CLI engine switch during migration.

**Tech Stack:** Python 3.9+, standard-library dataclasses/typing/json/hashlib/heapq/pathlib, Anthropic Python SDK through the existing `ClaudeJudgeClient`, `unittest`, observable-opencode Causal IR JSON.

## Global Constraints

- Analysis remains an offline passive sidecar and never mutates Trace input or feeds attribution results back into the evaluated Agent.
- Prompt, Context, Tool, LLM, Subagent, Harness, and result nodes remain semantically eligible; no component allowlist or denylist may decide root cause.
- Temporal proximity alone is never an attribution-eligible causal relation.
- `unknown`, unresolved refs, missing artifacts, Provider failures, and exhausted budgets can only produce `inconclusive`, never a confirmed root.
- Every LLM-cited node, edge, artifact, excerpt, and path must resolve to recorded or explicitly marked reconstructed/inferred evidence.
- Logical recursion is implemented with a serializable priority frontier so SIGINT, SIGTERM, Provider circuit opening, and process restart can resume without replaying completed judgments.
- The original Trace, Agent, benchmark request, tools, MCP, Skill, Subagent, and LLM execution behavior must remain unchanged.
- The implementation adds no runtime dependency beyond the already-required Anthropic SDK.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `trace_attribution/causal_state.py` | Defect states, predecessor relations, frontier items, hypotheses, confirmations, and recursive report types. |
| `trace_attribution/causal_retrieval.py` | Ranked semantic predecessor retrieval and recursive Judge context construction. |
| `trace_attribution/hypotheses.py` | Hypothesis lifecycle, branch merge, rejection, and backtracking. |
| `trace_attribution/causal_judge.py` | Relation prompts, schema validation, cache integration, and independent confirmation. |
| `trace_attribution/investigation.py` | Read-only offline investigation tools and bounded action dispatch. |
| `trace_attribution/recursive_analyzer.py` | Recursive frontier orchestration, budgets, loops, backtracking, and ranking. |
| `trace_attribution/checkpoint.py` | Durable frontier, hypothesis, and investigation journals. |
| `trace_attribution/cache.py` | Backward-compatible generic JSON result caching. |
| `trace_attribution/claude.py` | Public structured-request transport adapter. |
| `trace_attribution/cli.py` | Engine, perspective, budgets, checkpoint paths, and report output. |
| `trace_attribution/trace_improvement.py` | Recursive evidence-gap feedback. |
| `tests/test_causal_state.py` | State identity, transformation, and serialization. |
| `tests/test_causal_retrieval.py` | Candidate layers, provenance, and ranking. |
| `tests/test_hypotheses.py` | Competing hypotheses and backtracking. |
| `tests/test_causal_judge.py` | Prompt, schema, grounding, cache, and confirmation. |
| `tests/test_recursive_analyzer.py` | Recursive propagation, transformation, and outcomes. |
| `tests/test_investigation.py` | Tool execution, trigger, deduplication, and budgets. |
| `tests/test_causal_checkpoint.py` | Journal durability and resume. |
| `tests/test_recursive_cli.py` | CLI migration and output paths. |
| `tests/fixtures/recursive_cases/*.json` | Sphinx, Pydantic, requirement, compaction, tool, Subagent, multi-root, missing-edge, timeout, and negative-control regressions. |
| `scripts/evaluate_recursive_attribution.py` | Human-label agreement and cost metrics. |

---

### Task 1: Causal State And Identity Model

**Files:**
- Create: `tools/trace_attribution/trace_attribution/causal_state.py`
- Create: `tools/trace_attribution/tests/test_causal_state.py`
- Modify: `tools/trace_attribution/trace_attribution/__init__.py`

**Interfaces:**
- Consumes: `TraceNode`, `JsonDict`, and `stable_json` from `trace_attribution.models`.
- Produces: `DefectState`, `CausalCandidate`, `PredecessorAssessment`, `CausalStepJudgment`, `FrontierItem`, `HypothesisEvidence`, `AttributionHypothesis`, `RootConfirmation`, `ConfirmedRoot`, `CausalFactor`, `RejectedCandidate`, `RecursiveAttributionReport`, and `semantic_visit_key()`.

- [ ] **Step 1: Write failing state and transformation tests**

```python
import unittest

from trace_attribution.causal_state import DefectState, FrontierItem, semantic_visit_key


class CausalStateTest(unittest.TestCase):
    def test_transformed_defect_preserves_chain_and_changes_visit_identity(self):
        downstream = DefectState.create(
            label="missing_parse_namespace_object_contract",
            expected="compatibility method exists",
            actual="method is absent",
            mechanism="implementation omission",
            scope="parser_contract_recovery",
        )
        upstream = downstream.transformed(
            label="premature_repository_search_closure",
            mechanism="the plan stopped before searching call sites",
            transformation_reason="the incomplete search produced the incomplete plan",
        )
        self.assertEqual(upstream.derived_from_defect_state_id, downstream.defect_state_id)
        self.assertNotEqual(
            semantic_visit_key("record:dec_85", downstream, "hyp_a"),
            semantic_visit_key("record:dec_85", upstream, "hyp_a"),
        )

    def test_frontier_round_trip_preserves_semantic_identity(self):
        item = sample_frontier_item()
        self.assertEqual(FrontierItem.from_dict(item.to_dict()), item)
```

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_causal_state -v
```

Expected: import failure because `trace_attribution.causal_state` does not exist.

- [ ] **Step 3: Implement immutable state types**

```python
@dataclass(frozen=True)
class DefectState:
    defect_state_id: str
    label: str
    expected: str
    actual: str
    mechanism: str
    scope: str
    fingerprint: str
    derived_from_defect_state_id: str = ""
    transformation_reason: str = ""

    @classmethod
    def create(
        cls,
        label: str,
        expected: str,
        actual: str,
        mechanism: str,
        scope: str,
        *,
        derived_from_defect_state_id: str = "",
        transformation_reason: str = "",
    ) -> "DefectState":
        semantic = {
            "label": label,
            "expected": expected,
            "actual": actual,
            "mechanism": mechanism,
            "scope": scope,
            "derived_from_defect_state_id": derived_from_defect_state_id,
            "transformation_reason": transformation_reason,
        }
        fingerprint = hashlib.sha256(stable_json(semantic).encode("utf-8")).hexdigest()[:20]
        return cls(defect_state_id=f"defect:{fingerprint}", fingerprint=fingerprint, **semantic)
```

Implement `transformed()`, `to_dict()`, and `from_dict()` for every state type. Define relation values exactly as `same_defect_propagation`, `defect_transformation`, `introduction_candidate`, `contributing_condition`, `outcome_evidence`, `unrelated`, and `unknown`. `RecursiveAttributionReport.to_dict()` retains compatibility fields `case_id`, `objective`, `root_causes`, `taint_paths`, `visited_order`, `unresolved_refs`, and `metadata` alongside recursive fields.

- [ ] **Step 4: Export types and run GREEN**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_causal_state tests.test_backward_taint -v
```

Expected: new tests and all 119 existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/trace_attribution/trace_attribution/causal_state.py \
  tools/trace_attribution/trace_attribution/__init__.py \
  tools/trace_attribution/tests/test_causal_state.py
git commit -m "feat(attribution): add recursive causal state model"
```

---

### Task 2: Layered Semantic Predecessor Retrieval

**Files:**
- Create: `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- Create: `tools/trace_attribution/tests/test_causal_retrieval.py`
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Modify: `tools/trace_attribution/trace_attribution/judgment_context.py`

**Interfaces:**
- Consumes: `TraceGraph`, `DefectState`, `AttributionHypothesis`, Progress Window candidates, and hydrated nodes.
- Produces: `SemanticPredecessorRetriever.retrieve(graph, node_ref, defect_state, hypothesis) -> List[CausalCandidate]` and `build_recursive_judgment_context(request) -> JsonDict`.

- [ ] **Step 1: Write failing evidence-layer tests**

```python
import unittest


class CausalRetrievalTest(unittest.TestCase):
    def test_retriever_prefers_confirmed_edges_without_dropping_inferred_candidates(self):
        graph = TraceGraph.from_trace(trace_with_confirmed_and_inferred_predecessors())
        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            hypothesis=sample_hypothesis(),
            limit=8,
            allow_semantic_fallback=True,
        )
        self.assertEqual(
            [item.ref for item in candidates],
            ["record:prompt", "record:context", "record:sibling"],
        )
        self.assertEqual(candidates[0].source, "confirmed_edge")
        self.assertEqual(candidates[-1].source, "semantic_fallback")

    def test_temporal_only_lineage_is_not_a_direct_predecessor(self):
        candidates = retrieve_from(trace_with_temporal_only_lineage())
        self.assertNotIn("record:temporally_previous", [item.ref for item in candidates])
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_causal_retrieval -v
```

Expected: import failure for `SemanticPredecessorRetriever`.

- [ ] **Step 3: Add provenance-preserving graph APIs**

```python
def semantic_predecessor_edges(self, ref: str) -> List[JsonDict]:
    resolved = self.resolve(ref) or ref
    output = []
    for upstream_ref in self.upstream_refs(resolved):
        for edge in self.edge_context(upstream_ref, resolved):
            output.append({"ref": upstream_ref, **edge})
    return sorted(output, key=lambda item: (-float(item.get("confidence", 0.0)), self.position(item["ref"])))

def semantic_search(
    self, query_terms: List[str], *, before_ref: str, limit: int
) -> List[JsonDict]:
    before_position = self.position(self.resolve(before_ref) or before_ref)
    terms = {term.lower() for term in query_terms if len(term) >= 3}
    scored = []
    for node in self.nodes.values():
        if self.position(node.ref) >= before_position:
            continue
        tokens = set(re.findall(r"[a-zA-Z0-9_]{3,}", stable_json(node.compact()).lower()))
        overlap = len(terms & tokens)
        if overlap:
            scored.append({
                "ref": node.ref,
                "score": overlap / max(len(terms), 1),
                "evidence_type": "semantic_inferred",
            })
    return sorted(scored, key=lambda item: (-item["score"], self.position(item["ref"])))[:limit]
```

Import `re` in `graph.py`. Keep matches retrieval-only rather than adding Trace edges; tests must prove that calling `semantic_search()` leaves `graph.edges` unchanged.

- [ ] **Step 4: Implement layered retrieval and recursive context**

```python
class SemanticPredecessorRetriever:
    def retrieve(
        self,
        graph: TraceGraph,
        node_ref: str,
        defect_state: DefectState,
        hypothesis: AttributionHypothesis,
        *,
        limit: int = 24,
        allow_semantic_fallback: bool = False,
    ) -> List[CausalCandidate]:
        layers = [
            self._direct_candidates(graph, node_ref),
            self._episode_candidates(graph, node_ref),
            self._sibling_candidates(graph, node_ref, defect_state),
        ]
        if allow_semantic_fallback:
            layers.append(self._semantic_candidates(graph, node_ref, defect_state))
        return merge_ranked_candidates(layers, limit=limit)
```

`build_recursive_judgment_context()` includes the defect transformation chain, hypothesis evidence, candidate edge semantics, downstream path and judgments, obligations, Agent/Subagent scope, Episode/window context, and artifact hydration manifest. It does not label candidates defective before Judge evaluation.

- [ ] **Step 5: Run focused and legacy tests**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_causal_retrieval tests.test_backward_taint.TraceGraphTest -v
```

Expected: all tests pass; temporal-only facts remain visible but cannot drive direct recursion.

- [ ] **Step 6: Commit**

```bash
git add tools/trace_attribution/trace_attribution/causal_retrieval.py \
  tools/trace_attribution/trace_attribution/graph.py \
  tools/trace_attribution/trace_attribution/judgment_context.py \
  tools/trace_attribution/tests/test_causal_retrieval.py
git commit -m "feat(attribution): retrieve semantic causal predecessors"
```

---

### Task 3: Hypothesis Ledger And Serializable Frontier

**Files:**
- Create: `tools/trace_attribution/trace_attribution/hypotheses.py`
- Create: `tools/trace_attribution/tests/test_hypotheses.py`

**Interfaces:**
- Consumes: `AttributionHypothesis`, `FrontierItem`, `DefectState`, and evidence refs.
- Produces: `HypothesisLedger` and `RecursiveFrontier` snapshots consumed by the analyzer and checkpoint.

- [ ] **Step 1: Write failing branch and backtracking tests**

```python
import unittest


class HypothesisLedgerTest(unittest.TestCase):
    def test_rejected_leading_hypothesis_restores_best_unresolved_alternative(self):
        ledger = HypothesisLedger()
        first = ledger.create("prompt omission is root", "record:prompt", sample_defect_state())
        second = ledger.create("agent search closure is root", "record:decision", sample_defect_state())
        ledger.add_support(first.hypothesis_id, "record:prompt", "prompt omits method", 0.8)
        ledger.add_support(second.hypothesis_id, "record:decision", "plan declares completion", 0.75)
        ledger.reject(first.hypothesis_id, "repository search could avoid the failure")
        self.assertEqual(ledger.best_active().hypothesis_id, second.hypothesis_id)

    def test_frontier_merges_same_visit_but_keeps_defect_transformations(self):
        frontier = RecursiveFrontier()
        self.assertTrue(frontier.push(item_for("record:decision", defect_a(), "hyp_1")))
        self.assertFalse(frontier.push(item_for("record:decision", defect_a(), "hyp_1")))
        self.assertTrue(frontier.push(item_for("record:decision", defect_b(), "hyp_1")))
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_hypotheses -v
```

Expected: import failure for `trace_attribution.hypotheses`.

- [ ] **Step 3: Implement explicit ledger transitions**

```python
class HypothesisLedger:
    def __init__(self):
        self._items: Dict[str, AttributionHypothesis] = {}

    def create(self, claim: str, candidate_root_ref: str, defect_state: DefectState) -> AttributionHypothesis:
        hypothesis = AttributionHypothesis.create(claim, candidate_root_ref, defect_state)
        self._items[hypothesis.hypothesis_id] = hypothesis
        return hypothesis

    def add_support(self, hypothesis_id: str, ref: str, reason: str, confidence: float) -> None:
        item = self._items[hypothesis_id]
        evidence = HypothesisEvidence(ref, reason, confidence)
        self._items[hypothesis_id] = replace(
            item,
            supporting_evidence=dedupe_evidence([*item.supporting_evidence, evidence]),
        )

    def reject(self, hypothesis_id: str, reason: str) -> None:
        item = self._items[hypothesis_id]
        self._items[hypothesis_id] = replace(item, status="rejected", resolution_reason=reason)

    def get(self, hypothesis_id: str) -> AttributionHypothesis:
        return self._items[hypothesis_id]

    def best_active(self) -> Optional[AttributionHypothesis]:
        active = [item for item in self._items.values() if item.status in {"active", "supported"}]
        return min(active, key=hypothesis_order_key) if active else None

    def snapshot(self) -> List[JsonDict]:
        return [self._items[key].to_dict() for key in sorted(self._items)]
```

Implement `add_opposition`, `add_unresolved_question`, and `supersede` with the same immutable `replace()` pattern. Confidence orders investigation only and never auto-confirms a hypothesis. `hypothesis_order_key()` orders by negative support strength, unresolved-question count, then stable hypothesis ID.

- [ ] **Step 4: Implement heap-backed frontier**

```python
class RecursiveFrontier:
    def __init__(self):
        self._heap: List[Tuple[Tuple[float, int, int, str], FrontierItem]] = []
        self._queued: Set[str] = set()
        self._completed_evidence: Dict[str, str] = {}

    def push(self, item: FrontierItem) -> bool:
        if item.visit_key in self._queued or item.visit_key in self._completed_evidence:
            return False
        heapq.heappush(self._heap, (item.heap_key, item))
        self._queued.add(item.visit_key)
        return True

    def pop(self) -> FrontierItem:
        _, item = heapq.heappop(self._heap)
        self._queued.remove(item.visit_key)
        return item

    def __bool__(self) -> bool:
        return bool(self._heap)

    def mark_completed(self, item: FrontierItem, evidence_hash: str) -> None:
        self._completed_evidence[item.visit_key] = evidence_hash

    def reopen(self, item: FrontierItem, *, evidence_hash: str, reason: str) -> bool:
        if self._completed_evidence.get(item.visit_key) == evidence_hash:
            return False
        self._completed_evidence.pop(item.visit_key, None)
        return self.push(replace(item, reopen_reason=reason, evidence_hash=evidence_hash))

    def snapshot(self) -> List[JsonDict]:
        return [item.to_dict() for _, item in sorted(self._heap)]
```

Use `semantic_visit_key(node_ref, defect_state, hypothesis.semantic_hash)` for merge identity and a heap key of negative priority, depth, graph position, and item ID. The semantic hash is derived from the normalized claim, candidate root, active defect fingerprint, and unresolved questions rather than a run-local hypothesis ID. Reopen only when evidence hash changes or root verification rejects the dominant candidate.

- [ ] **Step 5: Run tests and commit**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_hypotheses tests.test_causal_state -v
git add tools/trace_attribution/trace_attribution/hypotheses.py \
  tools/trace_attribution/tests/test_hypotheses.py
git commit -m "feat(attribution): track causal hypotheses and frontier"
```

Expected: all listed tests pass.

---

### Task 4: LLM Causal Relation Judge

**Files:**
- Create: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Create: `tools/trace_attribution/tests/test_causal_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/cache.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`

**Interfaces:**
- Consumes: recursive context, current node, defect state, candidates, and the existing Anthropic-compatible transport.
- Produces: `CausalStepRequest`, `RootConfirmationRequest`, `CausalJudge`, `ClaudeCausalJudge.judge_step(request)`, `confirm_candidate(request)`, and generic validated JSON cache methods.

- [ ] **Step 1: Write failing relation and transformation tests**

```python
import unittest


class CausalJudgeValidationTest(unittest.TestCase):
    def test_validator_accepts_grounded_defect_transformation(self):
        payload = {
            "current_node_ref": "record:change",
            "current_defect_status": "present",
            "current_defect_reason": "the change omits the method",
            "predecessors": [{
                "ref": "record:decision",
                "relation": "defect_transformation",
                "reason": "the incomplete plan produced the incomplete change",
                "confidence": 0.9,
                "recurse": True,
                "upstream_defect": {
                    "label": "incomplete_implementation_plan",
                    "mechanism": "call sites were not searched",
                    "transformation_reason": "the plan controlled authored methods",
                },
            }],
            "candidate_introduction": False,
            "missing_evidence": [],
            "suggested_investigation": None,
            "confidence": 0.9,
        }
        result = validate_causal_step_payload(payload, request=sample_step_request())
        self.assertTrue(result.predecessors[0].upstream_defect.derived_from_defect_state_id)

    def test_validator_rejects_fabricated_predecessor_ref(self):
        with self.assertRaisesRegex(ValueError, "candidate predecessor"):
            validate_causal_step_payload(
                valid_step_payload(predecessor_ref="record:not_offered"),
                request=sample_step_request(),
            )
```

- [ ] **Step 2: Run Judge tests and verify RED**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_causal_judge -v
```

Expected: import failure for `trace_attribution.causal_judge`.

- [ ] **Step 3: Generalize cache payloads without breaking legacy entries**

```python
def get_payload(self, *, key: str) -> Optional[JsonDict]:
    entry = self._entries.get(key)
    payload = entry.get("payload") if entry else None
    if payload is None and entry:
        payload = entry.get("judgment")
    return dict(payload) if isinstance(payload, dict) else None

def put_payload(
    self, *, key: str, stage: str, model: str, node_ref: str, payload: JsonDict
) -> None:
    entry = self._entry(key, stage, model, node_ref, payload)
    self._append_and_fsync(entry)
```

Keep `JudgmentCache.get()` and `put()` as wrappers. `_load()` accepts old `judgment` and new `payload` records. Add a test that an old cache line still hydrates `NodeJudgment`.

- [ ] **Step 4: Expose the existing Provider transport**

```python
def create_message_text(
    self, *, system: str, messages: List[Dict[str, str]], max_tokens: int
) -> str:
    return self._create_message_text(system=system, messages=messages, max_tokens=max_tokens)
```

The new Judge composes this public adapter and therefore shares request counting, timeout handling, error classification, and circuit state.

- [ ] **Step 5: Implement causal prompt, validation, repair, and cache**

```python
class CausalJudge(Protocol):
    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        raise NotImplementedError

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        raise NotImplementedError


class ClaudeCausalJudge:
    def __init__(self, *, transport: ClaudeJudgeClient, cache: JudgmentCache):
        self.transport = transport
        self.cache = cache

    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        prompt = build_causal_step_prompt(request)
        payload = self._request_validated(
            stage="recursive_causal_step",
            schema_version="recursive-causal-step-v1",
            system=CAUSAL_STEP_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref=request.current_node.ref,
            validator=lambda value: validate_causal_step_payload(value, request=request),
        )
        return causal_step_from_payload(payload, request=request)

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        prompt = build_recursive_confirmation_prompt(request)
        payload = self._request_validated(
            stage="recursive_root_confirmation",
            schema_version="recursive-root-confirmation-v1",
            system=ROOT_CONFIRMATION_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref=request.candidate_ref,
            validator=lambda value: validate_recursive_confirmation(value, request=request),
        )
        return root_confirmation_from_payload(payload, request=request)
```

The prompt allows any component to be causal when evidence supports it, distinguishes propagation from transformation, and requires evidence refs. Validation requires offered predecessor refs, resolvable evidence refs, `recurse=true` only for propagation/transformation, an upstream defect for transformation, and no defective predecessor for an introduction candidate.

Use cache schema versions `recursive-causal-step-v1` and `recursive-root-confirmation-v1`. One focused repair receives the exact validation error; an invalid repair returns unknown and is not cached.

Implement `_request_validated()` as one shared path: derive the cache key from stage, schema version, model, canonical request context, and prompt; return a validated cache hit when present; otherwise call `transport.create_message_text()`, parse one JSON object, and run the supplied validator. On the first parse or validation failure, make exactly one repair request containing the invalid payload and exact error. Persist only a successfully validated payload with `put_payload()`; return a typed `unknown` result with Provider/validation evidence when both attempts fail.

- [ ] **Step 6: Verify new and legacy Judge behavior**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest \
  tests.test_causal_judge \
  tests.test_backward_taint.JudgmentCacheTest \
  tests.test_backward_taint.ClaudeJudgeClientTest -v
```

Expected: all tests pass and old cache records remain readable.

- [ ] **Step 7: Commit**

```bash
git add tools/trace_attribution/trace_attribution/causal_judge.py \
  tools/trace_attribution/trace_attribution/cache.py \
  tools/trace_attribution/trace_attribution/claude.py \
  tools/trace_attribution/tests/test_causal_judge.py
git commit -m "feat(attribution): judge recursive causal relations"
```

---

### Task 5: Recursive Analyzer Core

**Files:**
- Create: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Create: `tools/trace_attribution/tests/test_recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/__init__.py`

**Interfaces:**
- Consumes: `CausalJudge`, `SemanticPredecessorRetriever`, `HypothesisLedger`, `RecursiveFrontier`, and `TraceGraph`.
- Produces: `RecursiveAnalysisState`, `AgenticRecursiveAnalyzer.analyze(graph, start_refs, objective, analysis_perspective) -> RecursiveAttributionReport`; independent confirmation is added in Task 7.

- [ ] **Step 1: Write failing propagation and transformation tests**

```python
import unittest


class RecursiveTraversalTest(unittest.TestCase):
    def test_analyzer_follows_transformed_defect_to_agent_decision(self):
        graph = TraceGraph.from_trace(minimal_change_decision_prompt_trace())
        judge = ScriptedCausalJudge({
            "record:change": step_with_transformation("record:decision", "incomplete_plan"),
            "record:decision": step_as_introduction_candidate("premature_search_closure"),
        })
        report = AgenticRecursiveAnalyzer(judge=judge, max_frontier_items=16).analyze(
            graph,
            start_refs=["record:observed_defect"],
            objective="Find why the required method is missing.",
            analysis_perspective="Improve Agent repository reasoning.",
        )
        self.assertEqual(report.introduction_candidates[0].node_ref, "record:decision")
        self.assertIn("defect_transformation", [item.relation for item in report.causal_relations])

    def test_unknown_branch_is_unresolved_not_root(self):
        judge = ScriptedCausalJudge({"record:change": unknown_step("artifact truncated")})
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(minimal_trace()),
            start_refs=["record:change"],
            objective="Find the defect.",
        )
        self.assertEqual(report.confirmed_roots, [])
        self.assertEqual(report.analysis_outcome, "inconclusive")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_recursive_analyzer -v
```

Expected: import failure for `AgenticRecursiveAnalyzer`.

- [ ] **Step 3: Implement seed construction and priority traversal**

```python
class AgenticRecursiveAnalyzer:
    def __init__(
        self,
        *,
        judge: CausalJudge,
        retriever: Optional[SemanticPredecessorRetriever] = None,
        max_frontier_items: int = 96,
        max_depth: int = 20,
        max_hypotheses: int = 24,
        max_investigation_rounds: int = 12,
        max_artifact_bytes: int = 1_048_576,
        max_judge_requests: int = 128,
    ):
        self.judge = judge
        self.retriever = retriever or SemanticPredecessorRetriever()
        self.max_frontier_items = max_frontier_items
        self.max_depth = max_depth
        self.max_hypotheses = max_hypotheses
        self.max_investigation_rounds = max_investigation_rounds
        self.max_artifact_bytes = max_artifact_bytes
        self.max_judge_requests = max_judge_requests

    def analyze(
        self,
        graph: TraceGraph,
        *,
        start_refs: Optional[Iterable[str]] = None,
        objective: str,
        analysis_perspective: str = "Find the best-supported causal explanation.",
    ) -> RecursiveAttributionReport:
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=start_refs or graph.default_start_refs(),
            objective=objective,
            analysis_perspective=analysis_perspective,
        )
        while state.frontier and state.processed_items < self.max_frontier_items:
            item = state.frontier.pop()
            if item.depth > self.max_depth:
                state.mark_unresolved(item, "depth_limit")
                continue
            candidates = self.retriever.retrieve(
                graph,
                item.node_ref,
                item.defect_state,
                state.ledger.get(item.hypothesis_id),
            )
            request = state.build_step_request(graph, item, candidates)
            judgment = self.judge.judge_step(request)
            state.apply_step(item, judgment, graph_position=graph.position)
        return state.build_report(judge=self.judge)
```

Evaluation assertions seed Defect State and enqueue concrete predecessors as outcome evidence. Each frontier item hydrates context, retrieves candidates, invokes `judge_step`, updates the ledger, and pushes propagation/transformation predecessors. Conditions and evidence remain in the report but do not carry the current taint.

Implement `RecursiveAnalysisState.create()` to initialize seed defects, ledger hypotheses, frontier entries, and deterministic counters. `build_step_request()` captures the current node, defect chain, active hypothesis, candidate provenance, downstream path, obligations, perspective, and evidence hash. `apply_step()` records the judgment, updates support/opposition, transforms defects when requested, enqueues eligible predecessors, and marks the visit complete. `mark_unresolved()` stores the node, defect, hypothesis, reason, and exhausted budget. `build_report()` serializes all paths, judgments, candidates, unresolved branches, counters, and compatibility fields without mutating `TraceGraph`.

- [ ] **Step 4: Implement merge, loop, and budget outcomes**

Use `semantic_visit_key`. Duplicate visits merge evidence; changed defect fingerprints remain separate. Depth, frontier, hypothesis, investigation, artifact-byte, Judge-request, and Provider-circuit limits add explicit unresolved reasons and return `inconclusive`. Check the request budget before every Judge and repair call. Queue exhaustion with no defective nodes returns `no_defect`; introduction candidates remain inconclusive until independent confirmation.

- [ ] **Step 5: Run recursive and legacy tests**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest \
  tests.test_recursive_analyzer \
  tests.test_backward_taint.BackwardTaintAnalyzerTest -v
```

Expected: all tests pass and legacy behavior is unchanged.

- [ ] **Step 6: Commit**

```bash
git add tools/trace_attribution/trace_attribution/recursive_analyzer.py \
  tools/trace_attribution/trace_attribution/__init__.py \
  tools/trace_attribution/tests/test_recursive_analyzer.py
git commit -m "feat(attribution): traverse recursive semantic defects"
```

---

### Task 6: Bounded Agentic Investigation Tools

**Files:**
- Create: `tools/trace_attribution/trace_attribution/investigation.py`
- Create: `tools/trace_attribution/tests/test_investigation.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`

**Interfaces:**
- Consumes: unknown/conflicting steps, graph/artifacts, current hypothesis, and budgets.
- Produces: `InvestigationDirective`, `InvestigationResult`, and `CausalInvestigationTools.execute(directive)`.

- [ ] **Step 1: Write failing tool and budget tests**

```python
import unittest


class InvestigationTest(unittest.TestCase):
    def test_unknown_relation_inspects_artifact_then_rejudges(self):
        graph = graph_with_hydratable_artifact()
        judge = ScriptedInvestigatingJudge([
            unknown_step_with_directive("inspect_artifact", {"artifact_id": "artifact_1"}),
            step_as_introduction_candidate("constraint visible after hydration"),
        ])
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            tools=CausalInvestigationTools(graph),
            max_investigation_rounds=2,
        ).analyze(graph, start_refs=["record:decision"], objective="Find the defect.")
        self.assertEqual(
            [item.tool_name for item in report.investigation_journal],
            ["inspect_artifact"],
        )
        self.assertEqual(judge.step_call_count, 2)

    def test_investigation_budget_exhaustion_is_inconclusive(self):
        report = run_always_unknown_case(max_investigation_rounds=1)
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertTrue(report.metadata["investigation_budget_exhausted"])
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_investigation -v
```

Expected: import failure for `CausalInvestigationTools`.

- [ ] **Step 3: Implement local read-only tools**

```python
class CausalInvestigationTools:
    ALLOWED = {
        "inspect_node", "expand_upstream", "expand_downstream", "inspect_artifact",
        "inspect_episode", "inspect_context_lineage", "inspect_task_obligations",
        "compare_causal_paths", "search_semantic_nodes",
    }

    def execute(self, directive: InvestigationDirective) -> InvestigationResult:
        if directive.tool_name not in self.ALLOWED:
            return InvestigationResult.rejected(directive, "unsupported offline investigation tool")
        return getattr(self, f"_{directive.tool_name}")(directive)
```

Results include requested/resolved refs, provenance, truncation, byte count, and evidence hash. Artifact reads enforce range and cumulative byte budgets. Tools never write files, invoke shell/network, or mutate `TraceGraph`.

- [ ] **Step 4: Add directive validation and re-entry**

Allow one evidence directive only for unknown, conflicting, truncated, or missing evidence. Validate tool and arguments. Reopen a visit only when the returned evidence hash changes; deduplicate identical directives.

Treat `record_hypothesis`, `reject_hypothesis`, and `request_root_confirmation` as separately validated attribution-control directives. The analyzer applies them to `HypothesisLedger` or the confirmation queue and journals their before/after state; they never mutate `TraceGraph`, Agent inputs, or benchmark artifacts. Rejecting a hypothesis requires cited opposing evidence or an independent verifier result, not a bare LLM preference.

- [ ] **Step 5: Run tests and commit**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_investigation tests.test_recursive_analyzer -v
git add tools/trace_attribution/trace_attribution/investigation.py \
  tools/trace_attribution/trace_attribution/recursive_analyzer.py \
  tools/trace_attribution/trace_attribution/causal_judge.py \
  tools/trace_attribution/tests/test_investigation.py \
  tools/trace_attribution/tests/test_recursive_analyzer.py
git commit -m "feat(attribution): investigate unresolved causal branches"
```

Expected: all tests pass; identical directives execute once.

---

### Task 7: Independent Confirmation And Multi-Factor Report

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Modify: `tools/trace_attribution/trace_attribution/trace_improvement.py`
- Modify: `tools/trace_attribution/tests/test_causal_judge.py`
- Modify: `tools/trace_attribution/tests/test_recursive_analyzer.py`

**Interfaces:**
- Consumes: introduction candidates, evidence, recursive path, hypotheses, obligations, and perspective.
- Produces: grounded `RootConfirmation`, roots/co-roots, conditions, amplifiers, rejections, and gaps.

- [ ] **Step 1: Write failing verifier and backtracking tests**

```python
import unittest


class RecursiveRootRankingTest(unittest.TestCase):
    def test_rejected_prompt_candidate_backtracks_to_agent_decision(self):
        judge = ScriptedCausalJudge(
            steps=sphinx_competing_hypothesis_steps(),
            confirmations={
                "record:prompt": RootConfirmation.rejected(
                    "record:prompt", "repository search could still satisfy the task"
                ),
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Now I have a comprehensive understanding",
                    reason="the decision stopped discovery before checking call sites",
                    counterfactual="searching call sites would reveal the method",
                    confidence=0.9,
                ),
            },
        )
        report = run_sphinx_minimal(judge, perspective="Improve Agent repository reasoning")
        self.assertEqual([root.node_ref for root in report.confirmed_roots], ["record:decision"])
        self.assertEqual(
            [item.node_ref for item in report.contributing_conditions],
            ["record:prompt"],
        )
```

Place the preceding test in `tests/test_recursive_analyzer.py`. Place this independent confirmation test in `tests/test_causal_judge.py`:

```python
import unittest


class CausalRootConfirmationTest(unittest.TestCase):
    def test_unknown_confirmation_never_becomes_root(self):
        report = run_single_candidate_case(
            RootConfirmation.unknown("record:decision", "missing artifact")
        )
        self.assertEqual(report.confirmed_roots, [])
        self.assertEqual(report.analysis_outcome, "inconclusive")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest \
  tests.test_causal_judge.CausalRootConfirmationTest \
  tests.test_recursive_analyzer.RecursiveRootRankingTest -v
```

Expected: failures because recursive candidates are not confirmed or ranked.

- [ ] **Step 3: Implement independent confirmation request**

```python
@dataclass(frozen=True)
class RootConfirmationRequest:
    candidate_ref: str
    defect_state: DefectState
    recursive_path: List[str]
    supporting_evidence: List[JsonDict]
    opposing_evidence: List[JsonDict]
    competing_hypotheses: List[JsonDict]
    task_obligations: List[JsonDict]
    analysis_perspective: str
```

The verifier prompt omits the first Judge's verdict wording. It attempts to falsify defect presence, precedence, absence of a stronger predecessor, counterfactual causality, and alternative explanations. Confirmed excerpts must be grounded in the candidate node or hydrated artifacts.

- [ ] **Step 4: Implement ranking and backtracking**

After rejection, mark the hypothesis rejected and reopen the best unresolved alternative. Perspective changes primary ordering and factor labels, never fact visibility or component eligibility.

```python
confirmed_roots: List[ConfirmedRoot]
co_roots: List[ConfirmedRoot]
contributing_conditions: List[CausalFactor]
amplifying_factors: List[CausalFactor]
rejected_candidates: List[RejectedCandidate]
unresolved_hypotheses: List[AttributionHypothesis]
```

- [ ] **Step 5: Extend deterministic trace-improvement feedback**

Add gap kinds `recursive_path_missing_edge`, `defect_transformation_missing_intermediate`, `competing_hypotheses_unresolved`, `root_confirmation_missing_evidence`, and `semantic_anchor_unstable`. Each recommendation names the component and missing fields without extra model calls.

- [ ] **Step 6: Run the complete offline suite**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Expected: zero failures and errors.

- [ ] **Step 7: Commit**

```bash
git add tools/trace_attribution/trace_attribution/causal_judge.py \
  tools/trace_attribution/trace_attribution/recursive_analyzer.py \
  tools/trace_attribution/trace_attribution/causal_state.py \
  tools/trace_attribution/trace_attribution/trace_improvement.py \
  tools/trace_attribution/tests/test_causal_judge.py \
  tools/trace_attribution/tests/test_recursive_analyzer.py
git commit -m "feat(attribution): confirm and rank causal roots"
```

---

### Task 8: Durable Checkpoint And CLI Migration

**Files:**
- Create: `tools/trace_attribution/trace_attribution/checkpoint.py`
- Create: `tools/trace_attribution/tests/test_causal_checkpoint.py`
- Create: `tools/trace_attribution/tests/test_recursive_cli.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Modify: `tools/trace_attribution/README.md`

**Interfaces:**
- Consumes: frontier/ledger/action state, cache path, circuit state, and CLI inputs.
- Produces: `CheckpointState`, `CheckpointBundle`, exact restore, engine/perspective/budgets, and documented output paths.

- [ ] **Step 1: Write failing checkpoint tests**

```python
import tempfile
import unittest
from pathlib import Path


class CausalCheckpointTest(unittest.TestCase):
    def test_checkpoint_restores_state_after_corrupt_tail(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.record_frontier("push", sample_frontier_item().to_dict())
            bundle.record_hypothesis("create", sample_hypothesis().to_dict())
            bundle.record_action(sample_action().to_dict())
            with bundle.frontier_path.open("a", encoding="utf-8") as handle:
                handle.write('{"truncated"')
            state = CheckpointBundle(root).restore()
            self.assertEqual(len(state.frontier_items), 1)
            self.assertEqual(len(state.hypotheses), 1)
            self.assertEqual(len(state.actions), 1)
            self.assertEqual(state.corrupt_entries, 1)

    def test_resume_does_not_repeat_completed_calls(self):
        with tempfile.TemporaryDirectory() as tempdir:
            interrupted, resumed, judge = interrupt_and_resume_recursive_case(Path(tempdir))
            self.assertEqual(
                resumed.confirmed_roots,
                uninterrupted_recursive_case().confirmed_roots,
            )
            self.assertEqual(judge.repeated_step_calls, 0)
            self.assertEqual(judge.repeated_investigation_calls, 0)
```

- [ ] **Step 2: Run checkpoint tests and verify RED**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_causal_checkpoint -v
```

Expected: import failure for `CheckpointBundle`.

- [ ] **Step 3: Implement three fsync-backed journals**

```python
class CheckpointBundle:
    def __init__(self, root: Path):
        self.root = root
        self.frontier_path = root / "frontier.jsonl"
        self.hypotheses_path = root / "hypotheses.jsonl"
        self.actions_path = root / "investigation-actions.jsonl"

    def record_frontier(self, operation: str, payload: JsonDict) -> None:
        self._append(self.frontier_path, operation, payload)

    def record_hypothesis(self, operation: str, payload: JsonDict) -> None:
        self._append(self.hypotheses_path, operation, payload)

    def record_action(self, payload: JsonDict) -> None:
        self._append(self.actions_path, "record", payload)

    def restore(self) -> CheckpointState:
        frontier, frontier_corrupt = read_journal(self.frontier_path)
        hypotheses, hypothesis_corrupt = read_journal(self.hypotheses_path)
        actions, action_corrupt = read_journal(self.actions_path)
        return replay_checkpoint(
            frontier,
            hypotheses,
            actions,
            corrupt_entries=frontier_corrupt + hypothesis_corrupt + action_corrupt,
        )
```

Each append writes version, sequence, timestamp, operation, semantic key, and payload, then flushes and fsyncs. Restore ignores corrupt tail records and applies events deterministically. Record state before the next Provider request.

- [ ] **Step 4: Write failing CLI tests**

```python
import unittest
from pathlib import Path


class RecursiveCliTest(unittest.TestCase):
    def test_recursive_engine_cli_defaults_and_paths(self):
        args = parse_args([
            "--trace", "/tmp/trace.json",
            "--out", "/tmp/case.attribution.json",
            "--engine", "recursive-agentic",
            "--analysis-perspective", "Improve Agent repository reasoning",
        ])
        self.assertEqual(args.engine, "recursive-agentic")
        self.assertEqual(args.max_frontier_items, 96)
        self.assertEqual(
            recursive_checkpoint_path(Path(args.out), args.checkpoint_dir),
            Path("/tmp/case.attribution.checkpoint"),
        )
```

- [ ] **Step 5: Add CLI engine and budgets**

```text
--engine legacy|recursive-agentic
--analysis-perspective TEXT
--checkpoint-dir PATH
--max-frontier-items 96
--max-depth 20
--max-hypotheses 24
--max-investigation-rounds 12
--max-artifact-bytes 1048576
--max-judge-requests 128
```

Keep `legacy` as default until Task 9 acceptance passes. The recursive engine writes attribution JSON, message lineage, Judge cache, and checkpoint directory. Reuse the existing SIGINT/SIGTERM shutdown path to fsync frontier, hypotheses, and investigation actions before returning an inconclusive partial report; SIGKILL remains recoverable only from the last fsynced journal entry. Document exact resume commands and explain that perspective affects ranking, not eligibility.

- [ ] **Step 6: Run CLI, checkpoint, and legacy tests**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest \
  tests.test_causal_checkpoint \
  tests.test_recursive_cli \
  tests.test_backward_taint -v
```

Expected: all tests pass and legacy output paths remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add tools/trace_attribution/trace_attribution/checkpoint.py \
  tools/trace_attribution/trace_attribution/recursive_analyzer.py \
  tools/trace_attribution/trace_attribution/cli.py \
  tools/trace_attribution/tests/test_causal_checkpoint.py \
  tools/trace_attribution/tests/test_recursive_cli.py \
  tools/trace_attribution/README.md
git commit -m "feat(attribution): resume agentic recursive analysis"
```

---

### Task 9: Sphinx Regression, Semantic Anchors, And Metrics

**Files:**
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/sphinx_recursive_minimal.json`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/prompt_wrong_agent_faithful.json`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/context_compaction_loss.json`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/tool_error_ignored.json`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/subagent_warning_ignored.json`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/multi_root_failure.json`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/inferred_missing_edge.json`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/timeout_amplifier.json`
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/success_negative_control.json`
- Create: `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`
- Create: `tools/trace_attribution/tests/test_recursive_benchmarks.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Modify: `tools/trace_attribution/README.md`
- Create: `docs/superpowers/reports/2026-07-20-agentic-recursive-attribution-results.md`

**Interfaces:**
- Consumes: recursive reports, human root labels, legacy reports, metrics, and normalized nodes.
- Produces: `semantic_anchor_id(case_id, node)`, benchmark comparison JSON, and measured results report.

- [ ] **Step 1: Freeze the baseline and add the regression fixture matrix**

Before changing the default engine, run the current legacy analyzer on Sphinx, Pydantic, and the available successful control, then record the exact semantic root labels, candidate recall, Top-1 result, Provider requests, and unresolved refs in the result report. This frozen baseline is the denominator for acceptance and prevents retrospective metric selection.

Add the minimal Sphinx fixture:

```json
{
  "case_id": "sphinx-recursive-minimal",
  "records": [
    {"record_id": "prompt", "component": "prompt", "event_type": "prompt.assembly", "data": {"text": "Implement the listed interfaces using the repository."}},
    {"record_id": "decision", "component": "processor", "event_type": "decision", "source_refs": ["record:prompt"], "data": {"decision_type": "reasoning_block", "rationale": "Now I have a comprehensive understanding. Implement the five listed methods."}},
    {"record_id": "change", "component": "tool", "event_type": "change", "source_refs": ["record:decision"], "data": {"files": ["sphinx/domains/c/_parser.py"], "summary": "implemented listed methods without parse_namespace_object"}},
    {"record_id": "verification", "component": "verification", "event_type": "verification", "source_refs": ["record:change"], "data": {"status": "failed", "failure": "parse_namespace_object is absent"}},
    {"record_id": "timeout", "component": "harness", "event_type": "run.interruption", "source_refs": ["record:verification"], "data": {"reason": "headers timeout"}},
    {"record_id": "observed", "component": "evaluation", "event_type": "case.observed_defect", "source_refs": ["record:verification", "record:timeout"], "data": {"failure_type": "missing_parse_namespace_object_contract"}}
  ]
}
```

Add eight focused fixtures covering a prompt that is explicitly wrong but faithfully implemented, context-compaction loss, ignored Tool error, ignored Subagent warning, independent co-roots, one missing direct edge requiring inferred retrieval, Harness timeout as amplifier only, and a successful no-defect control. Each fixture includes human labels for expected roots, conditions, amplifiers, allowed unresolved outcomes, and forbidden roots; fixture labels are never included in Judge prompts.

- [ ] **Step 2: Write failing anchor and benchmark tests**

```python
import unittest


class RecursiveBenchmarkTest(unittest.TestCase):
    def test_semantic_anchor_survives_run_local_id_and_cwd_changes(self):
        left = decision_node(record_id="decisionnode_dec_85_aaa", cwd="/run/a")
        right = decision_node(record_id="decisionnode_dec_85_bbb", cwd="/run/b")
        self.assertEqual(
            semantic_anchor_id("sphinx-case", left),
            semantic_anchor_id("sphinx-case", right),
        )

    def test_sphinx_perspective_ranks_decision_and_retains_factors(self):
        report = run_fixture("recursive_cases/sphinx_recursive_minimal.json", scripted_sphinx_judge())
        self.assertEqual(report.confirmed_roots[0].node_ref, "record:decision")
        self.assertIn("record:prompt", [item.node_ref for item in report.contributing_conditions])
        self.assertIn("record:timeout", [item.node_ref for item in report.amplifying_factors])

    def test_fixture_matrix_preserves_distinct_causal_roles(self):
        results = run_scripted_fixture_matrix()
        self.assertEqual(results["prompt_wrong_agent_faithful"].primary_root, "record:prompt")
        self.assertEqual(results["context_compaction_loss"].primary_root, "record:compaction")
        self.assertEqual(results["tool_error_ignored"].primary_root, "record:decision")
        self.assertEqual(results["subagent_warning_ignored"].primary_root, "record:main_agent_decision")
        self.assertEqual(len(results["multi_root_failure"].co_roots), 2)
        self.assertEqual(results["success_negative_control"].analysis_outcome, "no_defect")
```

- [ ] **Step 3: Implement cross-run anchors**

```python
def semantic_anchor_id(case_id: str, node: TraceNode) -> str:
    identity = {
        "case_id": case_id,
        "component": node.component,
        "event_type": node.event_type,
        "semantic_role": semantic_role(node),
        "normalized_semantics": normalized_anchor_semantics(node.data),
        "code_locations": normalized_code_locations(node.data),
        "action_identity": normalized_action_identity(node.data),
    }
    digest = hashlib.sha256(stable_json(identity).encode("utf-8")).hexdigest()[:24]
    return f"semantic_anchor:{digest}"
```

Normalization removes run IDs, absolute repository prefixes, timestamps, PIDs, ports, and provider request IDs while retaining relative paths, action names, rationale, and relevant artifact hashes.

- [ ] **Step 4: Implement comparison metrics**

```python
def compare_report(report: JsonDict, labels: JsonDict) -> JsonDict:
    candidates = report.get("confirmed_roots") or report.get("introduction_candidates") or []
    predicted = [str(item.get("semantic_anchor_id") or "") for item in candidates]
    expected = [str(item) for item in labels.get("root_semantic_anchor_ids") or []]
    return {
        "candidate_recall": bool(set(predicted) & set(expected)),
        "top1_match": bool(predicted and predicted[0] in expected),
        "judge_request_count": int(report.get("metadata", {}).get("judge_request_count") or 0),
        "fabricated_ref_count": len(report.get("metadata", {}).get("fabricated_refs") or []),
        "unresolved_hypothesis_count": len(report.get("unresolved_hypotheses") or []),
    }
```

The script accepts `--report`, `--labels`, `--legacy-report`, and `--out`, and adds request reduction, causal-path length, factor-role precision, unknown rate, confirmation rejection rate, investigation yield, checkpoint reuse, and human/LLM disagreement details. It exits nonzero when fabricated refs exist or a confirmed root cannot resolve its path, evidence, and confirmation.

- [ ] **Step 5: Run all offline tests**

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Expected: zero failures, zero errors, and zero fabricated refs.

- [ ] **Step 6: Run authorized real benchmark comparisons**

```bash
PYTHONPATH=tools/trace_attribution python3 -m trace_attribution.cli \
  --engine recursive-agentic \
  --trace /path/to/sphinx/partial/latest.json \
  --review docs/superpowers/reports/2026-07-15-loop2-causal-ir-featurebench-sphinx-review.json \
  --out /tmp/sphinx-recursive.attribution.json \
  --analysis-perspective "Improve Agent repository reasoning and implementation quality." \
  --model deepseek-v4-pro \
  --judge-timeout-sec 3600
```

Repeat with Pydantic, one requirements-understanding case, one context-compaction case, and one successful negative control. Enter the API key with the existing interactive secret pattern. Run each recursive case once cold and once from cache/checkpoint; record roots, factors, unresolved hypotheses, cache hits, request count, investigation actions, and manual agreement. Do not send human labels to the model.

- [ ] **Step 7: Evaluate acceptance**

```bash
python3 tools/trace_attribution/scripts/evaluate_recursive_attribution.py \
  --report /tmp/sphinx-recursive.attribution.json \
  --labels /tmp/sphinx-human-labels.json \
  --legacy-report /private/tmp/causal-judgment-context-sphinx-attribution-windowed.json \
  --out /tmp/sphinx-recursive.comparison.json
```

Expected: semantic `dec_85` is recalled and ranked first under the Harness-improvement perspective; prompt is a condition; timeout is an amplifier; fabricated refs are zero; Provider requests are at most 98, a 50% reduction from 196. Across the regression matrix, candidate-root recall is no lower than the frozen legacy baseline, the successful control has no confirmed root, and every unknown or budget-exhausted branch remains inconclusive.

- [ ] **Step 8: Report results and decide the default**

Write exact test counts, benchmark versions, labels, factors, request counts, cache behavior, unresolved hypotheses, human disagreements, and trace gaps to `docs/superpowers/reports/2026-07-20-agentic-recursive-attribution-results.md`. Change `--engine` default to `recursive-agentic` only if Sphinx, Pydantic, requirements-understanding, context-compaction, negative-control, and fixture-matrix acceptance pass; otherwise keep `legacy` and record blocking metrics.

- [ ] **Step 9: Commit**

```bash
git add tools/trace_attribution/tests/fixtures/recursive_cases \
  tools/trace_attribution/scripts/evaluate_recursive_attribution.py \
  tools/trace_attribution/tests/test_recursive_benchmarks.py \
  tools/trace_attribution/trace_attribution/causal_state.py \
  tools/trace_attribution/trace_attribution/cli.py \
  tools/trace_attribution/README.md \
  docs/superpowers/reports/2026-07-20-agentic-recursive-attribution-results.md
git commit -m "test(attribution): validate recursive causal investigator"
```

---

## Final Verification

- [ ] Run the complete suite:

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Expected: zero failures and errors.

- [ ] Check whitespace and malformed patches:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] Confirm Trace immutability by hashing the same real Trace before and after recursive analysis:

```bash
shasum -a 256 /path/to/sphinx/partial/latest.json
PYTHONPATH=tools/trace_attribution python3 -m trace_attribution.cli --engine recursive-agentic [same authorized arguments]
shasum -a 256 /path/to/sphinx/partial/latest.json
```

Expected: the SHA-256 values are identical.

- [ ] Scan for hard-coded semantic exclusions:

```bash
rg -n "component_allowlist|component_denylist|prompt_cannot_be_root|tool_must_be_root" \
  tools/trace_attribution/trace_attribution tools/trace_attribution/tests
```

Expected: no production matches.

- [ ] Inspect integration state:

```bash
git status --short --branch
git log --oneline -10
```

Expected: pre-existing user changes remain untouched and the nine implementation commits appear in order.
