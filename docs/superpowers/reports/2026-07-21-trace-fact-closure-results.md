# Trace Fact Closure Results

## Acceptance Summary

| Gate | Result |
| --- | --- |
| Passive behavior equivalence | PASS |
| Current Astropy claim atomization | PASS |
| Current CaseTrace artifact closure | PASS |
| Current revision-bound TerminalBench seed | PASS |
| Frozen historical archive portability | LIMITED, fail-closed |
| Current producer phase acceptance | PASS |

The phase accepts the current producer. It does not claim that old archives
were repaired. Axios, Astropy, and TerminalBench must be regenerated to gain
formal revision binding, portable artifact evidence, and external evaluation
seeds.

## Passive Behavior

Tracing disabled, enabled, and enabled with a file-valued invalid trace root ran
in separate processes with identical tool inputs and callbacks. Disabled ran
first. Neither execution read trace or attribution output.

The observed public result is byte-for-byte structurally equal across modes:

- exact decoded success, error, and pre-aborted inputs;
- exact success tool output and metadata;
- original thrown error identity captured before `SessionProcessor.failToolCall`:
  constructor/name/type/message `PassiveToolError` / `PassiveToolError` /
  `PassiveToolError` / `passive benchmark failure`;
- metadata callback count 3 and permission callback count 3;
- exact production-generated three-message Agent-visible projection containing
  three tool calls and three corresponding tool results, without synthesized or
  appended messages.

Only the valid enabled process creates a trace case directory and trace records.

## Historical Compatibility

Each frozen `partial/latest.json` was reconstructed twice through current
`TraceGraph`; all nodes were hydrated and both passes produced identical
metrics. No Agent, attribution Judge, LLM, API, or network call was used.

Artifact outcomes are `loaded / missing / slice fallback / hash mismatch`.

| Frozen case | Broken fragments | Artifact outcomes | Artifact files | External seeds | Revision | IR nodes/edges | Graph nodes/edges |
| --- | ---: | --- | ---: | ---: | --- | ---: | ---: |
| Axios | 0 | `0 / 170 / 0 / 0` | 0 | 0 | absent | `507 / 1334` | `522 / 1277` |
| Astropy | 1 | `0 / 184 / 0 / 0` | 0 | 0 | absent | `518 / 1318` | `533 / 1257` |
| TerminalBench | 0 | `0 / 110 / 0 / 0` | 0 | 0 | absent | `258 / 566` | `267 / 535` |

The frozen bundles index 234, 258, and 129 artifacts respectively, but their
artifact directories contain zero files. Their old manifests also contain no
verified semantic slices, so current hydration honestly reports every unique
referenced artifact as missing. Zero hash mismatches means no bytes were
available to compare; it is not evidence of artifact integrity.

Astropy record `responseclaim_claim_515_882c4186` still begins with a comma.
The historical bytes were not rewritten.

## Current Producer

The exact frozen Astropy final response text was emitted through current
CaseTrace. It creates three claims:

1. `All 11 tests pass.`
2. The one-character `separable.py:245` change.
3. One complete `_cstack` sentence containing the full parenthetical
   `i.e.` explanation.

No current claim begins with punctuation. Source byte ranges reverse-slice the
exact final response.

Current CaseTrace also writes a formal subject revision and a bundled artifact
whose relative path, byte length, content hash, semantic-slice byte range,
slice hash, and `truncated=true` marker are verified from disk.

A hermetic TerminalBench-style external failure bound to
`git:task5-terminalbench` reconstructs as one revision-matched failed seed. It
is decisive outcome evidence but remains ineligible as a root introduction.
With its bundle file intentionally absent, its verified semantic slice yields
`loaded=1, missing=0, slice_fallbacks=1, hash_mismatches=0`.

## Verification

- Passive focused Bun gate: 1 pass, 0 fail.
- Required four-file Bun suite: 241 pass, 0 fail.
- Full Python suite: 555 tests, OK, with no skips or retries.
- TypeScript typecheck: passed.
- Python compileall with isolated pycache: passed.
- `git diff --check`: passed.

## Remaining Limitation

Current closure is forward-looking. Regeneration is the only honest way to add
the missing artifact bytes, immutable formal revision, corrected claim records,
and revision-matched external evaluation facts to the three historical cases.

## Review Closure Update (2026-07-22)

The passive result above is now established at the real production projection
boundary. A real `Tool.define` result runs through its normal truncation wrapper
and then through `SessionProcessor.process`, which invokes production
`completeToolCall` / `failToolCall`. The equality oracle deep-compares the
persisted tool-part projection and `MessageV2.toModelMessages` output. It does
not synthesize or append result/error messages.

