import crypto from "node:crypto"
import fs from "node:fs"
import path from "node:path"

export type TraceSegmentStatus = "running" | "completed" | "failed" | "cancelled" | "interrupted_unfinalized"

export type TraceSegmentDescriptor = {
  segment_id: string
  run_id: string
  case_id: string
  session_id?: string
  path: string
  records: string
  artifacts: string
  index: string
  started_at: string
  status: TraceSegmentStatus
  continuation_of?: string
}

export type TraceSessionManifest = {
  schema_version: "1.0"
  logical_case_id: string
  session_id?: string
  lock_key: string
  generation: number
  created_at: string
  updated_at: string
  segments: TraceSegmentDescriptor[]
}

export type TraceSegment = {
  logicalCaseID: string
  logicalRoot: string
  segmentDir: string
  segmentFile: string
  sessionFile: string
  lockKey: string
  descriptor: TraceSegmentDescriptor
  bindSessionID(sessionID: string): boolean
  finalize(status: Exclude<TraceSegmentStatus, "running" | "interrupted_unfinalized">): boolean
}

type TraceSegmentLockOwner = {
  pid: number
  nonce: string
  acquired_at_ms: number
}

const DEFAULT_LOCK_WAIT_MS = 5_000
const DEFAULT_LOCK_STALE_MS = 30_000
const LOCK_POLL_MS = 10

function isRecord(input: unknown): input is Record<string, unknown> {
  return Boolean(input) && typeof input === "object" && !Array.isArray(input)
}

function readSessionManifest(file: string): TraceSessionManifest | undefined {
  if (!fs.existsSync(file)) return undefined
  const manifest = JSON.parse(fs.readFileSync(file, "utf8")) as unknown
  if (
    !isRecord(manifest) ||
    manifest.schema_version !== "1.0" ||
    typeof manifest.logical_case_id !== "string" ||
    typeof manifest.created_at !== "string" ||
    !Array.isArray(manifest.segments)
  )
    throw new Error(`${file}: invalid trace session manifest`)
  const session = manifest as unknown as TraceSessionManifest
  return {
    ...session,
    lock_key:
      typeof session.lock_key === "string" && session.lock_key
        ? session.lock_key
        : session.session_id
          ? `session:${session.session_id}`
          : `case:${session.logical_case_id}`,
    generation: Number.isSafeInteger(session.generation) && session.generation >= 0 ? session.generation : 0,
  }
}

function readFirstJournalEntry(file: string) {
  const handle = fs.openSync(file, "r")
  const chunk = Buffer.allocUnsafe(64 * 1024)
  const parts: Buffer[] = []
  try {
    while (true) {
      const bytes = fs.readSync(handle, chunk, 0, chunk.length, null)
      if (!bytes) break
      const newline = chunk.indexOf(0x0a, 0)
      if (newline !== -1 && newline < bytes) {
        parts.push(Buffer.from(chunk.subarray(0, newline)))
        break
      }
      parts.push(Buffer.from(chunk.subarray(0, bytes)))
    }
  } finally {
    fs.closeSync(handle)
  }
  const line = Buffer.concat(parts).toString("utf8").replace(/\r$/, "")
  const entry = JSON.parse(line) as unknown
  if (!isRecord(entry) || typeof entry.run_id !== "string" || typeof entry.case_id !== "string")
    throw new Error(`${file}: invalid legacy journal identity`)
  return entry
}

function readLastJournalEntry(file: string) {
  const handle = fs.openSync(file, "r")
  try {
    const size = fs.fstatSync(handle).size
    const chunk = Buffer.allocUnsafe(64 * 1024)
    const trailingByte = Buffer.allocUnsafe(1)
    let candidateEnd = size
    const parseCandidate = (start: number, initialEnd: number) => {
      let end = initialEnd
      while (end > start) {
        fs.readSync(handle, trailingByte, 0, 1, end - 1)
        if (trailingByte[0] !== 0x0d && trailingByte[0] !== 0x0a) break
        end -= 1
      }
      if (end <= start) return undefined
      const line = Buffer.allocUnsafe(end - start)
      fs.readSync(handle, line, 0, line.length, start)
      try {
        const entry = JSON.parse(line.toString("utf8")) as unknown
        if (isRecord(entry)) return entry
      } catch {}
      return undefined
    }

    for (let position = size; position > 0; ) {
      const start = Math.max(0, position - chunk.length)
      const bytes = position - start
      fs.readSync(handle, chunk, 0, bytes, start)
      for (let index = bytes - 1; index >= 0; index -= 1) {
        if (chunk[index] !== 0x0a) continue
        const parsed = parseCandidate(start + index + 1, candidateEnd)
        if (parsed) return parsed
        candidateEnd = start + index
      }
      position = start
    }
    return parseCandidate(0, candidateEnd)
  } finally {
    fs.closeSync(handle)
  }
}

