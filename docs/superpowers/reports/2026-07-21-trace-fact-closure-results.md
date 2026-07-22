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

Tracing disabled and enabled ran in separate processes with identical tool
inputs and callbacks. Disabled ran first; enabled ran second. Neither execution
read trace or attribution output.

The observed public result is byte-for-byte structurally equal across modes:

- exact decoded success and error inputs;
- exact success tool output and metadata;
- exact error class/type/message: `PassiveToolError` /
  `PassiveToolError` / `passive benchmark failure`;
- metadata callback count 2 and permission callback count 2;
- exact six-item Agent-visible callback/result/error message sequence.

Only the enabled process creates a trace case directory and trace records.

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
- Full Python suite: 551 tests, OK, with no skips or retries.
- TypeScript typecheck: passed.
- Python compileall with isolated pycache: passed.
- `git diff --check`: passed.

## Remaining Limitation

Current closure is forward-looking. Regeneration is the only honest way to add
the missing artifact bytes, immutable formal revision, corrected claim records,
and revision-matched external evaluation facts to the three historical cases.
