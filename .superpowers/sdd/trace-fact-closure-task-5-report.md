# Trace Fact Closure Task 5 Report

## Status

Current producer acceptance: PASS.

Historical archive compatibility: FAIL-CLOSED WITH KNOWN GAPS. The frozen Axios,
Astropy, and TerminalBench archives were inspected without rewriting them. They
have no formal subject revision, their bundle directories contain zero artifact
files, and they contain no revision-bound external evaluation seed. Astropy also
retains one comma-leading historical claim fragment.

Phase result: Task 5 accepts the current producer only. Historical archive
portability remains limited and the three old cases must be regenerated before
they can satisfy current fact-closure gates.

## Scope And Method

- Base and starting HEAD: `206d3f5670d0f1d965436df1c451c698683e668e`.
- No Agent, attribution Judge, LLM, API, or network call was made.
- Passive equivalence ran the real `Tool.define` wrapper in separate child
  processes. Disabled ran first and enabled ran second with independent trace
  roots. Neither child reads trace or attribution output; the parent inspects
  the enabled trace directory only after both executions complete.
- Historical inspection loaded each frozen `partial/latest.json` twice with
  current `TraceGraph.from_file()`, hydrated every graph node, and asserted both
  metric dictionaries were identical.
- Production regressions are hermetic. They use generated temporary CaseTrace
  bundles and minimal in-test Python fixtures, never `/private/tmp` archives.

## TDD Evidence

### RED

1. The new passive child harness initially failed before producing JSON because
   a temporary script could not resolve `effect`; after using the package's
   resolved module URL, it reached execution but timed out because its managed
   runtime was still live. Explicit runtime disposal fixed the harness. A final
   RED showed `Schema.Literal("success", "error")` accepted only the first
   literal under Effect v4, so the test moved to the existing `Schema.Union`
   pattern. No production behavior change was required.
2. The exact Astropy producer test initially failed in a broad Bun matcher that
   touched the received artifact path. Direct field assertions preserved the
   object and verified its bytes and hashes.
3. The current Python closure fixture initially reported
   `loaded=0, missing=1, slice_fallbacks=0`: its declared full artifact byte
   length was shorter than its semantic slice. This demonstrated fail-closed
   integrity enforcement. Correct internally consistent metadata produced the
   expected verified fallback.

Task 1-4 production support already existed at the requested base, so Task 5 is
an acceptance and characterization task. The observed RED failures were in the
new acceptance harness and fixture metadata, not missing production code.

### GREEN

- Passive focused gate: 1 pass, 0 fail, 3 filtered.
- Exact Astropy producer gate: 1 pass, 0 fail, 142 filtered.
- Hermetic Python digest fixtures: 4 tests, OK.
- Required full TypeScript command: 241 pass, 0 fail.
- Full offline Python suite: 555 tests, OK; no skipped tests or retries.
- TypeScript typecheck: exit 0.
- Python compileall with `PYTHONPYCACHEPREFIX` under `/private/tmp`: exit 0.
- `git diff --check`: exit 0.

## Passive Equivalence

The disabled, enabled, and invalid-root child outputs are exactly equal for all
three scenarios. The test fixes and asserts:

- decoded tool inputs: success, error, and pre-aborted with
  `payload="fixed-input"`;
- success output: title `passive success`, output `stable tool output`, and
  metadata `{scenario: "success", truncated: false}`;
- original thrown error constructor/name/type/message, captured before it is
  passed to `SessionProcessor.failToolCall`: `PassiveToolError`,
  `PassiveToolError`, `PassiveToolError`, `passive benchmark failure`;
- callback counts: metadata 3, permission 3;
- the exact production-generated three-message Agent-visible projection with
  three tool calls and three tool results, not synthesized callback/result/error
  items.

The disabled trace case directory is absent. The enabled directory contains
`records.jsonl`. No behavioral output includes trace state.

## Frozen Historical Reconstruction

Artifact columns are `loaded / missing / slice fallback / hash mismatch` after
hydrating every reconstructed node. IR counts come from frozen Causal IR arrays;
Graph counts include current deterministic offline reconstruction.

