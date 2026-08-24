# Root-Cause Analysis Skill Historical RED Baseline

This report preserves the pre-Skill authentication attempts made before the
canonical Skill and seven-case isolation builder existed. It is historical
evidence, not a command recipe for the current tree. The current
`prepare_isolated_bundle.py` intentionally includes the canonical Skill and is
used for scored post-Skill runs; it cannot reproduce a Skill-absent RED run.

## Baseline Setup

- Date: 2026-08-24
- Working directory for provider runs: the then-current two-case bundle under `/private/tmp/rootcause-skill-red.XXXXXX`
- Skill state: `.claude/skills/rootcause-analysis` was absent.
- Invocation mode: `claude --bare --disable-slash-commands -p --permission-mode plan --allowedTools "Read,Bash(python3 *)"`.
- Prompt source: the bundle-local `prompt.md` rendered with its bundle-local `trace.json` path. Claude Code 2.1.138 treats `--allowedTools` as variadic and consumes a trailing positional prompt, so the prompt is supplied on stdin.

## Historical Pressure-Run Procedure

The original attempt used the command below. Do not rerun it against the current
builder to claim a RED baseline because the current builder copies the Skill:

```bash
BUNDLE_ROOT=$(mktemp -d /private/tmp/rootcause-skill-red.XXXXXX)
BUNDLE="$BUNDLE_ROOT/known-root"
python3 tools/rootcause_skill_tests/pressure/prepare_isolated_bundle.py \
  --case known-root --destination "$BUNDLE"
cd "$BUNDLE"
claude --bare --disable-slash-commands -p \
  --permission-mode plan \
  --allowedTools "Read,Bash(python3 *)" < prompt.md
```

The historical command was repeated with `--case ambiguous`. At that time the
bundle contained only `prompt.md`, the selected `trace.json`, and declared
Artifacts. The current scored protocol instead builds all seven cases with the
canonical Skill and runtime-specific prompts while preserving evaluator-data
isolation.

## Forward-Test Result

Both RED baseline prerequisites were blocked by Claude authentication. No model response was received, so no baseline behavior has been inferred or fabricated.

| Case | Exit status | Exact provider condition | Model result |
| --- | ---: | --- | --- |
| known-root | 1 | `Not logged in · Please run /login` | None; blocked before analysis. |
| ambiguous | 1 | `Not logged in · Please run /login` | None; blocked before analysis. |

Neither command ran for five minutes or required timeout termination.

## Fixture Verification

- All seven `trace.json` files parse as JSON and contain `causal_ir_version`, `manifest`, `nodes`, `edges`, `artifacts`, `records`, and `dataflow_edges`.
- The known-root fixture has six nodes, four attribution-eligible edges, and one explicitly ineligible temporal-advisory edge.
- The known-root Artifact digest matches its required filename and declared `sha256:` digest: `3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1`.
- The baseline prompts do not contain evaluator-only outcome fields.
- The future Skill package was absent throughout these baseline attempts.

## Follow-Up

For current validation, build all seven post-Skill workspaces with
`prepare_isolated_bundle.py`, execute Claude/OpenCode with each workspace as
cwd, capture outputs outside that workspace, and let only the evaluator read
`tools/rootcause_skill_tests/pressure/cases.json` and `rubric.json`. Do not
reinterpret a current run as the historical RED baseline.