function legacyRootDescriptor(logicalRoot: string, sessionID: string | undefined): TraceSegmentDescriptor | undefined {
  const recordsFile = path.join(logicalRoot, "records.jsonl")
  if (!fs.existsSync(recordsFile)) return undefined
  const first = readFirstJournalEntry(recordsFile)
  const last = readLastJournalEntry(recordsFile)
  const terminal = last?.operation === "case.runtime_closed" || last?.operation === "case.finalized"
  const closeData = isRecord(last?.data) ? last.data : undefined
  const closeStatus = closeData?.status
  const status: TraceSegmentStatus = !terminal
    ? "interrupted_unfinalized"
    : closeStatus === "error"
      ? "failed"
      : closeStatus === "cancelled"
        ? "cancelled"
        : "completed"
  return {
    segment_id: "legacy-root",
    run_id: first.run_id as string,
    case_id: first.case_id as string,
    session_id: sessionID,
    path: ".",
    records: "records.jsonl",
    artifacts: "artifacts",
    index: "index.sqlite",
    started_at:
      typeof first.time === "string" ? first.time : new Date(fs.statSync(recordsFile).birthtimeMs).toISOString(),
    status,
  }
}

function safePart(input: string, label: string) {
  if (/^[a-zA-Z0-9._-]{1,160}$/.test(input) && input !== "." && input !== "..") return input
  const readable =
    input
      .replace(/[^a-zA-Z0-9._-]+/g, "_")
      .replace(/^\.+$/, "")
      .slice(0, 120) || label
  const digest = crypto.createHash("sha256").update(input).digest("hex").slice(0, 16)
  return `${readable}--${digest}`
}

function boundedEnvironmentMilliseconds(name: string, fallback: number) {
  const parsed = Number(process.env[name])
  return Number.isSafeInteger(parsed) && parsed >= 0 ? Math.min(parsed, 300_000) : fallback
}

function traceSegmentLockDirectory(rootDir: string, key: string) {
  const digest = crypto.createHash("sha256").update(key).digest("hex").slice(0, 32)
  return path.join(path.dirname(rootDir), `.${path.basename(rootDir)}.trace-session-locks`, `${digest}.lock`)
}

function readLockOwner(lockDir: string): TraceSegmentLockOwner | undefined {
  try {
    const input = JSON.parse(fs.readFileSync(path.join(lockDir, "owner.json"), "utf8")) as unknown
    if (
      !isRecord(input) ||
      !Number.isSafeInteger(input.pid) ||
      typeof input.nonce !== "string" ||
      !Number.isFinite(input.acquired_at_ms)
    )
      return undefined
    return input as TraceSegmentLockOwner
  } catch {
    return undefined
  }
}

function processIsAlive(pid: number) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM"
  }
}

function lockAgeMilliseconds(lockDir: string, owner: TraceSegmentLockOwner | undefined) {
  if (owner) return Math.max(0, Date.now() - owner.acquired_at_ms)
  try {
    return Math.max(0, Date.now() - fs.statSync(lockDir).mtimeMs)
  } catch {
    return 0
  }
}

function recoverStaleLock(lockDir: string, staleMilliseconds: number) {
  const owner = readLockOwner(lockDir)
  if (owner && processIsAlive(owner.pid)) return false
  if (!owner && lockAgeMilliseconds(lockDir, owner) <= staleMilliseconds) return false
  const recovered = `${lockDir}.stale.${process.pid}.${crypto.randomUUID()}`
  try {
    fs.renameSync(lockDir, recovered)
  } catch (error) {
    if (["ENOENT", "EEXIST", "ENOTEMPTY"].includes((error as NodeJS.ErrnoException).code ?? "")) return true
    throw error
  }
  fs.rmSync(recovered, { recursive: true, force: true })
  return true
}

function waitSynchronously(milliseconds: number) {
  if (milliseconds <= 0) return
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds)
}