| Case | Broken fragments | Artifact outcomes | Bundle files | External seeds | Formal revision | Causal IR nodes/edges | TraceGraph nodes/edges |
| --- | ---: | --- | ---: | ---: | --- | ---: | ---: |
| Axios `axios__axios-5892` | 0 | `0 / 170 / 0 / 0` | 0 | 0 | absent | `507 / 1334` | `522 / 1277` |
| Astropy `astropy__astropy-12907` | 1 | `0 / 184 / 0 / 0` | 0 | 0 | absent | `518 / 1318` | `533 / 1257` |
| TerminalBench `cancel-async-tasks` | 0 | `0 / 110 / 0 / 0` | 0 | 0 | absent | `258 / 566` | `267 / 535` |

Additional artifact facts:

| Case | Indexed artifacts | Record references | Unique referenced artifacts | Truncated loads |
| --- | ---: | ---: | ---: | ---: |
| Axios | 234 | 208 | 170 | 0 |
| Astropy | 258 | 232 | 184 | 0 |
| TerminalBench | 129 | 124 | 110 | 0 |

The unchanged Astropy fragment is record
`responseclaim_claim_515_882c4186`, beginning `, a pre-computed separability
matrix...`. Current reconstruction reports it; it does not repair or replace
the historical bytes.

## Current Producer Acceptance

1. The exact historical Astropy final response now produces three claims. The
   full `_cstack` sentence, including `(i.e., a pre-computed separability matrix
   from a nested compound model)`, remains one claim and no claim starts with
   punctuation.
2. Current CaseTrace captures an immutable formal subject revision and writes a
   bundled artifact with a relative path, matching byte length and SHA-256
   content hash, plus a byte-addressed semantic slice with a matching slice
   hash and `truncated=true`.
3. The current TerminalBench-style fixture binds a failed external evaluation
   to the exact formal revision. It reconstructs as the sole default failed
   seed with `revision_status=matched`,
   `revision_provenance_status=valid`, and
   `eligible_for_decisive_judgment=true`; it remains root-ineligible. Its absent
   bundle file falls back only to a verified truncated semantic slice:
   `loaded=1, missing=0, slice_fallbacks=1, hash_mismatches=0`.

## Concerns

- The three frozen archives cannot pass current closure. Missing historical
  artifact bytes cannot be recovered from their manifests, and their old
  descriptors contain no verified semantic slices.
- TerminalBench's old external grader result cannot become decisive because
  the archive has neither a formal subject revision nor an imported external
  evaluation fact. Adding a synthetic fact to the old bytes would falsify
  provenance, so regeneration is required.
- Astropy's current producer regression proves future output is atomized
  correctly; it does not alter the archived comma-leading record.
- The first compileall attempt targeted macOS's protected global pycache and
  failed with permission errors only. The required compile check then passed
  with an isolated writable pycache under `/private/tmp`.

## Files

- `packages/opencode/test/tool/semantic-observability.test.ts`
- `packages/opencode/test/observability/case-trace.test.ts`
- `tools/trace_attribution/tests/test_recursive_benchmarks.py`
- `.superpowers/sdd/trace-fact-closure-task-5-report.md`
- `docs/superpowers/reports/2026-07-21-trace-fact-closure-results.md`

## Commit

Planned commit: `test(trace): verify passive fact closure on benchmarks`.

## Review Closure Addendum (2026-07-22)

This addendum corrects the earlier passive-oracle description. The corrected
test does not append result or error messages.
It executes a real `Tool.define` definition through the normal wrapper branch
(the tool result does not predeclare `metadata.truncated`), then sends the
resulting `tool-result` or `tool-error` stream event through
`SessionProcessor.process`. Production `completeToolCall` / `failToolCall`
persist the final tool part, and `MessageV2.toModelMessages` produces the
Agent-visible projection.

The deep-equality oracle compares the full stable projection for tracing off,
tracing on, and tracing on with a trace root that is an ordinary file. It
covers success, a thrown `PassiveToolError`, and a pre-aborted signal. Exact
assertions cover decoded inputs; three metadata and three permission callback
payloads; completed/error status; wrapper-added `truncated=false`; title,
output, metadata, attachment projection; and production-formatted error text.
The invalid trace root changes none of these values or callback behaviors.

The focused passive test took 1.72-2.09 seconds in fresh local runs, including
three child executions. This is characterization only: the test has no elapsed
time assertion and this phase defines no hard latency SLA.

The artifact oracle now derives source bytes as
`Buffer.from(JSON.stringify({ payload: artifactPayload }), "utf8")`, without
reading expected values from the producer manifest. It compares exact bytes
and UTF-8 text, derives the full SHA-256 independently, checks the public
16-hex manifest projection, exact byte length, and the exact 512-character
semantic prefix, byte range, truncated marker, and independently computed
slice hash. Both new TypeScript temporary roots are removed in `finally`
blocks.

