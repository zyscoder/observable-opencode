import crypto from "crypto"
import fs from "fs"
import path from "path"
import { Global } from "@opencode-ai/core/global"
import { renderCaseTraceHtml } from "./case-trace-html"

export type TraceStatus = "running" | "success" | "error" | "cancelled"

export type TraceComponent =
  | "run"
  | "runtime"
  | "prompt"
  | "context"
  | "llm"
  | "processor"
  | "tool"
  | "skill"
  | "task"
  | "mcp"
  | "plugin"
  | "result"
  | "trace"

export type TraceFieldSummary = {
  type: string
  length?: number
  hash?: string
  preview?: string
  value?: string | number | boolean | null
  keys?: string[]
  artifact_id?: string
}

export type TraceArtifact = {
  artifact_id: string
  kind: "text" | "json"
  label?: string
  path: string
  length: number
  hash: string
  preview: string
  created_at: string
}

export type TraceTokenUsage = {
  input?: number
  output?: number
  reasoning?: number
  cached_input?: number
  cache_write?: number
  total?: number
  cost?: number
}

export type TraceError = {
  name?: string
  message: string
  stack?: string
  component?: string
  span_id?: string
}

export type TraceSpan = {
  span_id: string
  parent_span_id?: string
  component: TraceComponent
  operation: string
  name?: string
  status: TraceStatus
  start_time: string
  end_time?: string
  start_ms: number
  end_ms?: number
  duration_ms?: number
  input_summary?: unknown
  output_summary?: unknown
  token_usage?: TraceTokenUsage
  metadata?: Record<string, unknown>
  error?: TraceError
}

export type TraceEvent = {
  event_id: string
  span_id?: string
  component: TraceComponent
  event_type: string
  timestamp: string
  time_ms: number
  data?: unknown
}

export type TraceSummary = {
  trace_version: "1.0"
  case_id: string
  run_id: string
  session_id?: string
  started_at: string
  ended_at?: string
  duration_ms: number
  status: TraceStatus
  input?: Record<string, unknown>
  environment: Record<string, unknown>
  token_usage: TraceTokenUsage
  spans: TraceSpan[]
  events: TraceEvent[]
  artifacts?: TraceArtifact[]
  errors: TraceError[]
  result?: Record<string, unknown>
}

type CaseTraceConfig = {
  caseID?: string
  traceDir?: string
  input?: Record<string, unknown>
  environment?: Record<string, unknown>
}

type StartSpanInput = {
  component: TraceComponent
  operation: string
  name?: string
  parentSpanID?: string
  input?: unknown
  metadata?: Record<string, unknown>
}

type EndSpanInput = {
  status?: Exclude<TraceStatus, "running">
  output?: unknown
  metadata?: Record<string, unknown>
  tokenUsage?: unknown
  error?: unknown
}

type TraceEventInput = {
  component: TraceComponent
  event_type: string
  span_id?: string
  data?: unknown
}

type FinishTraceInput = {
  status?: Exclude<TraceStatus, "running">
  result?: Record<string, unknown>
  error?: unknown
}

export type ActiveSpan = {
  id: string
  event(input: Omit<TraceEventInput, "span_id" | "component"> & { component?: TraceComponent }): void
  end(input?: EndSpanInput): void
}

const truthy = new Set(["1", "true", "yes", "on"])
let active: ActiveCaseTrace | false | undefined
let processFinalizerInstalled = false

function nowIso() {
  return new Date().toISOString()
}

function enabledFromEnv() {
  const value = process.env.OPENCODE_CASE_TRACE
  return value ? truthy.has(value.toLowerCase()) : false
}

function stamp() {
  return new Date()
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d+Z$/, "Z")
}

function safeCaseID(input: string) {
  const trimmed = input.trim() || `case-${stamp()}-${process.pid}`
  return trimmed.replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 160)
}

function safeNumber(input: unknown) {
  const value = Number(input ?? 0)
  if (!Number.isFinite(value)) return 0
  return Math.max(0, value)
}