export function acquireTraceSessionLock(rootDir: string, key: string) {
  const lockDir = traceSegmentLockDirectory(rootDir, key)
  const waitMilliseconds = boundedEnvironmentMilliseconds("OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS", DEFAULT_LOCK_WAIT_MS)
  const staleMilliseconds = boundedEnvironmentMilliseconds(
    "OPENCODE_TRACE_SEGMENT_LOCK_STALE_MS",
    DEFAULT_LOCK_STALE_MS,
  )
  const deadline = Date.now() + waitMilliseconds
  fs.mkdirSync(path.dirname(lockDir), { recursive: true })

  while (true) {
    const owner: TraceSegmentLockOwner = {
      pid: process.pid,
      nonce: crypto.randomUUID(),
      acquired_at_ms: Date.now(),
    }
    try {
      fs.mkdirSync(lockDir)
      try {
        fs.writeFileSync(path.join(lockDir, "owner.json"), JSON.stringify(owner) + "\n", { flag: "wx" })
      } catch (error) {
        fs.rmSync(lockDir, { recursive: true, force: true })
        throw error
      }
      let released = false
      return () => {
        if (released) return
        const current = readLockOwner(lockDir)
        if (current?.nonce !== owner.nonce) return
        const tombstone = `${lockDir}.released.${owner.nonce}`
        try {
          fs.renameSync(lockDir, tombstone)
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code === "ENOENT") return
          throw error
        }
        const moved = readLockOwner(tombstone)
        if (moved?.nonce !== owner.nonce) {
          try {
            fs.renameSync(tombstone, lockDir)
          } catch {}
          return
        }
        released = true
        fs.rmSync(tombstone, { recursive: true, force: true })
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
    }

    if (recoverStaleLock(lockDir, staleMilliseconds)) continue
    if (Date.now() >= deadline) throw new Error(`${lockDir}: timed out waiting for trace session lock`)
    waitSynchronously(Math.min(LOCK_POLL_MS, Math.max(1, deadline - Date.now())))
  }
}

export function withTraceSessionLock<T>(rootDir: string, key: string, operation: () => T) {
  const release = acquireTraceSessionLock(rootDir, key)
  try {
    return operation()
  } finally {
    release()
  }
}

