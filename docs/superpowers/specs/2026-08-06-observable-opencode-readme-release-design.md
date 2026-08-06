# Observable OpenCode README and Release Design

## Goal

Make the GitHub repository immediately usable as the distribution point for
Observable OpenCode and its offline causal-attribution module.

## README Structure

The root `README.md` will start with a Chinese Observable OpenCode guide and
retain the upstream OpenCode README below a clear separator. The observable
guide will cover:

- project positioning and the passive, read-only observability boundary;
- the Trace-to-attribution architecture and output artifacts;
- GitHub Release binary selection, checksum verification, installation, and
  replacement of an existing `opencode` binary;
- DeepSeek configuration without embedding credentials;
- `opencode serve` plus HTTP session/request execution;
- Trace environment variables, terminal receipts, signal semantics, and
  directory layout;
- trace viewer usage;
- attribution CLI usage from any working directory via an absolute
  `PYTHONPATH`;
- the equivalent Python API and question-bound output fields;
- source-development and verification commands.

The guide will not claim that the attribution package is globally installed.
Its commands will work from arbitrary directories by referring to the absolute
checkout path.

## Publication Scope

Publish the current `codex/message-context-lineage` branch to
`zyscoder/observable-opencode`. Include current source, tests, required fixtures,
scripts, specifications, plans, reports, and the root README. Exclude runtime
benchmark results, Python bytecode/cache directories, generated judge caches,
local environment files, and credentials.

The existing `release observable` workflow remains the release authority. It
will be triggered with the next available observable tag and must build Linux
and macOS CLI assets plus `SHA256SUMS`.

## Safety and Verification

- Trace collection remains passive and does not alter agent-visible inputs or
  outputs.
- Attribution remains offline relative to the observed Agent and never writes
  findings back to the Agent or input Trace.
- README commands must match actual CLI flags, environment variables, workflow
  asset names, and HTTP endpoints.
- Run observability tests, attribution tests, TypeScript typecheck, README link
  and command checks, `git diff --check`, and a credential scan before commit.
- Inspect the staged file list before publishing.

