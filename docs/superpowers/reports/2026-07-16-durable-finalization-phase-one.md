# Durable Finalization Phase One Report

## Scope

This report verifies catchable-signal finalization, compact Causal IR lifecycle
journaling, canonical-first publication, runner signal forwarding, and passive
behavior. It does not claim `SIGKILL` handling; `SIGKILL` remains recoverable
only through the latest periodic checkpoint.

## Automated Verification

| Verification | Result |
| --- | ---: |
| Causal IR tests | 53 passed, 0 failed |
| CaseTrace tests | 125 passed, 0 failed |
| FeatureBench runner tests | 8 passed, 0 failed |
| TypeScript typecheck | passed |
| Passive sidecar/write-failure regressions | passed |
| Journal graph replay | equivalent to canonical graph |
| Compact trace replay | deep-equal by every top-level field |

## Real Runner Regressions

Two production-shaped paths were exercised after the synthetic scale fixture:

- A FeatureBench `mwaskom__seaborn` HTTP run was interrupted through the runner
  with `SIGINT`. The runner exited with code 130 only after `trace.json` was
  published. All terminal files exist, the manifest consistently records the
  cancelled signal lifecycle, journal replay is exact, and the compact final
  journal entry contains neither a snapshot nor a full trace.
- `semantic-requirement-priority` completed naturally through `opencode serve`,
  HTTP session creation, and an HTTP message request. It produced a successful
  189-node, 1,157-edge trace whose terminal partial was byte-identical to the
  canonical trace.

The first real signal attempts also exposed the original ordering defect: an
earlier server signal listener could call `process.exit` before CaseTrace
finished, and a runner child sharing the terminal foreground process group
could receive Ctrl-C independently. `prependOnceListener`, detached child
process groups, and group-level forwarding close both paths.

## Large Signal Fixture

The controlled fixture generated 3,002 nodes and 60,001 edges, then sent
`SIGTERM` while the process remained active.

| Metric | Result |
| --- | ---: |
| Signal-to-process-exit duration | 6,448 ms |
| Process exit code | 143 |
| `trace.json` | 66 MB |
| `partial/latest.json` | 66 MB |
| `records.jsonl` | 44 MB |
| `provenance-trace.json` | 30 MB |
| `trace.html` | 24 MB |
| Final `case.finalized` entry | 4,584 bytes |
| Final entry contains `snapshot` | no |
| Final entry contains full `trace` | no |

The terminal manifest reports `cancelled` consistently for status, server,
process, and case, with `shutdown_signal=SIGTERM`. `partial/latest.json` is
identical to `trace.json`. Journal replay reconstructs identical nodes, edges,
artifacts, and diagnostics. Compact full-trace replay is deep-equal for every
top-level field; plain `JSON.stringify` ordering differs but semantic object
equality does not.

## Comparison With Previous Failure

The earlier Pydantic run left only a 164 MB running partial and a 115 MB
journal, with 133 open records and no canonical `trace.json`. The new path
publishes `trace.json` first, writes terminal manifest and partial next, and
generates compatibility outputs afterward. The final lifecycle entry is now
bounded metadata rather than another full graph copy.

This is not a direct byte-for-byte benchmark because the semantic payloads
differ, but it validates the failed boundary at a comparable graph scale.

## Phase-Two Baseline

The remaining amplification is visible even after finalization is fixed:

- 60,001 edges for 3,002 nodes;
- 66 MB canonical trace plus a second 66 MB terminal partial;
- 30 MB provenance projection and 24 MB embedded HTML;
- 44 MB incremental journal.

Phase two should therefore target context membership cardinality and eager
projection copies. Its regression gate must preserve direct attribution paths,
artifact access, lifecycle state, redaction, and canonical replay while
measuring graph edges, total bytes, projection time, viewer load time, and
backward-attribution candidate count.
