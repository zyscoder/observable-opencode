# Loop 4 FeatureBench Sphinx Attribution Review

## Case And Evaluation

- Benchmark: FeatureBench Lite
- Instance: `sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1`
- Execution path: `opencode serve -> session -> HTTP message`
- Trace records: 1,768
- Message-lineage reconstruction: 70 turns, 550 snapshots, 1,048 edges
- Attribution-eligible reconstructed edges: 955
- Reconstruction gaps: 0

The HTTP request stopped returning after the last recorded response step. A SIGTERM was sent after the trace had stopped growing, which finalized `trace.json` and `trace.html` with status `cancelled`. The signal is external run termination and is not treated as an agent semantic defect.

The official Docker evaluator was unavailable. The hidden C-domain test file was restored and run with Python 3.12 plus the case dependencies. Of the 14 parser/AST tests that could run independently, 8 passed and 6 failed. Another 21 tests failed during fixture setup because the host lacked `roman_numerals`; those setup errors were excluded from feature correctness.

## Manual Backward Analysis

### Parser Contract Defect

Observed outcome: `parse_declaration()` requires `struct`, `union`, and `enum` inputs to repeat the directive keyword, and accepts at least one invalid macro declaration.

Backward path:

1. Hidden-test failures expose the incorrect contract.
2. `chgnode_chg_2_1ee08ab1` materializes the faulty parser.
3. `decisionnode_dec_162_d4c07ef3` issues the edit with the faulty implementation.
4. `decisionnode_dec_160_d3011460` introduces the semantic assumption that these declaration kinds should parse their keywords from the input.

Manual root: `decisionnode_dec_160_d3011460`. The edit decision and change record are action/materialization stages of the same causal episode, not independent roots.

### Verification Strategy Defect

Observed outcome: the handwritten smoke test prints `ALL TESTS PASSED!` but does not exercise the real parser contract.

Backward path:

1. The successful tool result truthfully reports the script output and is evidence only.
2. The tool call contains test cases such as `struct MyStruct`, which encode the same incorrect contract as the implementation.
3. `decisionnode_dec_362_abbf250d` authors and executes that test script.

Manual root: the test-authoring action represented by `decisionnode_dec_362_abbf250d` and its corresponding tool call should be one causal episode. `decisionnode_dec_360_0d63baad` merely states an intent to run a comprehensive test and is not defective by itself.

### Scope Management Defect

Observed outcome: the patch modifies unrelated logging, datetime, and types compatibility code to accommodate host Python 3.9.

Backward path:

1. Import and environment failures are truthful evidence that the host runtime is incompatible.
2. `decisionnode_dec_346_dac72e2f` chooses to repair the benchmark repository rather than use or identify the expected runtime.
3. Later edit and change nodes materialize that choice.

Manual root: `decisionnode_dec_346_dac72e2f`. The environment failure motivates the decision but does not itself contain or propagate an out-of-scope-change defect.

## Offline Module Result

The module completed with `root_found`, `queue_exhausted`, 17 node judgments, no judge errors, and no search-limit termination.

Correct behavior:

- It reached the parser planning decision recovered through message lineage.
- It did not promote SIGTERM, hidden-test output, or truthful tool results to parser roots.
- It identified the parser contract defect with high confidence.

Incorrect behavior:

- It reported the parser plan, edit decision, and code-change record as three independent roots instead of one causal episode.
- It labeled the defective self-test authoring action as `defect_evidence`, so the verification defect has no root.
- It labeled the out-of-scope compatibility decision as `defect_propagation` with no defective upstream influence, so the scope defect has no root.
- It treated a non-defective environment failure as if it transmitted the scope defect into later changes.

## Next Improvement

The trace is sufficient for these three manual conclusions. The next bottleneck is attribution precision, not missing trace semantics.

1. Add causal-episode clustering for `reasoning decision -> LLM tool-call decision -> tool.call -> change/result` so one action is not emitted as several roots.
2. Make `defect_propagation` invalid unless at least one cited upstream node is judged defective with the same defect lineage.
3. Explicitly distinguish `motivated_by_evidence` from `defect_propagated_from`; non-defective evidence may trigger a defective choice, in which case the choice is an introduction.
4. Judge authored verification semantics at the tool-call/action node. A truthful tool result remains evidence, but an inadequate test script can be a defect introduction.
5. Produce root coverage per observed defect. A global `root_found` result must not hide observed defects that terminate without a valid root.

## Acceptance

Scheme C passes the reachability and evidence/root separation gate on a second open-source repository. It does not yet pass the root-set precision gate. The next loop should optimize causal-episode deduplication and per-defect root completeness before adding more runtime trace fields.
