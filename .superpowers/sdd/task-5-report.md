# Task 5 Report: Recursive Analyzer Core

## Scope

- Base commit: `9f74e21d1`
- Initial task commit: `c50934e2b`
- Independent-review fix commit: `d67711401`
- Final Judge-capability fix commit: this commit
- Current task range: `9f74e21d1..HEAD`
- Changed production/test files:
  - `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
  - `tools/trace_attribution/trace_attribution/__init__.py`
  - `tools/trace_attribution/tests/test_recursive_analyzer.py`
  - `tools/trace_attribution/trace_attribution/causal_judge.py`
  - `tools/trace_attribution/tests/test_causal_judge.py`
- The only adjacent production edit is the bounded physical-request allowance in
  `ClaudeCausalJudge`, required to enforce the Task 5 Judge budget immediately
  before initial and repair transport requests while preserving cache hits.
- No network calls or runtime dependencies were added.

## RED Evidence

1. Initial propagation/transformation tests failed with `ModuleNotFoundError: No module named 'trace_attribution.recursive_analyzer'`.
2. Boundary tests then exposed four implementation gaps:
   - empty `start_refs` did not fall back to graph defaults;
   - an empty trace incorrectly returned `no_defect`;
   - propagation with missing evidence still recursed;
   - a fabricated predecessor crashed context construction with `KeyError`.
3. A frontier accounting test showed depth-limited popped items were omitted from the 96-item traversal budget (`processed_frontier_items` was 1 instead of 2).
4. A public-interface test showed `RecursiveAnalysisState.create()` incorrectly required an explicit `max_hypotheses` argument.
5. A duplicate-transformation test showed an existing semantic hypothesis was rejected at the budget boundary (`hypotheses: 1` was incorrectly exhausted).
6. Independent review adversarial tests reproduced four additional failures before fixes:
   - a retained `present` defect with no recursive explanation or introduction produced `no_defect`;
   - competing same-defect predecessors shared the downstream seed hypothesis and bypassed `max_hypotheses`;
   - ordinary `data.content` text incorrectly consumed the artifact-byte budget;
   - a one-request Claude allowance made zero calls, while a cache hit was reported as one physical request.
7. Thirteen review-specific probes were run together. Before implementation, 12 failed or errored; only the already-correct duplicate grounded-artifact identity case passed.
8. The final P1 adversarial test ran a protocol-compatible Judge whose single
   `judge_step()` incremented its transport count twice under
   `max_judge_requests=1`. Before the fix, the Judge was invoked, produced an
   introduction candidate, and consumed two physical requests. A second RED
   run failed at import because the explicit offline capability and adapter did
   not yet exist. After implementation, the unbounded Judge is rejected before
   invocation with `judge_budget_unenforceable`, while the adapted legacy
   offline Judge remains callable with a zero physical-request allowance.

Each failure was observed before the corresponding production change, then rerun to GREEN.

## Design Choices

- The analyzer clones `TraceGraph.raw_trace` into an analysis-owned graph. Artifact hydration and context reconstruction therefore cannot mutate the caller's graph or Agent behavior.
- Evaluation assertions become `outcome_evidence` relations and seed concrete upstream records; the evaluator node is not treated as a defect introducer.
- Traversal uses the existing `RecursiveFrontier`, `HypothesisLedger`, `semantic_visit_key`, immutable `DefectState`, and `build_recursive_judgment_context` contracts.
- Every recursive predecessor explanation creates or reuses a hypothesis whose
  `candidate_root_ref` is that predecessor. Same-defect branches preserve the
  fingerprint; transformations retain a deterministic defect chain.
- Duplicate visits merge only when candidate, defect, and hypothesis semantics
  are identical. Distinct candidates, defects, or hypothesis claims remain
  separate frontier visits and consume the shared hypothesis budget.
- Introduction candidates carry a serialized binding to their exact hypothesis
  and defect fingerprint for Task 7 confirmation. Unrelated candidates are
  retained both as rejected candidates and ledger opposition.
- Only grounded propagation/transformation assessments recurse. Missing evidence and unresolved predecessor refs become explicit unresolved branches.
- A `present` defective dead end is explicitly unresolved. Report finalization
  also prevents `no_defect` whenever any visited judgment retained a defect.
  Introduction candidates remain unresolved/inconclusive and produce no
  confirmed root; independent confirmation is explicitly deferred to Task 7.
- Depth, frontier, hypothesis, artifact-byte, Judge-request, investigation, and provider-circuit boundaries are represented in report metadata and unresolved branches.
- Artifact-byte accounting scans only `hydrated_artifacts` manifests with a
  stable artifact identity; ordinary message, context, LLM, and tool `content`
  fields consume zero artifact bytes. Repeated grounded artifacts are charged once.
- Claude-backed Judge calls use a bounded allowance inside the cache/transport
  request path. Cache hits cost zero, one valid initial request is allowed at
  `max_judge_requests=1`, and a repair is blocked before its physical call when
  no allowance remains. Reports separate actual physical request delta from
  logical Judge call count.
- Analyzer invocation now requires one of two nominal, runtime-verifiable
  capabilities. `BoundedJudgeCapability` requires the allowance-aware call and
  is implemented by `ClaudeCausalJudge`; `OfflineJudgeCapability` guarantees
  zero transport requests. `OfflineCausalJudgeAdapter` is the explicit opt-in
  bridge for legacy in-process Judges. A Judge with neither capability is never
  invoked and becomes an explicit unresolved branch. The legacy direct
  `judge_step()` API remains available outside the analyzer.
- Recursive request contexts include objective, analysis perspective, active hypothesis/visit, checked evidence, obligations, downstream judgments/path, candidate provenance, transformation chain, and a SHA-256 evidence hash.
- Reports preserve frontier checkpoints, hypothesis snapshots, merged evidence, paths, judgments, candidates, unresolved facts, and legacy-compatible fields.

## Verification

Focused Task 5 tests:

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache PYTHONPATH=. \
python3 -m unittest tests.test_recursive_analyzer -v
Ran 34 tests - OK
```

Task 5 plus legacy analyzer regression:

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache PYTHONPATH=. \
python3 -m unittest tests.test_recursive_analyzer \
  tests.test_backward_taint.BackwardTaintAnalyzerTest -v
Ran 76 tests - OK
```

Task 5 plus recursive Judge tests:

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache PYTHONPATH=. \
python3 -m unittest tests.test_recursive_analyzer tests.test_causal_judge -v
Ran 92 tests - OK
```

Full offline suite:

```text
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache PYTHONPATH=. \
python3 -m unittest discover -s tests -v
Ran 264 tests - OK
```

Static whitespace check:

```text
git diff --check
exit 0
```

## Risks and Deferred Work

- Bounded semantic investigation is intentionally deferred to Task 6. Suggested investigations are preserved as unresolved facts.
- Independent root confirmation is intentionally deferred to Task 7; this task cannot emit confirmed roots.
- Graph cloning adds offline memory/CPU overhead proportional to trace size, in exchange for strict input immutability.
- Third-party provider-backed Judges must explicitly implement
  `BoundedJudgeCapability`; otherwise recursive analysis records
  `judge_budget_unenforceable` without invoking them. Legacy zero-transport
  Judges require the explicit offline capability or adapter.
- Semantic fallback retrieval remains bounded by the existing retriever limit and is evidence candidate generation only; temporal proximity is never promoted into a causal relation.
