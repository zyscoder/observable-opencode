# Trace Structure And Query Protocol

Use `scripts/trace_query.py` as a deterministic, read-only fact retriever. It validates and normalizes recorded data but does not judge defects, rank causes, infer missing edges, or alter the Trace.

## Canonical Input

Pass the logical case-root `trace.json` produced by `observable-trace finalize`. A finalized envelope includes version fields, manifest/lifecycle data, diagnostics and metrics, plus at least one complete semantic projection:

- canonical Causal IR: `nodes[]` and `edges[]`;
- compatibility projection: `records[]` and `dataflow_edges[]`.

Canonical fields win when both projections exist. Nodes preserve kind/event type, component, status, title, scope, payload, typed `source_refs`, `artifact_refs`, and aliases. Artifacts are declared in top-level `artifacts[]` and may be referenced by nodes.

HTML reports and physical `records.jsonl` segment journals are not canonical inputs. If validation rejects either, finalize the case and pass the resulting `trace.json`.

## Normalized Fact Shapes

The helper normalizes a canonical node to `node:<node_id>` and a compatibility record to `record:<record_id>`. Declared aliases resolve on lookup but returned refs remain canonical.

A node result preserves:

- `ref`, `kind`, `component`, `status`, and `title` as identity and locator fields;
- `scope` and `payload` as recorded semantic data;
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
python3 scripts/trace_query.py search --trace <absolute-trace-path> --query <text> [--kind <kind>] [--component <component>] [--status <status>] [--limit <batch-limit>]
python3 scripts/trace_query.py node --trace <absolute-trace-path> --ref <node-or-record-ref>
python3 scripts/trace_query.py neighbors --trace <absolute-trace-path> --ref <ref> --direction upstream|downstream [--depth <batch-depth>] [--limit <batch-limit>]
python3 scripts/trace_query.py paths --trace <absolute-trace-path> --start <ref> [--max-depth <batch-depth>] [--limit <batch-limit>]
python3 scripts/trace_query.py artifact --trace <absolute-trace-path> --id <artifact-id> [--max-chars <batch-chars>]
```

- `validate` verifies the finalized semantic envelope.
- `summary` returns lifecycle, component, node, edge, and Artifact counts.
- `search` filters normalized nodes in recorded order.
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

`node` may display ineligible recorded edges for inspection; only `neighbors` and `paths` apply the traversal rules. Do not silently promote a displayed edge into causal evidence.

## Bounded Retrieval Is Not Bounded Reasoning

`--depth`, `--max-depth`, `--limit`, and `--max-chars` control one response size. They do not cap semantic recursion or candidate count. If a query returns `truncated: true`, record its `remaining_frontier_refs` and request another relevant batch whenever that frontier could change the verdict. Empty eligible paths mean only that the recorded eligible graph does not connect the requested nodes.

## Artifact Evidence

Use `artifact` only for a top-level declared Artifact ID. The helper requires a path below the Trace case directory, rejects symlink/path escape and replacement races, and checks the declared SHA-256 before returning text. Cite the Artifact ID and digest with claims based on its content. On missing files, invalid UTF-8, path rejection, or hash mismatch, preserve the reference and mark its semantic content `unknown`.
