# Root Confirmation Stability Task 2 Critical Fix Wave 19

Base commit: `3e5d8b878`

Close every finding from the third independent whole-Task-2 review.

## 1. Sanitize evidence objects atomically

The graph-aware Judge sanitizer must classify a mapping before producing any
output. For a typed evidence/reference/artifact mapping, inspect all scalar and
list identities first. If any required identity is stale, unresolved,
ambiguous, malformed, or ineligible, remove the entire associated mapping,
including sibling content, excerpts, labels, summaries, and metadata.

- `evidence_refs: [stale]` plus `content` must not become empty refs plus content.
- Recognize typed identity values under supported envelope/reference fields,
  including aliases and supported citation shapes.
- Unknown keys must not create a bypass merely by carrying a resolvable
  `record:`/`artifact:` value.
- Ordinary business data such as `config_ref` and `customer_ref` must remain
  intact outside typed evidence/reference contexts.
- Remove duplicate non-graph-aware final filtering. Global, recursive-step, and
  confirmation final JSON must all pass through the same graph-aware sanitizer.

Add exact final-payload RED tests for stale list refs plus sibling content,
unknown-key citations, aliases, encoded JSON, nested lists/mappings, and valid
ordinary business fields.

## 2. Require trusted graph identity for every artifact

An artifact hash proves byte integrity, not provenance. Judge-visible artifacts
must either:

- resolve to a manifest artifact that is available and passes the verified
  artifact reader; or
- be a verified hydration slice with a resolved manifest artifact, exact owner
  envelope, declared byte range, canonical SHA-256, and matching bytes.

Unindexed/self-declared artifacts, missing/truncated/unresolved artifacts, and
unowned embedded content are audit-only and excluded as whole facts. Cover all
three Judge stages and restored capsules.

## 3. Persist exact owner identity for local analysis state

Add exact seed binding, hypothesis identity, and visit/occurrence ownership to
causal step judgments, causal relations, visited entries, global-pass metadata,
decisive evidence, expansion summaries, and confirmation journal entries that
can survive checkpoint/report restore.

- Live creation must record owner identity.
- Partial and completed restore must filter only stale-owned entries.
- Shared node refs used by active and stale seeds remain separate by owner.
- Rebuild aggregate metadata from retained owner-bound entries; never clear all
  active state because one seed is stale.
- Old ownerless persisted state must be rejected or conservatively quarantined,
  not assigned to an active seed.

Cover active/stale shared refs, mixed seeds, partial/completed restore, live
equivalence, and exact current round trips. Bump affected persisted identities.

## 4. Project episodes before every investigation budget

Episode investigation must obtain the authoritative active episode projection
before adjacency scan, `scan_limit`, output limit, inspected counts, window
construction, and truncation metadata. All-stale episodes consume zero budget
and cannot hide later active episodes.

## 5. Make policy migration explicit

For conservative legacy report migration, record source and target evidence
policy identities and an `evidence_policy_migration_required` blocker, or reject
the legacy report. Never emit a generic seed-binding-only reason for policy
incompatibility.

## Constraints And Verification

- Root Confirmation Task 2 only; no Task 3 expansion.
- Offline, passive, read-only; no Agent feedback or behavior changes.
- Retrieval rank/score remains navigation-only.
- Missing evidence produces `evidence_gap`/`inconclusive`, never a forced root.
- Preserve per-seed isolation and valid business semantics.
- No new dependency.
- Strict TDD with reviewer reproductions recorded as RED.
- Run focused findings, all affected attribution suites, full attribution
  suite, compileall, and `git diff --check 3e5d8b878..HEAD`.
- Commit only this wave and write
  `.superpowers/sdd/root-confirmation-task-2-fix19-report.md`.
