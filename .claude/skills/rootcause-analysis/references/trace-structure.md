# Trace Structure And Query Protocol

Use `scripts/trace_query.py` as a deterministic, read-only fact retriever. It validates and normalizes recorded data but does not judge defects, rank causes, infer missing edges, or alter the Trace.

## Canonical Input

Pass the logical case-root `trace.json` produced by `observable-trace finalize`. A finalized envelope includes version fields, manifest/lifecycle data, diagnostics and metrics, plus at least one complete semantic projection:

- canonical Causal IR: `nodes[]` and `edges[]`;
- compatibility projection: `records[]` and `dataflow_edges[]`.

The helper supports `trace_version` `6.0` and `causal_ir_version` `1.0`. The manifest status must be terminal: `success`, `error`, or `cancelled`. It rejects a `running` snapshot, missing terminal status, or unsupported version with `observable-trace finalize` guidance. A `cancelled` status or shutdown signal does not make a finalized Trace invalid.

Successful `validate` and `summary` responses expose `lifecycle`, `recovery`, `segments`, and `diagnostics` facts. Inspect `source_complete`, `source_data_complete`, `historical_interruptions`, `dropped_lines`, `journal_poisoned`, segment `interrupted_unfinalized`/`running` counts, recorded diagnostics, and Trace-health availability before judging evidence coverage. A finalized Trace can remain queryable when journal replay or historical segment recovery is incomplete; qualify claims to the available data instead of treating missing source data as negative evidence.

Canonical fields win when both projections exist. Nodes preserve kind/event type, component, status, title, scope, payload, typed `aliases`, `input_refs`, `output_refs`, `source_refs`, and `artifact_refs`. Compatibility records expose empty lists for ref fields absent from that projection. Artifacts are declared in top-level `artifacts[]` and may be referenced by nodes.

HTML reports and physical `records.jsonl` segment journals are not canonical inputs. If validation rejects either, finalize the case and pass the resulting `trace.json`.

## Normalized Fact Shapes

The helper normalizes a canonical node to `node:<node_id>` and a compatibility record to `record:<record_id>`. Declared aliases resolve on lookup but returned refs remain canonical.

A node result preserves:

- `ref`, `kind`, `component`, `status`, and `title` as identity and locator fields;
- `scope` and `payload` as recorded semantic data;
- `aliases` as declared, normalized lookup identities;
- `input_refs` and `output_refs` as explicit recorded dataflow references;
- `source_refs` as explicit typed upstream references;
- `artifact_refs` as declared semantic-content references.

The `node` command also returns `incoming_edges` and `outgoing_edges`. These include recorded edges regardless of traversal eligibility so the analyst can inspect exclusions.

Each edge result preserves:

- `ref`, `source`, `target`, and `relation`;
- `eligible_for_attribution` as the producer's explicit eligibility declaration;
- `evidence_refs`, `evidence_tier`, and `derivation_method` for provenance.

Do not treat empty strings or absent payload fields as implied semantics. Resolve needed content through cited nodes or verified Artifacts; otherwise record it as unknown.

## Query Commands

Run from the Skill directory, or replace `scripts/trace_query.py` with its absolute path:

```bash
python3 scripts/trace_query.py validate --trace <absolute-trace-path>
python3 scripts/trace_query.py summary --trace <absolute-trace-path>
python3 scripts/trace_query.py search --trace <absolute-trace-path> [--query <text>] [--kind <kind>] [--component <component>] [--status <status>] [--limit <batch-limit>] [--offset <next-offset>]
python3 scripts/trace_query.py node --trace <absolute-trace-path> --ref <node-or-record-ref>
python3 scripts/trace_query.py neighbors --trace <absolute-trace-path> --ref <ref> --direction upstream|downstream [--depth <batch-depth>] [--limit <batch-limit>]
python3 scripts/trace_query.py paths --trace <absolute-trace-path> --start <ref> [--max-depth <batch-depth>] [--limit <batch-limit>]
python3 scripts/trace_query.py artifact --trace <absolute-trace-path> --id <artifact-id> [--max-chars <batch-chars>]
```

- `validate` verifies the finalized semantic envelope.
- `summary` returns lifecycle, component, node, edge, and Artifact counts.
- `search` returns a recorded-order page with `nodes`, `matched_count`, `returned_count`, `offset`, `limit`, `truncated`, and `next_offset`.
- `node` hydrates one node plus all recorded incoming and outgoing edges.
- `neighbors` traverses eligible edges in one direction and returns relation metadata.
- `paths` returns bounded backward paths without ranking them.
- `artifact` reads declared UTF-8 content only after path and SHA-256 verification.

All output is compact JSON. A nonzero exit or integrity error is evidence unavailability, not permission to bypass validation.

## Formal Traversal Eligibility

An upstream or downstream hop is eligible only when it is one of:

1. a canonical Causal IR edge whose `eligible_for_attribution` value is exactly `true`;
2. a compatibility dataflow edge whose top-level or metadata eligibility is exactly `true`;
3. a `record_source` hop synthesized by the helper from an explicit `source_refs` entry that resolves to a different known node.

The helper excludes an edge from traversal when it is explicitly ineligible, has `evidence_tier: temporal_advisory`, has an unresolved endpoint, or is a self-edge. Temporal adjacency alone is never causal evidence, even if two events appear consecutively. Preserve each returned relation, derivation method, evidence tier, and `evidence_refs` when making a causal claim.

Traversal uses one traversal relation per source-target pair. A recorded edge takes precedence over a synthesized `record_source` relation for the same pair; otherwise the first eligible recorded relation is stable. This prevents equivalent provenance projections from multiplying paths. The `node` command still returns all recorded edges, including duplicates and ineligible relations, for inspection.

`node` may display ineligible recorded edges for inspection; only `neighbors` and `paths` apply the traversal rules. Do not silently promote a displayed edge into causal evidence.

## Bounded Retrieval Is Not Bounded Reasoning

`--depth`, `--max-depth`, `--limit`, and `--max-chars` control one response size. They do not cap semantic recursion or candidate count. If a query returns `truncated: true`, record its `remaining_frontier_refs` and request another relevant batch whenever that frontier could change the verdict. Empty eligible paths mean only that the recorded eligible graph does not connect the requested nodes.

Search has a separate continuation contract. Begin at `offset` zero, then pass each non-null `next_offset` into `--offset` until `truncated` is `false`. If analysis stops earlier, preserve the remaining page as unexplored candidates in the hypothesis ledger; never treat one page as all matches.

## Artifact Evidence

Use `artifact` only for a top-level declared Artifact ID. Content reads require a valid declared SHA-256 in `content_hash` or compatible `hash`; the helper fails closed when the digest is absent, malformed, conflicting, or mismatched. It also requires a path below the Trace case directory and rejects symlink/path escape and replacement races. Cite the Artifact ID and digest with claims based on returned content. On any integrity failure, preserve the reference and mark its semantic content `unknown`.
