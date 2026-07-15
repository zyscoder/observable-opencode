# Causal IR Unified Kernel Migration Verification

## Scope And Result

Round 4 closes both findings in
`.superpowers/sdd/final-review-round4.md`: 1 Critical and 1 Important. Together
with the first three unified-fix rounds, the local Causal IR migration
acceptance gate is green.

This round changes only the owned CaseTrace implementation, its focused tests,
and the two requested reports. `causal-ir.ts`, the viewers, semantic
observability, and Python attribution did not need changes. Partial, final,
signal recovery, viewer, and attribution consumers continue to receive edges
through canonical compatibility projections.

## Commands And Counts

All final commands were run on 2026-07-15. The Bun executable reports version
1.3.13.

| Working directory | Command | Result |
| --- | --- | --- |
| `packages/opencode` | `/private/tmp/bun-1.3.13/bin/bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts test/tool/semantic-observability.test.ts --timeout 30000` | 169 passed, 0 failed, 1,738 expectations, 26.80 s |
| `packages/opencode` | `/private/tmp/bun-1.3.13/bin/bun run typecheck` | exit 0 (`tsgo --noEmit`) |
| repository root | `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_backward_taint -v` | 85 passed, 0 failed, 0.129 s |
| repository root | `git diff --check` | exit 0 |

The full Bun total comprises 50 Causal IR tests, 116 CaseTrace tests, and 3
semantic-observability tests.

## Round 4 TDD Evidence

- The redaction regression first used neutral property names so key-based
  sanitation could not mask the text bug. It failed with all four seeded
  Cookie/Set-Cookie secrets present: embedded single-quoted and double-quoted
  values in otherwise unquoted shell headers.
- The canonical ownership regression wrote the same `edge_id` before and
  after late alias resolution. RED observed two legacy edges while the
  canonical store correctly held one last-write-wins edge.
- GREEN replaces the complete header value, keeps agent inputs byte-for-byte
  unchanged, and projects one legacy edge from the canonical late-alias state.
- The regression also checks legacy field order, optional-field omission,
  original relation names, partial/final parity, compatibility endpoints, and
  viewer marker isolation.

## Redaction And Token Usage

- Neutral shell text recognizes Cookie and Set-Cookie headers at any command
  position, including single-quoted and double-quoted `curl -H` arguments and
  unquoted header forms whose values contain embedded single or double quotes.
  The complete header value is replaced.
- The regression fixture scans events, raw events, canonical journal, final
  and legacy JSON, provenance projection, partial snapshot, manifest, HTML,
  and every artifact. None contains seeded plaintext secrets.
- Agent/model/tool inputs retain their original values and identities. Trace
  sanitation operates on copies.
- Root `token_usage` is omitted when it is a string, `undefined`, an array,
  or another non-object value. The closed numeric object schema still keeps
  only finite values for `input`, `output`, `reasoning`,
  `cached_input`, `cache_write`, `total`, and `cost`.

## Temporal And Derived Provenance

- The canonical node and edge boundary recursively normalizes all semantic
  `*_ref`/`*_refs` fields, typed refs, evidence refs, payload refs, and
  compatibility ref maps. Structured relation metadata such as
  `inference: "recent_failed_verification"` remains data rather than being
  mistaken for a selector.
- No `recent_*` selector reaches canonical refs, payload ref fields,
  compatibility refs, typed identities, or legacy edge evidence refs.
- CaseTrace resolves actual fallback candidates and reconciles advisory edges
  after repeated producer updates. Generic nodes, compaction/checks, changes,
  decisions, prompt assembly, context transforms, response output, exit
  gates, and legacy semantic edges are covered.
- Fallback links are emitted only with
  `evidence_tier: "temporal_advisory"`,
  `eligible_for_attribution: false`, and
  `derivation_method: "recent_source_fallback"`.
- Deterministic-derived nodes require non-empty valid node `input_refs` and
  derivation `input_refs`; their canonical sets must be exactly equal.
  Referenced nodes and artifacts must exist, external IDs must be non-empty,
  and replacement snapshots are validated against the replacement graph.
  Derived nodes without provenance or with mismatched provenance are rejected,
  as are observed nodes carrying derivation metadata.

## Alias Durability And Collision Policy

- Alias ownership is a set, not first-wins. Multiple owners emit one stable
  `alias_collision` diagnostic and leave affected refs external. Removing or
  updating an owner resolves the ref incrementally once identity is unique.
- A late unique owner appends replayable endpoint/node reconciliation rows.
  The same durable operation deterministically removes stale unresolved or
  collision diagnostics during replay. Every journal prefix reproduces the
  corresponding live snapshot.
- Reconciliation rows use valid Causal IR operations and legacy
  `record_type` values, contiguous sequences, and canonical payload hash
  chaining. Node and edge replacement rebuild current payload hashes before
  checkpointing.
- Full replay, lifecycle checkpoints, poisoned finalization, compatibility
  projection, and current-diagnostic reconciliation remain green.

## Incremental Indexing

- `nodeById`, per-node aliases, `aliasOwners`, reverse alias-to-affected
  node/edge IDs, edge/artifact indexes, and diagnostic owner indexes are
  maintained incrementally.
- Normal node create/update and edge insertion avoid full graph/index rebuilds
  and reconcile only aliases and diagnostics whose ownership changed.
- Deterministic property-read guards avoid wall-clock thresholds. The alias
  fixture contains 64 existing nodes, 64 unresolved edges, and 64 diagnostics;
  create and update must each remain below 20 unrelated node reads, 48 edge
  reads, and 48 diagnostic reads. A separate 128-edge insertion fixture allows
  fewer than 8 existing-edge identity/relation reads.

## Compatibility And Lifecycle

- `records` and `dataflow_edges` remain projections of canonical
  nodes/edges. The static viewer and Python loader continue to use that
  surface.
- The independent mutable `semanticEdges` array is removed. `edge()` writes
  only the canonical store and uses a short-lived normalized value for its
  return/event compatibility contract.
- Canonical projection metadata identifies legacy semantic edges and records
  which optional legacy fields were present. The legacy projector joins those
  canonical edges to the existing compatibility endpoint projection, restores
  original relation names and field order, and removes the internal marker
  from compatibility JSON and HTML.
- Same-ID edge updates and late alias reconciliation therefore have one owner:
  `legacy-trace.json`, canonical `trace.json`, `trace.json.dataflow_edges`,
  `partial/latest.json`, and `trace.html` all reflect the same canonical edge
  order and endpoints.
- Normal exit, SIGINT, SIGTERM, post-finish SIGTERM, and preflushed SIGKILL
  recovery remain green. Redaction, artifact rendering, journal poisoning,
  hash chains, replay, and Python backward attribution are covered by the full
  and focused gates.

## Remaining External Scope

No live DeepSeek request, external HTTP stress campaign, or historical corpus
re-attribution run was executed. Those environment-dependent release gates
remain outside local acceptance. There are no known remaining local findings
from the four unified migration review rounds.
