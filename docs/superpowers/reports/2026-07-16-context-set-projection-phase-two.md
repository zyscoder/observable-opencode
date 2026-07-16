# Context Set and Projection Phase Two Report

## Scope

Phase two reduces repeated context-membership edges and terminal file copies.
It does not change prompt construction, context selection, tool execution, or
agent-visible inputs and outputs. Context sets are passive Causal IR projection
nodes created only after the corresponding runtime facts already exist.

## Implementation

- Confirmed tool-result selections are represented as content-addressed
  `context.pack` nodes scoped by session, message, and exact member list.
- Each tool result has one `selected_into_context` edge to its set, and each
  context transform has one `used_as_context` edge from that set.
- Temporal fallback windows keep their member refs in the set payload and emit
  one attribution-ineligible set-to-target edge. They never emit member-to-target
  edges or add temporal members to formal source refs.
- Terminal `partial/latest.json` is atomically hard-linked to `trace.json` on
  supported filesystems, with an atomic content-write fallback.

## Automated Verification

| Verification | Result |
| --- | ---: |
| Causal IR tests | 53 passed, 0 failed |
| CaseTrace tests | 128 passed, 0 failed |
| FeatureBench runner tests | 9 passed, 0 failed |
| Stress review tests | 13 passed, 0 failed |
| Offline attribution tests | 93 passed, 0 failed |
| TypeScript typecheck | passed |
| Native macOS build and smoke test | passed |

## Real HTTP Regression

The current source was compiled as a native Apple Silicon binary. The
`semantic-requirement-priority` case then ran through `opencode serve`, HTTP
session creation, and an HTTP message request with `deepseek-v4-pro`.

| Metric | Phase-one trace | Phase-two trace | Change |
| --- | ---: | ---: | ---: |
| Nodes | 189 | 206 | +9.0% |
| Edges | 1,157 | 445 | -61.5% |
| `trace.json` bytes | 4,162,004 | 2,901,909 | -30.3% |
| Artifacts | 63 | 63 | unchanged |
| Temporal advisory edges | 653 | 43 | -93.4% |
| Direct temporal member-to-target edges | 653 | 0 | -100% |
| Tool-context derivation edges | 150 | 79 | -47.3% |
| Confirmed context sets | 0 | 10 | +10 |
| Temporal context sets | 0 | 15 | +15 |

The two model runs are not byte-for-byte deterministic, so the table is not a
microbenchmark. The strongest comparable invariants are unchanged artifact
count, 25 tool-bearing context transforms in both traces, and the explicit
reachability checks below.

## Attribution Preservation

- All 25 tool-bearing context transforms contain a resolvable context-set ref,
  and the offline Python `TraceGraph` expands all 25 into
  tool-result -> set -> transform paths.
- The real trace contains 9 unique tool outcomes selected into model context;
  all 9 are backward reachable from decision nodes over attribution-eligible
  edges.
- No temporal fallback edge is attribution eligible.
- Causal IR reports no unknown-relation diagnostic and the offline graph reports
  no unresolved context-set alias.

The real offline judge completed with zero judge errors and zero unresolved
refs. It remained inconclusive about why evidence grounding scored 17/25: the
trace review identifies missing `claim_direct_evidence_refs`, and some broad
claim-to-response provenance does not establish a defect-introduction boundary.
No blocking gap attributes the failure to context-set traversal. This is an
existing result-attribution quality gap, not a phase-two reachability regression.

## Storage and Lifecycle

Both canonical paths remain available. In the phase-one trace,
`trace.json` and `partial/latest.json` had different inodes and each occupied a
full 4,162,004-byte copy. In phase two they share one inode with link count 2,
removing one physical 2,901,909-byte terminal copy while preserving identical
path contents. The case and manifest still finish successfully.

## Remaining Projection Work

Compatibility provenance, legacy JSON, and HTML are still materialized because
current readers consume those files directly. A later unified projection
refactor is valuable because it would derive viewer and analyzer views from one
indexed Causal IR graph, prevent semantic drift between source refs and edge
projections, reduce additional full-file copies, and make set expansion a shared
query primitive.

That refactor is best started after compatibility readers understand context
sets, golden replay tests cover every projection, large signal traces establish
memory and viewer-load baselines, and release telemetry or an explicit version
gate shows that legacy readers can be migrated safely. Until then, phase two
keeps the compatibility file contract while removing the largest redundant
canonical copy and the highest-cardinality edge families.