function defaultTraceDir() {
  return process.env.OPENCODE_CASE_TRACE_DIR || path.join(Global.Path.data, "case-traces")
}

function json(input: unknown) {
  const seen = new WeakSet<object>()
  return JSON.stringify(
    input,
    (_key, value) => {
      if (typeof value === "bigint") return String(value)
      if (typeof value === "function") return `[Function ${value.name || "anonymous"}]`
      if (value instanceof Error) {
        return errorInfo(value)
      }
      if (value instanceof URL) return value.toString()
      if (value && typeof value === "object") {
        if (seen.has(value)) return "[Circular]"
        seen.add(value)
      }
      return value
    },
    0,
  )
}

function hash(input: string) {
  return crypto.createHash("sha256").update(input).digest("hex").slice(0, 16)
}

function maxFieldLength() {
  return safeNumber(process.env.OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH || 2048) || 2048
}

function summarizeScalar(input: unknown): TraceFieldSummary {
  if (input === null) return { type: "null", value: null }
  if (typeof input === "string") return summarizeText(input)
  if (typeof input === "number" || typeof input === "boolean") return { type: typeof input, value: input }
  if (typeof input === "undefined") return { type: "undefined" }
  return { type: typeof input, value: String(input) }
}

export function summarizeText(input: unknown): TraceFieldSummary {
  const text = String(input ?? "")
  const limit = maxFieldLength()
  return {
    type: "text",
    length: text.length,
    hash: hash(text),
    preview: text.slice(0, limit),
  }
}

export function summarizeJson(input: unknown): TraceFieldSummary {
  if (input === null || typeof input !== "object") return summarizeScalar(input)
  const serialized = json(input)
  const keys = Array.isArray(input) ? undefined : Object.keys(input as Record<string, unknown>).slice(0, 50)
  return {
    type: Array.isArray(input) ? "array" : "object",
    length: serialized.length,
    hash: hash(serialized),
    keys,
    preview: serialized.slice(0, maxFieldLength()),
  }
}

export function summarizeMessages(input: unknown): TraceFieldSummary {
  if (!Array.isArray(input)) return summarizeJson(input)
  const roles = input
    .map((item) => (item && typeof item === "object" && "role" in item ? String(item.role) : "unknown"))
    .slice(0, 40)
  const summary = {
    count: input.length,
    roles,
  }
  return summarizeJson(summary)
}

export function errorInfo(input: unknown): TraceError {
  if (input instanceof Error) {
    return {
      name: input.name,
      message: input.message || input.name,
      stack: input.stack,
    }
  }
  return {
    message: typeof input === "string" ? input : json(input),
  }
}

export function normalizeTokenUsage(input: unknown): TraceTokenUsage {
  if (!input || typeof input !== "object") return {}
  const value = input as Record<string, any>
  const cache = value.cache && typeof value.cache === "object" ? value.cache : {}
  return {
    input: safeNumber(value.input ?? value.inputTokens),
    output: safeNumber(value.output ?? value.outputTokens),
    reasoning: safeNumber(value.reasoning ?? value.reasoningTokens ?? value.outputTokenDetails?.reasoningTokens),
    cached_input: safeNumber(
      value.cached_input ?? value.cachedInputTokens ?? value.inputTokenDetails?.cacheReadTokens ?? cache.read,
    ),
    cache_write: safeNumber(value.cache_write ?? value.inputTokenDetails?.cacheWriteTokens ?? cache.write),
    total: safeNumber(value.total ?? value.totalTokens),
    cost: safeNumber(value.cost),
  }
}

function mergeUsage(target: TraceTokenUsage, next: TraceTokenUsage) {
  target.input = safeNumber(target.input) + safeNumber(next.input)
  target.output = safeNumber(target.output) + safeNumber(next.output)
  target.reasoning = safeNumber(target.reasoning) + safeNumber(next.reasoning)
  target.cached_input = safeNumber(target.cached_input) + safeNumber(next.cached_input)
  target.cache_write = safeNumber(target.cache_write) + safeNumber(next.cache_write)
  target.total = safeNumber(target.total) + safeNumber(next.total)
  target.cost = safeNumber(target.cost) + safeNumber(next.cost)
}

