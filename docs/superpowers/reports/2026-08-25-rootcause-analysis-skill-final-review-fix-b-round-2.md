# Root Cause Analysis Skill Final Review Fix B Round 2

Date: 2026-08-25

Branch: `codex/trace-stability-integration`

## Security Trust Roots

Scored execution now has three independent trust checks before a case can run:

1. The container image is mandatory and must be an immutable lowercase
   `name@sha256:<64hex>` reference. Mutable defaults, option-like names, and
   control characters are rejected.
2. The external OpenCode release must match the evaluator-supplied SHA-256 and
   contain a complete ELF64 little-endian executable header for x86_64 or
   aarch64. The validated architecture determines `linux/amd64` or
   `linux/arm64`.
3. A provider-secret-free container preflight forces
   `--entrypoint /opt/opencode`, runs `--version`, and requires a self-exit code
   of zero plus plausible version output. Its host audit contains only release
   digest, architecture/platform, pinned image, version, and return code.

The canary/probe forces `--entrypoint /bin/sh`; the Agent command forces
`--entrypoint /opt/opencode`. Image defaults and image-provided entrypoints are
not trusted.

## Mount And Secret Boundaries

Workspace and binary mounts must be absolute, strict resolved paths of the
expected kind. Commas, control characters, newlines, symlinks, relative paths,
and noncanonical aliases are rejected before command construction.

`OPENCODE_CONFIG_CONTENT` is parsed recursively. Sensitive normalized keys
such as apiKey, token, password, secret, authorization, and credential mark
their leaf values as secret. Those values, explicit API keys, provider secret
environment values, and the full config are expanded to raw, JSON-escaped,
slash-escaped, URL percent/plus-encoded, and layered serialized variants.
stdout, stderr, wrapper errors, and final JSONL are redacted. Scored mode fails
closed on malformed config; smoke mode treats the complete malformed value and
its encodings as secret. Child inputs remain unchanged.

## Integration Status

The current host still cannot access the Docker daemon socket and has no
Podman. The scored container integration therefore remains BLOCKED. Structural
and adversarial refusal tests validate command construction and fail-closed
behavior but are not reported as container integration success.

An explicit scored CLI attempt used a synthetic, structurally valid ELF64
x86_64 header whose expected SHA-256 was supplied independently to the runner,
plus a syntactically digest-pinned test image. It exited with code `2` before
creating the requested batch root and printed:

```text
error: Scored filesystem sandbox runtime is unavailable: permission denied while trying to connect to the docker API at unix:///Users/zys/.colima/observable-benchmark/docker.sock
```

This is a recorded prerequisite refusal, not an Agent run and not a scored
integration pass. No daemon wait or container installation was attempted.

## Deterministic Verification

- `python3 -m unittest discover -s tools/rootcause_skill_tests -p 'test_*.py' -v`
  discovered 114 tests: 113 passed and one integration test was explicitly
  skipped with the same inaccessible-Docker-daemon BLOCKED reason.
- `python3 -m py_compile` succeeded for `trace_query.py`,
  `prepare_isolated_bundle.py`, and `run_isolated_opencode.py` with
  `PYTHONPYCACHEPREFIX` directed to `/tmp`.
- The official Skill validator reported `Skill is valid!`.
- All seven opaque bundles were rebuilt under
  `/tmp/rootcause-fix-b-round2-all7-20260825-01`; the audit reported seven
  opaque cases, zero hidden-data leaks, and zero symlinks.
- `git diff --exit-code -- tools/rootcause_skill_tests/fixtures` and
  `git diff --check` both succeeded.

These checks establish deterministic contract and isolation behavior only.
They do not replace a scored container execution once a trusted daemon,
digest-pinned image, and hash-verified release binary are available.
