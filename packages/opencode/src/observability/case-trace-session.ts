export type TraceRouteHint = {
  scope?: "process"
  sessionID?: string
  refs: string[]
}

const directSessionKeys = new Set(["sessionID", "session_id", "parentSessionID", "parent_session_id"])
const directSessionContainers = new Set(["input", "data", "metadata"])
const referenceKeys = new Map<string, string[]>([
  ["span_id", ["span"]],
  ["turn_id", ["turn"]],
  ["decision_id", ["decision"]],
  ["snapshot_id", ["snapshot"]],
  ["record_id", ["record"]],
  ["node_id", ["node"]],
  ["fact_id", ["fact"]],
  ["verification_id", ["verification"]],
  ["change_id", ["change"]],
  ["edge_id", ["edge"]],
  ["check_id", ["check", "compaction_check"]],
  ["constraint_id", ["constraint"]],
  ["design_id", ["design"]],
  ["gate_id", ["gate", "exit_gate"]],
  ["segment_id", ["segment", "response_segment"]],
  ["claim_id", ["claim", "response_claim"]],
  ["lifecycle_id", ["lifecycle"]],
])
const referenceContainers = new Set(["source_refs", "evidence_refs", "aliases"])
const maxRecentReferencesPerCategory = 256
const maxRecentOwnersPerReference = 256
const knownReferenceCategories = new Set([...referenceKeys.values()].flat())

function referenceCategory(ref: string) {
  const separator = ref.indexOf(":")
  if (separator <= 0) return "untyped"
  const category = ref.slice(0, separator)
  return knownReferenceCategories.has(category) ? category : "other"
}

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

  const scope =
    isRecord(input) && (input.trace_scope === "process" || input.traceScope === "process")
      ? "process"
      : undefined
  let sessionID = isRecord(input) ? directSessionID(input) : undefined
  if (isRecord(input) && !sessionID) {
    for (const [key, value] of Object.entries(input)) {
      if (!directSessionContainers.has(key) || !isRecord(value)) continue
      sessionID = directSessionID(value)
      if (sessionID) break
    }
  }

  const visit = (value: unknown): void => {
    if (value === null || typeof value !== "object" || seenObjects.has(value)) return
    seenObjects.add(value)
    if (Array.isArray(value)) {
      for (const item of value) visit(item)
      return
    }
    if (!isRecord(value)) return

    const refID = typeof value.ref_id === "string" ? value.ref_id : undefined
    const refType = typeof value.ref_type === "string" ? value.ref_type : undefined
    if (refID) {
      rememberRef(refID)
      if (refType) rememberRef(`${refType}:${refID}`)
    }

    for (const [key, field] of Object.entries(value)) {
      const prefixes = referenceKeys.get(key)
      if (prefixes) {
        if (typeof field === "string" && field) {
          rememberRef(field)
          for (const prefix of prefixes) rememberRef(`${prefix}:${field}`)
        }
        if (Array.isArray(field)) {
          for (const item of field) {
            if (typeof item !== "string" || !item) continue
            rememberRef(item)
            for (const prefix of prefixes) rememberRef(`${prefix}:${item}`)
          }
        }
      }

      if (referenceContainers.has(key)) {
        if (typeof field === "string") rememberRef(field)
        if (Array.isArray(field)) for (const item of field) rememberRef(item)
      }

      if (key === "from" || key === "to") rememberRef(endpointRef(field))
      visit(field)
    }
  }

  visit(input)
  return {
    ...(scope ? { scope } : {}),
    ...(sessionID ? { sessionID } : {}),
    refs,
  }
}

export type SessionTraceKind = "root" | "process" | "compatibility"

export class SessionTraceRegistry<T extends object> {
  private readonly roots = new Map<string, T>()
  private readonly orphans = new Set<T>()
  private readonly aliases = new Map<string, string>()
  private readonly owners = new Map<string, Set<T>>()
  private readonly recentOwnerRefs = new Map<string, Set<string>>()
  private finalized = new WeakSet<T>()
  private processTrace: T | undefined
  private compatibilityTrace: T | undefined
  private compatibilitySessionID: string | undefined
  private rootOrdinal = 0
  private processOrdinal = 0

  constructor(
    private readonly create: (sessionID: string | undefined, ordinal: number, kind: SessionTraceKind) => T,
  ) {}

