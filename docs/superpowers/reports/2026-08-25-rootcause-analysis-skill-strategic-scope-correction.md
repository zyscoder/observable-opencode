# Root-Cause Analysis Skill Strategic Scope Correction

Date: 2026-08-25

Branch: `codex/trace-stability-integration`

## Decision

This repository now owns only deterministic preparation of root-cause Skill
evaluation inputs. The experimental in-repository Agent execution path is removed.
The repository does not implement scored Provider-backed execution, credential
handling, execution isolation, network policy, runtime cleanup, image attestation,
process supervision, or execution audit security.

This is an ownership correction, not a relaxation of scored evaluation security.
Those controls belong in an existing trusted benchmark Harness or isolated
execution service that is independently operated and reviewed.

## Retained Handoff

`prepare_isolated_bundle.py` remains the sole evaluation helper. For each of the
seven fixture cases it:

1. validates the finalized source Trace through the authoritative
   `trace_query.py validate` contract and checks canonical node integrity;
2. verifies every declared Artifact digest and containment boundary;
3. atomically reserves a no-overwrite workspace and writes an exclusive owner
   marker with a fresh opaque case identity;
4. sanitizes manifest and scope identities and recomputes derived integrity;
5. includes the canonical `rootcause-analysis` Skill and runtime-specific prompts;
6. preserves the exact user question while excluding hidden expected outcomes;
7. validates the sanitized derived Trace through the same authoritative contract;
8. writes fixture mapping, source/derived digests, exact lifecycle and
   source-completeness diagnostics, Artifact digests, and derivation provenance
   to a required evaluator-side file outside the Agent-visible bundle;
9. publishes an external READY marker last, binding the opaque identity,
   provenance digest, and deterministic bundle-tree digest.

Every canonical node must provide a valid source hash. The Harness-visible Trace
path embedded in prompts must be an absolute POSIX path without control characters
or parent traversal.

Artifact paths are collision-checked against reserved bundle paths and their
ancestors/descendants, the canonical Skill prefix, duplicates, portable
casefold/Unicode normalization, Windows device names, trailing dots/spaces,
forbidden characters, and ambiguous components. Source Trace and Artifact bytes
are read through one held fixture-root directory descriptor and component-wise
no-follow `openat`; the same bytes are hashed, parsed/validated, and published.
The canonical Skill is enumerated and copied through a held directory descriptor
and rejects symlink traversal.

Construction writes directly into the reserved final destination while it is
unready. Provenance and READY use no-replace publication outside the bundle;
READY is published last. A Harness may consume only after
`--verify-ready-handoff` confirms both READY-bound digests. Crashes can leave an
unready directory or provenance, so the protocol does not claim cross-path
transaction atomicity. The owner marker is removed before digest publication;
the digest covers every remaining Agent-visible file. The builder performs no
automatic rollback or deletion of reserved destination, provenance, or READY.
Failures remain unready, retries require a new opaque ID, and manual operator
cleanup is allowed only after confirming READY is absent.

The bundle contains no case catalog, rubric, sibling fixture, report, Git history,
semantic fixture identity, or symlink back to the repository.

## Handoff Contract

The evaluator submits exactly one opaque bundle to a trusted benchmark Harness.
The Harness independently owns credentials, filesystem and network isolation,
runtime cleanup, execution environment attestation, termination, and audit
storage. It returns model outputs to an evaluator that alone can access the
fixture-to-opaque mapping and hidden rubric.

Local or source-tree Agent attempts remain unscored smoke only. This repository
ships no root-cause helper that launches an Agent or accepts Provider credentials.

## Historical Records

Earlier reports described successive attempts to construct an execution trust
boundary in this repository. Those sections are superseded. Historical Claude
authentication and OpenCode provider-discovery failures remain factual unscored
observations, not evidence of Skill quality or safe scored execution.

## Verification Intent

The acceptance gate now covers only repository-owned guarantees: deterministic
tests, Python compilation for the builder and query helper, official Skill
validation, seven fresh opaque bundle builds, hidden-answer and symlink audits,
fixture immutability, and clean diffs. Actual model scoring is deliberately
outside this repository and must not be inferred from these checks.

## Verified Result

The round-2 numbers below are retained as historical evidence. Current round-3
verification is recorded in the dedicated round-3 report: 127 deterministic
tests and seven fresh full-tree READY handoffs passed.

- 121 deterministic tests passed with no skips in bundle handoff fix round 2.
- `trace_query.py` and `prepare_isolated_bundle.py` compiled successfully.
- The official Skill validator reported `Skill is valid!`.
- Seven fresh bundles were built under
  `/tmp/rootcause-handoff-r2-final-gnabzke7` with opaque identities, zero hidden-answer
  leaks, zero symlinks, external provenance and READY files, and successful
  verification of all seven provenance and bundle-tree digest pairs.
- The canonical fixtures were unchanged and repository diff checks passed.

No Agent was launched and no scored result is claimed by this verification.
