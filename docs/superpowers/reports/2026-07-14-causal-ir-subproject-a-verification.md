# Causal IR Unified Kernel Migration Verification

## Scope And Result

Round 2 closes all five findings in `.superpowers/sdd/final-review-round2.md`:
2 Critical and 3 Important. Together with the first unified-fix round, the
local Causal IR migration acceptance gate is green.

This round changes only the owned TypeScript Causal IR/CaseTrace
implementation, their focused tests, and this report. The viewer, semantic
observability implementation, and Python attribution implementation did not
need changes: they continue to consume the compatibility projection and their
existing behavior remains covered by final verification.

## Commands And Counts

All final commands were run on 2026-07-15. The Bun executable reports version
1.3.13.

| Working directory | Command | Result |
| --- | --- | --- |
| `packages/opencode` | `/private/tmp/bun-1.3.13/bin/bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts test/tool/semantic-observability.test.ts --timeout 30000` | 160 passed, 0 failed, 1,598 expectations, 23.73 s |
| `packages/opencode` | `/private/tmp/bun-1.3.13/bin/bun typecheck` | exit 0 (`tsgo --noEmit`) |
| repository root | `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_backward_taint -v` | 85 passed, 0 failed, 0.134 s |
| `packages/opencode` | signal filter: `-t "(SIGINT\|SIGTERM\|SIGKILL\|process exits)"` | 6 passed, 0 failed, 158 expectations, 1.89 s |
| `packages/opencode` | viewer filter: `-t "(trace.html\|renders component\|renders all agent\|renders v6.0 provenance\|renders artifact-backed)"` | 8 passed, 0 failed, 184 expectations, 1.453 s |
| `packages/opencode` | redaction filter: `-t "(redact\|token metrics)"` | 4 passed, 0 failed, 69 expectations, 1.70 s |
| `packages/opencode` | replay filter over Causal IR and CaseTrace: `-t "(replay\|canonical partial\|poisoned journal semantics)"` | 16 passed, 0 failed, 221 expectations, 1.74 s |
| repository root | `git diff --check` | exit 0 |

The full Bun total comprises 43 Causal IR tests, 114 CaseTrace tests, and 3
semantic-observability tests. Focused filters overlap that full total and are
listed as targeted acceptance evidence, not additional tests.

## Canonical Reference Identity

- Nodes publish canonical `record:<node_id>` and `node:<node_id>` aliases plus
  kind-specific legacy aliases. The store maintains a legacy type/ID to
  canonical `node_id` index and resolves node, edge, evidence, and derivation
  refs through it.
- Tool calls and tool results with the same legacy call ID resolve to distinct
  canonical node IDs. Canonical edges therefore cannot collapse into a shared
  call-ID self-loop.
- A reference that has no indexed node, including an explicit missing
  `node:<id>` ref, is emitted as `ref_type: "external"` and receives a stable
  `unresolved_ref` diagnostic.
- Canonical refs retain `legacy_ref`. The compatibility projector derives both
  legacy endpoint type and ID from that field, so historical consumers still
  see endpoints such as `tool_call:<call_id>` and
  `tool_result:<call_id>`.

## Redaction And Token Usage

- `token_usage` is a closed schema: `input`, `output`, `reasoning`,
  `cached_input`, `cache_write`, `total`, and `cost`. A field survives only
  when its value is a finite number; unknown children and string impostors are
  discarded, and `undefined` is omitted.
- Authorization assignments, complete Cookie/Set-Cookie header values, quoted
  JSON credentials, headers, shell flags and assignments, URL userinfo/query
  credentials, and `Error` messages/stacks are redacted.
- The regression fixture scans events, raw events, canonical journal, final and
  legacy JSON, provenance projection, partial snapshot, manifest, HTML, and
  every artifact. None contains seeded plaintext secrets.
- Agent/model/tool input objects retain their original values and identities;
  tracing sanitizes copies and does not mutate agent-visible data.

## Temporal And Derived Provenance

- The shared node/edge boundary strips `recent_*` selectors. Producer entry
  points carry the resolved fallback selection separately, including actual
  compaction, compaction checks, decisions, prompt assembly, context
  transforms, response output, and exit gates.
- Resolved fallback nodes appear only on edges with
  `evidence_tier: "temporal_advisory"`,
  `eligible_for_attribution: false`, and
  `derivation_method: "recent_source_fallback"`. Compatibility `source_refs`
  and compaction reference payloads contain no selector or inferred fallback
  dependency.
- Python attribution still skips an explicitly false eligibility flag while
  accepting a legacy edge with no flag; all 85 compatibility/attribution tests
  remain green without a Python change.
- `response.claim` and `claim.support_assessment` nodes are
  `deterministic_derived`. They carry algorithm, version, derivation time,
  reproducibility, and non-empty typed input refs. The store rejects derived
  nodes without valid derivation/input provenance and rejects observed nodes
  that claim derivation provenance.

## Finalization And Replay Semantics

- `finalize()` returns the durable commit result: operation, committed state,
  durable sequence, committed payload hash when present, and poison state.
- On the normal path, `case.finalized` is the last durable row and full replay
  remains deep-equal to `trace.json`. The lifecycle payload continues to use
  the non-recursive pre-entry journal summary.
- If only the final append fails, no `case.finalized` row exists. The emitted
  `trace.json` and `partial/latest.json` refresh their journal summary with
  `poisoned: true` and retain the last durable sequence/hash. They intentionally
  do not claim finalized/full-replay equality; replay can reconstruct the last
  durable prefix, while the emitted poisoned trace records the failed
  finalization attempt.
- The failure remains passive: agent output and exit status are unchanged.

## Compatibility And Lifecycle

- `records` and `dataflow_edges` remain explicit projections of canonical
  nodes/edges. The static HTML viewer and Python loader continue to use that
  surface; no duplicate mutable graph was introduced.
- Normal exit, SIGINT, SIGTERM, post-finish SIGTERM, and preflushed SIGKILL
  recovery are green. Artifact rendering, redaction, hash-chain checks, and
  replay behavior are covered by both the full suite and focused filters.

## Remaining External Scope

No live DeepSeek request, external HTTP stress campaign, or historical corpus
re-attribution run was executed. Those environment-dependent release gates are
outside local acceptance. There are no known remaining local findings from the
two unified migration review rounds.
