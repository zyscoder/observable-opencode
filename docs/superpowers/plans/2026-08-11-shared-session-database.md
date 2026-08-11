# Observable OpenCode Shared Session Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Build Observable OpenCode release binaries so they use the same `opencode.db` session database as formal OpenCode installations.

**Architecture:** Keep channel-specific databases for local development and preview channels. Set the Observable Release build identity to the stable `latest` channel and inject the release tag as its version, which makes the existing database path logic resolve to `opencode.db` without changing runtime session behavior. Document explicit database-path diagnostics and a safe compatibility override.

**Tech Stack:** GitHub Actions, Bun build-time defines, TypeScript, Bun Test, Markdown.

## Global Constraints

- Do not change session schema, session IDs, or Trace semantics.
- Do not make unrelated local/preview builds share the formal database.
- Do not include user API keys or local database contents in the repository.
- Existing untracked evaluation reports remain untouched.

### Task 1: Release build identity

**Files:**
- Modify: `.github/workflows/release-observable-linux.yml`

- [ ] Add a setup step before typecheck/build that derives `OPENCODE_VERSION` from the pushed tag and exports `OPENCODE_CHANNEL=latest` through `GITHUB_ENV`.
- [ ] Keep the existing tag-triggered build and asset publishing steps unchanged.
- [ ] Verify the workflow expressions support both tag pushes and `workflow_dispatch` tag input.

### Task 2: Database-path regression coverage

**Files:**
- Modify: `packages/opencode/test/storage/db.test.ts`

- [ ] Add a test asserting `OPENCODE_DISABLE_CHANNEL_DB` selects `Global.Path.data/opencode.db` for a non-stable channel, restoring the process flag after the assertion.
- [ ] Keep the existing current-channel test unchanged so preview/local isolation remains covered.
- [ ] Run only the storage database test first and verify both behaviors.

### Task 3: User-facing migration and diagnostics

**Files:**
- Modify: `README.md`

- [ ] Explain that new Observable Release binaries use `opencode.db` and can resume sessions created by formal OpenCode.
- [ ] Add `opencode db path` and `observable-opencode db path` comparison commands.
- [ ] Document the temporary `OPENCODE_DB=/absolute/path/to/opencode.db` override and require closing the other OpenCode process before sharing a live database.
- [ ] State that existing branch-built Observable binaries keep their old channel database and must use the override or a new release.

### Task 4: Verification and release

- [ ] Run `bun --cwd packages/opencode test test/storage/db.test.ts`.
- [ ] Run the relevant package typecheck and `git diff --check`.
- [ ] Commit only the workflow, test, README, and this plan; preserve unrelated untracked reports.
- [ ] Push the branch and create the next `v1.14.48-observable.*` tag so the release workflow builds the corrected binaries.
