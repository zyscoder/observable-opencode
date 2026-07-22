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

## Review Fix Wave

### RED Evidence

- Focused Python RED ran 14 tests and failed with 9 failures plus 1 error. It
  demonstrated gapped slice concatenation, unchecked/malformed
  `byte_length`, duplicate-reference counter overcounting, lineage inference
  from mismatched bytes, and investigation exposure of invalid UTF-8 and
  mismatched content.
- Focused Bun RED reached the intended producer mismatch after fixture
  correction: the generated artifact declared 5014 bytes while the actual
  normalized UTF-8 file contained 5015 bytes.
- A follow-up Python RED proved hydration clipping exposed 64,000 UTF-8 bytes
  while still reporting the original 80,000-byte semantic slice range.

### Implementation

- Added `VerifiedArtifactReader` as the sole artifact-content trust boundary.
  It validates case-relative paths before resolution, rejects root escape via
  symlink, hashes actual bytes using accepted raw/prefixed 16- or 64-hex
  SHA-256 forms, checks declared byte length, and strictly decodes UTF-8.
- Routed graph hydration, message-lineage reconstruction, artifact reference
  status, and artifact investigation through the centralized reader. Failed
  verification returns status only and never content bytes.
- Validated every semantic slice against the artifact's declared
  `byte_length`, sorted deterministically, rejected overlaps and gaps, and
  reported only the UTF-8 byte ranges actually exposed after size clipping.
- Kept `referenced` as the sum of per-record unique references, added
  `unique_referenced`, and made `loaded`, `missing`, `truncated`,
  `slice_fallbacks`, and `hash_mismatches` count each artifact identity once
  across shared record references.
- Normalized artifact text through a UTF-8 encode/decode round trip before
  hashing, byte sizing, semantic slicing, and writing, replacing unpaired
  surrogates deterministically.

### Commands And Results

- `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_artifact_hydration.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_backward_taint.py tools/trace_attribution/tests/test_investigation.py` - PASS, 185 tests.
- `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'` - PASS, 504 tests.
- `cd packages/opencode && /private/tmp/bun-v1.3.13/bun-darwin-aarch64/bun test test/observability/case-trace.test.ts --timeout 30000` - PASS, 140 tests and 1,917 expectations.
- `cd packages/opencode && /private/tmp/bun-v1.3.13/bun-darwin-aarch64/bun run typecheck` - PASS (`tsgo --noEmit`).
- `PYTHONPYCACHEPREFIX=/private/tmp/observable-opencode-pycache python3 -m py_compile tools/trace_attribution/trace_attribution/artifact_reader.py tools/trace_attribution/trace_attribution/graph.py tools/trace_attribution/trace_attribution/reconstruction.py tools/trace_attribution/trace_attribution/investigation.py tools/trace_attribution/tests/test_artifact_hydration.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_backward_taint.py tools/trace_attribution/tests/test_investigation.py` - PASS.
- `git diff --check` - PASS.

### Files

- `tools/trace_attribution/trace_attribution/artifact_reader.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/reconstruction.py`
- `tools/trace_attribution/trace_attribution/investigation.py`
- `tools/trace_attribution/tests/test_artifact_hydration.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_backward_taint.py`
- `tools/trace_attribution/tests/test_investigation.py`
- `packages/opencode/src/observability/case-trace.ts`
- `packages/opencode/test/observability/case-trace.test.ts`
- `.superpowers/sdd/trace-fact-closure-task-3-report.md`

### Concerns

- The existing 16-hex SHA-256 compatibility form remains accepted alongside
  full 64-hex raw and `sha256:`-prefixed forms because current CaseTrace
  manifests emit it. Legacy content with no accepted declared hash still
  fails closed.
- `artifact_reference_status.availability` retains its public meaning of a
  physically present safe-path file; `hydration_status` and integrity facts
  separately indicate whether its content was verified and exposed.
