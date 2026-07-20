# Task 8 Report: Durable Checkpoint And CLI Migration

## Scope

- Base Task 8 commit: `76ec68f23`.
- Hardened the recursive checkpoint after the independent review's five P1 and two P2
  findings.
- Closed the follow-up re-review's three remaining P1 consistency findings on top of
  `81c8ffe3f36d79e54bf4c7eade9d8df004f75ec7`.
- Kept `legacy` as the default engine and preserved all Task 1-7 report, Judge,
  investigation, confirmation, cache, and output compatibility behavior.
- Added no dependency, network request, shell execution, or mutation of the input Trace,
  Agent, or benchmark.

## RED Evidence

The review fixes were implemented with adversarial tests observed failing before their
production changes:

1. `build_checkpoint_config` rejected the required runtime identity, and no output
   transaction API existed.
2. A valid journal copied from another run restored successfully because records had no
   immutable run identity.
3. Removing a complete committed tail was accepted because no durable head anchored its
   count and hash.
4. A crash after frontier and hypotheses appends could leave an independently restored
   mixed snapshot.
5. A parseable final record without `\n` was accepted, so the next append could produce
   adjacent JSON objects.
6. An interrupted bounded Judge call with `max_judge_requests=1` resumed with physical
   usage `0`, no uncertainty, and an available budget.
7. Attribution could be marked complete before attribution and lineage outputs existed.
8. A simulated crash between attribution and lineage publication had no repair protocol.
9. Initialization had no observable proof that all journal directory entries were
   durable before a Provider boundary.
10. Tail repair was lost after initialization and absent from report metadata.
11. A changed `judge_timeout_sec` was accepted by the checkpoint fingerprint.
12. Even after valid hashes were recomputed, duplicate global transaction sequences were
    accepted.
13. Individually committed frontier, hypotheses, and action snapshots from different
    transactions could be assembled into one recursive state.
14. A durable exact `provider_call_failed` restored as an interrupted call, retaining
    the full reservation and changing the original terminal reason. The failing probes
    covered initial step, investigation rejudge, and root confirmation paths.
15. Provider circuit/cache/accounting identity was published after the three-member
    recursive snapshot, allowing a crash to pair current frontier state with stale
    Provider control state.
16. `mark_analysis_completed` accepted report B after output transaction A was durably
    published because it did not bind the report argument to the attribution bytes.

## WAL Protocol

- `manifest.json` contains an immutable UUID run ID and exact behavior compatibility
  config. Every journal record repeats that run ID and carries both a journal-local
  sequence and a strictly increasing global transaction sequence.
- `commit_snapshot` appends, flushes, and fsyncs frontier, hypotheses, and action members
  under one transaction. It then atomically replaces and directory-fsyncs `commit.json`.
- `commit.json` has an exact schema and hash and anchors the count, sequence, and record
  hash of all three journal heads. Restore rejects mixed run IDs, missing committed
  records, wrong heads, hash breaks, reorder, duplicate transaction numbers, and mixed
  recursive snapshot transaction/semantic identities.
- Complete or partial records beyond committed heads are crash tails. Initialization
  truncates them before another append. A committed parseable record missing only its
  final newline receives a durable delimiter repair; other incomplete tails are
  truncated. Every repair is persisted in the next commit and projected through
  `metadata.checkpoint_audit`.
- Initialization creates and fsyncs all three journal files and the checkpoint directory
  before any Provider request. `checkpoint_directory_durable` is available as a fault
  injection boundary.

## Provider Accounting

- Step and confirmation calls reserve and durably debit their complete bounded allowance
  before crossing the Provider boundary.
- A durable exact result reconciles the reservation to the actual physical delta without
  double charging, including cache-hit zero deltas and typed bounded failures.
- A durable exact failed step or rejudge is terminal replay state, not in-flight state.
  Resume restores its original terminal/unresolved reason, exact physical delta, and
  Provider circuit state without another Provider call. Exact failed confirmation is
  likewise replayed from its completed unknown confirmation result.
- A lone started call is never repeated and never fabricated as successful. Its
  reservation remains consumed, its result becomes explicit unknown, and
  `judge_request_uncertainty_count` is incremented. The same policy applies to recursive
  step/rejudge and root confirmation boundaries.
- Local investigations retain their existing started/completed replay policy and do not
  consume Provider request budget.
- Provider circuit, cache identity/statistics, and accounting are identity-bound inside
  the action member of the same global snapshot transaction as frontier and hypotheses.
  Missing provider state, a mismatched identity, cache identity drift, or snapshot
  accounting mismatch rejects restore. Historical Provider-result payloads retain
  strict schema/type/identity validation while allowing recursive accounting to have
  advanced monotonically through later investigation work.

## Output Transaction And Signals

- The analyzer now commits `analysis_ready`, not `analysis_completed`.
- Attribution and message lineage are each staged and fsynced. `output-commit.json`
  durably binds both paths and content hashes before either output is considered a
  complete transaction.
- Resume repairs a crash after the first output, after both replacements, or before the
  completion marker. Only after both final files and directories validate does the CLI
  append `analysis_completed`.
- Completion canonical-serializes the supplied report to the exact deterministic bytes
  used by output publication, requires that hash and bytes to equal the published
  attribution, and records attribution, lineage, output transaction, and output commit
  identities together. A mismatch raises before any completion journal side effect.
- SIGINT and SIGTERM share one stop flag whose handler remains installed through output
  publication. A signal between the two publications cannot split the transaction.
  Interrupted analyses publish explicit partial/inconclusive outputs and remain
  resumable.
- SIGKILL cannot execute a handler. Recovery uses only the last fsynced journal or output
  transaction state; the README makes no stronger claim.

## Compatibility Fingerprint

The fingerprint binds the Trace (including injected review), case, objective, perspective,
start refs, every recursive budget, model identity, effective endpoint, thinking config,
maximum tokens, request timeout, Provider error threshold, and Judge cache identity.
Changing any of them requires a new checkpoint directory.

## Verification

- Checkpoint and recursive CLI adversarial suite: **41 passed**.
- Checkpoint/CLI/recursive/legacy focused suite: **209 passed**.
- Complete offline suite: **392 passed**.
- Isolated `py_compile`: passed.
- `git diff --check`: passed.

## Residual Risks

- Real Provider process interruption remains intentionally untested; all boundary probes
  are deterministic offline fakes and fault hooks.
- SIGINT/SIGTERM received during a blocking SDK request become visible when that call
  returns or times out. The signal handler itself performs no filesystem I/O.
- Atomic rename and directory fsync use the local filesystem durability contract. Remote
  filesystems with weaker semantics remain outside this task's guarantee.
