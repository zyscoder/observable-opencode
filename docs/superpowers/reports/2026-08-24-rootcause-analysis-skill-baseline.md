# Root-Cause Analysis Skill Historical RED Baseline

This report preserves pre-Skill observations made before the canonical Skill and
seven-case bundle builder existed. It is historical and unscored, not a current
execution recipe.

## Observed Result

The `known-root` and `ambiguous` attempts were both blocked before inference:

| Case | Exit status | Observed condition | Model result |
| --- | ---: | --- | --- |
| `known-root` | 1 | `Not logged in - Please run /login` | None |
| `ambiguous` | 1 | `Not logged in - Please run /login` | None |

No baseline behavior or rubric score was inferred or fabricated.

## Fixture Facts

- All seven finalized fixture Traces use Causal IR and remain evaluator inputs.
- The known-root Artifact digest matches its declared SHA-256.
- Hidden expected outcomes and the rubric are evaluator-only data.
- The canonical Skill was absent during these historical attempts.

## Current Boundary

Current validation uses `prepare_isolated_bundle.py` to emit opaque post-Skill
handoffs. The repository does not execute or score them. A trusted benchmark
Harness or isolated execution service must run each bundle, while only the
external evaluator reads fixture mappings, provenance, expected outcomes, and
the rubric.
