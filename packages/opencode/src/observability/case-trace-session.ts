export type TraceRouteHint = {
  sessionID?: string
  refs: string[]
}

const directSessionKeys = new Set(["sessionID", "session_id", "parentSessionID", "parent_session_id"])
const referenceKeys = new Set([
  "span_id",
  "turn_id",
  "decision_id",
  "snapshot_id",
  "record_id",
  "node_id",
  "fact_id",
  "verification_id",
  "change_id",
])

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function endpointRef(value: unknown): string | undefined {
  if (typeof value === "string") return value
  if (!isRecord(value)) return undefined

  const id = typeof value.id === "string" ? value.id : undefined
  const type = typeof value.type === "string" ? value.type : undefined
  if (id && type) return `${type}:${id}`

  const refID = typeof value.ref_id === "string" ? value.ref_id : undefined
  const refType = typeof value.ref_type === "string" ? value.ref_type : undefined
  if (refID && refType) return `${refType}:${refID}`
  return refID
}

export function traceRouteHint(input: unknown): TraceRouteHint {
  let sessionID: string | undefined
  const refs: string[] = []
  const seenRefs = new Set<string>()
  const seenObjects = new WeakSet<object>()

  const rememberRef = (value: unknown) => {
    if (typeof value !== "string" || !value || seenRefs.has(value)) return
    seenRefs.add(value)
    refs.push(value)
  }

  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      for (const item of value) visit(item)
      return
    }
    if (!isRecord(value) || seenObjects.has(value)) return
    seenObjects.add(value)

    for (const key of directSessionKeys) {
      const candidate = value[key]
      if (!sessionID && typeof candidate === "string" && candidate) sessionID = candidate
    }

    for (const [key, field] of Object.entries(value)) {
      if (referenceKeys.has(key)) {
        const prefix = key.slice(0, -"_id".length)
        if (typeof field === "string" && field) rememberRef(`${prefix}:${field}`)
        if (Array.isArray(field)) {
          for (const item of field) if (typeof item === "string" && item) rememberRef(`${prefix}:${item}`)
        }
      }

      if (key === "source_refs") {
        if (typeof field === "string") rememberRef(field)
        if (Array.isArray(field)) for (const item of field) rememberRef(item)
      }

      if (key === "from" || key === "to") rememberRef(endpointRef(field))
      visit(field)
    }
  }

  visit(input)
  return sessionID ? { sessionID, refs } : { refs }
}

export class SessionTraceRegistry<T extends object> {
  private readonly roots = new Map<string, T>()
  private readonly aliases = new Map<string, string>()
  private readonly owners = new Map<string, T>()
  private finalized = new WeakSet<T>()
  private processTrace: T | undefined
  private ordinal = 0

  constructor(private readonly create: (sessionID: string | undefined, ordinal: number) => T) {}

  resolve(hint?: Partial<TraceRouteHint>): T {
    const sessionID = hint?.sessionID
    if (sessionID) return this.rootTrace(sessionID)

    for (const ref of hint?.refs ?? []) {
      const owner = this.owners.get(ref)
      if (owner) return owner
    }

    if (this.roots.size === 1) return this.roots.values().next().value!
    if (!this.processTrace) this.processTrace = this.create(undefined, this.ordinal++)
    return this.processTrace
  }

  alias(childSessionID: string, parentSessionID: string): T {
    const parent = this.rootSessionID(parentSessionID)
    const trace = this.rootTrace(parent)
    this.aliases.set(childSessionID, parent)
    this.roots.delete(childSessionID)
    return trace
  }

  remember(trace: T, refs: string[]): void {
    for (const ref of refs) if (ref) this.owners.set(ref, trace)
  }

  finishSession(sessionID: string, finish: (trace: T) => void): void {
    const trace = this.roots.get(this.rootSessionID(sessionID))
    if (trace) this.finish(trace, finish)
  }

  finishAll(finish: (trace: T) => void): void {
    for (const trace of this.values()) this.finish(trace, finish)
  }

  values(): T[] {
    const traces = new Set<T>(this.roots.values())
    if (this.processTrace) traces.add(this.processTrace)
    return [...traces]
  }

  reset(finish?: (trace: T) => void): void {
    if (finish) this.finishAll(finish)
    this.roots.clear()
    this.aliases.clear()
    this.owners.clear()
    this.processTrace = undefined
    this.finalized = new WeakSet<T>()
  }

  private rootTrace(sessionID: string): T {
    const rootSessionID = this.rootSessionID(sessionID)
    let trace = this.roots.get(rootSessionID)
    if (!trace) {
      trace = this.create(rootSessionID, this.ordinal++)
      this.roots.set(rootSessionID, trace)
    }
    return trace
  }

  private rootSessionID(sessionID: string): string {
    const seen = new Set<string>()
    let current = sessionID
    while (!seen.has(current)) {
      seen.add(current)
      const parent = this.aliases.get(current)
      if (!parent || parent === current) break
      current = parent
    }
    return current
  }

  private finish(trace: T, callback: (trace: T) => void): void {
    if (this.finalized.has(trace)) return
    this.finalized.add(trace)
    callback(trace)
  }
}
