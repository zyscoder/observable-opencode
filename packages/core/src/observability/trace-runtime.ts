import fs from "node:fs"
import path from "node:path"
import { Global } from "@opencode-ai/core/global"
import { CausalIRRuntimeStore } from "./causal-ir-runtime-store"
import type { CausalEdgeLike } from "./causal-ir"
import { isFormalRecordType } from "./trace-semantic-contract"
import type { TraceComponent } from "./case-trace"
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
  readonly runID: string
  record(record: TraceRuntimeRecord): void
  edge(edge: CausalEdgeLike): void
  recordArtifact(input: Omit<TraceArtifactInput, "segmentDir">): TraceArtifactRef | undefined
  close(status: Exclude<TraceSegmentStatus, "running" | "interrupted_unfinalized">): void
}

const activeHandles = new Set<TraceHandle>()
const signalHandlers = new Map<NodeJS.Signals, () => void>()

function uninstallSignalHandlers() {
  for (const [signal, handler] of signalHandlers) process.removeListener(signal, handler)
  signalHandlers.clear()
}

function installSignalHandlers() {
  if (signalHandlers.size > 0) return
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as const) {
    const handler = () => {
      const hasOtherHandlers = process.listenerCount(signal) > 1
      for (const trace of [...activeHandles]) trace.close("cancelled")
      uninstallSignalHandlers()
      if (hasOtherHandlers) return
      try {
        process.kill(process.pid, signal)
      } catch {
        // The original signal has already completed process shutdown.
      }
    }
    signalHandlers.set(signal, handler)
    process.once(signal, handler)
  }
}

function appendJournalEntry(segment: TraceSegment, entry: unknown) {
  const recordsFile = path.join(segment.logicalRoot, segment.descriptor.records)
  fs.appendFileSync(recordsFile, `${JSON.stringify(entry)}\n`, "utf8")
}

const TRACE_COMPONENTS = new Set<TraceComponent>([
  "run",
  "runtime",
  "prompt",
  "context",
  "llm",
  "processor",
  "tool",
  "skill",
  "task",
  "mcp",
  "plugin",
  "result",
  "evaluation",
  "trace",
])

function componentOf(input: unknown): TraceComponent {
  return typeof input === "string" && TRACE_COMPONENTS.has(input as TraceComponent)
    ? (input as TraceComponent)
    : "runtime"
}

function dataOf(record: TraceRuntimeRecord, caseID: string, runID: string) {
  const data = record.data && typeof record.data === "object" && !Array.isArray(record.data) ? record.data : {}
  const { operation: _operation, component: _component, node_id: _nodeID, data: _data, ...metadata } = record
  return {
    ...data,
    ...metadata,
    case_id: caseID,
    run_id: runID,
    original_operation: record.operation,
  }
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
  const store = new CausalIRRuntimeStore({
    indexPath: path.join(segment.logicalRoot, segment.descriptor.index),
    caseID: input.caseID,
    runID: input.runID,
    append: (entry) => appendJournalEntry(segment, entry),
  })
  safe(undefined, () => {
    const timestamp = new Date().toISOString()
    store.createNode({
      node_id: `run_${input.runID}`,
      kind: "run.start",
      component: "run",
      timestamp,
      time_ms: Date.now(),
      data: {
        run_id: input.runID,
        case_id: input.caseID,
        ...(input.sessionID ? { session_id: input.sessionID } : {}),
      },
    })
  })
  let closed = false
  let nodeSequence = 0
  const handle: TraceHandle = {
    caseDir: segment.logicalRoot,
    segmentDir: segment.segmentDir,
    sessionFile: segment.sessionFile,
    runID: input.runID,
    record(record) {
      if (closed) return
      nodeSequence += 1
      safe(undefined, () => {
        const nodeID = record.node_id ?? `observation_${input.runID}_${nodeSequence}`
        const kind = isFormalRecordType(record.operation) ? record.operation : "execution.observation"
        store.createNode({
          node_id: nodeID,
          kind,
          component: componentOf(record.component),
          timestamp: new Date().toISOString(),
          time_ms: Date.now(),
          title: typeof record.name === "string" ? record.name : undefined,
          data: dataOf(record, input.caseID, input.runID),
        })
      })
    },
    edge(edge) {
      if (closed) return
      safe(undefined, () => store.createEdge(edge))
    },
    recordArtifact(input) {
      if (closed) return undefined
      return safe(undefined, () => {
        const artifact = writeTraceArtifact({ ...input, segmentDir: segment.segmentDir })
        store.createArtifact({
          artifact_id: artifact.artifact_id,
          hash: artifact.sha256,
          path: path.relative(segment.logicalRoot, path.join(segment.segmentDir, artifact.path)).split(path.sep).join("/"),
          media_type: artifact.media_type,
          bytes: artifact.bytes,
          preview: artifact.preview,
        })
        return artifact
      })
    },
    close(status) {
      if (closed) return
      closed = true
      activeHandles.delete(handle)
      if (activeHandles.size === 0) uninstallSignalHandlers()
      safe(undefined, () => {
        store.closeRuntime({
          format: "runtime_close",
          status: status === "completed" ? "success" : status === "cancelled" ? "cancelled" : "error",
          closed_at: new Date().toISOString(),
          manifest: {
            case_id: input.caseID,
            run_id: input.runID,
            session_id: input.sessionID,
          },
        })
        store.close()
        segment.finalize(status)
      })
    },
  }
  activeHandles.add(handle)
  installSignalHandlers()
  return handle
}

export const TraceRuntime = { open }
