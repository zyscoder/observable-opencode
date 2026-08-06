# Seed-Bound Attribution Labels v5 Design

## Goal

Bind every human attribution judgment to the exact factual inputs and the exact
defect seed it evaluates. A v5 score must remain correct when one case contains
multiple defects, repeated semantic occurrences, shared factors, review facts,
or ordered external evaluations. Labels remain offline evaluation data and are
never exposed to the Agent, candidate discovery, or any Judge request.

## Compatibility Policy

- v3 and v4 remain valid only for reports with exactly one seed.
- A v3 or v4 label document evaluated against a multi-seed report is rejected;
  aggregate labels are never broadcast across seeds.
- v5 is mandatory for multi-defect acceptance and quality claims.
- v5 changes evaluation only. It does not change Agent execution or attribution.
- Any source, revision, seed, or ownership mismatch is a hard error and writes
  no comparison output.

## Contract

`recursive-attribution-labels/v5` has these exact top-level fields:

```json
{
  "schema_version": "recursive-attribution-labels/v5",
  "case_id": "case-id",
  "source_binding": {
    "trace_sha256": "sha256:<64 lowercase hex>",
    "review_sha256": "sha256:<64 lowercase hex> or empty string",
    "evaluation_sha256": ["sha256:<64 lowercase hex>"],
    "effective_trace_sha256": "sha256:<64 lowercase hex>",
    "subject_revision": "non-empty revision",
    "revision_provenance_status": "valid"
  },
  "seeds": [],
  "case_shared_factors": []
}
```

`evaluation_sha256` preserves the repeated `--evaluation` argument order.
Injection order may affect the effective Trace and must never be digest-sorted.

Each seed has these exact fields:

```json
{
  "defect_id": "stable human-authored defect identity",
  "start_ref": "record:<id>",
  "defect_fingerprint": "non-empty attribution defect fingerprint",
  "seed_binding_identity": "seed:<id>",
  "seed_semantic_anchor_id": "semantic_anchor:v2:<id>",
  "seed_semantic_occurrence_id": "semantic_occurrence:v1:<id>",
  "seed_source_bindings": [
    {
      "source_kind": "raw_trace | quality_review | external_evaluation",
      "source_index": 0,
      "source_sha256": "sha256:<64 lowercase hex>"
    }
  ],
  "defect_origin_kind": "introduced_by_agent | baseline_existing_unrepaired",
  "expected_outcome": "confirmed_root | no_defect | unresolved",
  "roots": [],
  "conditions": [],
  "amplifiers": [],
  "materializations": [],
  "unrelated": [],
  "forbidden_roots": [],
  "allowed_unresolved_outcomes": []
}
```

`seed_source_bindings` identifies every factual input that introduced or
semantically merged into the defect seed. Entries are duplicate-free and kept
in composition order: raw Trace, review, then external evaluations by caller
index. `source_index` is zero for Trace/review and the zero-based argument index
for an evaluation. Its digest must equal the corresponding top-level source
binding. This preserves valid review + evaluation seed merges and duplicate
evaluation provenance without reducing origin to whichever source ran first.
`defect_origin_kind` identifies whether the failed behavior was authored by the
Agent or already existed and the Agent failed to repair it. These are separate
causal questions and must not share one enum.

`expected_outcome` removes empty-label ambiguity. `confirmed_root` requires at
least one root. `no_defect` requires all positive causal-role lists and
`allowed_unresolved_outcomes` to be empty. `unresolved` requires no positive
causal-role labels and at least one explicitly allowed unresolved outcome.
`unrelated` and `forbidden_roots` may still label reviewed negative candidates.

Role entries retain the v4 identity triple: `node_ref`,
`semantic_anchor_id`, and `semantic_occurrence_id`.

Each case-shared factor has these exact fields:

```json
{
  "factor_role": "contributing_condition | amplifying_factor | downstream_materialization | unrelated",
  "seed_binding_identities": ["seed:<id>", "seed:<id>"],
  "node_ref": "record:<id>",
  "semantic_anchor_id": "semantic_anchor:v2:<id>",
  "semantic_occurrence_id": "semantic_occurrence:v1:<id>"
}
```

