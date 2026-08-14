import { existsSync, readFileSync } from "node:fs"
import path from "node:path"
import type { TraceMaterializationRequest } from "./case-trace"

export type TracePublicationStatus = "completed" | "failed" | "cancelled" | "partial"

export type TracePublication = {
  sessionID?: string
  caseID: string
  status: TracePublicationStatus
  caseDir: string
  traceFile?: string
  partialFile?: string
}

export type TracePublicationInput = {
  sessionID?: string
  caseID: string
  status: TracePublicationStatus
  caseDir: string
  traceFile?: string
  partialFile?: string
}

function existingAbsolutePath(file: string | undefined): string | undefined {
  if (!file) return undefined
  const absolutePath = path.resolve(file)
  return existsSync(absolutePath) ? absolutePath : undefined
}

export function collectTracePublication(input: TracePublicationInput): TracePublication | undefined {
  const traceFile = existingAbsolutePath(input.traceFile)
  const partialFile = existingAbsolutePath(input.partialFile)

  if (!traceFile && !partialFile) return undefined

  const status = partialFile && !traceFile ? "partial" : input.status
  return {
    sessionID: input.sessionID,
    caseID: input.caseID,
    status,
    caseDir: path.resolve(input.caseDir),
    traceFile,
    partialFile,
  }
}

export function collectMaterializedTracePublication(input: TraceMaterializationRequest): TracePublication | undefined {
  const manifestFile = path.join(input.caseDir, "manifest.json")
  let status: TracePublicationStatus = "completed"
  try {
    const manifest = JSON.parse(readFileSync(manifestFile, "utf8")) as { status?: unknown }
    if (manifest.status === "error") status = "failed"
    if (manifest.status === "cancelled") status = "cancelled"
  } catch {
    // The finalized JSON remains useful even if its manifest cannot be read.
  }
  return collectTracePublication({
    sessionID: input.sessionID,
    caseID: input.caseID,
    status,
    caseDir: input.caseDir,
    traceFile: path.join(input.caseDir, "trace.json"),
    partialFile: path.join(input.caseDir, "partial", "latest.json"),
  })
}

export function formatTracePublication(input: TracePublication): string {
  const lines = [
    "[observable-opencode] Session trace saved",
    input.sessionID ? `  session: ${input.sessionID}` : undefined,
    `  case: ${input.caseID}`,
    `  status: ${input.status}`,
    `  directory: ${input.caseDir}`,
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