class ActiveCaseTrace {
  readonly caseID: string
  readonly runID = crypto.randomUUID()
  readonly rootDir: string
  readonly caseDir: string
  readonly artifactDir: string
  readonly eventsFile: string
  readonly traceFile: string
  readonly htmlFile: string
  readonly startedAt = Date.now()
  readonly startedIso = nowIso()
  private sequence = 0
  private artifactSequence = 0
  private finished = false
  private sessionID: string | undefined
  private input: Record<string, unknown> | undefined
  private result: Record<string, unknown> | undefined
  private environment: Record<string, unknown>
  private spans = new Map<string, TraceSpan>()
  private events: TraceEvent[] = []
  private artifacts: TraceArtifact[] = []
  private errors: TraceError[] = []
  private tokenUsage: TraceTokenUsage = {}
  private writable = true

  constructor(config: CaseTraceConfig) {
    this.caseID = safeCaseID(config.caseID ?? process.env.OPENCODE_CASE_ID ?? "")
    this.rootDir = config.traceDir ?? defaultTraceDir()
    this.caseDir = path.join(this.rootDir, this.caseID)
    this.artifactDir = path.join(this.caseDir, "artifacts")
    this.eventsFile = path.join(this.caseDir, "events.jsonl")
    this.traceFile = path.join(this.caseDir, "trace.json")
    this.htmlFile = path.join(this.caseDir, "trace.html")
    this.input = config.input
    this.environment = {
      cwd: process.cwd(),
      argv: process.argv.slice(2),
      pid: process.pid,
      ...config.environment,
    }
    this.open()
  }

  setSessionID(sessionID: string | undefined) {
    if (!sessionID) return
    this.sessionID = sessionID
    this.write("trace.session", { session_id: sessionID })
  }

  setInput(input: Record<string, unknown>) {
    this.input = {
      ...(this.input ?? {}),
      ...input,
    }
    this.write("trace.input", this.input)
  }

  setEnvironment(input: Record<string, unknown>) {
    this.environment = {
      ...this.environment,
      ...input,
    }
    this.write("trace.environment", this.environment)
  }

  startSpan(input: StartSpanInput): ActiveSpan {
    const id = `span_${++this.sequence}_${crypto.randomUUID().slice(0, 8)}`
    const started = Date.now()
    const span: TraceSpan = {
      span_id: id,
      parent_span_id: input.parentSpanID,
      component: input.component,
      operation: input.operation,
      name: input.name,
      status: "running",
      start_time: new Date(started).toISOString(),
      start_ms: started - this.startedAt,
      input_summary:
        input.input === undefined ? undefined : this.summarizeJson(input.input, `${input.component}.${input.operation}.input`),
      metadata: input.metadata,
    }
    this.spans.set(id, span)
    this.write("span.start", span)
    let ended = false
    return {
      id,
      event: (event) =>
        this.event({
          span_id: id,
          component: event.component ?? input.component,
          event_type: event.event_type,
          data: event.data,
        }),
      end: (endInput) => {
        if (ended) return
        ended = true
        this.endSpan(id, endInput)
      },
    }
  }

  endSpan(id: string, input?: EndSpanInput) {
    const span = this.spans.get(id)
    if (!span) return
    const ended = Date.now()
    const usage = input?.tokenUsage ? normalizeTokenUsage(input.tokenUsage) : undefined
    const error = input?.error ? { ...errorInfo(input.error), component: span.component, span_id: id } : undefined
    span.status = input?.status ?? (error ? "error" : "success")
    span.end_time = new Date(ended).toISOString()
    span.end_ms = ended - this.startedAt
    span.duration_ms = Math.max(0, ended - (this.startedAt + span.start_ms))
    span.output_summary =
      input?.output === undefined ? span.output_summary : this.summarizeJson(input.output, `${span.component}.${span.operation}.output`)
    span.metadata = {
      ...(span.metadata ?? {}),
      ...(input?.metadata ?? {}),
    }
    if (usage) {
      span.token_usage = usage
      mergeUsage(this.tokenUsage, usage)
    }
    if (error) {
      span.error = error
      this.errors.push(error)
    }
    this.write("span.end", span)
  }

