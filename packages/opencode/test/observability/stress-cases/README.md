# Trace Root-Cause Stress Cases

This suite creates intentionally difficult synthetic development cases and checks whether the generated trace contains enough semantic evidence for offline root-cause attribution.

The trace runtime must not diagnose root cause. The review scripts compare trace evidence against case metadata and report whether the trace is sufficient, partially sufficient, or insufficient.

## Local metadata test

```bash
node --test packages/opencode/test/observability/stress-cases/stress-cases.test.mjs
```

## Dry run

```bash
node packages/opencode/test/observability/stress-cases/run-stress-cases.mjs --dry-run
```

## Real HTTP run

```bash
DEEPSEEK_API_KEY=... node packages/opencode/test/observability/stress-cases/run-stress-cases.mjs \
  --binary /tmp/observable-opencode-v52-run/bin/opencode-observable-darwin-arm64 \
  --out /tmp/observable-opencode-stress-run
```

Use `--case <case_id>` to run a subset. The runner starts `opencode serve`, creates a session through HTTP, sends the case prompt through HTTP, stops the server, and writes trace sufficiency reviews.