Tracing off, tracing on, and tracing on with an invalid file-valued trace root
produce identical success, error, and pre-aborted projections. Exact checks
include callback counts and payloads, decoded inputs, status, title, metadata,
wrapper-added `truncated=false`, output, attachment content, and production
error text. The focused passive test took 1.72-2.09 seconds locally for three
child executions. This is descriptive only; there is no wall-clock assertion
or hard latency SLA.

Artifact acceptance is also independent of the producer manifest. Expected
bytes are derived directly from the fixed payload using the public minified
JSON contract. The test checks exact bytes/text, independently computes the
full SHA-256 and its public 16-hex projection, and checks exact byte length plus
the semantic slice prefix, byte range, hash, and truncation marker. Every new
TypeScript temporary root is removed in a `finally` block.

### Historical Reproduction

| Case | Exact local archive | SHA-256 |
| --- | --- | --- |
| Axios | `/private/tmp/observable-opencode-multibench/runs/formal-v3/swebench-multilingual/axios__axios-5892/traces/axios__axios-5892/partial/latest.json` | `1c934c47fe01719abaed45b2825ab4b88b1f1ea7fc1d8885ea2f76feed9c9ead` |
| Astropy | `/private/tmp/observable-opencode-multibench/runs/formal-v3/swebench-verified/astropy__astropy-12907/traces/astropy__astropy-12907/partial/latest.json` | `e5209173e7576b822921025b6cd9f80fde3d1062c3f85ceeae312c56e3012b17` |
| TerminalBench | `/private/tmp/observable-opencode-multibench/runs/formal-v3/terminalbench/cancel-async-tasks/traces/cancel-async-tasks/partial/latest.json` | `3760043e3016d0ee29278cb5dd8b63f94575e8f3dcca70551c324e2409a5c011` |

Run the deterministic no-LLM characterizer from the repository root:

```bash
PYTHONPATH=tools/trace_attribution python3 tools/trace_attribution/scripts/characterize_trace_fact_closure.py \
  --expected-sha256 1c934c47fe01719abaed45b2825ab4b88b1f1ea7fc1d8885ea2f76feed9c9ead \
  --expected-sha256 e5209173e7576b822921025b6cd9f80fde3d1062c3f85ceeae312c56e3012b17 \
  --expected-sha256 3760043e3016d0ee29278cb5dd8b63f94575e8f3dcca70551c324e2409a5c011 \
  /private/tmp/observable-opencode-multibench/runs/formal-v3/swebench-multilingual/axios__axios-5892/traces/axios__axios-5892/partial/latest.json \
  /private/tmp/observable-opencode-multibench/runs/formal-v3/swebench-verified/astropy__astropy-12907/traces/astropy__astropy-12907/partial/latest.json \
  /private/tmp/observable-opencode-multibench/runs/formal-v3/terminalbench/cancel-async-tasks/traces/cancel-async-tasks/partial/latest.json
```

The characterizer requires one explicit expected SHA-256 per archive and
validates all source bytes before reconstructing, hydrating, or publishing any
metrics. It then reconstructs and hydrates each archive twice, fails on metric
drift, and prints the published closure metrics. Its tests are hermetic and
create only small synthetic archives.

### Corrective Verification

- RED: missing production-path fixture and missing archive characterizer;
  subsequent typecheck RED exposed fixture contract errors before correction.
- GREEN: passive focused `1/1`; semantic-observability `4/4`; exact Astropy
  `1/1`; Task 5 Python digest gate `4 tests, OK`.
- GREEN: four-file Bun observability gate `241 pass, 0 fail`; full Python suite
  `555 tests, OK`; TypeScript typecheck, isolated-pycache compileall, and
  `git diff --check` all exited `0`.

These corrections change test evidence and reproducibility tooling only. The
historical portability limitation remains unchanged.

## Fix Wave 2 (2026-07-22)

- RED: the passive identity assertion failed with `errorOracles` undefined;
  the digest CLI tests failed because `--expected-sha256` was unrecognized and
  a missing digest still returned zero.
- GREEN: the focused passive gate passed `1/1`; the hermetic characterizer gate
  passed `4 tests`; matching inputs publish metrics while missing or mismatched
  digests return nonzero with no stdout.
- GREEN: the published three-archive command with all three explicit digests
  completed successfully; the four-file Bun gate passed `241/241`, the Python
  suite passed `555 tests`, and typecheck, isolated-pycache compileall, and
  `git diff --check` exited `0`.
