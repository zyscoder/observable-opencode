# Root-Cause Analysis Skill RED Baseline

## Baseline Setup

- Date: 2026-08-24
- Working directory for provider runs: a freshly created isolated bundle under `/private/tmp/rootcause-skill-red.XXXXXX`
- Skill state: `.claude/skills/rootcause-analysis` was absent.
- Invocation mode: `claude --bare --disable-slash-commands -p --permission-mode plan --allowedTools "Read,Bash(python3 *)"`.
- Prompt source: the bundle-local `prompt.md` rendered with its bundle-local `trace.json` path. Claude Code 2.1.138 treats `--allowedTools` as variadic and consumes a trailing positional prompt, so the prompt is supplied on stdin.

## Isolated Pressure-Run Procedure

Prepare one case without reading or copying `pressure/cases.json`:

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

Repeat with `--case ambiguous`. The bundle contains only `prompt.md`, the
selected `trace.json`, and Artifacts declared by that trace. It has no copy or
reference to evaluator-only cases, so the Agent receives no traversable path
to `pressure/cases.json` from the pressure-run working directory.

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

Authenticate Claude Code and rerun the same two isolated bundle prompts with
the Skill still absent. Record the unmodified raw model responses and evaluate
them outside the Agent context against `tools/rootcause_skill_tests/pressure/rubric.json`.
