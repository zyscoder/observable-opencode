# Loop 2 Causal IR Attribution Comparison

## Scope

- Benchmark: FeatureBench Lite
- Case: `sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1`
- Agent path: `opencode serve -> POST /session -> POST /session/:id/message`
- Model: DeepSeek `deepseek-v4-pro`
- Trace: Causal IR 1.0 / trace 6.0
- Attribution: backward semantic taint, depth 24, 128 nodes per branch, one-hour judge timeout

## Independent Evaluation

The restored official fail-to-pass file produced 28 passes and 7 failures. Six
failures share one missing compatibility method,
`DefinitionParser.parse_namespace_object`. One failure accepts the invalid
macro form `M(arg1, arg2..., arg3)`. The five pass-to-pass files produced 81
passes and no failures.

The HTTP client timed out before response headers after about 300 seconds and
the runner sent SIGINT while the agent was still editing. The agent did not
claim completion and did not reach its planned pytest step. Signal finalization
preserved a complete partial trace and correctly marked it cancelled.

## Attribution Result

The offline module completed with zero judge errors. Its primary outcome was
`partial_root_found`:

- `invalid_named_variadic_macro_is_accepted`: root found at
  `record:decisionnode_dec_107_6ff6b852`, confidence 0.95.
- `missing_parse_namespace_object_contract`: root reported at the same
  decision, confidence 0.95.
- `http_client_headers_timeout_interrupts_case_before_verification`:
  inconclusive because the trace starts at the observed SIGINT boundary and
  does not contain the external Undici timeout event.
- Trace-health branches remained advisory and did not replace the task-quality
  outcome.

## Agreement With Manual Analysis

The macro root is exact. Decision `dec_107` contains the complete authored edit,
and its `_parse_macro` branch permits a comma after a named variadic parameter.
The Causal IR links the prompt, message transformations, LLM generation,
decision, semantic tool call, tool result, and edit episode closely enough for
the module to explain the failure without guessing.

The lifecycle branch is also handled correctly. The module identifies the
cancelled case and unfinished verification as evidence, but does not invent an
in-process origin for an external HTTP client timeout that the server never
observed.

## Disagreement And Root Boundary Error

The namespace result stops too late. `parse_namespace_object` was already
absent in the masked baseline before `dec_107`; the edit adds other methods but
does not delete this one. Therefore `dec_107` did not introduce the missing
method. It failed to repair a pre-existing repository defect.

The deeper causal chain is:

1. The benchmark masking patch removes a source block containing
   `parse_namespace_object`.
2. The prompt lists `parse_declaration` and `parse_expression` but omits the
   compatibility method still used by unchanged consumers.
3. Decision `dec_90` explicitly proceeds from the listed interfaces after no
   visible C-domain test is found, without searching consumers for additional
   `DefinitionParser` contracts.
4. Decision `dec_107` materializes that incomplete recovery plan.

The model recognized steps 2 and 3 as upstream defect propagation, but the root
algorithm still labeled step 4 as introduction because it treats the first
authored defective action as the materialization boundary. This is wrong for
missing-behavior defects when the behavior was absent before the action.

Prompt evidence was also clipped at 16,000 artifact characters during judge
prompt construction. The decisive end of the interface list fell outside the
excerpt, causing one prompt node to be marked unknown even though the full
18-KiB artifact exists in the trace.

## Trace Quality

Strengths:

- Zero unresolved-reference diagnostics; prior alias amplification is fixed.
- 144 of 174 decisions carry prompt, context-transform, and LLM-generation
  provenance.
- Exact call identity proves which historical tool results entered each later
  model request.
- SIGINT preserves all eight changes, open lifecycles, artifacts, manifest, and
  HTML without feeding any trace-derived information back to the agent.

Weaknesses:

- The semantic tool-call node and span-level tool-call node for the same call ID
  remain distinct. The decision points to one while the repository change edge
  starts at the other.
- 3,920 repeated context-membership edges and 2,927 temporal advisory edges
  dominate an 8,888-edge graph with only 804 nodes.
- Full message snapshots are repeatedly materialized. The trace directory is
  323,208 KiB; artifacts contain 112,715,768 stored bytes and `records.jsonl`
  alone is 88,589,338 bytes.
- 18,704 raw stream delta events remain useful for forensic replay but are poor
  default inputs for semantic review.

## Next Loop Changes

### Causal IR

1. Canonicalize all semantic and span-level tool nodes by call ID, or add an
   explicit confirmed identity bridge so `decision -> tool -> result -> change`
   is continuous.
2. Represent a model request's selected tool results as one context-membership
   set attached to a context snapshot. Decisions should point to the snapshot;
   offline expansion should be lazy.
3. Move `recent_source_fallback` edges out of the attribution-bearing core into
   an optional advisory projection or aggregate them by source and lifecycle.
4. Store message transformations as content-addressed message units plus an
   ordered composition manifest. Reconstruct full inputs in HTML instead of
   persisting a new 600-KiB snapshot at every stage.
5. Add an explicit harness request lifecycle record. A client abort should send
   a passive control-plane reason such as `http_headers_timeout` before SIGINT;
   this must not alter agent prompts or decisions.

### Offline Attribution

1. Add change-aware defect origin semantics: `pre_existing`, `introduced`,
   `propagated`, and `failed_to_remediate`. An additive edit cannot introduce a
   missing symbol that was already absent in its before-state.
2. Add remediation-boundary analysis for benchmark tasks. For a pre-existing
   defect, locate the earliest discovery/planning decision that made successful
   repair unlikely, rather than automatically selecting the final edit.
3. Use target-aware artifact excerpts. Select prompt sections by objective
   keywords and interface names, with head/tail fallback, instead of always
   taking the first 16,000 characters.
4. Let review defects declare `task_quality`, `trace_health`, or
   `harness_quality`; do not hard-code every observed defect as task quality.
5. Prefer explicit identity and execution edges during traversal. Context-set
   membership should be expanded only when a judge asks whether a particular
   fact influenced the action.

## Loop 2 Verdict

Causal IR is now good enough to identify a concrete faulty action and explain
its local semantics. It is not yet reliable enough to distinguish defect
introduction from failure to repair a pre-existing defect, and graph/storage
amplification remains substantial. The next loop should optimize both causal
boundary correctness and trace compactness, not add more raw events.

## Post-Run Hardening

A review after the benchmark run found four provenance-boundary risks that did
not change the Sphinx evaluation above but could corrupt later traces. The
implementation now:

- anchors production-shaped LLM and context records to one assistant turn and
  selects context snapshots through the exact LLM span;
- extracts tool call IDs only from structured fields, scopes tool call/result
  nodes by session, and uses canonical node refs for context and evidence
  backfill while preserving legacy compatibility refs;
- accepts subagent consumption only after child completion and within the
  parent session; and
- keeps trace-health judge errors, unresolved refs, and search limits advisory
  when an independent task-quality branch has a confirmed root.

These changes are covered by cross-session, call-ID-prefix, subagent ordering,
and attribution-domain regressions. They do not claim to solve the remaining
`pre_existing` versus `failed_to_remediate` boundary; that remains the first
objective of the next benchmark loop.
