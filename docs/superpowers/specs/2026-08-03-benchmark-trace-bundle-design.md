# Benchmark Trace Bundle v1 Design

## Goal

Create a reproducible, self-contained factual input closure for benchmark
attribution. A bundle must survive deletion of every source path and must give
attribution and evaluation identical effective Trace and artifact bytes.

## Immutable Content-Addressed Layout

```text
<store>/sha256/<bundle-id>/
  bundle.json
  inputs/trace.json
  inputs/review.json                         # optional
  inputs/evaluations/0000-<sha256>.json      # optional, caller order
  inputs/labels.json                         # optional, evaluator-only role
  effective/trace.json
  artifacts/sha256/<first-two>/<full-sha256>
```

`bundle-id` is the SHA-256 of canonical `bundle.json` with only `bundle_id`
omitted. Identity excludes timestamps and absolute source paths. A published
bundle is immutable: an existing identical ID is fully validated and reused;
different content gets a different ID. Human-readable aliases, if needed, are
small ref files updated atomically outside the immutable bundle.

## Composition Contract

Effective Trace composition has one declared version and fixed order:

1. parse original Trace bytes;
2. inject review facts if present;
3. inject external evaluations in caller order;
4. validate artifact declarations and references against an explicit source
   artifact root;
5. rewrite every artifact path and content identity to a content-addressed,
   full-SHA-256 bundle member;
6. serialize effective Trace with the repository's canonical JSON encoding.

`compose_effective_trace()` is a pure data transformation. It does not infer an
artifact root, read artifact payloads, or instantiate `TraceGraph`.

Alias resolution and active-revision eligibility are shared pure policies used
by both composition and `TraceGraph`; neither path may carry a copied policy.
Cyclic progress-episode membership is invalid and fails closed in both paths.

Source bindings preserve every evaluation argument, including duplicate bytes,
in caller order. Effective fact injection retains the repository's existing
content-idempotent behavior: identical evaluation payloads may coalesce to one
fact record. This multiplicity difference is explicit in the composition
contract and never changes the ordered source-byte binding.

## Manifest Contract

`bundle.json` uses `benchmark-trace-bundle/v1` and contains:

- `bundle_id` and `composition_contract`;
- case, run, and valid subject revision identity;
- ordered source bindings for Trace, optional review, and evaluations;
- a canonical effective Trace binding;
- a role-tagged optional labels binding;
- a path-sorted member table with full SHA-256, byte length, and media type;
- an artifact binding table mapping every unique artifact ID to its declared
  source identity and content-addressed member;
- declared, referenced, and bundled artifact counts.

All bundle integrity identities use `sha256:<64 lowercase hex>`. Legacy 8/16
digit source declarations may be checked only as source assertions; they are
never bundle integrity identities.

The exact top-level schema is:

```json
{
  "schema_version": "benchmark-trace-bundle/v1",
  "bundle_id": "sha256:<64 lowercase hex>",
  "composition_contract": "benchmark-trace-composition/v1",
  "case_id": "non-empty",
  "run_id": "non-empty",
  "subject_revision": "non-empty",
  "revision_provenance_status": "valid",
  "sources": {
    "trace": {"path": "inputs/trace.json", "sha256": "sha256:<64>", "byte_length": 1, "media_type": "application/json"},
    "review": null,
    "evaluations": [],
    "labels": null
  },
  "effective_trace": {"path": "effective/trace.json", "sha256": "sha256:<64>", "byte_length": 1, "media_type": "application/json"},
  "artifacts": [],
  "members": [],
  "counts": {
    "source_count": 1,
    "evaluation_count": 0,
    "declared_artifact_count": 0,
    "referenced_artifact_count": 0,
    "artifact_member_count": 0,
    "member_count": 2
  }
}
```

Every source/effective binding has exactly `path`, `sha256`, `byte_length`, and
`media_type`; an evaluation additionally has `source_index`. Evaluation indices
are contiguous from zero and list order is authoritative. `members` is a
path-sorted list with exactly `role`, `path`, `sha256`, `byte_length`, and
`media_type`. Roles are `source_trace`, `source_review`, `source_evaluation`,
`source_labels`, `effective_trace`, or `artifact`.

Each artifact binding has exactly:

```json
{
  "artifact_id": "unique non-empty identity",
  "source_relative_path": "validated original relative path",
  "declared_content_hash": "legacy source assertion or empty string",
  "member_path": "artifacts/sha256/ab/<64 lowercase hex>",
  "content_sha256": "sha256:<64 lowercase hex>",
  "byte_length": 1,
  "media_type": "non-empty"
}
```

Multiple artifact IDs may share one content member. `bundle.json` is not a
member because it carries the identity; every other file is declared exactly
once in `members`, and no undeclared file is accepted. `bundle_id` hashes
canonical JSON after removing only the `bundle_id` field.

## Safe Build Flow

