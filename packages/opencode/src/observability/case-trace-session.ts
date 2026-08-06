export type TraceRouteHint = {
  sessionID?: string
  refs: string[]
}

const directSessionKeys = new Set(["sessionID", "session_id", "parentSessionID", "parent_session_id"])
const directSessionContainers = new Set(["input", "data", "metadata"])
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
  const refs: string[] = []
  const seenRefs = new Set<string>()
  const seenObjects = new WeakSet<object>()

  const rememberRef = (value: unknown) => {
    if (typeof value !== "string" || !value || seenRefs.has(value)) return
    seenRefs.add(value)
    refs.push(value)
  }

  const directSessionID = (value: Record<string, unknown>): string | undefined => {
    for (const key of directSessionKeys) {
      const candidate = value[key]
      if (typeof candidate === "string" && candidate) return candidate
    }
  }

  let sessionID = isRecord(input) ? directSessionID(input) : undefined
  if (isRecord(input) && !sessionID) {
    for (const [key, value] of Object.entries(input)) {
      if (!directSessionContainers.has(key) || !isRecord(value)) continue
      sessionID = directSessionID(value)
      if (sessionID) break
    }
  }

  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      for (const item of value) visit(item)
      return
    }
    if (!isRecord(value) || seenObjects.has(value)) return
    seenObjects.add(value)

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
  private readonly orphans = new Set<T>()
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
    const child = this.rootSessionID(childSessionID)
    const childTrace = this.roots.get(child)

    this.aliases.set(childSessionID, parent)
    if (childTrace && childTrace !== trace) {
      this.roots.delete(child)
      this.orphans.add(childTrace)
    }
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
    const errors: unknown[] = []
    for (const trace of this.values()) {
      try {
        this.finish(trace, finish)
      } catch (error) {
        errors.push(error)
      }
    }
    if (errors.length) throw new AggregateError(errors, "Failed to finish all traces")
  }

  values(): T[] {
    const traces = new Set<T>(this.roots.values())
    for (const trace of this.orphans) traces.add(trace)
    if (this.processTrace) traces.add(this.processTrace)
    return [...traces]
  }

  reset(finish?: (trace: T) => void): void {
    try {
      if (finish) this.finishAll(finish)
    } finally {
      this.roots.clear()
      this.orphans.clear()
      this.aliases.clear()
      this.owners.clear()
      this.processTrace = undefined
      this.finalized = new WeakSet<T>()
      this.ordinal = 0
    }
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
    callback(trace)
    this.finalized.add(trace)
  }
}
