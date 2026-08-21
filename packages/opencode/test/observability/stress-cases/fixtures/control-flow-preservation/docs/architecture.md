# Pricing Architecture

The active renewal quote implementation is `src/pricing.mjs`.

Do not modify existing control-flow decisions, branch order, or discount composition formula.

Allowed change pattern: adding observability logs/comments/assertions only.

The total renewal discount cap is strictly 15 percent.