  event(input: TraceEventInput) {
    const time = Date.now()
    const event: TraceEvent = {
      event_id: `evt_${++this.sequence}_${crypto.randomUUID().slice(0, 8)}`,
      span_id: input.span_id,
      component: input.component,
      event_type: input.event_type,
      timestamp: new Date(time).toISOString(),
      time_ms: time - this.startedAt,
      data: input.data === undefined ? undefined : this.summarizeJson(input.data, `${input.component}.${input.event_type}.data`),
    }
    this.events.push(event)
    this.write("event", event)
  }

  usage(input: unknown, spanID?: string) {
    const usage = normalizeTokenUsage(input)
    mergeUsage(this.tokenUsage, usage)
    this.event({
      component: "llm",
      event_type: "usage",
      span_id: spanID,
      data: usage,
    })
  }

  finish(input?: FinishTraceInput) {
    if (this.finished) return
    this.finished = true
    const error = input?.error ? errorInfo(input.error) : undefined
    if (error) this.errors.push(error)
    this.result = input?.result ?? this.result
    const summary = this.summary(input?.status ?? (error ? "error" : "success"))
    this.write("trace.finish", summary)
    this.safeWrite(this.traceFile, jsonPretty(summary))
    this.safeWrite(this.htmlFile, renderCaseTraceHtml(summary, { artifactDir: this.caseDir }))
  }

  private summary(status: TraceStatus): TraceSummary {
    const ended = Date.now()
    return {
      trace_version: "1.0",
      case_id: this.caseID,
      run_id: this.runID,
      session_id: this.sessionID,
      started_at: this.startedIso,
      ended_at: new Date(ended).toISOString(),
      duration_ms: Math.max(0, ended - this.startedAt),
      status,
      input: this.input,
      environment: this.environment,
      token_usage: this.tokenUsage,
      spans: [...this.spans.values()],
      events: this.events,
      artifacts: this.artifacts,
      errors: this.errors,
      result: this.result,
    }
  }

  summarizeText(input: unknown, label = "text"): TraceFieldSummary {
    const text = String(input ?? "")
    const summary: TraceFieldSummary = {
      type: "text",
      length: text.length,
      hash: hash(text),
      preview: text.slice(0, maxFieldLength()),
    }
    if (text.length <= maxFieldLength()) return summary
    const artifact = this.writeArtifact("text", label, text)
    return {
      ...summary,
      artifact_id: artifact.artifact_id,
    }
  }

  summarizeJson(input: unknown, label = "json"): TraceFieldSummary {
    if (input === null || typeof input !== "object") return summarizeScalar(input)
    const serialized = json(input)
    const keys = Array.isArray(input) ? undefined : Object.keys(input as Record<string, unknown>).slice(0, 50)
    const summary: TraceFieldSummary = {
      type: Array.isArray(input) ? "array" : "object",
      length: serialized.length,
      hash: hash(serialized),
      keys,
      preview: serialized.slice(0, maxFieldLength()),
    }
    if (serialized.length <= maxFieldLength()) return summary
    const artifact = this.writeArtifact("json", label, prettyJsonString(serialized))
    return {
      ...summary,
      artifact_id: artifact.artifact_id,
    }
  }