  resolve(hint?: Partial<TraceRouteHint>): T {
    if (hint?.scope === "process") return this.resolveProcess()

    const sessionID = hint?.sessionID
    const sessionTrace = sessionID ? this.roots.get(this.rootSessionID(sessionID)) : undefined

    let ownerCandidates: Set<T> | undefined
    for (const ref of hint?.refs ?? []) {
      const owners = this.owners.get(ref)
      if (!owners) continue
      if (!ownerCandidates) {
        ownerCandidates = new Set(owners)
        continue
      }
      for (const candidate of ownerCandidates) {
        if (!owners.has(candidate)) ownerCandidates.delete(candidate)
      }
    }
    if (sessionID) {
      if (!ownerCandidates) return this.rootTrace(sessionID)
      if (sessionTrace && ownerCandidates.has(sessionTrace)) return sessionTrace
      return this.resolveProcess()
    }
    if (ownerCandidates) {
      if (ownerCandidates.size === 1) return ownerCandidates.values().next().value!
      return this.resolveProcess()
    }

    if (this.roots.size === 1) return this.roots.values().next().value!
    return this.resolveProcess()
  }

  resolveActive(hint?: Partial<TraceRouteHint>): T | undefined {
    const trace = this.resolve(hint)
    return this.finalized.has(trace) ? undefined : trace
  }

  hasRoots(): boolean {
    return this.roots.size > 0
  }

  resolveCompatibility(): T {
    if (this.compatibilityTrace && !this.compatibilitySessionID) return this.compatibilityTrace
    if (this.roots.size === 1) return this.roots.values().next().value!
    if (this.roots.size > 1) return this.resolve()
    this.compatibilityTrace = this.create(undefined, 0, "compatibility")
    return this.compatibilityTrace
  }

  resolveCompatibilityActive(): T | undefined {
    const trace = this.resolveCompatibility()
    return this.finalized.has(trace) ? undefined : trace
  }

  claimCompatibility(sessionID: string): T | undefined {
    const rootSessionID = this.rootSessionID(sessionID)
    if (this.compatibilitySessionID) {
      return this.compatibilitySessionID === rootSessionID ? this.compatibilityTrace : undefined
    }
    if (!this.compatibilityTrace || this.roots.size > 0) return undefined

    this.compatibilitySessionID = rootSessionID
    this.roots.set(rootSessionID, this.compatibilityTrace)
    this.rootOrdinal++
    return this.compatibilityTrace
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
    if (this.finalized.has(trace)) return
    for (const ref of refs) {
      if (!ref) continue
      const category = referenceCategory(ref)
      const recent = this.recentOwnerRefs.get(category) ?? new Set<string>()
      recent.delete(ref)
      recent.add(ref)
      this.recentOwnerRefs.set(category, recent)
      while (recent.size > maxRecentReferencesPerCategory) {
        const evicted = recent.values().next().value
        if (evicted === undefined) break
        recent.delete(evicted)
        this.owners.delete(evicted)
      }
      let owners = this.owners.get(ref)
      if (!owners) {
        owners = new Set<T>()
        this.owners.set(ref, owners)
      }
      owners.delete(trace)
      owners.add(trace)
      while (owners.size > maxRecentOwnersPerReference) {
        const evicted = owners.values().next().value
        if (evicted === undefined) break
        owners.delete(evicted)
      }
    }
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
    if (this.compatibilityTrace) traces.add(this.compatibilityTrace)
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
      this.recentOwnerRefs.clear()
      this.processTrace = undefined
      this.compatibilityTrace = undefined
      this.compatibilitySessionID = undefined
      this.finalized = new WeakSet<T>()
      this.rootOrdinal = 0
      this.processOrdinal = 0
    }
  }

  private rootTrace(sessionID: string): T {
    const rootSessionID = this.rootSessionID(sessionID)
    let trace = this.roots.get(rootSessionID)
    if (!trace) {
      trace = this.create(rootSessionID, this.rootOrdinal++, "root")
      this.roots.set(rootSessionID, trace)
    }
    return trace
  }

  private resolveProcess(): T {
    if (!this.processTrace) this.processTrace = this.create(undefined, this.processOrdinal++, "process")
    return this.processTrace
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
