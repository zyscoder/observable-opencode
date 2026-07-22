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
- Hermetic Python closure fixtures: 2 tests, OK.
- Required full TypeScript command: 241 pass, 0 fail.
- Full offline Python suite: 551 tests, OK; no skipped tests or retries.
- TypeScript typecheck: exit 0.
- Python compileall with `PYTHONPYCACHEPREFIX` under `/private/tmp`: exit 0.
- `git diff --check`: exit 0.

## Passive Equivalence

The disabled and enabled child outputs are exactly equal for both success and
error execution. The test fixes and asserts:

- decoded tool inputs: success and error with `payload="fixed-input"`;
- success output: title `passive success`, output `stable tool output`, and
  metadata `{scenario: "success", truncated: false}`;
- thrown error class, type, and message: `PassiveToolError`,
  `PassiveToolError`, `passive benchmark failure`;
- callback counts: metadata 2, permission 2;
- the exact six-message Agent-visible metadata, permission, result, metadata,
  permission, error sequence.

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
