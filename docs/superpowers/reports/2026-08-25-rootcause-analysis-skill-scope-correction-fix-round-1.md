# Root-Cause Skill Scope-Correction Fix Round 1

Date: 2026-08-25

Branch: `codex/trace-stability-integration`

## Purpose

This round hardens the repository-owned boundary: deterministic construction of
opaque root-cause evaluation bundles for submission to an independently managed,
trusted benchmark Harness. It adds no Agent runner, Provider credential handling,
runtime behavior, Trace query behavior, or changes to the paused attribution
module.

## Handoff Guarantees

- Source and sanitized derived Traces are both accepted by the authoritative
  `trace_query.py validate` command. Unsupported versions, malformed envelopes,
  and nonterminal manifests are rejected.
- A terminal source-incomplete Trace remains accepted when the authoritative
  validator accepts it. Exact lifecycle, recovery/source-completeness, segment,
  and diagnostic facts are copied into evaluator provenance.
- Artifact paths are preflighted before writes. Reserved bundle paths, the
  canonical Skill prefix, duplicates, file/directory ambiguity, and portable
  casefold or Unicode-normalized collisions are rejected.
- Declared Artifact hashes are checked before copying and every copied Artifact is
  reopened and checked again after complete construction.
- `--provenance-output` is required, external to the bundle, and exclusively
  created. Every opaque case uses a distinct evaluator-side provenance file.
- Construction is transactional. Validation, collision, hash, or write failure
  leaves no destination bundle, provenance file, or temporary partial handoff.

## Verification Result

- 109 deterministic tests passed with no skips.
- `trace_query.py` and `prepare_isolated_bundle.py` compiled successfully.
- The official Skill validator reported `Skill is valid!`.
- Seven fresh bundles were built under
  `/tmp/rootcause-scope-fix-r1-0jhkc8cs`. All seven used opaque identities,
  passed authoritative derived-Trace validation, stored provenance outside the
  bundle, contained no symlinks or hidden-answer leaks, and matched all recorded
  post-copy Artifact hashes.
- Canonical fixtures were unchanged and repository diff checks passed.

The test suite includes adversarial cases for reserved-path, duplicate,
casefold, Unicode-normalized, and file/directory collisions; postwrite Artifact
corruption; unsupported Trace versions; missing envelope fields; nonterminal
manifests; accepted terminal source-incomplete diagnostics; derived-Trace
revalidation; missing or internal provenance paths; overwrite refusal; and
transactional cleanup.

No Provider-backed Agent execution was performed or scored in this repository.
