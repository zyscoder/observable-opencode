import { Installation } from "@/installation"
import { type TraceMaterializationRequest } from "@/observability/case-trace"
import { collectMaterializedTracePublication, type TracePublication } from "@/observability/trace-publication"
import path from "node:path"

export type TraceMaterializerOptions = {
  command?: string[]
  env?: NodeJS.ProcessEnv | Record<string, string | undefined>
  onWarning?: (warning: string) => unknown
}

function traceMaterializerEnv(env: NodeJS.ProcessEnv | Record<string, string | undefined>) {
  return Object.fromEntries(
    Object.entries(env).filter(
      (entry): entry is [string, string] => entry[1] !== undefined && entry[0].startsWith("OPENCODE_CASE_TRACE"),
    ),
  )
}

function traceMaterializerCommand(options: TraceMaterializerOptions) {
  if (options.command) return options.command
  if (Installation.isLocal() && process.argv[1]) return [process.execPath, process.argv[1]]
  return [process.execPath]
}

function warn(options: TraceMaterializerOptions, message: string) {
  try {
    options.onWarning?.(message)
  } catch {
    // Trace diagnostics must not affect the original TUI outcome.
  }
}

export async function materializeWorkerTraces(
  requests: TraceMaterializationRequest[],
  options: TraceMaterializerOptions = {},
): Promise<TracePublication[]> {
  const unique = requests.filter(
    (request, index) =>
      requests.findIndex((candidate) => path.resolve(candidate.caseDir) === path.resolve(request.caseDir)) === index,
  )
  const command = traceMaterializerCommand(options)
  const env = traceMaterializerEnv(options.env ?? process.env)
  const publications: TracePublication[] = []

  for (const request of unique) {
    try {
      const child = Bun.spawn([...command, "trace-finalize", request.caseDir], {
        env,
        stdout: "ignore",
        stderr: "ignore",
      })
      const exit = await child.exited
      if (exit !== 0) {
        warn(options, `[observable-opencode] Trace materializer exited with exit ${exit}: ${request.caseDir}\n`)
        continue
      }
      const publication = collectMaterializedTracePublication(request)
      if (publication) publications.push(publication)
    } catch (error) {
      warn(
        options,
        `[observable-opencode] Trace materializer failed for ${request.caseDir}: ${error instanceof Error ? error.message : String(error)}\n`,
      )
    }
  }

  return publications
}
