import { expect, test } from "bun:test"
import { CaseTrace } from "@/observability/case-trace"
import { finishInteractiveTraceSessions, finishReplacedTraceSession } from "@/cli/cmd/run/runtime"

test("finalizes a replaced interactive root independently from a later failed root", () => {
  const finished: Array<{ sessionID: string; input: Record<string, unknown> | undefined }> = []
  const all: Array<Record<string, unknown> | undefined> = []
  const originalFinishSession = CaseTrace.finishSession
  const originalFinishAll = CaseTrace.finishAll

  ;(CaseTrace as unknown as {
    finishSession: (sessionID: string, input?: Record<string, unknown>) => void
  }).finishSession = (sessionID, input) => {
    finished.push({ sessionID, input })
  }
  ;(CaseTrace as unknown as { finishAll: (input?: Record<string, unknown>) => void }).finishAll = (input) => {
    all.push(input)
  }

  try {
    finishReplacedTraceSession("ses_a")
    finishInteractiveTraceSessions({
      sessionID: "ses_b",
      error: new Error("B failed"),
    })
  } finally {
    ;(CaseTrace as unknown as { finishSession: typeof CaseTrace.finishSession }).finishSession = originalFinishSession
    ;(CaseTrace as unknown as { finishAll: typeof CaseTrace.finishAll }).finishAll = originalFinishAll
  }

  expect(finished).toEqual([
    {
      sessionID: "ses_a",
      input: {
        status: "success",
        result: { reason: "session.replaced" },
      },
    },
    {
      sessionID: "ses_b",
      input: expect.objectContaining({
        status: "error",
        error: expect.any(Error),
      }),
    },
  ])
  expect(all).toEqual([
    {
      status: "success",
      result: { reason: "interactive.runtime.closed" },
    },
  ])
})
