# Observable OpenCode README and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a complete repository guide and the current Observable OpenCode plus offline attribution implementation.

**Architecture:** Add an Observable OpenCode guide before the preserved upstream README. Publish only source-controlled implementation material, then use the repository's existing manual release workflow to build Linux and macOS binaries.

**Tech Stack:** Markdown, Bun/TypeScript, Python 3, Git, GitHub CLI, GitHub Actions.

## Global Constraints

- Do not commit `.benchmark-runs`, `__pycache__`, bytecode, judge caches, local environment files, or credentials.
- CLI examples must work outside the repository by using an absolute `PYTHONPATH`.
- Trace collection and attribution must remain passive and read-only with respect to Agent behavior.
- Release assets must be produced by `.github/workflows/release-observable-linux.yml`.

---

### Task 1: Root README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: current release asset names, Trace environment variables, HTTP API, and attribution API.
- Produces: the repository's primary installation and operation guide.

- [ ] Add the Observable OpenCode overview and architecture.
- [ ] Document release installation, checksum verification, and binary replacement.
- [ ] Document DeepSeek configuration and HTTP server execution.
- [ ] Document Trace files, terminal receipts, signal behavior, and viewer use.
- [ ] Document attribution CLI and Python API from an arbitrary directory.
- [ ] Preserve the upstream README below a clear separator.

### Task 2: Publication Audit

**Files:**
- Modify: `.gitignore`
- Inspect: all modified and untracked files

**Interfaces:**
- Consumes: dirty shared worktree.
- Produces: a reviewed staged set containing implementation material only.

- [ ] Ignore local benchmark runs and Python caches.
- [ ] Scan candidate files for credentials and generated artifacts.
- [ ] Stage tracked implementation changes and required untracked source/docs/tests.
- [ ] Review `git diff --cached --stat` and `git diff --cached --name-only`.

### Task 3: Verification and Publication

**Files:**
- Test: `packages/opencode/test/observability/*.test.ts`
- Test: `tools/trace_attribution/tests`
- Inspect: `.github/workflows/release-observable-linux.yml`

**Interfaces:**
- Consumes: the reviewed staged source tree.
- Produces: a pushed branch and a running GitHub release workflow.

- [ ] Run focused observability tests and TypeScript typecheck.
- [ ] Run the full attribution test suite and public API smoke test.
- [ ] Run `git diff --check` and the final credential scan.
- [ ] Commit and push `codex/message-context-lineage`.
- [ ] Determine the next observable tag and dispatch `release-observable-linux.yml`.
- [ ] Confirm the workflow URL and initial job status.

