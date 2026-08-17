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
  CaseTrace.observation({
    source: "run",
    category: "resume_close",
    summary: "close resumed durability segment",
    data: { session_id: sessionID },
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
