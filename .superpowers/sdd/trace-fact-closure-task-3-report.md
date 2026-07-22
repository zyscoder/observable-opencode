# Trace Fact Closure Task 3 Report

## Status

Complete. Artifact manifests now carry a redacted, byte-verifiable semantic
slice, and offline TraceGraph hydration prefers verified bundle files while
falling back only to verified embedded slices.

## RED

- Python focused RED: 9 tests ran; 8 errored because hydration had no slice
  fallback, source/hash status, integrity failures, or new counters.
- Bun artifact RED: 7 passed and 1 failed because `availability` and the new
  artifact manifest fields were absent.

## Implementation

- Extended `TraceArtifact` and `writeArtifact()` with `availability`,
  `content_hash`, `byte_length`, and a redacted `semantic_slices` entry.
- Reused the existing CaseTrace SHA-256 helper and added a Unicode-safe prefix
  boundary so semantic slice content never ends with a split surrogate.
- Added strict case-relative path validation, UTF-8 file reads, existing hash
  format verification, file-first hydration, and verified semantic-slice
  fallback to TraceGraph.
- Sorted slices by byte range, rejected overlaps/range errors/hash mismatches,
  and exposed per-artifact integrity states without using rejected content.
- Added compatible `slice_fallbacks` and `hash_mismatches` counters. Every
  semantic-slice fallback is reported as truncated.
- Updated legacy hydration fixtures to use real manifest hashes under the new
  fail-closed contract.

## GREEN

- Python focused artifact hydration and evidence capsule: 9/9 passed.
- Python TraceGraph/evidence risk-expanded regression: 175/175 passed.
- Full Python trace attribution suite: 494/494 passed.
- Bun focused artifact tests: 8/8 passed.
- Full Bun CaseTrace suite: 139/139 passed.
- TypeScript `bun typecheck`: passed.
- Python compileall with an isolated pycache: passed.
- `git diff --check`: passed.

## Commit

Planned commit: `feat(trace): close artifact evidence bundles`. This report is
included in that Task 3 commit; the final response records the resulting SHA.

## Files

- `packages/opencode/src/observability/case-trace.ts`
- `packages/opencode/test/observability/case-trace.test.ts`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/tests/test_artifact_hydration.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_backward_taint.py`
- `tools/trace_attribution/tests/test_episodes.py`
- `tools/trace_attribution/tests/test_investigation.py`
- `.superpowers/sdd/trace-fact-closure-task-3-report.md`

## Concerns

- Intentional compatibility boundary: a bundled file with a missing,
  malformed, or mismatched manifest hash is no longer hydrated from disk. It
  remains usable only when at least one embedded semantic slice has a valid
  byte range and hash; that fallback is always truncated.
- No new dependencies were added, and hydration remains confined to offline
  graph reads.
