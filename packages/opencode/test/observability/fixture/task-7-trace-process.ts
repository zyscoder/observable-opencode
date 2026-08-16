import fs from "node:fs"
import path from "node:path"
import { CaseTrace } from "../../../src/observability/case-trace"

const mode = process.argv[2]
const sessionID = process.env.TASK7_TRACE_SESSION_ID ?? "ses_task_7"
const trace = CaseTrace.configure({
  caseID: process.env.OPENCODE_CASE_ID,
  sessionID,
}) as any
CaseTrace.setSessionID(sessionID)

if (mode === "hold") {
  CaseTrace.observation({
    source: "tool",
    category: "sigkill_ready",
    summary: "durable evidence before SIGKILL",
    data: { payload: `killed-segment:${"x".repeat(16 * 1024)}` },
  })
  fs.writeFileSync(
    process.env.TASK7_TRACE_READY_FILE!,
    JSON.stringify({
      logicalRoot: path.dirname(path.dirname(trace.caseDir)),
      segmentDir: trace.caseDir,
      recordsFile: trace.recordsFile,
    }),
  )
  setInterval(() => {}, 1000)
} else if (mode === "close") {
  const compaction = CaseTrace.compaction({
    trigger: "overflow",
    provider_id: "fixture",
    model_id: "task-7",
    session_id: sessionID,
    message_id: "msg_task_7_compaction",
    input_tokens: 32_000,
    context_limit: 24_000,
    selected_head_messages: 4,
    selected_tail_messages: 2,
    hidden_compaction_messages: 1,
    previous_summary: `previous:${"p".repeat(32 * 1024)}`,
    serialized_tail: `tail:${"t".repeat(16 * 1024)}`,
    output_summary: `retained requirement:${"s".repeat(32 * 1024)}`,
    auto_continue: true,
    result: "continue",
    context_ledger: {
      algorithm: "head-tail-summary",
      algorithm_version: "head-tail-summary/v1",
      token_estimate_before: 32_000,
      token_estimate_after: 4_000,
      retained_fact_refs: ["evidence:task_7_requirement"],
      dropped_fact_refs: [],
    },
  })
  CaseTrace.event({
    component: "processor",
    event_type: "tool.call",
    data: {
      sessionID,
      messageID: "msg_task_7_tool",
      callID: "call_task_7_tool",
      tool: "read",
      input: { filePath: "docs/requirement.md" },
    },
  })
  CaseTrace.event({
    component: "processor",
    event_type: "tool.result",
    data: {
      sessionID,
      messageID: "msg_task_7_tool",
      callID: "call_task_7_tool",
      tool: "read",
      output: "durable local requirement",
    },
  })
  for (const item of [
    { component: "skill" as const, operation: "load", name: "task-7-skill" },
    { component: "mcp" as const, operation: "tool.call", name: "local:repo_fact" },
    { component: "task" as const, operation: "delegate", name: "task-7-subagent" },
  ]) {
    CaseTrace.startSpan({
      ...item,
      input: {
        sessionID,
        messageID: `msg_${item.component}`,
        callID: `call_${item.component}`,
        childSessionID: item.component === "task" ? "ses_task_7_child" : undefined,
      },
    })?.end({
      status: "success",
      output: { result: `${item.component} deterministic result` },
    })
  }
  CaseTrace.agentLifecycle({
    session_id: sessionID,
    message_id: "msg_task_7_agent",
    agent: "build",
    phase: "response.completed",
    status: "success",
    summary: "Task 7 fixture completed",
  })
  CaseTrace.responseOutput({
    text: "Task 7 deterministic response",
    source_refs: compaction ? [`compaction:${compaction.node_id}`, "tool_result:call_task_7_tool"] : [],
    metadata: { sessionID, messageID: "msg_task_7_agent", is_final_for_case: true },
  })
  const request = CaseTrace.closeAll({
    status: "success",
    result: { reason: "task_7_fixture" },
  })[0]
  if (!request) throw new Error("Task 7 fixture did not produce a materialization request")
  fs.writeFileSync(process.env.TASK7_TRACE_REQUEST_FILE!, JSON.stringify(request))
} else {
  throw new Error(`unknown Task 7 trace fixture mode: ${mode}`)
}
