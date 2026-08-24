# Root-Cause Analysis Skill RED Baseline

## Baseline Setup

- Date: 2026-08-24
- Working directory for provider runs: `/private/tmp/rootcause-skill-red.48YcZn`
- Skill state: `.claude/skills/rootcause-analysis` was absent.
- Invocation mode: `claude --bare --disable-slash-commands -p --permission-mode plan --allowedTools "Read,Bash(python3 *)"`.
- Prompt source: the rendered baseline prompt with the absolute finalized Trace path. Claude Code 2.1.138 treats `--allowedTools` as variadic and consumes a trailing positional prompt, so the brief's positional-prompt form failed locally before provider access with: `Error: Input must be provided either through stdin or as a prompt argument when using --print`. The equivalent prompt was therefore supplied on stdin without changing its content, isolation, permissions, or disabled-Skill configuration.

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

Authenticate Claude Code and rerun the same two rendered prompts from a temporary directory with the Skill still absent. Record the unmodified raw model responses and evaluate them against `tools/rootcause_skill_tests/pressure/rubric.json`.
