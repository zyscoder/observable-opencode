# Loop 3 Open-Source FeatureBench Review

## Benchmark Selection

- Benchmark: FeatureBench Lite (`LiberCoders/FeatureBench`, MIT)
- Instance: `pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1`
- Repository: `pydantic/pydantic`
- Capability focus: requirement understanding, cross-module impact analysis, descriptor design
- Execution path: `opencode serve -> HTTP session -> HTTP message -> agent loop`
- Model used for case execution: `deepseek-v4-pro`

The runner used the official problem statement and masked repository state. Neither the hidden test file nor the reference implementation was exposed to the agent.

## Execution Result

- Case status: completed normally
- Duration: 1,399,458 ms (about 23.3 minutes)
- Trace records: 5,101
- Repository changes: 24 revisions across 6 production files
- Trace token total: 49,598,074, including cached input accounting
- Agent-reported public tests: 94 passed, 3 failed, 4 skipped, 1 xfailed
- Restored hidden FeatureBench test: 10 passed, 2 failed (83.3%, unofficial host-side result)
- Official Docker evaluation: not run because Docker is unavailable on this host

The two hidden-test failures are semantically meaningful rather than simple syntax errors:

1. `GenerateJsonSchema.encode_default` is missing, so deprecated fields with defaults fail JSON Schema generation.
2. `rebuild_model_fields` loses a default while rebuilding a forward annotation, making an optional defaulted field required.

The pass-to-pass set cannot be scored reliably on this macOS host because its system Python 3.9 cannot collect one typing test and lacks compatibility dependencies required by another test.

## Manual Backward Taint Analysis

### Partial correctness

The final 10/12 hidden-test outcome points backward to incomplete cross-module implementations in `pydantic/json_schema.py` and `pydantic/_internal/_fields.py`. The trace contains the corresponding file changes and diffs, but the old run did not create formal verification records for the public pytest commands, so the change-to-failure path is weaker than it should be.

### Premature completion

The decisive chain is visible in the trace:

1. The broad public run reports 3 failed and 94 passed.
2. Decision `decisionnode_dec_1093_ee253ed8` concludes that the failures are edge cases outside the core interfaces.
3. The agent replaces the broad failing suite with a narrow hand-written smoke script covering only happy paths.
4. The smoke script passes, after which every todo is marked complete.
5. The agent stops and opens the final answer with “All interfaces are implemented and verified,” while later disclosing the 94/102 result.

The hidden 10/12 result disproves the scope judgment in step 2: two failures do affect required field/default behavior. The primary optimization point is therefore the agent decision/action layer's test-scope assessment, followed by the incomplete implementation it accepted.

### Trace evidence defect

The old trace records absolute-path pytest calls as generic command observations because verification detection only recognized bare `pytest` or `python -m pytest`. Commands also piped output through `tail`, so the shell exit code represented `tail`, not pytest. Consequences:

- no formal verification records were emitted;
- a false `missing_verification_after_change` issue was generated;
- failed broad-suite evidence was not linked as conflicting evidence;
- the final verification claim was instead matched to narrower passing smoke checks.

## Applied Improvements

1. Verification command classification now recognizes virtualenv/absolute-path pytest and `uv run pytest` forms.
2. Pipelined verification status is inferred conservatively from the test summary; a trailing command's zero exit code is no longer treated as proof of success.
3. The FeatureBench runner waits for asynchronous `trace.json` finalization after process shutdown and preserves server stdout/stderr logs per case.
4. The attribution graph now resolves `decision_id` aliases, restoring existing `decision -> tool_call` dataflow edges that were previously dropped during graph construction.
5. The attribution judge now disables DeepSeek thinking mode by default for the Anthropic-compatible endpoint. This prevents the reasoning budget from consuming the structured JSON answer budget; the setting and resolved thinking configuration are recorded in report metadata.

All changes remain passive: they observe existing commands, outputs, repository deltas, and process lifecycle events without changing prompts, tool results, or agent control flow.

## Offline Attribution Comparison

The first attribution attempt kept DeepSeek thinking enabled with a 512-token output budget. It completed without API errors, but all four visited judgments were `unknown` and no root was found. The model spent the available answer budget on reasoning instead of the required structured result.

After the endpoint-aware thinking change, `deepseek-v4-pro` completed the full three-defect analysis with `max_depth=8`, `max_nodes=48`, a one-hour per-call timeout, no judge errors, and 22 visited nodes. It correctly found:

- the contradiction between “All interfaces are implemented and verified” and the known three test failures;
- the defective final response, response claim, and generating LLM call;
- the missing link between failed broad verification and the final completion claim.

It did not reproduce the complete manual root-cause chain. In particular:

- it stopped at the final response/LLM surface instead of reaching the incorrect scope decision `decisionnode_dec_1093_ee253ed8`;
- it treated the pytest tool call as a root boundary merely because its truthful output contained failing tests, conflating defect evidence with defect introduction;
- it followed `decisionnode_dec_1085_76b39b97`, the decision to run tests, rather than the later decision that dismissed their failures;
- three code-change nodes remained `unknown` because the trace did not link each changed symbol to the assertions it affected.

The offline module is therefore useful for detecting observable answer defects and evidence conflicts, but it is not yet reliable enough to identify the earliest responsible component. Relative to the manual chain, its current result is a partial attribution rather than a root-cause diagnosis.

## Remaining Gaps

### Semantic trace

- Reasoning-block decision nodes are not explicitly linked to the subsequent tool decision or final stop gate, even though the tool decision duplicates `recent_reasoning` in metadata.
- Verification evidence needs scope and strength semantics so broad failing suites outrank narrow passing smoke checks for completion claims.
- Host/offline benchmark evaluation is not represented as a structured external-evaluation sidecar node, so hidden-test quality remains available only through the review input.
- Claim records serialize very large candidate evidence lists and duplicate them in metadata artifacts. These lists are expensive but add little causal value once direct, conflicting, and dependency evidence have been selected.
- Repetitive context transforms and loop decisions dominate the record count. They should remain available in raw events but be collapsed into semantic stages in the default causal view.

### Offline attribution

- The graph still depends heavily on instrumentation-specific aliases; semantic IDs need a general alias registry rather than one-off event handling.
- Attribution should persist each completed node judgment so a slow or interrupted model call does not discard prior work.
- The judge should support an explicit latency-aware fallback policy from the high-quality model to a faster model while preserving the model used for each judgment.
- Node judgment must distinguish `defect_evidence`, `defect_carrier`, and `defect_introduction`; a faithful failing test result is evidence, not the cause of the implementation defect.
- Root-cause scoring should compare the inferred chain with the manual chain: failed broad verification -> incorrect scope decision -> narrow smoke verification -> completion/stop.

## Next Loop Proposal

1. Add explicit `reasoning decision -> action decision -> tool/result -> completion decision -> exit gate` provenance.
2. Add verification scope (`targeted`, `module`, `regression`, `hidden/external`) and conflict precedence based on revision, scope, status, and recency.
3. Add an immutable external evaluation sidecar schema for benchmark scores and failing assertions.
4. Introduce `defect_evidence`, `defect_carrier`, and `defect_introduction` roles in the offline judgment schema and only allow introduction nodes to become root causes.
5. Compact candidate evidence in `trace.json` to ranked summaries while retaining full lists as optional artifacts.
6. Re-run a different FeatureBench Lite instance, preferably the Sphinx symbol-graph case, to test architecture understanding and cross-reference reasoning rather than repeating the same Pydantic behavior.
