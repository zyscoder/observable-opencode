import fs from "node:fs"
import path from "node:path"
import { Global } from "@opencode-ai/core/global"
import { openTraceSegment, type TraceSegment, type TraceSegmentStatus } from "./trace-segment"
import { writeTraceArtifact, type TraceArtifactInput, type TraceArtifactRef } from "./trace-artifact"

export type TraceRuntimeRecord = {
  operation: string
  component?: string
  node_id?: string
  data?: unknown
  [key: string]: unknown
}

export type TraceRuntimeOpenInput = {
  rootDir?: string
  caseID: string
  sessionID?: string
  runID: string
}

export type TraceHandle = {
  readonly caseDir: string
  readonly segmentDir: string
  readonly sessionFile: string
  record(record: TraceRuntimeRecord): void
  recordArtifact(input: Omit<TraceArtifactInput, "segmentDir">): TraceArtifactRef | undefined
  close(status: Exclude<TraceSegmentStatus, "running" | "interrupted_unfinalized">): void
}

function appendRecord(segment: TraceSegment, record: TraceRuntimeRecord) {
  const recordsFile = path.join(segment.logicalRoot, segment.descriptor.records)
  const payload = {
    sequence: Date.now(),
    recorded_at: new Date().toISOString(),
    ...record,
  }
  fs.appendFileSync(recordsFile, `${JSON.stringify(payload)}\n`, "utf8")
}

function safe<T>(fallback: T, fn: () => T) {
  try {
    return fn()
  } catch {
    return fallback
  }
}

function open(input: TraceRuntimeOpenInput): TraceHandle {
  const segment = openTraceSegment({
    rootDir: input.rootDir ?? process.env.OPENCODE_CASE_TRACE_DIR ?? path.join(Global.Path.data, "case-traces"),
    logicalCaseID: input.caseID,
    sessionID: input.sessionID,
    runID: input.runID,
  })
  let closed = false
  return {
    caseDir: segment.logicalRoot,
    segmentDir: segment.segmentDir,
    sessionFile: segment.sessionFile,
    record(record) {
      if (closed) return
      safe(undefined, () => appendRecord(segment, record))
    },
    recordArtifact(input) {
      if (closed) return undefined
      return safe(undefined, () => writeTraceArtifact({ ...input, segmentDir: segment.segmentDir }))
    },
    close(status) {
      if (closed) return
      closed = true
      safe(undefined, () => {
        segment.finalize(status)
      })
    },
  }
}

export const TraceRuntime = { open }
