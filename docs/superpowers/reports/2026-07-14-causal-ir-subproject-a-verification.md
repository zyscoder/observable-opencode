# Causal IR Subproject A Verification

## Scope And Result

This report covers only Subproject A's local compatibility and regression gate.
The acceptance evidence is green: the Trace 6.0 Python characterization passed,
the requested Bun suites passed, and TypeScript typecheck exited zero. No Python
production code was changed.

## Commands And Counts

All commands were run on 2026-07-14 from the indicated directory.

| Working directory | Command | Result |
| --- | --- | --- |
| repository root | `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_backward_taint -v` | 84 passed, 0 failed, 0.148 s |
| `packages/opencode` | `/private/tmp/bun-1.3.13/bin/bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts test/tool/semantic-observability.test.ts --timeout 30000` | 147 passed, 0 failed, 1,420 expectations, 23.38 s |
| `packages/opencode` | `/private/tmp/bun-1.3.13/bin/bun typecheck` | exit 0 (`tsgo --noEmit`) |

The Bun executable was the project-pinned `1.3.13` runtime. The three suites
contain 36 Causal IR tests, 108 CaseTrace tests, and 3 semantic-observability
tests. Python required `PYTHONPATH=tools/trace_attribution`; this is the
existing repository convention for the attribution package when invoked from
the repository root.

## Trace 6.0 Python Compatibility

`TraceGraphTest.test_loads_trace_6_causal_ir_with_legacy_attribution_projection`
adds a minimal Trace 6.0 fixture with both canonical `nodes`/`edges` and the
compatibility `records`/`dataflow_edges` projection. `TraceGraph.from_trace`
continues to expose exactly `record:decision_1` and `record:claim_1`, resolves
the claim's upstream reference to the decision, and supports the same backward
taint path and root (`claim -> decision`).

The new test passed immediately. This is a green characterization of the
existing records/dataflow loader, not a reason to alter Python production code.

## Canonical And Compatibility Evidence

- Canonical `trace.json` uses `trace_version: "6.0"`,
  `causal_ir_version: "1.0"`, `nodes`, `edges`, `artifacts`, `diagnostics`,
  `compatibility`, `records`, and `dataflow_edges`.
- The `provenance-trace.json` projection retains the legacy-facing
  `manifest`, `records`, `dataflow_edges`, `artifacts`, `metrics`, and
  `trace_version` fields only. The suite asserts it has no canonical
  `causal_ir_version`, `nodes`, `edges`, `diagnostics`, or `compatibility`.
- The verified bundle assertion requires canonical node IDs to equal projected
  record IDs, canonical and projected record/edge/artifact collections to be
  equal, and projected edge metadata to retain original relation,
  normalized relation, evidence tier, attribution eligibility, and derivation
  method.
- Journal replay equality is checked against canonical `trace.json`: node IDs,
  edge IDs, artifacts as `[artifact_id, hash]`, diagnostics, and the complete
  snapshot fields match.
- The journal audit checks contiguous sequences, operation-to-`record_type`
  contracts, payload hashes, and each entity/category
  `previous_payload_hash` chain. Artifact tests additionally verify reuse and
  a single SHA-256 artifact path for repeated payloads.

## Lifecycle, Passive Collection, And Redaction

The passing CaseTrace suite exercises generated child processes for these
conditions:

| Condition | Verified result |
| --- | --- |
| Normal exit | `trace.json`, `legacy-trace.json`, and `trace.html` are written; the legacy trace is successful. |
| SIGINT | Manifest and provenance status are cancelled; final partial equals trace; journal replay and forced checkpoint match the trace. |
| SIGTERM | Process exits 143; canonical trace, partial, and HTML are available with cancelled case/server state and replay equality. |
| SIGKILL | It is intentionally unhandleable: a previously flushed partial snapshot and HTML remain readable, journal replay matches that partial, and no finalization is claimed. |

Passive behavior also passed: collection declares `passive_sidecar` and
`behavior_impact: "none"`. In the forced trace-write failure test, the traced
and untraced agent-visible stdout bytes and SHA-256 hash are identical, both
processes exit zero, and fallback trace artifacts remain present. Redaction
tests preserve original execution inputs while ensuring persisted trace,
journal, artifacts, HTML, and projection files exclude the seeded credentials;
journal hash-chain auditing remains valid after redaction.

## Remaining Work Outside Subproject A

- **Subproject B:** migrate typed producer families in bounded batches and
  remove each parallel mutable store only after projection-equivalence tests.
- **Subproject C:** migrate `trace.html`, Python loading, message-lineage
  reconstruction, and attribution overlays to canonical IDs and Causal IR
  edges while retaining compatibility regression coverage.
- **Subproject D:** remove remaining duplicate state, replay historical traces,
  rerun offline attribution, execute HTTP stress cases, recheck lifecycle and
  passive behavior, and complete release validation.

No HTTP stress cases and no DeepSeek attribution requests were executed for
this Subproject A report. The evidence above is limited to the named Python and
TypeScript local regression suites.
