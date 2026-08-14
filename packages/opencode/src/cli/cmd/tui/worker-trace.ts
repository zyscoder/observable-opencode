import { CaseTrace, type TraceMaterializationRequest } from "@/observability/case-trace"

export type WorkerTraceFinalizer = Pick<typeof CaseTrace, "closeAll">

export async function finalizeWorkerTraces(input: {
  failure?: unknown
  trace?: WorkerTraceFinalizer
} = {}): Promise<TraceMaterializationRequest[]> {
  const trace = input.trace ?? CaseTrace
  try {
    return trace.closeAll({
      status: input.failure ? "error" : "success",
      error: input.failure,
      result: { reason: "worker.shutdown" },
    })
  } catch {
    // Trace persistence is diagnostic only and must not affect worker shutdown.
    return []
  }
}
