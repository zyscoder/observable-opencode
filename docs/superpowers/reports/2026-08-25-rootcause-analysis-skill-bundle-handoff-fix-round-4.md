# Root-Cause Skill Bundle Handoff Fix Round 4

Date: 2026-08-25

Branch: `codex/trace-stability-integration`

## Scope

This round binds bundle construction to the exact directory inode reserved by
the publisher. It changes only the isolated bundle builder, deterministic tests,
README, implementation plan, and handoff reports. It does not change OpenCode
runtime, `trace_query.py`, Skill reasoning, fixtures, the retired Agent runner,
or the paused attribution module.

## FD-Bound Construction

1. `reserve_destination` returns a held `destination_fd` and its dev+ino.
2. Every Agent-visible mkdir, open, write, reopen, list, hash, fsync, and owner
   marker unlink is relative to that FD with no-follow semantics. No bundle
   payload is written through the destination pathname after reservation.
3. Before provenance and again before READY, `fstat(destination_fd)` and
   `lstat(destination)` must identify the same non-symlink directory. A renamed
   or replaced destination aborts without touching the replacement or publishing
   READY.
4. The canonical Skill is snapshotted while its held FD is enumerated: each file
   is opened and read immediately, every directory is recorded, and symlinks or
   special files are rejected. Bundle writes use only captured bytes and entries.
5. The final tree manifest is generated from `destination_fd`. It records every
   directory and file, exact and NFKC-normalized relative paths, modes, and file
   size/SHA-256. Empty-directory additions, removals, or renames change the digest.

## Direct Publication

Provenance and READY are written directly to their final external files using
`O_CREAT|O_EXCL|O_NOFOLLOW`, full write, and fsync. There is no temporary-file/
hard-link publication helper and no publication cleanup. A crash may leave a
partial provenance with no READY or a partial READY with invalid JSON/digests;
the verifier rejects both and the builder never deletes them.

## Adversarial Coverage

Tests cover destination replacement before provenance and between provenance and
READY, pathname-write rejection after reservation, Skill mutation after snapshot,
Skill symlink/special-entry rejection, empty directory preservation and digest
binding, direct partial provenance/READY, temporary-lookalike preservation, and
the prior fail-closed/race/portable-path contracts.

## Verification

- `python3 -m unittest discover -s tools/rootcause_skill_tests -p 'test_*.py'`:
  137 tests passed.
- `python3 -m py_compile` for `trace_query.py` and
  `prepare_isolated_bundle.py`: passed with bytecode cache redirected to `/tmp`.
- Official Skill Creator `quick_validate.py`: `Skill is valid!`.
- Seven fresh opaque handoffs were built under
  `/tmp/rootcause-handoff-r4-final.h16q26`; all seven passed
  `--verify-ready-handoff` with distinct provenance and bundle-tree digests.
- The seven Agent-visible bundle trees contained no symlinks. The deterministic
  fixture, hidden-answer, path, mutation, empty-directory, partial-publication,
  and destination-replacement audits are covered by the passing test suite.
- No Provider-backed execution or external wait was performed.
