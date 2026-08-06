import { existsSync } from "node:fs"
import path from "node:path"

export type TracePublicationStatus = "completed" | "failed" | "cancelled" | "partial"

export type TracePublication = {
  sessionID?: string
  caseID: string
  status: TracePublicationStatus
  caseDir: string
  traceFile?: string
  htmlFile?: string
  partialFile?: string
}

export type TracePublicationInput = {
  sessionID?: string
  caseID: string
  status: TracePublicationStatus
  caseDir: string
  traceFile?: string
  htmlFile?: string
  partialFile?: string
}

function existingAbsolutePath(file: string | undefined): string | undefined {
  if (!file) return undefined
  const absolutePath = path.resolve(file)
  return existsSync(absolutePath) ? absolutePath : undefined
}

export function collectTracePublication(input: TracePublicationInput): TracePublication | undefined {
  const traceFile = existingAbsolutePath(input.traceFile)
  const htmlFile = existingAbsolutePath(input.htmlFile)
  const partialFile = existingAbsolutePath(input.partialFile)

  if (!traceFile && !htmlFile && !partialFile) return undefined

  const status = partialFile && (!traceFile || !htmlFile) ? "partial" : input.status
  return {
    sessionID: input.sessionID,
    caseID: input.caseID,
    status,
    caseDir: path.resolve(input.caseDir),
    traceFile,
    htmlFile,
    partialFile,
  }
}

export function formatTracePublication(input: TracePublication): string {
  const lines = [
    "[observable-opencode] Session trace saved",
    input.sessionID ? `  session: ${input.sessionID}` : undefined,
    `  case: ${input.caseID}`,
    `  status: ${input.status}`,
    `  directory: ${input.caseDir}`,
    input.htmlFile ? `  html: ${input.htmlFile}` : undefined,
    input.traceFile ? `  json: ${input.traceFile}` : undefined,
    input.partialFile ? `  partial: ${input.partialFile}` : undefined,
  ]
  return lines.filter((line): line is string => line !== undefined).join("\n") + "\n"
}

export function reportTracePublication(
  input: TracePublication,
  write: (text: string) => unknown = (text) => process.stderr.write(text),
): void {
  try {
    const result = write(formatTracePublication(input))
    if (result && typeof (result as PromiseLike<unknown>).then === "function") {
      void Promise.resolve(result).catch(() => {})
    }
  } catch {
    // A diagnostic failure must not affect the original process outcome.
  }
}