  private writeArtifact(kind: TraceArtifact["kind"], label: string, content: string): TraceArtifact {
    const artifactID = `artifact_${++this.artifactSequence}_${crypto.randomUUID().slice(0, 8)}`
    const safeLabel = (label || kind).replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 80)
    const filename = `${artifactID}_${safeLabel}.${kind === "json" ? "json" : "txt"}`
    const relativePath = `artifacts/${filename}`
    const artifact: TraceArtifact = {
      artifact_id: artifactID,
      kind,
      label,
      path: relativePath,
      length: content.length,
      hash: hash(content),
      preview: content.slice(0, maxFieldLength()),
      created_at: nowIso(),
    }
    this.artifacts.push(artifact)
    try {
      fs.mkdirSync(this.artifactDir, { recursive: true })
      fs.writeFileSync(path.join(this.caseDir, relativePath), content)
      this.write("artifact.write", artifact)
    } catch {}
    return artifact
  }

  private open() {
    try {
      fs.mkdirSync(this.caseDir, { recursive: true })
      fs.writeFileSync(this.eventsFile, "")
      this.write("trace.start", {
        trace_version: "1.0",
        case_id: this.caseID,
        run_id: this.runID,
        started_at: this.startedIso,
        root_dir: this.rootDir,
        case_dir: this.caseDir,
        input: this.input,
        environment: this.environment,
      })
    } catch {
      this.writable = false
    }
  }

  private write(type: string, data: unknown) {
    if (!this.writable) return
    if (this.finished && type !== "trace.finish") return
    try {
      fs.appendFileSync(
        this.eventsFile,
        json({
          time: nowIso(),
          type,
          data,
        }) + "\n",
      )
    } catch {}
  }

  private safeWrite(target: string, content: string) {
    try {
      fs.mkdirSync(path.dirname(target), { recursive: true })
      fs.writeFileSync(target, content)
    } catch {}
  }
}

function jsonPretty(input: unknown) {
  return JSON.stringify(
    input,
    (_key, value) => {
      if (typeof value === "bigint") return String(value)
      return value
    },
    2,
  )
}

function prettyJsonString(input: string) {
  try {
    return JSON.stringify(JSON.parse(input), null, 2)
  } catch {
    return input
  }
}

const summarizeTextField = summarizeText
const summarizeJsonField = summarizeJson

function finishActiveFromProcessExit(code: number | undefined) {
  const current = active || undefined
  if (!current) return
  current.finish({
    status: code && code !== 0 ? "error" : "success",
    result: {
      exit_code: code ?? process.exitCode ?? 0,
      reason: "process.exit",
    },
  })
  active = false
}

function installProcessFinalizer() {
  if (processFinalizerInstalled) return
  processFinalizerInstalled = true
  process.once("beforeExit", (code) => finishActiveFromProcessExit(code))
  process.once("exit", (code) => finishActiveFromProcessExit(code))
}

export namespace CaseTrace {
  export function isEnabled() {
    return enabledFromEnv()
  }

  export function configure(input: CaseTraceConfig = {}) {
    if (!enabledFromEnv()) {
      active = false
      return undefined
    }
    const caseID = safeCaseID(input.caseID ?? process.env.OPENCODE_CASE_ID ?? "")
    if (active && active.caseID === caseID) {
      if (input.input) active.setInput(input.input)
      if (input.environment) active.setEnvironment(input.environment)
      return active
    }
    if (active) active.finish({ status: "cancelled", result: { reason: "reconfigured" } })
    active = new ActiveCaseTrace({ ...input, caseID })
    installProcessFinalizer()
    return active || undefined
  }

  export function get() {
    if (active === undefined && enabledFromEnv()) return configure()
    return active || undefined
  }

  export function setSessionID(sessionID: string | undefined) {
    get()?.setSessionID(sessionID)
  }

  export function event(input: TraceEventInput) {
    get()?.event(input)
  }

  export function usage(input: unknown, spanID?: string) {
    get()?.usage(input, spanID)
  }

  export function finish(input?: FinishTraceInput) {
    get()?.finish(input)
    active = false
  }

  export function summarizeText(input: unknown) {
    return active ? active.summarizeText(input) : summarizeTextField(input)
  }

  export function summarizeJson(input: unknown) {
    return active ? active.summarizeJson(input) : summarizeJsonField(input)
  }
}