1. `lstat`, read, and hash all source inputs without following symlinks.
2. Parse and compose factual JSON without creating a graph.
3. Resolve artifacts only beneath an explicit, caller-supplied source root.
   Bundle mode never calls manifest-based root inference.
4. Reject duplicate artifact IDs, duplicate paths, missing or unreferenced
   declarations, unknown references, invalid UTF-8, and source files whose
   identity changes while copied.
5. Stage normalized members under a hidden sibling directory.
6. Rewrite the effective Trace to content-addressed artifact paths and full
   hashes, write canonical JSON, then create the manifest and bundle ID.
7. Re-open the staged bundle with full verification, including a graph load
   with the explicit staged bundle root.
8. Rename staging to the previously nonexistent immutable bundle-ID path.
   If the ID already exists, verify and reuse it.

There is no normal `--allow-incomplete` mode. An incomplete source fails. Crash
leftovers remain hidden staging directories and are never discoverable as
published bundles.

## Opening And Consumption

```python
compose_effective_trace(trace, review=None, evaluations=()) -> JsonDict

build_benchmark_trace_bundle(
    *,
    trace_path: Path,
    store_dir: Path,
    artifact_root: Path,
    review_path: Optional[Path] = None,
    evaluation_paths: Sequence[Path] = (),
    labels_path: Optional[Path] = None,
) -> BenchmarkTraceBundle

open_benchmark_trace_bundle(
    path: Path,
    *,
    verify: str = "full",
    role: str = "attribution",
) -> BenchmarkTraceBundle

load_benchmark_case(bundle_or_legacy_source) -> LoadedBenchmarkCase
```

- `role="attribution"` never parses or returns labels.
- `role="evaluation"` may return the validated labels path.
- Bundle graph loading supplies an explicit bundle artifact root and never
  calls `artifact_root_for_trace_path`.
- Legacy raw-path loading remains a compatibility shim. Attribution and the
  evaluator share `load_benchmark_case()` so composition cannot drift.

CLI:

```text
python3 tools/trace_attribution/scripts/build_benchmark_trace_bundle.py \
  --trace <trace.json> --artifact-root <artifact-root> \
  --review <review.json> --evaluation <evaluation.json> \
  --labels <labels.json> --store <bundle-store>
```

Both attribution and evaluator add mutually exclusive
`--benchmark-bundle <bundle-dir>` mode. Corruption is rejected before Provider
construction. Labels in the same directory are an API isolation boundary, not
a confidentiality boundary; strict blind execution should not mount labels in
the attribution process.

## Validation

The bundle store is an integrity boundary, not a privilege boundary between
processes running as the same operating-system principal. The builder and
readers reject symlink traversal, special files, external hardlinks, identity
swaps observed during validation, and content/hash mismatches. A process that
can rename or rewrite entries in the same namespace as the builder can always
mutate a POSIX directory after publication; consumers therefore must open a
bundle through the full validator and must not trust a pathname merely because
its final component is a digest. Publication performs a full staging check,
descriptor-relative rename, full post-publication check, and identity-bound
rollback so a failed build leaves no accepted final bundle.

Full open verifies:

- exact manifest keys, schema, bundle ID, and member ordering;
- every member hash, byte length, media type, regular-file type, and path;
- no absolute paths, `..`, backslashes, symlinks, devices, sockets, or FIFOs;
- no undeclared files or reserved-path collisions;
- ordered source and canonical effective Trace bindings;
- unique artifact IDs and one-to-one declarations/references/member bindings;
- case, run, and subject revision agreement;
- effective graph construction with an explicit bundle root;
- optional labels bytes without exposing them in attribution role.

Hashing proves equality to a known bundle ID, not producer authenticity. A
future signing layer may bind a trusted producer identity without changing the
content-addressed core.

## Module Boundaries

- `trace_attribution/benchmark_composition.py`: pure ordered composition and
  normalization contract.
- `trace_attribution/trace_eligibility.py`: graph-independent alias,
  provenance, revision-authority, and cycle-safe eligibility policy shared by
  composition and `TraceGraph`.
- `trace_attribution/benchmark_bundle.py`: immutable manifest, secure builder,
  full validator, and role-gated bundle view.
- `trace_attribution/benchmark_case.py`: common bundle/legacy loader.
- `scripts/build_benchmark_trace_bundle.py`: thin CLI.
- `tests/test_benchmark_bundle.py`: contract, attacks, corruption, replay, and
  publication tests.

## Acceptance

- A bundle with multiple ordered evaluations and artifacts produces identical
  graph and strict score after every source file is deleted.
- Forged trace `manifest.files` cannot expand the bundle artifact root.
- Any changed member, duplicate artifact ID, unlisted file, path attack, or
  incomplete closure fails before attribution and before Provider creation.
- Every injected build failure exposes no new bundle and cannot damage an
  existing immutable bundle.
- Attribution cannot load labels through public bundle APIs.