function resolveLogicalRoot(rootDir: string, requestedCaseID: string, sessionID: string | undefined) {
  const fallback = path.join(rootDir, requestedCaseID)
  if (!sessionID || !fs.existsSync(rootDir)) return fallback
  const matches: string[] = []
  for (const entry of fs.readdirSync(rootDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const candidate = path.join(rootDir, entry.name)
    try {
      if (readSessionManifest(path.join(candidate, "session.json"))?.session_id === sessionID) matches.push(candidate)
    } catch {}
  }
  if (matches.length > 1) throw new Error(`${rootDir}: session ${sessionID} has multiple logical trace roots`)
  return matches[0] ?? fallback
}

function fsyncDirectory(directory: string) {
  const handle = fs.openSync(directory, "r")
  try {
    fs.fsyncSync(handle)
  } finally {
    fs.closeSync(handle)
  }
}

function writeJsonAtomic(file: string, value: unknown) {
  const temporary = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${crypto.randomUUID()}.tmp`)
  let handle: number | undefined
  try {
    handle = fs.openSync(temporary, "wx")
    fs.writeFileSync(handle, JSON.stringify(value, undefined, 2) + "\n")
    fs.fsyncSync(handle)
    fs.closeSync(handle)
    handle = undefined
    fs.renameSync(temporary, file)
    try {
      fsyncDirectory(path.dirname(file))
    } catch {}
  } catch (error) {
    if (handle !== undefined) {
      try {
        fs.closeSync(handle)
      } catch {}
    }
    try {
      fs.unlinkSync(temporary)
    } catch {}
    throw error
  }
}

function replaceDescriptor(
  sessionFile: string,
  segmentID: string,
  update: (manifest: TraceSessionManifest, descriptor: TraceSegmentDescriptor) => TraceSessionManifest,
) {
  const manifest = readSessionManifest(sessionFile)
  if (!manifest) return false
  const descriptor = manifest.segments.find((item) => item.segment_id === segmentID)
  if (!descriptor) return false
  writeJsonAtomic(sessionFile, update(manifest, descriptor))
  return true
}

export function openTraceSegment(input: {
  rootDir: string
  logicalCaseID: string
  sessionID?: string
  runID: string
}): TraceSegment {
  const rootDir = path.resolve(input.rootDir)
  const requestedCaseID = safePart(input.logicalCaseID, "case")
  let lockKey = input.sessionID ? `session:${input.sessionID}` : `case:${requestedCaseID}`
  while (true) {
    const opened = withTraceSessionLock(rootDir, lockKey, () => {
      const logicalRoot = resolveLogicalRoot(rootDir, requestedCaseID, input.sessionID)
      const sessionFile = path.join(logicalRoot, "session.json")
      const segmentsDir = path.join(logicalRoot, "segments")
      fs.mkdirSync(segmentsDir, { recursive: true })

      const current = readSessionManifest(sessionFile)
      if (current?.lock_key && current.lock_key !== lockKey) return { retryLockKey: current.lock_key } as const
      const logicalCaseID = current?.logical_case_id ?? requestedCaseID
      if (current?.session_id && input.sessionID && current.session_id !== input.sessionID)
        throw new Error(`${sessionFile}: session identity changed`)
      const legacy = current?.segments.some((segment) => segment.segment_id === "legacy-root")
        ? undefined
        : legacyRootDescriptor(logicalRoot, input.sessionID ?? current?.session_id)
      const existingSegments = [...(legacy ? [legacy] : []), ...(current?.segments ?? [])]
      if (existingSegments.some((segment) => segment.run_id === input.runID))
        throw new Error(`${sessionFile}: run ${input.runID} already exists`)
      const baseSegmentID = safePart(input.runID, "run")
      let ordinal = 1
      let segmentID = baseSegmentID
      let segmentDir = path.join(segmentsDir, segmentID)
      while (true) {
        try {
          fs.mkdirSync(segmentDir)
          break
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
          segmentID = `${baseSegmentID}-${++ordinal}`
          segmentDir = path.join(segmentsDir, segmentID)
        }
      }

      const now = new Date().toISOString()
      const relative = path.relative(logicalRoot, segmentDir)
      const previous = existingSegments.at(-1)
      const descriptor: TraceSegmentDescriptor = {
        segment_id: segmentID,
        run_id: input.runID,
        case_id: logicalCaseID,
        session_id: input.sessionID ?? current?.session_id,
        path: relative,
        records: path.join(relative, "records.jsonl"),
        artifacts: path.join(relative, "artifacts"),
        index: path.join(relative, "index.sqlite"),
        started_at: now,
        status: "running",
        ...(previous ? { continuation_of: previous.run_id } : {}),
      }
      const segments = existingSegments.map((segment, index, items) =>
        index === items.length - 1 && segment.status === "running"
          ? { ...segment, status: "interrupted_unfinalized" as const }
          : segment,
      )
      const manifest: TraceSessionManifest = {
        schema_version: "1.0",
        logical_case_id: logicalCaseID,
        session_id: input.sessionID ?? current?.session_id,
        lock_key: lockKey,
        generation: (current?.generation ?? 0) + 1,
        created_at: current?.created_at ?? now,
        updated_at: now,
        segments: [...segments, descriptor],
      }
      const segmentFile = path.join(segmentDir, "segment.json")
      try {
        writeJsonAtomic(segmentFile, descriptor)
        fsyncDirectory(segmentsDir)
        writeJsonAtomic(sessionFile, manifest)
      } catch (error) {
        fs.rmSync(segmentDir, { recursive: true, force: true })
        throw error
      }

      return {
        segment: {
          logicalCaseID,
          logicalRoot,
          segmentDir,
          segmentFile,
          sessionFile,
          lockKey,
          descriptor,
          bindSessionID(sessionID) {
            try {
              return withTraceSessionLock(rootDir, lockKey, () =>
                replaceDescriptor(sessionFile, segmentID, (latest) => {
                  if (latest.session_id && latest.session_id !== sessionID)
                    throw new Error(`${sessionFile}: session identity changed`)
                  return {
                    ...latest,
                    session_id: sessionID,
                    generation: latest.generation + 1,
                    updated_at: new Date().toISOString(),
                    segments: latest.segments.map((item) =>
                      item.segment_id === segmentID ? { ...item, session_id: sessionID } : item,
                    ),
                  }
                }),
              )
            } catch {
              return false
            }
          },
          finalize(status) {
            try {
              return withTraceSessionLock(rootDir, lockKey, () =>
                replaceDescriptor(sessionFile, segmentID, (latest) => ({
                  ...latest,
                  generation: latest.generation + 1,
                  updated_at: new Date().toISOString(),
                  segments: latest.segments.map((item) => (item.segment_id === segmentID ? { ...item, status } : item)),
                })),
              )
            } catch {
              return false
            }
          },
        } as TraceSegment,
      } as const
    }) as { segment: TraceSegment } | { retryLockKey: string }
    if ("retryLockKey" in opened) {
      lockKey = opened.retryLockKey
      continue
    }
    return opened.segment
  }
}
