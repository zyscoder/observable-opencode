# Trace v5.1 Claim, Inline Subagent, Lifecycle, And Compaction Design

Loop 5 v5.0 HTTP validation proved the trace path now follows the real benchmark execution style:

`opencode serve -> POST /session -> POST /session/:id/message`

The traces are useful, but these quality issues still reduce offline attribution value:

- `response.claim` still contains Markdown section scaffolding and table layout rows.
- Table rows that do contain facts remain raw Markdown strings instead of canonical comparison facts.
- Subagent internals can be present in the same trace, but `subagent.call` still reports `child_trace_file_not_found`, which is misleading.
- Forced compaction keeps summary and after refs, but token estimates can remain missing even when a nearby compaction check has usable estimates.
- `run.start` and `agent.lifecycle` records can be finalized as unexpected open records even when the trace is otherwise complete.
- Large payloads can be deduped as artifacts, but field summaries do not expose enough provenance for offline grouping.

## Goals

1. Make response claims attribution-ready by removing non-factual sections and canonicalizing factual table rows.
2. Represent subagent traces as inline child sessions when their records are already in the same trace.
3. Reduce lifecycle health noise for records that are expected to remain open until trace finalization.
4. Reuse compaction-check token estimates to fill forced compaction before/after estimate fields when possible.
5. Expose payload artifact refs and dedupe group ids directly on large field summaries.
6. Keep `trace.html` as the only HTML entry point and keep large text in artifacts.

## Non-Goals

- Do not implement root-cause attribution or diagnosis.
- Do not delete legacy trace files or old compatibility fields.
- Do not create a separate viewer HTML.
- Do not require benchmark runners to change their HTTP session/message flow.

## Design

### Claim Canonicalization

`response.claim` generation will continue to run only for final user-visible answers. Before records are emitted, the extractor will classify each candidate as one of:

- `section_scaffold`: headings and labels such as `各来源的关键结论对比`, `子 Agent 独立总结`, `是否需要改动`, and `使用的上下文资料`.
- `table_scaffold`: Markdown table header or separator rows.
- `table_fact`: Markdown table rows with concrete values, such as owner, discount cap, implementation entry, or test result rows.
- `factual_claim`: ordinary sentence/list factual claims.

Scaffold candidates are dropped. `table_fact` candidates are preserved but normalized into:

- `claim_format: "table_fact"`
- `raw_text`
- `canonical_text`
- `table_cells`
- `table_subject`
- `table_values`

This lets offline attribution consume factual values without re-parsing Markdown while the HTML can still show the original row.

### Inline Subagent Provenance

When a `subagent.call` has a `child_session_id`, finalization will scan the same trace for records that reference that child session. If found, the record will be enriched with:

- `child_trace_available: true`
- `child_trace_mode: "inline_same_trace"`
- `child_record_count`
- `child_record_refs`
- `child_prompt_refs`
- `child_result_refs`

Only when no inline records and no child trace directory exist should the trace say `child_trace_unavailable_reason: "child_trace_file_not_found"`.

### Lifecycle Close Semantics

`run.start` and `agent.lifecycle` records are long-lived semantic markers. If they are still running when the process finishes, finalization should close them as expected lifecycle records rather than unexpected missing-close records. The health metric should still expose how many records were finalized, but it should not label expected long-lived markers as unexpected warnings.

### Compaction Estimate Backfill

For each `context.compaction`, finalization will find the nearest earlier `context.compaction_check` for the same session/message when direct token estimates are missing. It will backfill:

- `token_estimate_before`
- `token_estimate_after` when a defensible value is available
- `estimate_source: "nearest_compaction_check"`

If no estimate source exists, `missing_token_estimate` remains honest.

### Viewer

`trace.html` should make inline subagent mode visible in the Subagents section and show canonical claim fields in the Claim Evidence Matrix. It must not introduce `viewer.html`.

### Payload Provenance

Large field summaries that spill into artifacts will keep the existing `artifact_id` and also expose `payload_ref` and `payload_dedupe_group_id`. Offline attribution can use these fields to collapse repeated model request/context payloads without comparing previews or hashes manually.

## Acceptance Criteria

- `trace_version` advances to `"5.1"`.
- Section scaffolding and table header/separator rows are not emitted as `response.claim`.
- Factual Markdown table rows produce `claim_format: "table_fact"` and include canonical table fields.
- Subagent records with child session records in the same trace use `child_trace_mode: "inline_same_trace"` instead of `child_trace_file_not_found`.
- Expected finalization of `run.start` and `agent.lifecycle` does not count as `unexpected_missing_close_records`.
- Forced compaction can reuse nearby compaction-check token estimates and records `estimate_source`.
- Large field summaries include `payload_ref` and `payload_dedupe_group_id` when an artifact is written.
- `trace.html` remains the only HTML file.
