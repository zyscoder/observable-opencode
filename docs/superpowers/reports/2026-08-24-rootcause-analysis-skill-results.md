# Root Cause Analysis Skill Validation Results

Date: 2026-08-25

Branch: `codex/trace-stability-integration`

## Executive Result

The repository validates the Skill, deterministic Trace queries, fixture
contracts, and seven opaque evaluation handoffs. It does not implement scored
Provider-backed execution and therefore reports no in-repository model score.

`prepare_isolated_bundle.py` is the only root-cause evaluation helper. Each
handoff contains one finalized derived Trace, digest-verified declared Artifacts,
the canonical Skill, and runtime-specific prompts. It excludes evaluator expected
outcomes, rubric, semantic fixture identity, sibling fixtures, reports, Git history,
and repository symlinks. Source fixture mapping and source/derived digest
provenance are emitted evaluator-side, outside the bundle.

## Execution Boundary

Scored bundles must be submitted to an existing **trusted benchmark Harness** or
isolated execution service. That external system independently manages
credentials, filesystem and network isolation, runtime cleanup, execution-image
attestation, process lifecycle, and audit storage. This repository does not
implement, verify, or claim those controls.

Local or repository-source Agent attempts are unscored smoke only. Root-cause
test helpers neither start an Agent nor accept Provider credentials. Only the
external evaluator may read `pressure/cases.json`, fixture-to-opaque provenance,
hidden expected outcomes, and `pressure/rubric.json` after execution.

## Historical Attempts

The preserved historical Claude attempt stopped at `Not logged in - Please run
/login`. Seven historical OpenCode source attempts reported `no providers found`
before model inference. They produced no root-cause reports and have no rubric
score. See the historical raw-attempt appendix; it is superseded and unscored.

## Deterministic Verification

The scope-correction fix round 1 run discovered and passed 109 tests with no skips. Python
compilation and the official Skill validator passed; the validator reported
`Skill is valid!`. A fresh seven-bundle audit under
`/tmp/rootcause-scope-fix-r1-0jhkc8cs` found zero hidden-answer leaks, zero
symlinks, opaque identity in all seven workspaces, and evaluator provenance only
outside those workspaces. Every derived Trace passed authoritative validation and
every copied Artifact matched its recorded `content_sha256`. Fixtures remained
unchanged and the diff checks passed.

The maintained verification protocol is:

```bash
python3 -m unittest discover -s tools/rootcause_skill_tests -p 'test_*.py' -v

PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache \
python3 -m py_compile \
  .claude/skills/rootcause-analysis/scripts/trace_query.py \
  tools/rootcause_skill_tests/pressure/prepare_isolated_bundle.py

UV_CACHE_DIR=/tmp/observable-opencode-uv-cache \
uv run --offline --with pyyaml \
  python /Users/zys/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .claude/skills/rootcause-analysis

git diff --exit-code -- tools/rootcause_skill_tests/fixtures
git diff --check
```

All seven cases must also be rebuilt with fresh opaque IDs and audited for:

- valid canonical source and derived node hashes;
- exact user question with no hidden expected data;
- no semantic fixture slug in Agent-visible paths or identity fields;
- no symlink or path escape;
- exact allowlisted files only;
- evaluator mapping and provenance stored outside every bundle.

## Remaining External Validation

Actual model quality remains to be measured by the trusted benchmark Harness.
The external evaluator should compare Agent JSON and Markdown against the hidden
rubric, verify evidence references, distinguish root introduction from
propagation, require `ambiguous` to remain inconclusive, and detect overfitting
across all seven cases.

No proposed change was applied to the analyzed system; all recommendations
require separate review and execution.
