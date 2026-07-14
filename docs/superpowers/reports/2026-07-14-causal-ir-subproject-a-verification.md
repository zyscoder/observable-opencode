# Causal IR Unified Kernel Migration Verification

## Scope And Result

This report records the final unified fix for all nine findings in
`.superpowers/sdd/final-review.md`: 4 Critical, 3 Important, and 2 Minor.
The local acceptance gate is green. Canonical Trace 6.0 output now carries the
formal Causal IR 1.0 envelope and full replay state while retaining the
`records`/`dataflow_edges` compatibility projection.

Production changes are limited to the owned TypeScript Causal IR/CaseTrace
implementation and the Python compatibility graph loader. No viewer change was
required because the viewer continues to consume the explicit compatibility
projection rather than canonical-only fields.

## Commands And Counts

All final commands were run on 2026-07-15 from the indicated directory.

| Working directory | Command | Result |
| --- | --- | --- |
| `packages/opencode` | `/private/tmp/bun-1.3.13/bin/bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts test/tool/semantic-observability.test.ts --timeout 30000` | 154 passed, 0 failed, 1,485 expectations, 21.06 s |
| `packages/opencode` | `/private/tmp/bun-1.3.13/bin/bun typecheck` | exit 0 (`tsgo --noEmit`) |
| repository root | `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_backward_taint -v` | 85 passed, 0 failed, 0.133 s |
| repository root | `git diff --check` | exit 0 |

The Bun executable reports version `1.3.13`. The Bun total comprises 40 Causal
IR tests, 111 CaseTrace tests, and 3 semantic-observability tests. Python uses
the repository-root `PYTHONPATH=tools/trace_attribution` convention.

## Canonical Envelope And Replay

- `trace.json` contains top-level `trace_version: "6.0"`,
  `causal_ir_version: "1.0"`, `manifest`, canonical `nodes` and `edges`,
  `artifacts`, `journal`, `metrics`, `diagnostics`, and `compatibility`.
- Every canonical node has `schema_version`, `origin`, `order`, `scope`,
  `payload`, typed input/output/source refs, source locations, artifact refs,
  aliases, derivation, and integrity hashes. Compatibility aliases remain a
  superset and are not used as substitutes for the envelope.
- Every canonical edge has typed endpoints, original and normalized relation,
  evidence tier, eligibility, derivation method, typed evidence refs,
  confidence/label when present, and metadata.
- `replayCausalIRTrace(records)` reconstructs the complete persisted Trace 6.0
  document. The bundle, normal-exit, signal, diagnostic, and partial-recovery
  gates deep-compare replayed documents with their canonical trace or partial.
- Lifecycle self-summary is intentionally non-recursive: a lifecycle entry's
  embedded trace summarizes the journal prefix immediately before that entry.
  Therefore final `trace.json.journal.last_sequence` is the finalization
  entry's sequence minus one. The convention is deterministic and is asserted
  together with the final payload hash.

## Attribution And Compatibility

- Explicit source refs remain attribution-bearing. Missing or `recent_*`
  selectors produce only `temporal_advisory` edges with
  `eligible_for_attribution: false` and `recent_source_fallback`; they do not
  enter compatibility `source_refs`.
- The rule covers response output/claims, tool-failure context, observations,
  changes, exit gates, and aggregated compaction checks. Tool-failure handling
  status remains observable without promoting recent failures to dependencies.
- Python skips compatibility edges when either the top-level or metadata
  eligibility flag is explicitly false. A legacy edge with no flag remains
  eligible. The Trace 6.0 fixture also proves canonical-only nodes/edges are not
  misread as compatibility graph input.
- `provenance-trace.json` retains the historical `manifest`, `records`,
  `dataflow_edges`, `artifacts`, and `metrics` contract. The static HTML viewer
  continues to render that projection and artifact links.

## Redaction And Durability

- Persisted events, journal rows, canonical/legacy/provenance JSON, partials,
  HTML, and artifact content redact plain token strings, non-numeric token
  metric impostors, quoted JSON credentials, authorization/API-key headers,
  shell assignments/options, URL userinfo/query credentials, and `Error`
  messages/stacks. Numeric values survive only in explicit token-usage metric
  fields. The seeded agent/model/tool objects are unchanged after tracing.
- `records.jsonl` contains only canonical operations with sequence and payload
  hashes. Duplicate-evidence and weak-observation suppression use stable
  `diagnostic.created` entries.
- Journal sequence and payload-hash state commit only after append succeeds.
  The first failure poisons later journal writes without a sequence gap or
  dangling previous hash and remains passive to the observed agent.
- Artifacts are written to a same-directory temporary file and atomically
  renamed before registration. Failure yields no artifact or HTML link and
  records `artifact_write_failed` plus `artifact_status: "write_failed"`.

## Performance, References, And Lifecycle

- Edge insertion uses edge-ID and diagnostic-ID indexes plus incremental
  unknown-relation reconciliation. The regression probe inserts after 128
  existing edges while observing fewer than eight existing-edge property reads.
- `parent_span_id`, `input_refs`, and `output_refs` round-trip through canonical
  typed refs and the legacy compatibility projection.
- Normal exit, SIGINT, SIGTERM, post-finish SIGTERM, and preflushed SIGKILL
  recovery remain green. Passive-sidecar metadata, redaction/hash-chain checks,
  artifact de-duplication and HTML viewing, and historical compatibility tests
  all run inside the 111-test CaseTrace suite.

## Remaining External Scope

No live DeepSeek request, external HTTP stress campaign, or historical corpus
re-attribution run was executed. Those environment-dependent release gates are
outside this local final-fix verification; there are no known remaining local
code findings from `final-review.md`.
