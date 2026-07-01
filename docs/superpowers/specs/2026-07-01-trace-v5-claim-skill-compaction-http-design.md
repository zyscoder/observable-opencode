# Trace v5 Claim, Skill, Compaction, And HTTP Validation Design

Loop 4 v4.9 validation showed the trace contract is now useful for offline attribution, but several records still create avoidable noise or hide causal links:

- Final-answer claims include Markdown headings, list ordinals, table separators, and table headers.
- Explicit user requests such as `repo-audit skill` can be missed when the text is nested under prompt `parts`.
- Forced compaction records expose `algorithm` and before refs, but can leave `after_context_refs` empty and `summary_artifact_ref` pending.
- Subagent records show parent/child ids but do not explain why the child trace cannot be opened.
- Release validation was still executed with `opencode run`; future validation must use the same server/session/request path as benchmark execution.

## Goals

1. Make `response.claim` contain only attribution-worthy factual claims.
2. Record explicit skill requests even when the model never invokes a skill tool.
3. Make compaction summary/tail artifacts and post-compaction refs easy for offline analysis to consume.
4. Explain subagent child-trace availability.
5. Validate release binaries through `opencode serve` plus HTTP session requests.

## Design

### Claim Extraction

Claim extraction will filter non-factual Markdown structure before `response.claim` records are created:

- Markdown headings such as `## Summary` and bold-only headings such as `**总结：**`.
- Pure ordinals such as `1.` and `6.`.
- Markdown table separator rows such as `|---|---|`.
- Markdown table header rows such as `| 项目 | 结果 |` when they contain no factual signal.
- Generic section labels such as `使用的上下文资料`, `资料交叉比对`, `一致性结论`, and `需求影响分析报告`.

Table rows with factual values are still allowed, but header/separator scaffolding is not.

### Skill Request Provenance

`collectTextCandidates` will recurse through prompt `parts`, `input`, `messages`, `prompt`, and `body` fields so explicit skill requests embedded in API payloads are visible. When a requested skill is not found in the available model context, trace will emit a `skill.load` record with:

- `request_status: "missing"`
- `request_source: "user_prompt"`
- `available_skill_names`
- `quality_flags: ["skill_request_unresolved"]`

If availability cannot be established yet, the request remains observable instead of disappearing.

### Compaction Provenance

`context.compaction` will always make `output_summary` retrievable from an artifact when present, even if the summary text is small enough to fit inline. It will also populate `after_context_refs` from explicit metadata and from `auto_continue_prompt_ref` when no stronger post-compaction context ref exists.

### Subagent Trace Availability

Subagent trace refs will keep `child_trace_available`, and when false add `child_trace_unavailable_reason: "child_trace_file_not_found"`. This does not claim diagnosis; it only records why the UI cannot open nested trace content.

### HTTP Validation

Release validation must start a headless server and interact through HTTP:

1. Start `opencode serve --hostname 127.0.0.1 --port 0` with isolated `HOME` and XDG directories.
2. Parse the listening URL from stdout.
3. `POST /session?directory=<synthetic-repo>` to create a session.
4. `POST /session/:id/message?directory=<synthetic-repo>` with `{ model, agent, parts }`.
5. Inspect the generated trace files.

The shell is only used to start the server process and send HTTP requests; case execution itself happens through server session APIs.

## Acceptance Criteria

- `trace_version` is advanced to `5.0`.
- No response claims are emitted for Markdown headings, pure ordinals, table separators, or table header scaffolding.
- A prompt containing `repo-audit skill` creates a `skill.load` record even if no skill tool is called.
- Forced compaction records include `summary_artifact_ref` and a non-empty post-compaction ref when an auto-continue prompt exists.
- Subagent records explain unavailable child traces.
- Release validation uses HTTP server/session/message requests, not `opencode run`.