### Reproducible Historical Inputs

| Case | Exact local `partial/latest.json` | SHA-256 |
| --- | --- | --- |
| Axios | `/private/tmp/observable-opencode-multibench/runs/formal-v3/swebench-multilingual/axios__axios-5892/traces/axios__axios-5892/partial/latest.json` | `1c934c47fe01719abaed45b2825ab4b88b1f1ea7fc1d8885ea2f76feed9c9ead` |
| Astropy | `/private/tmp/observable-opencode-multibench/runs/formal-v3/swebench-verified/astropy__astropy-12907/traces/astropy__astropy-12907/partial/latest.json` | `e5209173e7576b822921025b6cd9f80fde3d1062c3f85ceeae312c56e3012b17` |
| TerminalBench | `/private/tmp/observable-opencode-multibench/runs/formal-v3/terminalbench/cancel-async-tasks/traces/cancel-async-tasks/partial/latest.json` | `3760043e3016d0ee29278cb5dd8b63f94575e8f3dcca70551c324e2409a5c011` |

Deterministic no-LLM characterization command:

```bash
PYTHONPATH=tools/trace_attribution python3 tools/trace_attribution/scripts/characterize_trace_fact_closure.py \
  --expected-sha256 1c934c47fe01719abaed45b2825ab4b88b1f1ea7fc1d8885ea2f76feed9c9ead \
  --expected-sha256 e5209173e7576b822921025b6cd9f80fde3d1062c3f85ceeae312c56e3012b17 \
  --expected-sha256 3760043e3016d0ee29278cb5dd8b63f94575e8f3dcca70551c324e2409a5c011 \
  /private/tmp/observable-opencode-multibench/runs/formal-v3/swebench-multilingual/axios__axios-5892/traces/axios__axios-5892/partial/latest.json \
  /private/tmp/observable-opencode-multibench/runs/formal-v3/swebench-verified/astropy__astropy-12907/traces/astropy__astropy-12907/partial/latest.json \
  /private/tmp/observable-opencode-multibench/runs/formal-v3/terminalbench/cancel-async-tasks/traces/cancel-async-tasks/partial/latest.json
```

The script requires one expected SHA-256 per archive and validates every source
digest before it loads, hydrates, or emits metrics. It then loads each archive
twice, hydrates every graph node, rejects nondeterministic reconstruction, and
emits the artifact, seed, revision, and IR/graph metrics used by the tables
above. Its automated tests use only temporary synthetic archives; the large
local archives remain uncommitted.

### Corrective RED/GREEN Evidence

- RED: focused Bun failed because
  `test/tool/fixture/passive-tool-projection.ts` did not exist; focused Python
  failed with `ModuleNotFoundError` for
  `scripts.characterize_trace_fact_closure`.
- RED: the first typed fixture run rejected invalid branded IDs; the first
  full typecheck then rejected an error-channel tool definition, a missing
  permission metadata field, and an unbranded part ID.
- GREEN: passive focused gate `1 pass, 0 fail`; full semantic-observability
  file `4 pass, 0 fail`; exact Astropy gate `1 pass, 0 fail`; Task 5 Python
  digest gate `4 tests, OK`.
- GREEN: required four-file Bun gate `241 pass, 0 fail`; full offline Python
  suite `555 tests, OK`; TypeScript typecheck, isolated-pycache compileall, and
  `git diff --check` all exited `0`.

No production behavior change was needed. The correction replaces weak test
boundaries and adds a deterministic offline characterization utility.

## Fix Wave 2 Evidence (2026-07-22)

- RED: focused passive execution failed because the returned oracle omitted
  `errorOracles`; focused characterizer tests failed because the CLI did not
  accept `--expected-sha256` and allowed a missing digest.
- GREEN: the fixture now captures each squashed thrown value before passing it
  into the `tool-error` event consumed by `SessionProcessor.failToolCall`. The
  focused passive test passed `1/1`; the hermetic characterizer gate passed
  `4 tests`, including matching, mismatched, and missing digest behavior.
- GREEN: the documented command accepted all three explicit published digests
  before emitting metrics. The four-file Bun gate passed `241/241`, the full
  Python suite passed `555 tests`, and typecheck, isolated-pycache compileall,
  and `git diff --check` exited `0`.
