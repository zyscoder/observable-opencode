# Root-Cause Skill Bundle Handoff Fix Round 2

Date: 2026-08-25

Branch: `codex/trace-stability-integration`

> Superseded on 2026-08-25 by bundle handoff fix round 3. Round 3 removes
> automatic cleanup of reserved or published paths, removes the owner marker
> before the full tree digest, and binds fixture/Skill reads to held directory
> descriptors. The cleanup behavior below is historical, not the current contract.

## Scope

This round replaces optimistic cleanup-based publication with an explicit
readiness protocol. It changes only the isolated bundle builder, its deterministic
tests, and handoff documentation. It does not modify the Agent runner, OpenCode
runtime, `trace_query.py`, Skill reasoning, fixtures, or paused attribution module.

## Publish Protocol

1. Reserve the final destination atomically with no-overwrite `mkdir`.
2. Immediately write an exclusive unique `.handoff-owner` token and retain the
   destination inode identity.
3. Read the source Trace exactly once with no-follow where available. Hash and
   parse those bytes, write a private snapshot, and run the authoritative
   `trace_query.py validate` command on that snapshot.
4. Read each Artifact source once, verify its declared digest, and write those
   same bytes. Validate the derived Trace from the exact publisher-owned bytes.
5. Reopen published files, compute a deterministic sorted bundle-tree digest,
   and create evaluator provenance.
6. Publish provenance outside the bundle with no-replace temp/fsync/hard-link.
7. Publish READY last with no-replace semantics. READY binds the opaque case ID,
   provenance SHA-256, and bundle-tree SHA-256.
8. Permit Harness consumption only when `--verify-ready-handoff` verifies both
   READY-bound digests.

The owner marker is excluded from the bundle-tree digest because it is publisher
cleanup metadata, not Agent evaluation input. Ordinary cleanup removes a
destination only if both its original inode and owner token still match. It never
renames over a destination or deletes one that fails ownership validation.

This is not a cross-path atomic transaction. A process crash can leave an unready
destination, private snapshot, or provenance. Such files are deliberately
non-consumable without a valid READY marker and matching digests.

## Portable Artifact Policy

The preflight rejects reserved bundle files and their ancestors/descendants, the
canonical Skill tree, duplicate and file/directory collisions, NFKC+casefold
collisions, trailing dots/spaces, Windows device names, control characters,
colon, backslash, `<>|?*`, empty/dot/parent components, and repeated separators.

## Verification

- 121 deterministic tests passed with no skips.
- Tests cover destination and output races, owner-safe cleanup, source snapshot
  TOCTOU, one-read Artifact publication, portable paths, four injected crash
  phases, unready rejection, provenance/tree tampering, and no-overwrite outputs.
- `trace_query.py` and `prepare_isolated_bundle.py` compiled successfully.
- The official Skill validator reported `Skill is valid!`.
- Seven fresh bundles under `/tmp/rootcause-handoff-r2-final-gnabzke7` passed the
  external READY verifier. Hidden-answer leaks and symlinks were both zero.
- Canonical fixtures were unchanged and repository diff checks passed.

No Provider-backed Agent execution was performed or scored by this repository.
