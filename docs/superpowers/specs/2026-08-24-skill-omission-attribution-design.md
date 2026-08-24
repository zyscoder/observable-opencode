# Skill Omission Attribution Design

## Goal

Enable a user to ask why an applicable Skill was not invoked and receive an
auditable answer that distinguishes catalog discovery, source conflict,
permission exposure, trigger quality, Agent selection, and action generation.

## Constraints

- Runtime instrumentation is passive and must not alter Skill discovery,
  precedence, permissions, prompt content, tool availability, or Agent output.
- Offline attribution never feeds conclusions back into OpenCode or the Agent.
- Existing Trace fields and consumers remain compatible.
- The design must generalize to expected-but-absent tools, Skills, MCP calls,
  Subagents, and verification actions; it must not hard-code feature migration.
- Full Skill bodies remain unavailable before invocation. Trigger analysis uses
  the frontmatter description that was actually exposed to the model.

## Runtime Facts

The Skill service retains a sorted audit projection alongside the existing
runtime catalog. Every candidate records its name, description, location,
source family, scope, parse status, and content hash. The projection records
same-name candidates and the catalog winner after the existing loader has
finished; it observes the current winner without changing overwrite behavior.

When a model request is assembled, OpenCode emits a session-bound
`skill.catalog.exposed` Causal IR node containing:

- all discovered candidates and parse failures;
- the selected runtime catalog entries;
- per-Agent permission decisions;
- the entries actually exposed in the system prompt;
- whether the `skill` tool was available;
- the message, Agent, step, provider, and model identities.

Existing `skill.load`/Tool spans remain the invocation authority. No runtime
component decides that an absent invocation is a defect.

## Offline Expected-Action Projection

The attribution module recognizes questions about missing Skill invocation and
builds a read-only `expected_action_projection/v1` from immutable Trace facts:

1. user request and model-message evidence;
2. `skill.catalog.exposed` entries and descriptions;
3. Skill invocation records within the same root session;
4. relevant reasoning and tool-selection decisions.

The projection performs a deterministic, auditable lexical ranking over the
question, recent user requests, Skill names, and exposed descriptions. That
ranking only narrows the expected Skill candidates; it is not a root-cause
verdict. The premise Judge confirms whether the request entails the trigger and
whether the invocation is absent. A supported omission is represented as an
offline `case.observed_defect` seed with explicit contract, lifecycle,
post-exposure execution-window, and absence references. Externalized catalog
arrays are read through the Trace artifact verifier instead of being copied
back into every record.

## Root-Cause Branches

The recursive search follows these ordered branches:

1. request meaning was missing from the model context;
2. Skill candidate was not discovered or failed parsing;
3. the intended candidate lost a same-name source conflict;
4. permissions or tool resolution prevented exposure;
5. the exposed description did not clearly encode the trigger;
6. the description and request matched, but Agent selection omitted the Skill;
7. reasoning planned the Skill but action generation emitted no tool call;
8. the Skill was invoked but execution or result consumption failed.

Skill-oriented questions reserve relevant lifecycle facts before generic
semantic candidates. If premise evidence is insufficient, analysis terminates
as `inconclusive`; it must not traverse unrelated implementation decisions.

## Reporting

Structured output names the expected Skill action, observed invocation state,
catalog/exposure evidence, first deviation stage, confirmed root or evidence
gap, and remediation owner. Markdown must mirror the structured outcome:
`no_defect` and `inconclusive` reports cannot claim that a deviation occurred.

## Verification

Regression fixtures cover:

- applicable Skill exposed and invoked;
- applicable Skill exposed but omitted;
- candidate missing or parse-failed;
- same-name source conflict;
- permission-filtered Skill;
- trigger only present in the inaccessible Skill body;
- model planned a Skill but emitted no call;
- unrelated successful Skill and non-Skill questions as negative controls.
