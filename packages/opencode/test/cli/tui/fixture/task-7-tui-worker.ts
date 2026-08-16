import { finalizeWorkerTraces } from "../../../../src/cli/cmd/tui/worker-trace"
import { CaseTrace } from "../../../../src/observability/case-trace"

const channel = globalThis as unknown as {
  onmessage: (event: MessageEvent<{ id: number; method: "shutdown" | "closeTraces" }>) => void
  postMessage: (message: unknown) => void
}
const sessionID = `ses_task_7_tui_${process.env.TASK7_TUI_MODE}`

CaseTrace.configure({ caseID: process.env.OPENCODE_CASE_ID, sessionID })
CaseTrace.setSessionID(sessionID)
CaseTrace.node({
  node_id: `task_7_tui_${process.env.TASK7_TUI_MODE}`,
  kind: "execution.observation",
  component: "runtime",
  title: "Task 7 TUI lifecycle marker",
  data: { marker: process.env.TASK7_TUI_MODE },
})

channel.onmessage = async (event) => {
  const value =
    event.data.method === "shutdown"
      ? {}
      : await finalizeWorkerTraces()
  channel.postMessage({ id: event.data.id, value })
}
channel.postMessage({ ready: true })
