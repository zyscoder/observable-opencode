import { CaseTrace } from "@/observability/case-trace"

export type WorkerTraceFinalizer = Pick<typeof CaseTrace, "finishAll">

export async function finalizeWorkerTraces(input: {
  failure?: unknown
  trace?: WorkerTraceFinalizer
} = {}): Promise<void> {
  const trace = input.trace ?? CaseTrace
  try {
    await trace.finishAll({
      status: input.failure ? "error" : "success",
      error: input.failure,
      result: { reason: "worker.shutdown" },
    })
  } catch {
    // Trace persistence is diagnostic only and must not affect worker shutdown.
  }
}
