# Root-Cause Skill Bundle Handoff Fix Round 3

Date: 2026-08-25

Branch: `codex/trace-stability-integration`

> Superseded on 2026-08-25 by bundle handoff fix round 4. Round 4 binds all
> Agent-visible bundle operations to the reserved directory FD, replaces
> temporary hard-link publication with direct exclusive writes, snapshots Skill
> bytes during enumeration, and hashes directories as well as files.

## Scope

This round simplifies bundle publication to a fail-closed, monotonic handoff.
It changes only the isolated bundle builder, deterministic contracts, README,
implementation plan, and evaluation reports. It does not modify OpenCode runtime,
`trace_query.py`, Skill reasoning, fixtures, the retired Agent runner, or the
paused Python attribution module.

## Fail-Closed Protocol

1. The builder reserves the final destination using no-overwrite `mkdir` and
   writes `.handoff-owner` with `O_EXCL`. Marker failure can leave an unready
   reservation.
2. It never automatically deletes or rolls back destination, provenance, or
   READY. Every failure leaves audit evidence; retry requires a new opaque ID.
3. It opens the fixture root once and reads Trace and Artifact files through
   component-wise `openat` with `O_DIRECTORY` and `O_NOFOLLOW`. Hashing, parsing,
   authoritative validation, and publication use the same captured bytes.
4. The canonical Skill is enumerated and copied through a held directory FD;
   symlink entries and ancestor substitutions are rejected.
5. After all Agent-visible bytes are written and reopened for verification, the
   builder removes `.handoff-owner`. Failure aborts before provenance or READY.
6. The deterministic tree digest includes every remaining Agent-visible file.
   Provenance is published no-replace, then READY is published last and binds
   the opaque ID, provenance digest, and full tree digest.
7. Consumers accept a handoff only when READY exists and
   `--verify-ready-handoff` confirms both digests. Extra, removed, or changed
   files invalidate the handoff.

No cross-path transaction atomicity is claimed. Operator cleanup is manual and
is permitted only after confirming READY is absent. A present but invalid READY
must be quarantined and investigated rather than automatically deleted.

## Adversarial Coverage

The deterministic suite covers marker-write and marker-removal failures,
destination and publication races, failures after provenance replacement,
fixture-root replacement after the FD is held, fixture and Skill ancestor
symlink swaps, one-read source publication, full-tree mutation/addition,
portable path collisions, validator rejection, postwrite corruption, and all
four unready interruption phases.

## Verification

- 127 deterministic tests passed with no skips.
- `trace_query.py` and `prepare_isolated_bundle.py` compiled successfully.
- The official Skill validator reported `Skill is valid!`.
- Seven fresh bundles under `/tmp/rootcause-handoff-r3-final.3RbTdn` passed the
  external READY verifier. Every successful bundle had no owner marker, no
  symlink, no semantic fixture slug, and no hidden expected-data signature.
- The seven Agent-visible trees contained 11 files each, except `known-root`
  with its one declared Artifact and 12 files total. READY bound the complete
  tree in every case.
- Canonical fixtures and excluded runtime/query/attribution scope were unchanged;
  repository diff checks passed.

No Provider-backed Agent execution was required or claimed.
