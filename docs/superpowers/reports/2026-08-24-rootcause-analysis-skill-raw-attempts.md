# Root-Cause Analysis Skill Historical Attempts

Date: 2026-08-24

This compact appendix preserves the outcome facts from early forward attempts.
It is historical, **unscored**, and **superseded** by the bundle-only handoff
protocol. It is not a current command recipe or an execution-security claim.

## Claude Code Attempt

- Invocation used the runtime's `/rootcause-analysis` form.
- The process exited with status `1`.
- stdout reported `Not logged in - Please run /login`.
- stderr was empty.
- No model request or Skill analysis occurred.

## OpenCode Source Attempts

Seven fixture prompts were submitted through the then-current repository source
build. Every attempt reached `Provider.defaultModel` and reported
`error=no providers found` before model inference. None produced a root-cause
report suitable for rubric evaluation.

The legacy records used an eight-second timeout and requested process signals
after timeout. They did not independently establish signal delivery or causation,
so they do not establish an OpenCode self-exit code or termination cause.

## Interpretation

These observations only establish unavailable runtime prerequisites in the old
local setup. They do not measure Skill quality, filesystem isolation, execution
safety, or scored behavior. No rubric result is inferred from them.

Current evaluation begins by producing opaque bundles with
`prepare_isolated_bundle.py`. A trusted benchmark Harness outside this repository
must execute those bundles and independently manage its trust boundary.