A shared factor must reference a sorted, duplicate-free list of at least two
known seed identities. It expands to one expected role tuple per listed seed.

## Source Binding

- Source digests hash exact bytes, not parsed JSON.
- Evaluation digests preserve caller order and exact membership.
- `effective_trace_sha256` hashes the canonical JSON produced by the same pure
  composition path used by attribution and evaluation.
- `subject_revision` and `revision_provenance_status` come from
  `trace_execution_revision`; v5 requires valid provenance.
- Missing, extra, reordered, or byte-different inputs fail even when they parse
  to semantically equivalent JSON.

## Seed Binding And Ownership

- Recompute every `seed_binding_identity` from `start_ref` and
  `defect_fingerprint`; supplied values are assertions, not authority.
- `start_ref` is canonical and must resolve to itself; aliases are not accepted
  in labels.
- Reconstruct every seed's ordered source bindings across composition
  boundaries. Intentional structured-seed merges retain all sources; an
  unproven ID collision fails closed.
- Resolve `start_ref`, anchor, and occurrence against the effective graph and
  require one active report seed result with the same binding identity.
- Roots use `confirmation.seed_binding_identity`.
- Conditions and amplifiers use `confirmation.seed_binding_identity`.
- Materializations use `role_judgment.seed_binding_identity`.
- Unrelated factors use the nested factor judgment's
  `seed_binding_identity` from `rejected_candidates`.
- Introduction candidates are projected per seed only from owned step
  judgments. An unowned top-level list is not scoreable.
- Missing, unknown, conflicting, or ambiguous ownership is a safety failure.

## Metric Identity And Output

The comparison schema advances to `recursive-attribution-comparison/v8` for v5.
Its scoring identity is:

```text
(seed_binding_identity, semantic_occurrence_id, role)
```

The evaluator publishes:

- per-seed root precision, recall, F1, Top-1, role-pair precision, recall, F1,
  terminal outcome, and unresolved reasons;
- micro metrics over all owned seed-role-occurrence tuples;
- macro metrics as the arithmetic mean of per-seed metrics;
- label, report, and terminal seed coverage;
- `all_seed_coverage_passed`, true only when all coverage is 1.0 and every
  ownership, binding, and terminal-state safety condition passes.

An empty positive-role seed is valid only when `expected_outcome` explicitly
distinguishes `no_defect` from `unresolved`. A correct root assigned to the
wrong seed is incorrect, even if the same occurrence is expected elsewhere in
the case. Within one seed, neither a semantic occurrence nor a `node_ref` may
carry conflicting causal roles, including conflicts introduced through a
case-shared factor.

## Failure Policy

Evaluation exits non-zero and writes no comparison output for:

- any source hash, effective hash, case ID, or revision mismatch;
- duplicate defect IDs, start refs, or seed identities;
- an invalid or non-recomputable seed identity;
- a role occurrence assigned conflicting roles for one seed;
- a shared factor with fewer than two, duplicate, or unknown seed identities;
- any published role without exactly one valid owner;
- a missing or extra report seed;
- v3/v4 labels used with a multi-seed report;
- incomplete checkpoint action reconciliation.

## Module Boundaries

- `trace_attribution/label_contract.py`: exact v3/v4/v5 schemas and immutable
  validation.
- `trace_attribution/input_binding.py`: ordered source-byte and effective Trace
  bindings plus revision provenance.
- `trace_attribution/seed_projection.py`: report ownership projection and
  seed-scoped metric computation.
- `scripts/evaluate_recursive_attribution.py`: graph/report safety and CLI
  orchestration only.
- `tests/test_evaluation_labels_v5.py`: contract, binding, ownership, scoring,
  and leakage tests.

## Acceptance

- Existing single-seed v3/v4 fixtures retain their current metric semantics.
- v3/v4 plus a multi-seed report fails closed.
- A true two-defect fixture detects swapped roots even when aggregate node sets
  are identical.
- Per-seed, micro, macro, and all-seed metrics are stable under record and
  report-item permutation; evaluation input order remains significant.
- Every one-field source or seed mutation fails before output publication.
- Human labels are absent from Trace records, capsules, Judge requests, caches,
  checkpoints, and attribution reports.
