import { CaseTrace, type TraceMaterializationRequest } from "@/observability/case-trace"

export type WorkerTraceFinalizer = Pick<typeof CaseTrace, "closeAll">

export type WorkerTraceCloseResult = {
  requests: TraceMaterializationRequest[]
  failure?: string
}

export async function finalizeWorkerTraces(input: {
  failure?: unknown
  trace?: WorkerTraceFinalizer
} = {}): Promise<WorkerTraceCloseResult> {
  const trace = input.trace ?? CaseTrace
  try {
    const requests = trace.closeAll({
      status: input.failure ? "error" : "success",
      error: input.failure,
      result: { reason: "worker.shutdown" },
    })
    return {
      requests,
      ...(input.failure ? { failure: input.failure instanceof Error ? input.failure.message : String(input.failure) } : {}),
    }
  } catch {
    // Trace persistence is diagnostic only and must not affect worker shutdown.
    return {
      requests: [],
      ...(input.failure ? { failure: input.failure instanceof Error ? input.failure.message : String(input.failure) } : {}),
    }
  }
}
