# Process-Defect Navigation Design

## Problem

The Sphinx FeatureBench run retained the manually relevant decisions in the
Trace and in the attribution candidate pool, but the offline analyzer did not
judge them reliably:

- strict candidate-to-seed paths demoted pathless authored decisions to
  evidence context;
- progress navigation inspected only two candidates from an already truncated
  top-eight list;
- the local Judge compared a planning node directly with the baseline
  `AttributeError`, so it treated repair planning and non-repair as clean.

## Safety boundary

Offline navigation is retrieval, not causal evidence. A pathless decision may
be queued for an independent node judgment, but navigation alone must not:

- set `candidate_introduction=true`;
- create a confirmed causal path;
- publish a root or factor;
- alter the observed Agent run or feed information back to the Agent.

## Design

1. Progress navigation consumes the complete bounded retrieval page (24
   candidates) instead of the regular causal-step page (8 candidates).
2. At most 24 authored, active-revision, root-eligible concrete nodes are
   queued. The existing hypothesis and Judge budgets remain authoritative.
3. Each queued node receives a transformed candidate-local defect state. The
   Judge must decide whether the node contains an erroneous plan, priority,
   action choice, false commitment, or process omission that could transform
   into the downstream functional failure.
4. The prompt explicitly separates baseline functional defects from
   candidate-local process defects. A node may be locally defective without
   having created the pre-existing functional gap.
5. Independent root confirmation remains mandatory. Navigation score and
   offline edges stay non-attributable.

## Acceptance

- A navigation aggregate with more than eight authored candidates queues the
  later candidates within the bounded page.
- The candidate-local defect state and prompt state the baseline/process
  distinction.
- Existing signal, lifecycle, outcome, and direct-causal-path protections keep
  passing.
- On the neutral Sphinx Bundle, the analyzer judges `dec47`, `dec167`,
  `dec236`, and `dec238` without adding label-derived evidence.
