import { expect, test } from "bun:test"
import { createHash, randomUUID } from "node:crypto"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { CausalIRStore, type CausalIRJournalEntry } from "@/observability/causal-ir"
import { materializeTrace } from "@/observability/trace-materializer"
import {
  acquireTraceSessionLock,
  openTraceSegment,
  readTraceSessionManifest,
  relativeTraceManifestPath,
  resolveTraceManifestPath,
} from "@/observability/trace-segment"

const packageDir = path.resolve(import.meta.dir, "../..")
const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
const segmentModule = pathToFileURL(path.join(packageDir, "src/observability/trace-segment.ts")).href
const materializerModule = pathToFileURL(path.join(packageDir, "src/observability/trace-materializer.ts")).href
const globalManifestLockKey = "trace-root-manifest-v1"

async function waitForFile(file: string) {
  for (let attempt = 0; attempt < 200; attempt++) {
    try {
      return JSON.parse(await fs.readFile(file, "utf8")) as { caseDir: string; recordsFile: string }
    } catch {
      await Bun.sleep(10)
    }
  }
  throw new Error(`timed out waiting for ${file}`)
}

async function startKilledRun(input: {
  traceRoot: string
  readyFile: string
  marker: string
  caseID?: string
  sessionID?: string
  configureSession?: boolean
}) {
  const script = path.join(input.traceRoot, `${input.marker}.ts`)
  const sessionID = input.sessionID ?? "ses_immutable_resume"
  const config = {
    ...(input.caseID === undefined ? {} : { caseID: input.caseID }),
    ...(input.configureSession ? { sessionID } : {}),
  }
  await fs.writeFile(
    script,
    [
      `import fs from "node:fs"`,
      `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
      `const trace = CaseTrace.configure(${JSON.stringify(config)} as any) as any`,
      `CaseTrace.setSessionID(${JSON.stringify(sessionID)})`,
      `CaseTrace.node({ node_id: "observation_${input.marker}", kind: "execution.observation", component: "tool", data: { output: ${JSON.stringify(`${input.marker}:`)} + "x".repeat(8192) } })`,
      `fs.writeFileSync(${JSON.stringify(input.readyFile)}, JSON.stringify({ caseDir: trace.caseDir, recordsFile: trace.recordsFile }))`,
      `setInterval(() => {}, 1000)`,
    ].join("\n"),
  )
  const child = Bun.spawn([process.execPath, script], {
    cwd: packageDir,
    env: {
      ...globalThis.process.env,
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_CASE_TRACE_DIR: input.traceRoot,
      OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "64",
      OPENCODE_CASE_TRACE_QUIET: "1",
    },
    stdout: "ignore",
    stderr: "pipe",
  })
  const ready = await waitForFile(input.readyFile)
  child.kill("SIGKILL")
  const exitCode = await child.exited
  const stderr = await new Response(child.stderr).text()
  expect(exitCode).not.toBe(0)
  expect(stderr).toBe("")
  return ready
}

async function sha256(file: string) {
  return createHash("sha256")
    .update(await fs.readFile(file))
    .digest("hex")
}

async function treeHash(root: string) {
  const hash = createHash("sha256")
  const visit = async (directory: string) => {
    const entries = (await fs.readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))
    for (const entry of entries) {
      const file = path.join(directory, entry.name)
      const relative = path.relative(root, file).replaceAll(path.sep, "/")
      if (entry.isDirectory()) {
        hash.update(`directory:${relative}\n`)
        await visit(file)
      } else if (entry.isSymbolicLink()) {
        hash.update(`symlink:${relative}:${await fs.readlink(file)}\n`)
      } else if (entry.isFile()) {
        hash.update(`file:${relative}:`)
        hash.update(await fs.readFile(file))
        hash.update("\n")
      }
    }
  }
  await visit(root)
  return hash.digest("hex")
}

async function fileExists(file: string) {
  return fs
    .access(file)
    .then(() => true)
    .catch(() => false)
}

function lockDirectory(rootDir: string, key: string) {
  const digest = createHash("sha256").update(key).digest("hex").slice(0, 32)
  return path.join(path.dirname(rootDir), `.${path.basename(rootDir)}.trace-session-locks`, `${digest}.lock`)
}

async function openSegmentsConcurrently(input: {
  traceRoot: string
  sessionID: string
  count: number
  caseID: (index: number) => string
  runID: (index: number) => string
}) {
  const script = path.join(input.traceRoot, "concurrent-open.ts")
  const gate = path.join(input.traceRoot, "concurrent-open.gate")
  await fs.writeFile(
    script,
    [
      `import fs from "node:fs"`,
      `import { openTraceSegment } from ${JSON.stringify(segmentModule)}`,
      `const request = JSON.parse(process.argv[2])`,
      `fs.writeFileSync(request.readyFile, "ready")`,
      `while (!fs.existsSync(request.gate)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2)`,
      `const segment = openTraceSegment(request.input)`,
      `fs.writeFileSync(request.outputFile, JSON.stringify({ logicalRoot: segment.logicalRoot, segmentDir: segment.segmentDir, descriptor: segment.descriptor }))`,
    ].join("\n"),
  )
  const children = Array.from({ length: input.count }, (_, index) => {
    const readyFile = path.join(input.traceRoot, `concurrent-${index}.ready`)
    const outputFile = path.join(input.traceRoot, `concurrent-${index}.json`)
    const request = {
      readyFile,
      outputFile,
      gate,
      input: {
        rootDir: input.traceRoot,
        logicalCaseID: input.caseID(index),
        sessionID: input.sessionID,
        runID: input.runID(index),
      },
    }
    return {
      readyFile,
      outputFile,
      child: Bun.spawn([process.execPath, script, JSON.stringify(request)], {
        cwd: packageDir,
        stdout: "pipe",
        stderr: "pipe",
      }),
    }
  })
  for (const item of children) expect(await waitForFilePresence(item.readyFile)).toBe(true)
  await fs.writeFile(gate, "go")
  const output = []
  for (const item of children) {
    expect(await item.child.exited).toBe(0)
    expect(await new Response(item.child.stderr).text()).toBe("")
    output.push(JSON.parse(await fs.readFile(item.outputFile, "utf8")) as any)
  }
  return output
}

async function bindUnknownSegmentsConcurrently(input: { traceRoot: string; sessionIDs: [string, string] }) {
  const invocation = randomUUID()
  const script = path.join(input.traceRoot, `concurrent-bind-${randomUUID()}.ts`)
  const gate = path.join(input.traceRoot, `concurrent-bind-${randomUUID()}.gate`)
  await fs.writeFile(
    script,
    [
      `import fs from "node:fs"`,
      `import { openTraceSegment } from ${JSON.stringify(segmentModule)}`,
      `const request = JSON.parse(process.argv[2])`,
      `const segment = openTraceSegment(request.input)`,
      `fs.writeFileSync(request.readyFile, JSON.stringify({ logicalRoot: segment.logicalRoot }))`,
      `while (!fs.existsSync(request.gate)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2)`,
      `const bound = segment.bindSessionID(request.sessionID)`,
      `fs.writeFileSync(request.outputFile, JSON.stringify({ bound, logicalRoot: segment.logicalRoot }))`,
    ].join("\n"),
  )
  const children = input.sessionIDs.map((sessionID, index) => {
    const readyFile = path.join(input.traceRoot, `bind-${index}.ready.json`)
    const outputFile = path.join(input.traceRoot, `bind-${index}.output.json`)
    const request = {
      readyFile,
      outputFile,
      gate,
      sessionID,
      input: {
        rootDir: input.traceRoot,
        logicalCaseID: `unknown-bind-${invocation}-${index}`,
        runID: `run_unknown_bind_${invocation}_${index}`,
      },
    }
    return {
      readyFile,
      outputFile,
      child: Bun.spawn([process.execPath, script, JSON.stringify(request)], {
        cwd: packageDir,
        stdout: "pipe",
        stderr: "pipe",
      }),
    }
  })
  for (const child of children) expect(await waitForFilePresence(child.readyFile)).toBe(true)
  await fs.writeFile(gate, "go")
  return Promise.all(
    children.map(async (child) => {
      const exitCode = await child.child.exited
      const stderr = await new Response(child.child.stderr).text()
      expect(exitCode, stderr).toBe(0)
      expect(stderr).toBe("")
      return JSON.parse(await fs.readFile(child.outputFile, "utf8")) as { bound: boolean; logicalRoot: string }
    }),
  )
}

async function waitForFilePresence(file: string, timeoutMs = 5000) {
  const started = Date.now()
  while (Date.now() - started < timeoutMs) {
    if (await fileExists(file)) return true
    await Bun.sleep(5)
  }
  return fileExists(file)
}

async function artifactHashes(segmentDir: string) {
  const artifactDir = path.join(segmentDir, "artifacts", "sha256")
  const files = (await fs.readdir(artifactDir)).sort()
  return Promise.all(files.map(async (file) => [file, await sha256(path.join(artifactDir, file))] as const))
}

test("reopening a SIGKILLed session allocates a new segment without changing prior evidence bytes", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-immutable-resume-"))
  try {
    const first = await startKilledRun({
      traceRoot,
      readyFile: path.join(traceRoot, "first.ready.json"),
      marker: "first",
      caseID: "immutable-resume",
    })
    const firstJournalHash = await sha256(first.recordsFile)
    const firstArtifacts = await artifactHashes(first.caseDir)
    const firstIndexHash = await sha256(path.join(first.caseDir, "index.sqlite"))

    const second = await startKilledRun({
      traceRoot,
      readyFile: path.join(traceRoot, "second.ready.json"),
      marker: "second",
      caseID: "immutable-resume",
    })

    expect(await sha256(first.recordsFile)).toBe(firstJournalHash)
    expect(await artifactHashes(first.caseDir)).toEqual(firstArtifacts)
    expect(await sha256(path.join(first.caseDir, "index.sqlite"))).toBe(firstIndexHash)
    expect(second.caseDir).not.toBe(first.caseDir)

    const logicalRoot = path.join(traceRoot, "immutable-resume")
    const session = JSON.parse(await fs.readFile(path.join(logicalRoot, "session.json"), "utf8")) as any
    expect(session).toMatchObject({
      logical_case_id: "immutable-resume",
      session_id: "ses_immutable_resume",
      segments: [
        {
          status: "interrupted_unfinalized",
          path: path.relative(logicalRoot, first.caseDir),
        },
        {
          status: "running",
          path: path.relative(logicalRoot, second.caseDir),
        },
      ],
    })
    expect(typeof session.segments[0].run_id).toBe("string")
    expect(typeof session.segments[1].run_id).toBe("string")
    expect(session.segments[1].continuation_of).toBe(session.segments[0].run_id)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("a known session ID resolves the same logical root across separate processes without a case registry", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-session-root-discovery-"))
  try {
    const first = await startKilledRun({
      traceRoot,
      readyFile: path.join(traceRoot, "session-first.ready.json"),
      marker: "session-first",
      sessionID: "ses_cross_process_resume",
    })
    const second = await startKilledRun({
      traceRoot,
      readyFile: path.join(traceRoot, "session-second.ready.json"),
      marker: "session-second",
      sessionID: "ses_cross_process_resume",
      configureSession: true,
    })
    const firstLogicalRoot = path.dirname(path.dirname(first.caseDir))
    const secondLogicalRoot = path.dirname(path.dirname(second.caseDir))

    expect(secondLogicalRoot).toBe(firstLogicalRoot)
    expect(second.caseDir).not.toBe(first.caseDir)
    const session = JSON.parse(await fs.readFile(path.join(firstLogicalRoot, "session.json"), "utf8")) as any
    expect(session.session_id).toBe("ses_cross_process_resume")
    expect(session.segments).toHaveLength(2)
    expect(session.segments[1].continuation_of).toBe(session.segments[0].run_id)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("serializes concurrent first creators into one logical session root without losing segments", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-concurrent-first-segments-"))
  try {
    const count = 12
    const opened = await openSegmentsConcurrently({
      traceRoot,
      sessionID: "ses_concurrent_first",
      count,
      caseID: (index) => `concurrent-first-${index}`,
      runID: (index) => `run_concurrent_first_${index}`,
    })
    expect(new Set(opened.map((item) => item.logicalRoot)).size).toBe(1)
    expect(new Set(opened.map((item) => item.segmentDir)).size).toBe(count)

    const logicalRoot = opened[0].logicalRoot
    const session = JSON.parse(await fs.readFile(path.join(logicalRoot, "session.json"), "utf8")) as any
    expect(session.session_id).toBe("ses_concurrent_first")
    expect(session.segments).toHaveLength(count)
    expect(new Set(session.segments.map((segment: any) => segment.run_id)).size).toBe(count)
    for (let index = 1; index < session.segments.length; index++)
      expect(session.segments[index].continuation_of).toBe(session.segments[index - 1].run_id)
    expect(await fileExists(lockDirectory(traceRoot, globalManifestLockKey))).toBe(false)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("serializes concurrent resumes without lost descriptors or changed prior evidence", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-concurrent-resume-segments-"))
  try {
    const first = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "concurrent-resume",
      sessionID: "ses_concurrent_resume",
      runID: "run_concurrent_resume_initial",
    })
    await writeClosedSegment(first, {
      runID: "run_concurrent_resume_initial",
      marker: "initial",
      caseID: "concurrent-resume",
    })
    await fs.writeFile(path.join(first.segmentDir, "index.sqlite"), "immutable initial index")
    const oldHashes = {
      records: await sha256(path.join(first.segmentDir, "records.jsonl")),
      artifact: await sha256(path.join(first.segmentDir, "artifacts", "fact.txt")),
      index: await sha256(path.join(first.segmentDir, "index.sqlite")),
    }

    const count = 12
    const opened = await openSegmentsConcurrently({
      traceRoot,
      sessionID: "ses_concurrent_resume",
      count,
      caseID: (index) => `ignored-concurrent-resume-${index}`,
      runID: (index) => `run_concurrent_resume_${index}`,
    })
    expect(new Set(opened.map((item) => item.logicalRoot))).toEqual(new Set([first.logicalRoot]))
    const session = JSON.parse(await fs.readFile(first.sessionFile, "utf8")) as any
    expect(session.segments).toHaveLength(count + 1)
    expect(new Set(session.segments.map((segment: any) => segment.run_id)).size).toBe(count + 1)
    for (let index = 1; index < session.segments.length; index++)
      expect(session.segments[index].continuation_of).toBe(session.segments[index - 1].run_id)
    expect(await sha256(path.join(first.segmentDir, "records.jsonl"))).toBe(oldHashes.records)
    expect(await sha256(path.join(first.segmentDir, "artifacts", "fact.txt"))).toBe(oldHashes.artifact)
    expect(await sha256(path.join(first.segmentDir, "index.sqlite"))).toBe(oldHashes.index)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("recovers a stale session lock and always removes its ownership directory", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-stale-segment-lock-"))
  const sessionID = "ses_stale_lock"
  const lock = lockDirectory(traceRoot, globalManifestLockKey)
  try {
    await fs.mkdir(lock, { recursive: true })
    await fs.writeFile(
      path.join(lock, "owner.json"),
      JSON.stringify({ pid: 99999999, nonce: "dead-owner", acquired_at_ms: Date.now() - 60_000 }),
    )
    const segment = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "stale-lock-case",
      sessionID,
      runID: "run_after_stale_lock",
    })
    expect(segment.descriptor.run_id).toBe("run_after_stale_lock")
    expect(await fileExists(lock)).toBe(false)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
    await fs.rm(path.dirname(lock), { recursive: true, force: true })
  }
})

test("keeps the observer passive when a live session lock exceeds its bounded wait", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-live-segment-lock-"))
  const sessionID = "ses_live_lock"
  const lock = lockDirectory(traceRoot, globalManifestLockKey)
  const script = path.join(traceRoot, "passive-lock.ts")
  try {
    await fs.mkdir(lock, { recursive: true })
    await fs.writeFile(
      path.join(lock, "owner.json"),
      JSON.stringify({ pid: process.pid, nonce: "live-owner", acquired_at_ms: Date.now() }),
    )
    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const trace = CaseTrace.configure({ caseID: "passive-lock-case", sessionID: ${JSON.stringify(sessionID)} }) as any`,
        `CaseTrace.node({ node_id: "passive_lock_node", kind: "execution.observation", component: "test", data: { ok: true } })`,
        `process.stdout.write(JSON.stringify({ writable: trace.writable, caseDir: trace.caseDir }))`,
      ].join("\n"),
    )
    const child = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: traceRoot,
        OPENCODE_CASE_TRACE_QUIET: "1",
        OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS: "40",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await child.exited).toBe(0)
    expect(await new Response(child.stderr).text()).toBe("")
    expect(JSON.parse(await new Response(child.stdout).text())).toMatchObject({ writable: false })
    expect(await fileExists(path.join(traceRoot, "passive-lock-case", "session.json"))).toBe(false)
    expect(await fileExists(lock)).toBe(true)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
    await fs.rm(path.dirname(lock), { recursive: true, force: true })
  }
})

test("lock timeout over a legacy root disables persistence without changing or adding bytes", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-legacy-lock-timeout-"))
  const logicalRoot = path.join(traceRoot, "legacy-lock-case")
  const lock = lockDirectory(traceRoot, globalManifestLockKey)
  const script = path.join(traceRoot, "legacy-lock-timeout.ts")
  const files = [
    "records.jsonl",
    "index.sqlite",
    "artifacts/sha256/legacy.txt",
    "trace.json",
    "manifest.json",
    "legacy-trace.json",
    "provenance-trace.json",
    "partial/latest.json",
  ]
  try {
    await fs.mkdir(path.join(logicalRoot, "artifacts", "sha256"), { recursive: true })
    await fs.mkdir(path.join(logicalRoot, "partial"), { recursive: true })
    for (const relative of files) {
      const target = path.join(logicalRoot, relative)
      await fs.mkdir(path.dirname(target), { recursive: true })
      await fs.writeFile(target, `immutable:${relative}`)
    }
    const before = new Map(
      await Promise.all(files.map(async (file) => [file, await sha256(path.join(logicalRoot, file))] as const)),
    )
    await fs.mkdir(lock, { recursive: true })
    await fs.writeFile(
      path.join(lock, "owner.json"),
      JSON.stringify({ pid: process.pid, nonce: "legacy-live-owner", acquired_at_ms: Date.now() }),
    )
    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const trace = CaseTrace.configure({ caseID: "legacy-lock-case" }) as any`,
        `CaseTrace.node({ node_id: "must_stay_memory_only", kind: "execution.observation", component: "test", data: { payload: "x".repeat(4096) } })`,
        `CaseTrace.finish({ status: "success", result: { persisted: false } })`,
        `process.stdout.write(JSON.stringify({ writable: trace.writable, persistenceEnabled: trace.persistenceEnabled }))`,
      ].join("\n"),
    )
    const child = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: traceRoot,
        OPENCODE_CASE_TRACE_QUIET: "1",
        OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS: "20",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await child.exited).toBe(0)
    expect(await new Response(child.stderr).text()).toBe("")
    expect(JSON.parse(await new Response(child.stdout).text())).toEqual({
      writable: false,
      persistenceEnabled: false,
    })
    for (const relative of files) expect(await sha256(path.join(logicalRoot, relative))).toBe(before.get(relative)!)
    for (const relative of ["events.jsonl", "raw-events.jsonl", "segments", "session.json"])
      expect(await fileExists(path.join(logicalRoot, relative))).toBe(false)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
    await fs.rm(path.dirname(lock), { recursive: true, force: true })
  }
})

test("does not stale-evict an aged owner whose PID is still alive", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-aged-live-lock-"))
  const sessionID = "ses_aged_live"
  const lock = lockDirectory(traceRoot, globalManifestLockKey)
  const previousWait = process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
  const previousStale = process.env.OPENCODE_TRACE_SEGMENT_LOCK_STALE_MS
  try {
    process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = "20"
    process.env.OPENCODE_TRACE_SEGMENT_LOCK_STALE_MS = "1"
    await fs.mkdir(lock, { recursive: true })
    await fs.writeFile(
      path.join(lock, "owner.json"),
      JSON.stringify({ pid: process.pid, nonce: "aged-live-owner", acquired_at_ms: 1 }),
    )
    expect(() =>
      openTraceSegment({
        rootDir: traceRoot,
        logicalCaseID: "aged-live-case",
        sessionID,
        runID: "run_must_not_evict",
      }),
    ).toThrow("timed out waiting for trace session lock")
    expect(JSON.parse(await fs.readFile(path.join(lock, "owner.json"), "utf8"))).toMatchObject({
      nonce: "aged-live-owner",
    })
  } finally {
    if (previousWait === undefined) delete process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
    else process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = previousWait
    if (previousStale === undefined) delete process.env.OPENCODE_TRACE_SEGMENT_LOCK_STALE_MS
    else process.env.OPENCODE_TRACE_SEGMENT_LOCK_STALE_MS = previousStale
    await fs.rm(traceRoot, { recursive: true, force: true })
    await fs.rm(path.dirname(lock), { recursive: true, force: true })
  }
})

test("late session binding keeps the allocation lock key for competing identities", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-stable-bind-lock-"))
  const lock = lockDirectory(traceRoot, globalManifestLockKey)
  const previousWait = process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
  try {
    const segment = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "stable-bind-case",
      runID: "run_stable_bind",
    })
    const initial = JSON.parse(await fs.readFile(segment.sessionFile, "utf8")) as any
    expect(initial.lock_key).toBe(globalManifestLockKey)
    await fs.mkdir(lock, { recursive: true })
    await fs.writeFile(
      path.join(lock, "owner.json"),
      JSON.stringify({ pid: process.pid, nonce: "competing-owner", acquired_at_ms: Date.now() }),
    )
    process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = "0"
    expect(segment.bindSessionID("ses_late_a")).toBe(false)
    expect((JSON.parse(await fs.readFile(segment.sessionFile, "utf8")) as any).session_id).toBeUndefined()
    await fs.rm(lock, { recursive: true, force: true })
    expect(segment.bindSessionID("ses_late_a")).toBe(true)
    expect(segment.bindSessionID("ses_late_b")).toBe(false)
    expect((JSON.parse(await fs.readFile(segment.sessionFile, "utf8")) as any).session_id).toBe("ses_late_a")
  } finally {
    if (previousWait === undefined) delete process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
    else process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = previousWait
    await fs.rm(traceRoot, { recursive: true, force: true })
    await fs.rm(path.dirname(lock), { recursive: true, force: true })
  }
})

test("keeps ActiveCaseTrace unbound when the manifest bind cannot acquire the global lock", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-active-bind-timeout-"))
  const script = path.join(traceRoot, "active-bind-timeout.ts")
  const readyFile = path.join(traceRoot, "active-bind.ready.json")
  const gate = path.join(traceRoot, "active-bind.gate")
  const outputFile = path.join(traceRoot, "active-bind.output.json")
  let release: (() => void) | undefined
  try {
    await fs.writeFile(
      script,
      [
        `import fs from "node:fs"`,
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const trace = CaseTrace.configure({ caseID: "active-bind-timeout" }) as any`,
        `fs.writeFileSync(${JSON.stringify(readyFile)}, JSON.stringify({ sessionFile: trace.segment.sessionFile, recordsFile: trace.recordsFile }))`,
        `while (!fs.existsSync(${JSON.stringify(gate)})) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2)`,
        `const bound = trace.setSessionID("ses_active_bind_timeout")`,
        `const manifest = JSON.parse(fs.readFileSync(trace.segment.sessionFile, "utf8"))`,
        `const journal = fs.existsSync(trace.recordsFile) ? fs.readFileSync(trace.recordsFile, "utf8") : ""`,
        `fs.writeFileSync(${JSON.stringify(outputFile)}, JSON.stringify({ bound, sessionID: trace.sessionID, manifestSessionID: manifest.session_id, journal }))`,
      ].join("\n"),
    )
    const child = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: traceRoot,
        OPENCODE_CASE_TRACE_QUIET: "1",
        OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS: "20",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await waitForFilePresence(readyFile)).toBe(true)
    release = acquireTraceSessionLock(traceRoot, globalManifestLockKey)
    await fs.writeFile(gate, "go")
    expect(await waitForFilePresence(outputFile)).toBe(true)
    release()
    release = undefined
    expect(await child.exited).toBe(0)
    expect(await new Response(child.stderr).text()).toBe("")
    const output = JSON.parse(await fs.readFile(outputFile, "utf8")) as any
    expect(output).toMatchObject({ bound: false })
    expect(output.sessionID).toBeUndefined()
    expect(output.manifestSessionID).toBeUndefined()
    expect(output.journal).not.toContain("ses_active_bind_timeout")
  } finally {
    release?.()
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("rejects malformed session and legacy roots without changing their trees", async () => {
  for (const fixture of [
    { name: "session", file: "session.json", contents: '{"schema_version":"broken"}\n' },
    { name: "legacy", file: "records.jsonl", contents: '{"not":"a causal journal"}\n' },
  ]) {
    const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), `opencode-malformed-${fixture.name}-`))
    const logicalRoot = path.join(traceRoot, "malformed-case")
    try {
      await fs.mkdir(path.join(logicalRoot, "preserved", "nested"), { recursive: true })
      await fs.writeFile(path.join(logicalRoot, fixture.file), fixture.contents)
      await fs.writeFile(path.join(logicalRoot, "preserved", "nested", "evidence.bin"), Buffer.from([0, 1, 2, 255]))
      const before = await treeHash(logicalRoot)

      expect(() =>
        openTraceSegment({
          rootDir: traceRoot,
          logicalCaseID: "malformed-case",
          sessionID: "ses_malformed",
          runID: `run_malformed_${fixture.name}`,
        }),
      ).toThrow()

      expect(await treeHash(logicalRoot)).toBe(before)
      expect(await fileExists(path.join(logicalRoot, "segments"))).toBe(false)
    } finally {
      await fs.rm(traceRoot, { recursive: true, force: true })
    }
  }
})

test("validates every session manifest and descriptor field before mutating a logical root", async () => {
  const validManifest = () => ({
    schema_version: "1.0",
    logical_case_id: "validated-case",
    session_id: "ses_validated",
    lock_key: globalManifestLockKey,
    generation: 1,
    created_at: "2026-08-15T00:00:00.000Z",
    updated_at: "2026-08-15T00:00:01.000Z",
    segments: [
      {
        segment_id: "run_existing",
        run_id: "run_existing",
        case_id: "validated-case",
        session_id: "ses_validated",
        path: "segments/run_existing",
        records: "segments/run_existing/records.jsonl",
        artifacts: "segments/run_existing/artifacts",
        index: "segments/run_existing/index.sqlite",
        started_at: "2026-08-15T00:00:00.000Z",
        status: "completed",
      },
    ],
  })
  const invalid: Array<[string, (manifest: any) => void]> = [
    ["logical_case_id", (manifest) => (manifest.logical_case_id = 7)],
    ["session_id", (manifest) => (manifest.session_id = 7)],
    ["lock_key", (manifest) => (manifest.lock_key = "case:old-domain")],
    ["generation", (manifest) => (manifest.generation = -1)],
    ["created_at", (manifest) => (manifest.created_at = "yesterday")],
    ["updated_at", (manifest) => (manifest.updated_at = 7)],
    ["timestamp_order", (manifest) => (manifest.updated_at = "2026-08-14T23:59:59.000Z")],
    ["segments", (manifest) => (manifest.segments = {})],
    ["empty_segments", (manifest) => (manifest.segments = [])],
    ["segment_id", (manifest) => (manifest.segments[0].segment_id = 7)],
    ["run_id", (manifest) => (manifest.segments[0].run_id = "")],
    ["case_id", (manifest) => (manifest.segments[0].case_id = 7)],
    ["descriptor_session_id", (manifest) => (manifest.segments[0].session_id = 7)],
    [
      "descriptor_session_without_manifest",
      (manifest) => {
        delete manifest.session_id
      },
    ],
    ["path", (manifest) => (manifest.segments[0].path = "../outside")],
    [
      "path_segment_mismatch",
      (manifest) => {
        manifest.segments[0].path = "segments/other"
        manifest.segments[0].records = "segments/other/records.jsonl"
        manifest.segments[0].artifacts = "segments/other/artifacts"
        manifest.segments[0].index = "segments/other/index.sqlite"
      },
    ],
    ["records", (manifest) => (manifest.segments[0].records = "/tmp/records.jsonl")],
    ["records_contract", (manifest) => (manifest.segments[0].records = "segments/run_existing/other.jsonl")],
    ["artifacts", (manifest) => delete manifest.segments[0].artifacts],
    ["index", (manifest) => (manifest.segments[0].index = 7)],
    ["started_at", (manifest) => (manifest.segments[0].started_at = "not-a-time")],
    ["status", (manifest) => (manifest.segments[0].status = "unknown")],
    ["continuation_of", (manifest) => (manifest.segments[0].continuation_of = 7)],
    ["first_continuation", (manifest) => (manifest.segments[0].continuation_of = "run_before_first")],
    ["continuation_target", (manifest) => (manifest.segments[0].continuation_of = "run_missing")],
    [
      "missing_non_first_continuation",
      (manifest) => {
        manifest.segments.push({
          ...manifest.segments[0],
          segment_id: "run_second",
          run_id: "run_second",
          path: "segments/run_second",
          records: "segments/run_second/records.jsonl",
          artifacts: "segments/run_second/artifacts",
          index: "segments/run_second/index.sqlite",
          started_at: "2026-08-15T00:00:01.000Z",
        })
      },
    ],
    [
      "non_immediate_continuation",
      (manifest) => {
        manifest.segments.push(
          {
            ...manifest.segments[0],
            segment_id: "run_second",
            run_id: "run_second",
            path: "segments/run_second",
            records: "segments/run_second/records.jsonl",
            artifacts: "segments/run_second/artifacts",
            index: "segments/run_second/index.sqlite",
            started_at: "2026-08-15T00:00:01.000Z",
            continuation_of: "run_existing",
          },
          {
            ...manifest.segments[0],
            segment_id: "run_third",
            run_id: "run_third",
            path: "segments/run_third",
            records: "segments/run_third/records.jsonl",
            artifacts: "segments/run_third/artifacts",
            index: "segments/run_third/index.sqlite",
            started_at: "2026-08-15T00:00:02.000Z",
            continuation_of: "run_existing",
          },
        )
      },
    ],
  ]

  for (const [name, mutate] of invalid) {
    const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), `opencode-invalid-manifest-${name}-`))
    const logicalRoot = path.join(traceRoot, "validated-case")
    try {
      await fs.mkdir(logicalRoot, { recursive: true })
      const manifest = validManifest()
      mutate(manifest)
      const descriptor = manifest.segments?.[0]
      const descriptorPath =
        typeof descriptor?.path === "string" &&
        !path.isAbsolute(descriptor.path) &&
        !descriptor.path.split(/[\\/]/).includes("..")
          ? descriptor.path
          : "segments/run_existing"
      await fs.mkdir(path.join(logicalRoot, descriptorPath), { recursive: true })
      await fs.writeFile(path.join(logicalRoot, descriptorPath, "segment.json"), JSON.stringify(descriptor) + "\n")
      await fs.writeFile(path.join(logicalRoot, "session.json"), JSON.stringify(manifest) + "\n")
      await fs.writeFile(path.join(logicalRoot, "preserved.bin"), Buffer.from([0, 1, 2, 255]))
      const before = await treeHash(logicalRoot)
      const outputDir = path.join(traceRoot, `materialized-${name}`)

      expect(() => materializeTrace({ caseDir: logicalRoot, outputDir }), name).toThrow()
      expect(await treeHash(logicalRoot), name).toBe(before)
      expect(await fileExists(outputDir), name).toBe(false)

      expect(
        () =>
          openTraceSegment({
            rootDir: traceRoot,
            logicalCaseID: "validated-case",
            sessionID: "ses_validated",
            runID: `run_after_${name}`,
          }),
        name,
      ).toThrow()
      expect(await treeHash(logicalRoot), name).toBe(before)
      expect(await fileExists(path.join(logicalRoot, ".derived")), name).toBe(false)
      expect(await fileExists(path.join(logicalRoot, "segments", `run_after_${name}`)), name).toBe(false)
    } finally {
      await fs.rm(traceRoot, { recursive: true, force: true })
    }
  }
})

test("writes POSIX manifest paths and resolves them safely with Win32 path behavior", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-portable-manifest-paths-"))
  try {
    const winRoot = String.raw`C:\trace-root\portable-case`
    const winSegment = path.win32.join(winRoot, "segments", "run_win")
    const relative = relativeTraceManifestPath(winRoot, winSegment)
    expect(relative).toBe("segments/run_win")
    expect(resolveTraceManifestPath(winRoot, relative)).toBe(winSegment)

    for (const invalid of [
      "../outside",
      String.raw`..\outside`,
      "/absolute",
      "C:/absolute",
      String.raw`C:\absolute`,
      String.raw`\\server\share\records.jsonl`,
    ])
      expect(() => resolveTraceManifestPath(winRoot, invalid), invalid).toThrow()

    const segment = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "portable-case",
      sessionID: "ses_portable",
      runID: "run_portable",
    })
    const session = JSON.parse(await fs.readFile(segment.sessionFile, "utf8")) as any
    for (const field of ["path", "records", "artifacts", "index"])
      expect(session.segments[0][field]).not.toContain("\\")
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("globally serializes concurrent late bindings without duplicate session roots", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-global-bind-"))
  try {
    const same = await bindUnknownSegmentsConcurrently({
      traceRoot,
      sessionIDs: ["ses_same_global", "ses_same_global"],
    })
    expect(same.map((item) => item.bound).sort()).toEqual([false, true])
    const sameManifests = await Promise.all(
      same.map((item) => fs.readFile(path.join(item.logicalRoot, "session.json"), "utf8").then(JSON.parse)),
    )
    expect(sameManifests.filter((manifest) => manifest.session_id === "ses_same_global")).toHaveLength(1)
    expect(new Set(same.map((item) => item.logicalRoot)).size).toBe(2)
    expect(sameManifests.every((manifest) => manifest.lock_key === globalManifestLockKey)).toBe(true)

    const different = await bindUnknownSegmentsConcurrently({
      traceRoot,
      sessionIDs: ["ses_different_a", "ses_different_b"],
    })
    expect(different.map((item) => item.bound)).toEqual([true, true])
    const differentManifests = await Promise.all(
      different.map((item) => fs.readFile(path.join(item.logicalRoot, "session.json"), "utf8").then(JSON.parse)),
    )
    expect(new Set(differentManifests.map((manifest) => manifest.session_id))).toEqual(
      new Set(["ses_different_a", "ses_different_b"]),
    )
    expect(new Set(different.map((item) => item.logicalRoot)).size).toBe(2)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("an old release cannot remove a successor acquired under the same lock key", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-lock-release-race-"))
  const key = "case:release-race"
  const lock = lockDirectory(traceRoot, key)
  try {
    const releaseOld = acquireTraceSessionLock(traceRoot, key)
    const oldOwner = JSON.parse(await fs.readFile(path.join(lock, "owner.json"), "utf8")) as any
    releaseOld()
    const releaseSuccessor = acquireTraceSessionLock(traceRoot, key)
    const successor = JSON.parse(await fs.readFile(path.join(lock, "owner.json"), "utf8")) as any
    expect(successor.nonce).not.toBe(oldOwner.nonce)
    releaseOld()
    expect(JSON.parse(await fs.readFile(path.join(lock, "owner.json"), "utf8"))).toMatchObject({
      nonce: successor.nonce,
    })
    releaseSuccessor()
    expect(await fileExists(lock)).toBe(false)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
    await fs.rm(path.dirname(lock), { recursive: true, force: true })
  }
})

test("disambiguates orphan segment paths and rejects duplicate run identities without changing the manifest", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-segment-collision-"))
  const logicalRoot = path.join(traceRoot, "collision-case")
  const orphanDir = path.join(logicalRoot, "segments", "run_collision")
  const sentinel = path.join(orphanDir, "orphan-evidence.txt")
  try {
    await fs.mkdir(orphanDir, { recursive: true })
    await fs.writeFile(sentinel, "immutable orphan bytes")

    const segment = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "collision-case",
      sessionID: "ses_collision",
      runID: "run_collision",
    })

    expect(path.basename(segment.segmentDir)).toBe("run_collision-2")
    expect(await fs.readFile(sentinel, "utf8")).toBe("immutable orphan bytes")
    const manifestBefore = await fs.readFile(segment.sessionFile)
    expect(() =>
      openTraceSegment({
        rootDir: traceRoot,
        logicalCaseID: "collision-case",
        sessionID: "ses_collision",
        runID: "run_collision",
      }),
    ).toThrow("run run_collision already exists")
    expect(await fs.readFile(segment.sessionFile)).toEqual(manifestBefore)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("failed atomic manifest publication removes the unpublished segment and preserves existing bytes", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-segment-atomic-failure-"))
  const logicalRoot = path.join(traceRoot, "atomic-failure")
  const sessionDestination = path.join(logicalRoot, "session.json")
  const existingDescriptor = {
    segment_id: "run_existing",
    run_id: "run_existing",
    case_id: "atomic-failure",
    session_id: "ses_atomic_failure",
    path: "segments/run_existing",
    records: "segments/run_existing/records.jsonl",
    artifacts: "segments/run_existing/artifacts",
    index: "segments/run_existing/index.sqlite",
    started_at: "2026-08-15T00:00:00.000Z",
    status: "completed",
  }
  const original =
    JSON.stringify(
      {
        schema_version: "1.0",
        logical_case_id: "atomic-failure",
        session_id: "ses_atomic_failure",
        lock_key: globalManifestLockKey,
        generation: 1,
        created_at: "2026-08-15T00:00:00.000Z",
        updated_at: "2026-08-15T00:00:00.000Z",
        segments: [existingDescriptor],
      },
      undefined,
      2,
    ) + "\n"
  try {
    const existingSegmentDir = path.join(logicalRoot, existingDescriptor.path)
    await fs.mkdir(existingSegmentDir, { recursive: true })
    await fs.writeFile(path.join(existingSegmentDir, "segment.json"), JSON.stringify(existingDescriptor) + "\n")
    await fs.writeFile(sessionDestination, original)
    expect(readTraceSessionManifest(sessionDestination)).toMatchObject({
      lock_key: globalManifestLockKey,
      generation: 1,
      segments: [existingDescriptor],
    })
    const before = await treeHash(logicalRoot)
    await fs.chmod(logicalRoot, 0o555)

    let publicationError: unknown
    try {
      openTraceSegment({
        rootDir: traceRoot,
        logicalCaseID: "atomic-failure",
        sessionID: "ses_atomic_failure",
        runID: "run_atomic_failure",
      })
    } catch (error) {
      publicationError = error
    }
    expect(publicationError).toBeInstanceOf(Error)
    const publicationCode = (publicationError as NodeJS.ErrnoException).code
    expect(["EACCES", "EPERM", "EROFS"]).toContain(publicationCode ?? "missing_error_code")
    await fs.chmod(logicalRoot, 0o755)

    expect(await treeHash(logicalRoot)).toBe(before)
    expect(await fs.readFile(sessionDestination, "utf8")).toBe(original)
    expect(await fs.readdir(path.join(logicalRoot, "segments"))).toEqual(["run_existing"])
    expect(await fileExists(path.join(logicalRoot, "segments", "run_atomic_failure"))).toBe(false)
    expect((await fs.readdir(logicalRoot)).filter((file) => file.includes(".tmp"))).toEqual([])
  } finally {
    await fs.chmod(logicalRoot, 0o755).catch(() => {})
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("finalizing a segment changes root metadata only", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-segment-finalize-"))
  try {
    const segment = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "finalize-case",
      sessionID: "ses_finalize",
      runID: "run_finalize",
    })
    const segmentBytes = await fs.readFile(segment.segmentFile)

    expect(segment.finalize("completed")).toBe(true)
    const session = JSON.parse(await fs.readFile(segment.sessionFile, "utf8")) as any
    expect(session.segments).toMatchObject([{ run_id: "run_finalize", status: "completed" }])
    expect(await fs.readFile(segment.segmentFile)).toEqual(segmentBytes)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("runtime close returns the logical root while retaining the physical segment journal", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-segment-runtime-close-"))
  const script = path.join(traceRoot, "runtime-close.ts")
  try {
    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const trace = CaseTrace.configure({ caseID: "runtime-close-case" }) as any`,
        `CaseTrace.setSessionID("ses_runtime_close")`,
        `CaseTrace.node({ node_id: "runtime_close_observation", kind: "execution.observation", component: "runtime", data: { output: "closed" } })`,
        `const request = CaseTrace.closeAll({ status: "success" })[0]`,
        `process.stdout.write(JSON.stringify({ request, physicalCaseDir: trace.caseDir }))`,
      ].join("\n"),
    )
    const child = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: traceRoot,
        OPENCODE_CASE_TRACE_QUIET: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const exitCode = await child.exited
    const output = JSON.parse(await new Response(child.stdout).text()) as any
    expect(exitCode).toBe(0)
    expect(await new Response(child.stderr).text()).toBe("")

    const logicalRoot = path.join(traceRoot, "runtime-close-case")
    expect(output.request.caseDir).toBe(logicalRoot)
    expect(output.request.recordsFile).toBe(path.join(output.physicalCaseDir, "records.jsonl"))
    expect(output.physicalCaseDir).toStartWith(path.join(logicalRoot, "segments") + path.sep)
    const session = JSON.parse(await fs.readFile(path.join(logicalRoot, "session.json"), "utf8")) as any
    expect(session.segments).toMatchObject([{ status: "completed" }])
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("an uncommitted runtime close leaves the segment interrupted instead of advertising completion", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-segment-uncommitted-close-"))
  const script = path.join(traceRoot, "runtime-close-failure.ts")
  try {
    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const trace = CaseTrace.configure({ caseID: "uncommitted-close-case" }) as any`,
        `const durableAppend = trace.writeCausalIRRecord.bind(trace)`,
        `trace.writeCausalIRRecord = (entry: any) => entry.operation === "case.runtime_closed" ? false : durableAppend(entry)`,
        `const request = CaseTrace.closeAll({ status: "success" })[0]`,
        `process.stdout.write(JSON.stringify(request))`,
      ].join("\n"),
    )
    const child = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: traceRoot,
        OPENCODE_CASE_TRACE_QUIET: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await child.exited).toBe(0)
    const request = JSON.parse(await new Response(child.stdout).text()) as any
    expect(await new Response(child.stderr).text()).toBe("")
    const session = JSON.parse(await fs.readFile(path.join(request.caseDir, "session.json"), "utf8")) as any
    const journal = await fs.readFile(request.recordsFile, "utf8")

    expect(session.segments).toMatchObject([{ status: "running" }])
    expect(journal).not.toContain('"operation":"case.runtime_closed"')
    const materialized = materializeTrace({ caseDir: request.caseDir })
    const trace = JSON.parse(await fs.readFile(materialized.traceFile, "utf8")) as any
    expect(materialized.completeness).toBe("incomplete")
    expect(trace.manifest).toMatchObject({ status: "error", recovery_status: "incomplete_journal_replay" })
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("terminal fsync failure leaves runtime status unpublished and materialization reconciles journal authority", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-segment-terminal-fsync-"))
  const script = path.join(traceRoot, "terminal-fsync-failure.ts")
  try {
    await fs.writeFile(
      script,
      [
        `import fs from "node:fs"`,
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `CaseTrace.configure({ caseID: "terminal-fsync-case" })`,
        `const durableFsync = fs.fsyncSync`,
        `fs.fsyncSync = () => { throw new Error("injected terminal fsync failure") }`,
        `const request = CaseTrace.closeAll({ status: "success", result: { answer: "durable" } })[0]`,
        `fs.fsyncSync = durableFsync`,
        `process.stdout.write(JSON.stringify(request))`,
      ].join("\n"),
    )
    const child = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: traceRoot,
        OPENCODE_CASE_TRACE_QUIET: "1",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await child.exited).toBe(0)
    const request = JSON.parse(await new Response(child.stdout).text()) as any
    expect(await new Response(child.stderr).text()).toBe("")
    const before = JSON.parse(await fs.readFile(path.join(request.caseDir, "session.json"), "utf8")) as any
    expect(before.segments).toMatchObject([{ status: "running" }])
    expect(await fs.readFile(request.recordsFile, "utf8")).toContain('"operation":"case.runtime_closed"')

    const materialized = materializeTrace({ caseDir: request.caseDir })
    const trace = JSON.parse(await fs.readFile(materialized.traceFile, "utf8")) as any
    expect(trace.manifest).toMatchObject({
      status: "success",
      segments: [expect.objectContaining({ status: "completed" })],
      segment_summary: { completed: 1, running: 0 },
    })
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

async function writeClosedSegment(
  segment: ReturnType<typeof openTraceSegment>,
  input: {
    runID: string
    marker: string
    caseID?: string
    referenceFixture?: boolean
    terminal?: {
      manifest: Record<string, unknown>
      metrics: Record<string, unknown>
    }
  },
) {
  const caseID = input.caseID ?? "unified-case"
  const entries: CausalIRJournalEntry[] = []
  const store = new CausalIRStore({
    runID: input.runID,
    caseID,
    append: (entry) => entries.push(entry),
  })
  store.createNode({
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-15T00:00:00.000Z",
    time_ms: 0,
    status: "running",
    data: { marker: input.marker, run_id: input.runID, case_id: caseID },
  })
  if (input.referenceFixture)
    for (const nodeID of ["shared_design", "shared_claim", "shared_verification"])
      store.createNode({
        node_id: nodeID,
        kind: "execution.observation",
        component: "runtime",
        timestamp: "2026-08-15T00:00:00.500Z",
        time_ms: 0.5,
        status: "success",
        data: { collision: nodeID },
      })
  store.createNode({
    node_id: "shared_fact",
    kind: "evidence.semantic_fact",
    component: "tool",
    timestamp: "2026-08-15T00:00:01.000Z",
    time_ms: 1,
    status: "success",
    aliases: input.referenceFixture
      ? [
          "design:shared_design",
          "claim:shared_claim",
          "evidence:shared_evidence",
          "verification:shared_verification",
          "change:shared_change",
          "context:shared_context",
          "response:shared_response",
          "response_segment:shared_segment",
          "tool_call:shared_tool_call",
          "skill:shared_skill",
          "mcp_call:shared_mcp_call",
        ]
      : [],
    data: {
      claim: `${input.marker} fact`,
      ...(input.referenceFixture
        ? {
            documentation_url: "https://example.test/node:run_start",
            output_path: "artifact:shared_artifact",
            ordinary_text: "node:run_start",
            node_id: "run_start",
            parent_node_id: "run_start",
            design_id: "shared_design",
            claim_id: "shared_claim",
            evidence_id: "shared_evidence",
            verification_id: "shared_verification",
            change_id: "shared_change",
            context_id: "shared_context",
            response_id: "shared_response",
            segment_id: "shared_segment",
            tool_call_id: "shared_tool_call",
            skill_id: "shared_skill",
            mcp_call_id: "shared_mcp_call",
            customer_node_id: "run_start",
            artifact_id: "shared_artifact",
            missing_artifact_id: "missing_artifact",
            typed_ref: {
              ref_type: "node",
              ref_id: "shared_evidence",
              legacy_ref: "evidence:shared_evidence",
            },
            candidate_ref: { ref_type: "node", ref_id: "run_start", legacy_ref: "node:run_start" },
            missing_typed_ref: { ref_type: "node", ref_id: "missing_node", legacy_ref: "node:missing_node" },
            legacy_refs: ["node:run_start", "artifact:shared_artifact"],
            generation_context_ref: "node:run_start",
            direct_evidence_refs: ["evidence:shared_evidence"],
            verification_refs: ["verification:shared_verification"],
            generation_provenance_refs: ["node:run_start"],
            changed_test_refs: ["change:shared_change"],
            documentation_ref: "https://example.test/node:run_start",
            customer_node_ref: "customer supplied node:run_start",
          }
        : {}),
    },
    input_refs: ["node:run_start"],
    source_refs: ["evidence:run_start"],
    artifact_refs: ["shared_artifact"],
  })
  store.createEdge({
    edge_id: "shared_edge",
    from: { type: "node", id: "run_start" },
    to: { type: "node", id: "shared_fact" },
    relation: "produced",
    eligible_for_attribution: true,
    evidence_refs: ["evidence:shared_fact"],
  })
  store.createArtifact({
    artifact_id: "shared_artifact",
    hash: createHash("sha256").update(input.marker).digest("hex"),
    path: "artifacts/fact.txt",
  })
  if (input.referenceFixture) {
    store.createDiagnostic({
      diagnostic_id: "shared_diagnostic",
      artifact_id: "shared_artifact",
      node_id: "shared_fact",
      edge_id: "shared_edge",
      reference: { ref_type: "artifact", ref_id: "shared_artifact", legacy_ref: "artifact:shared_artifact" },
      documentation_url: "https://example.test/artifact:shared_artifact",
    })
  }
  if (input.terminal) {
    const snapshot = store.snapshot()
    store.finalize({
      trace_version: "6.0",
      causal_ir_version: snapshot.version,
      manifest: input.terminal.manifest,
      nodes: snapshot.nodes,
      edges: snapshot.edges,
      artifacts: snapshot.artifacts,
      diagnostics: snapshot.diagnostics,
      journal: store.journalSummary(),
      metrics: input.terminal.metrics,
      compatibility: { provenance_projection: "provenance-trace.json" },
      records: [],
      dataflow_edges: [],
    })
  } else {
    store.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-15T00:00:02.000Z",
      manifest: {
        case_id: caseID,
        run_id: input.runID,
        session_id: "ses_unified",
      },
    })
  }
  await fs.writeFile(
    path.join(segment.segmentDir, "records.jsonl"),
    entries.map((entry) => JSON.stringify(entry)).join("\n") + "\n",
  )
  await fs.mkdir(path.join(segment.segmentDir, "artifacts"), { recursive: true })
  await fs.writeFile(path.join(segment.segmentDir, "artifacts", "fact.txt"), `${input.marker} artifact`)
  if (input.terminal) {
    await fs.writeFile(
      path.join(segment.segmentDir, "legacy-trace.json"),
      JSON.stringify({
        trace_version: "1.3",
        run_id: input.runID,
        marker: input.marker,
        artifacts: [
          {
            artifact_id: "legacy_fact",
            path: "artifacts/fact.txt",
            hash: createHash("sha256").update(`${input.marker} artifact`).digest("hex"),
          },
        ],
      }),
    )
  }
  expect(segment.finalize(input.terminal?.manifest.status === "error" ? "failed" : "completed")).toBe(true)
}

test("uses the global manifest lock for flat, segmented-output, and publication materialization", async () => {
  const previousWait = process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-materializer-global-lock-"))
  try {
    process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = "0"
    const flatCase = path.join(traceRoot, "legacy-flat")
    await fs.mkdir(flatCase)
    const flatEntries: CausalIRJournalEntry[] = []
    const flatStore = new CausalIRStore({
      runID: "run_flat_lock",
      caseID: "legacy-flat",
      append: (entry) => flatEntries.push(entry),
    })
    flatStore.createNode({
      node_id: "run_start",
      kind: "run.start",
      component: "run",
      timestamp: "2026-08-15T00:00:00.000Z",
      time_ms: 0,
      status: "running",
      data: { run_id: "run_flat_lock", case_id: "legacy-flat" },
    })
    flatStore.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-15T00:00:01.000Z",
      manifest: { case_id: "legacy-flat", run_id: "run_flat_lock" },
    })
    await fs.writeFile(
      path.join(flatCase, "records.jsonl"),
      flatEntries.map((entry) => JSON.stringify(entry)).join("\n") + "\n",
    )

    const segment = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "segmented-lock",
      sessionID: "ses_segmented_lock",
      runID: "run_segmented_lock",
    })
    await writeClosedSegment(segment, {
      runID: "run_segmented_lock",
      marker: "locked",
      caseID: "segmented-lock",
    })
    const outputDir = path.join(traceRoot, "read-only-output")
    const flatBefore = await treeHash(flatCase)
    const segmentedBefore = await treeHash(segment.logicalRoot)
    const release = acquireTraceSessionLock(traceRoot, globalManifestLockKey)
    try {
      expect(() => materializeTrace({ caseDir: flatCase })).toThrow("timed out waiting for trace session lock")
      expect(() => materializeTrace({ caseDir: segment.logicalRoot, outputDir })).toThrow(
        "timed out waiting for trace session lock",
      )
      expect(() => materializeTrace({ caseDir: segment.logicalRoot })).toThrow(
        "timed out waiting for trace session lock",
      )
    } finally {
      release()
    }
    expect(await treeHash(flatCase)).toBe(flatBefore)
    expect(await treeHash(segment.logicalRoot)).toBe(segmentedBefore)
    expect(await fileExists(outputDir)).toBe(false)
  } finally {
    if (previousWait === undefined) delete process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS
    else process.env.OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS = previousWait
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("chooses segmented materialization from the session snapshot read after locking", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-materializer-lock-gap-"))
  const logicalRoot = path.join(traceRoot, "lock-gap-case")
  const readyFile = path.join(traceRoot, "materializer-lock.ready")
  const gateFile = path.join(traceRoot, "materializer-lock.gate")
  const outputFile = path.join(traceRoot, "materializer-output.json")
  const script = path.join(traceRoot, "materialize-lock-gap.ts")
  try {
    const legacyEntries: CausalIRJournalEntry[] = []
    const legacy = new CausalIRStore({
      runID: "run_gap_legacy",
      caseID: "lock-gap-case",
      append: (entry) => legacyEntries.push(entry),
    })
    legacy.createNode({
      node_id: "run_start",
      kind: "run.start",
      component: "run",
      timestamp: "2026-08-15T00:00:00.000Z",
      time_ms: 0,
      status: "running",
      data: { marker: "legacy-only", run_id: "run_gap_legacy", case_id: "lock-gap-case" },
    })
    legacy.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-15T00:00:01.000Z",
      manifest: { case_id: "lock-gap-case", run_id: "run_gap_legacy" },
    })
    await fs.mkdir(logicalRoot, { recursive: true })
    await fs.writeFile(
      path.join(logicalRoot, "records.jsonl"),
      legacyEntries.map((entry) => JSON.stringify(entry)).join("\n") + "\n",
    )
    await fs.writeFile(
      script,
      [
        `import fs from "node:fs"`,
        `const request = JSON.parse(process.argv[2])`,
        `const mkdirSync = fs.mkdirSync.bind(fs)`,
        `let paused = false`,
        `fs.mkdirSync = ((directory, options) => {`,
        `  if (!paused && String(directory) === request.lockDir) {`,
        `    paused = true`,
        `    fs.writeFileSync(request.readyFile, "ready")`,
        `    while (!fs.existsSync(request.gateFile)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2)`,
        `  }`,
        `  return mkdirSync(directory, options)`,
        `})`,
        `const { materializeTrace } = await import(${JSON.stringify(materializerModule)})`,
        `const result = materializeTrace({ caseDir: request.logicalRoot })`,
        `fs.writeFileSync(request.outputFile, JSON.stringify(result))`,
      ].join("\n"),
    )
    const child = Bun.spawn(
      [
        process.execPath,
        script,
        JSON.stringify({
          logicalRoot,
          lockDir: lockDirectory(traceRoot, globalManifestLockKey),
          readyFile,
          gateFile,
          outputFile,
        }),
      ],
      { cwd: packageDir, stdout: "pipe", stderr: "pipe" },
    )
    expect(await waitForFilePresence(readyFile)).toBe(true)

    const segment = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "lock-gap-case",
      sessionID: "ses_lock_gap",
      runID: "run_gap_segmented",
    })
    await writeClosedSegment(segment, {
      runID: "run_gap_segmented",
      marker: "segmented-after-lock",
      caseID: "lock-gap-case",
    })
    await fs.writeFile(gateFile, "continue")

    const exitCode = await child.exited
    const stderr = await new Response(child.stderr).text()
    expect(exitCode, stderr).toBe(0)
    expect(stderr).toBe("")
    const result = JSON.parse(await fs.readFile(outputFile, "utf8")) as any
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8")) as any
    const session = JSON.parse(await fs.readFile(segment.sessionFile, "utf8")) as any
    expect(trace.manifest.session_generation).toBe(session.generation)
    expect(trace.manifest.segments).toHaveLength(2)
    expect(trace.nodes.some((node: any) => node.scope?.run_id === "run_gap_segmented")).toBe(true)
  } finally {
    await fs.writeFile(gateFile, "continue").catch(() => {})
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("materializes two colliding segments as one scoped trace with continuation provenance", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-unified-segments-"))
  try {
    const first = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "unified-case",
      sessionID: "ses_unified",
      runID: "run_first",
    })
    await writeClosedSegment(first, { runID: "run_first", marker: "first" })
    const second = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "unified-case",
      sessionID: "ses_unified",
      runID: "run_second",
    })
    await writeClosedSegment(second, { runID: "run_second", marker: "second" })

    const result = materializeTrace({ caseDir: first.logicalRoot })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8")) as any

    expect(result).toMatchObject({ caseDir: first.logicalRoot, completeness: "complete", recoveredLines: 10 })
    expect(trace.manifest).toMatchObject({
      case_id: "unified-case",
      run_id: "run_second",
      session_id: "ses_unified",
      segments: [
        { run_id: "run_first", status: "completed" },
        { run_id: "run_second", status: "completed", continuation_of: "run_first" },
      ],
    })
    const replayedNodes = trace.nodes.filter((node: any) => node.kind !== "case.completed")
    expect(replayedNodes).toHaveLength(4)
    expect(new Set(replayedNodes.map((node: any) => node.node_id)).size).toBe(4)
    expect(new Set(replayedNodes.map((node: any) => node.scope.run_id))).toEqual(new Set(["run_first", "run_second"]))
    expect(trace.nodes.at(-1)).toMatchObject({ kind: "case.completed", scope: { run_id: "run_second" } })

    const localEdges = trace.edges.filter((edge: any) => edge.metadata?.provenance_type !== "run.continuation")
    expect(localEdges).toHaveLength(2)
    expect(new Set(localEdges.map((edge: any) => edge.edge_id)).size).toBe(2)
    expect(new Set(localEdges.map((edge: any) => edge.scope.run_id))).toEqual(new Set(["run_first", "run_second"]))
    const continuation = trace.edges.find((edge: any) => edge.metadata?.provenance_type === "run.continuation")
    expect(continuation).toMatchObject({
      original_relation: "continued_from",
      normalized_relation: "continued_from",
      scope: { run_id: "run_second", case_id: "unified-case" },
      metadata: { continuation_of: "run_first", run_id: "run_second" },
    })

    const facts = trace.records.filter((item: any) => item.event_type === "evidence.semantic_fact")
    expect(facts.map((item: any) => item.data.claim)).toEqual(["first fact", "second fact"])
    expect(new Set(facts.map((item: any) => item.record_id)).size).toBe(2)
    expect(trace.artifacts).toHaveLength(2)
    expect(new Set(trace.artifacts.map((artifact: any) => artifact.artifact_id)).size).toBe(2)
    expect(new Set(facts.flatMap((item: any) => item.artifact_refs))).toEqual(
      new Set(trace.artifacts.map((artifact: any) => artifact.artifact_id)),
    )
    for (const fact of facts) {
      const node = trace.nodes.find((item: any) => item.node_id === fact.record_id)
      expect(node.metadata).toMatchObject({
        original_node_id: "shared_fact",
        run_id: node.scope.run_id,
        segment_id: node.scope.run_id,
      })
      expect(node.input_refs[0].ref_id).toBe(`${node.scope.run_id}::node::run_start`)
      expect(node.source_refs[0].legacy_ref).toBe(`evidence:${node.scope.run_id}::node::run_start`)
      expect(fact.input_refs).toEqual([`node:${node.scope.run_id}::node::run_start`])
    }
    for (const edge of localEdges) {
      expect(edge.metadata).toMatchObject({
        original_edge_id: "shared_edge",
        run_id: edge.scope.run_id,
        segment_id: edge.scope.run_id,
      })
      expect(edge.evidence_refs[0].ref_id).toBe(`${edge.scope.run_id}::node::shared_fact`)
    }
    for (const artifact of trace.artifacts) {
      expect(artifact).toMatchObject({
        original_artifact_id: "shared_artifact",
        scope: {
          run_id: artifact.artifact_id.split("::artifact::")[0],
          segment_id: artifact.artifact_id.split("::artifact::")[0],
        },
      })
    }
    expect(
      await Promise.all(
        trace.artifacts.map((artifact: any) => fs.readFile(path.join(first.logicalRoot, artifact.path), "utf8")),
      ),
    ).toEqual(["first artifact", "second artifact"])
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

async function writeUnclosedSegment(
  segment: ReturnType<typeof openTraceSegment>,
  input: { runID: string; caseID: string; marker: string },
) {
  const entries: CausalIRJournalEntry[] = []
  const store = new CausalIRStore({ runID: input.runID, caseID: input.caseID, append: (entry) => entries.push(entry) })
  store.createNode({
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-15T00:00:00.000Z",
    time_ms: 0,
    status: "running",
    data: { marker: input.marker, run_id: input.runID, case_id: input.caseID },
  })
  await fs.writeFile(
    path.join(segment.segmentDir, "records.jsonl"),
    entries.map((entry) => JSON.stringify(entry)).join("\n") + "\n",
  )
}

test("latest interrupted segment controls top-level status while preserving the prior terminal result", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-terminal-selection-"))
  try {
    const first = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "terminal-case",
      sessionID: "ses_terminal",
      runID: "run_interrupted_before",
    })
    await writeUnclosedSegment(first, {
      runID: "run_interrupted_before",
      caseID: "terminal-case",
      marker: "before",
    })
    const terminal = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "terminal-case",
      sessionID: "ses_terminal",
      runID: "run_terminal_success",
    })
    await writeClosedSegment(terminal, {
      runID: "run_terminal_success",
      marker: "terminal",
      caseID: "terminal-case",
      terminal: {
        manifest: {
          status: "success",
          server_status: "success",
          process_status: "success",
          case_status: "success",
          case_id: "terminal-case",
          run_id: "run_terminal_success",
          result: { marker: "terminal result" },
          recovery_status: "terminal_recovery_preserved",
          recovery: { source: "terminal" },
        },
        metrics: { spans: 2, events: 3, token_usage: {}, trace_health: { issues: [] } },
      },
    })
    const later = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "terminal-case",
      sessionID: "ses_terminal",
      runID: "run_interrupted_after",
    })
    await writeUnclosedSegment(later, {
      runID: "run_interrupted_after",
      caseID: "terminal-case",
      marker: "after",
    })

    const result = materializeTrace({ caseDir: first.logicalRoot })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8")) as any
    const legacy = JSON.parse(await fs.readFile(path.join(first.logicalRoot, "legacy-trace.json"), "utf8")) as any
    expect(result.completeness).toBe("incomplete")
    expect(trace.manifest).toMatchObject({
      run_id: "run_interrupted_after",
      status: "error",
      server_status: "error",
      process_status: "error",
      case_status: "error",
      recovery_status: "incomplete_journal_replay",
      previous_terminal: {
        run_id: "run_terminal_success",
        status: "success",
        result: { marker: "terminal result" },
        recovery_status: "terminal_recovery_preserved",
        recovery: { source: "terminal" },
      },
      historical_interruptions: true,
      segment_summary: { interrupted_unfinalized: 1, running: 1 },
    })
    expect(legacy).toMatchObject({ run_id: "run_interrupted_after", status: "error" })
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("detects a legacy terminal journal line larger than four MiB without changing its bytes", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-large-legacy-terminal-"))
  const logicalRoot = path.join(traceRoot, "large-legacy")
  try {
    const entries: CausalIRJournalEntry[] = []
    const store = new CausalIRStore({
      runID: "run_large_legacy",
      caseID: "large-legacy",
      append: (entry) => entries.push(entry),
    })
    store.createNode({
      node_id: "run_start",
      kind: "run.start",
      component: "run",
      timestamp: "2026-08-15T00:00:00.000Z",
      time_ms: 0,
      status: "running",
      data: { marker: "legacy" },
    })
    store.closeRuntime({
      format: "runtime_close",
      status: "success",
      closed_at: "2026-08-15T00:00:01.000Z",
      result: { padding: "x".repeat(5 * 1024 * 1024) },
      manifest: { case_id: "large-legacy", run_id: "run_large_legacy", session_id: "ses_large_legacy" },
    })
    await fs.mkdir(logicalRoot, { recursive: true })
    const recordsFile = path.join(logicalRoot, "records.jsonl")
    await fs.writeFile(recordsFile, entries.map((entry) => JSON.stringify(entry)).join("\n") + "\n")
    const before = await sha256(recordsFile)

    const resumed = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "large-legacy",
      sessionID: "ses_large_legacy",
      runID: "run_after_large_legacy",
    })
    const session = JSON.parse(await fs.readFile(resumed.sessionFile, "utf8")) as any
    expect(session.segments[0]).toMatchObject({ segment_id: "legacy-root", status: "completed" })
    expect(session.segments[1].continuation_of).toBe("run_large_legacy")
    expect(await sha256(recordsFile)).toBe(before)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("content-addresses compatibility artifacts when a resumed segment reuses a path with different bytes", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-compat-artifact-collision-"))
  try {
    const first = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "compat-artifact-case",
      sessionID: "ses_compat_artifact",
      runID: "run_artifact_first",
    })
    await writeClosedSegment(first, {
      runID: "run_artifact_first",
      marker: "first",
      caseID: "compat-artifact-case",
      terminal: { manifest: { status: "success", run_id: "run_artifact_first" }, metrics: {} },
    })
    materializeTrace({ caseDir: first.logicalRoot })
    expect(await fs.readFile(path.join(first.logicalRoot, "artifacts/fact.txt"), "utf8")).toBe("first artifact")

    const second = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "compat-artifact-case",
      sessionID: "ses_compat_artifact",
      runID: "run_artifact_second",
    })
    await writeClosedSegment(second, {
      runID: "run_artifact_second",
      marker: "second",
      caseID: "compat-artifact-case",
      terminal: { manifest: { status: "success", run_id: "run_artifact_second" }, metrics: {} },
    })
    materializeTrace({ caseDir: first.logicalRoot })
    const legacy = JSON.parse(await fs.readFile(path.join(first.logicalRoot, "legacy-trace.json"), "utf8")) as any
    const projectedPath = legacy.artifacts[0].path as string
    expect(projectedPath).not.toBe("artifacts/fact.txt")
    expect(projectedPath).toStartWith("artifacts/sha256/")
    expect(await fs.readFile(path.join(first.logicalRoot, projectedPath), "utf8")).toBe("second artifact")
    expect(await fs.readFile(path.join(first.logicalRoot, "artifacts/fact.txt"), "utf8")).toBe("first artifact")
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

function expectValidScopedGraph(trace: any) {
  const nodes = new Set(trace.nodes.map((node: any) => node.node_id))
  const edges = new Set(trace.edges.map((edge: any) => edge.edge_id))
  const artifacts = new Set(trace.artifacts.map((artifact: any) => artifact.artifact_id))
  const diagnostics = new Set(trace.diagnostics.map((diagnostic: any) => diagnostic.diagnostic_id))
  const expectRef = (ref: any) => {
    if (ref.ref_type === "node") expect(nodes.has(ref.ref_id)).toBe(true)
    if (ref.ref_type === "artifact") expect(artifacts.has(ref.ref_id)).toBe(true)
  }
  for (const node of trace.nodes) {
    for (const ref of [...node.input_refs, ...node.output_refs, ...node.source_refs]) expectRef(ref)
    for (const artifactID of node.artifact_refs) expect(artifacts.has(artifactID)).toBe(true)
  }
  for (const edge of trace.edges) {
    expectRef(edge.from)
    expectRef(edge.to)
    for (const ref of edge.evidence_refs) expectRef(ref)
  }
  for (const diagnostic of trace.diagnostics) {
    expect(diagnostics.has(diagnostic.diagnostic_id)).toBe(true)
    if (diagnostic.node_id) expect(nodes.has(diagnostic.node_id)).toBe(true)
    if (diagnostic.edge_id) expect(edges.has(diagnostic.edge_id)).toBe(true)
    if (diagnostic.artifact_id) expect(artifacts.has(diagnostic.artifact_id)).toBe(true)
    if (diagnostic.reference) expectRef(diagnostic.reference)
  }
  const visit = (value: unknown) => {
    if (typeof value === "string") {
      const scoped =
        /^(node|record|design|claim|context|response|response_segment|tool_call|skill|mcp_call|evidence|verification|change|observation|span):(.+::node::.+)$/.exec(
          value,
        )
      if (scoped) expect(nodes.has(scoped[2]!)).toBe(true)
      const artifact = /^artifact:(.+::artifact::.+)$/.exec(value)
      if (artifact) expect(artifacts.has(artifact[1]!)).toBe(true)
      return
    }
    if (Array.isArray(value)) {
      for (const item of value) visit(item)
      return
    }
    if (value && typeof value === "object") for (const item of Object.values(value)) visit(item)
  }
  visit({
    nodes: trace.nodes,
    edges: trace.edges,
    diagnostics: trace.diagnostics,
    manifest: trace.manifest,
    metrics: trace.metrics,
  })
}

async function visibleDerivedState(caseDir: string) {
  const files = ["trace.json", "manifest.json", "legacy-trace.json", "provenance-trace.json", "partial/latest.json"]
  const hashes = await Promise.all(
    files.map(async (relative) => [
      relative,
      (await fileExists(path.join(caseDir, relative))) ? await sha256(path.join(caseDir, relative)) : null,
    ]),
  )
  const current = path.join(caseDir, ".derived", "current")
  return {
    current: (await fileExists(current)) ? await fs.readlink(current) : null,
    hashes,
  }
}

test("rewrites only schema-declared references with per-segment identity maps", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-schema-scoped-refs-"))
  try {
    const terminal = (runID: string, marker: string) => ({
      manifest: {
        status: "success",
        case_id: "schema-ref-case",
        run_id: runID,
        result: {
          artifact_id: "shared_artifact",
          node_ref: "node:shared_fact",
          typed_ref: { ref_type: "node", ref_id: "shared_fact", legacy_ref: "node:shared_fact" },
          verification_ref: "node:shared_fact",
          documentation_url: "https://example.test/node:shared_fact",
          ordinary_text: "artifact:shared_artifact",
        },
      },
      metrics: {
        result_node_id: "shared_fact",
        evidence_refs: ["node:shared_fact", "artifact:shared_artifact"],
        documentation_url: "https://example.test/artifact:shared_artifact",
      },
    })
    const first = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "schema-ref-case",
      sessionID: "ses_schema_refs",
      runID: "run_schema_first",
    })
    await writeClosedSegment(first, {
      runID: "run_schema_first",
      marker: "first",
      caseID: "schema-ref-case",
      referenceFixture: true,
      terminal: terminal("run_schema_first", "first"),
    })
    const second = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "schema-ref-case",
      sessionID: "ses_schema_refs",
      runID: "run_schema_second",
    })
    await writeClosedSegment(second, {
      runID: "run_schema_second",
      marker: "second",
      caseID: "schema-ref-case",
      referenceFixture: true,
      terminal: terminal("run_schema_second", "second"),
    })

    const result = materializeTrace({ caseDir: first.logicalRoot })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8")) as any
    const fact = trace.nodes.find(
      (node: any) => node.scope?.run_id === "run_schema_second" && node.metadata?.original_node_id === "shared_fact",
    )
    const prefix = "run_schema_second"
    expect(fact.payload).toMatchObject({
      documentation_url: "https://example.test/node:run_start",
      output_path: "artifact:shared_artifact",
      ordinary_text: "node:run_start",
      node_id: `${prefix}::node::run_start`,
      parent_node_id: `${prefix}::node::run_start`,
      design_id: `${prefix}::node::shared_fact`,
      claim_id: `${prefix}::node::shared_fact`,
      evidence_id: `${prefix}::node::shared_fact`,
      verification_id: `${prefix}::node::shared_fact`,
      change_id: `${prefix}::node::shared_fact`,
      context_id: `${prefix}::node::shared_fact`,
      response_id: `${prefix}::node::shared_fact`,
      segment_id: `${prefix}::node::shared_fact`,
      tool_call_id: `${prefix}::node::shared_fact`,
      skill_id: `${prefix}::node::shared_fact`,
      mcp_call_id: `${prefix}::node::shared_fact`,
      customer_node_id: "run_start",
      artifact_id: `${prefix}::artifact::shared_artifact`,
      missing_artifact_id: "missing_artifact",
      typed_ref: {
        ref_type: "node",
        ref_id: `${prefix}::node::shared_fact`,
        legacy_ref: `evidence:${prefix}::node::shared_fact`,
      },
      candidate_ref: {
        ref_type: "node",
        ref_id: `${prefix}::node::run_start`,
        legacy_ref: `node:${prefix}::node::run_start`,
      },
      missing_typed_ref: { ref_type: "node", ref_id: "missing_node", legacy_ref: "node:missing_node" },
      legacy_refs: [`node:${prefix}::node::run_start`, `artifact:${prefix}::artifact::shared_artifact`],
      generation_context_ref: `node:${prefix}::node::run_start`,
      direct_evidence_refs: [`evidence:${prefix}::node::shared_fact`],
      verification_refs: [`verification:${prefix}::node::shared_fact`],
      generation_provenance_refs: [`node:${prefix}::node::run_start`],
      changed_test_refs: [`change:${prefix}::node::shared_fact`],
      documentation_ref: "https://example.test/node:run_start",
      customer_node_ref: "customer supplied node:run_start",
    })
    const diagnostic = trace.diagnostics.find((item: any) => item.scope?.run_id === "run_schema_second")
    expect(diagnostic).toMatchObject({
      diagnostic_id: `${prefix}::diagnostic::shared_diagnostic`,
      artifact_id: `${prefix}::artifact::shared_artifact`,
      node_id: `${prefix}::node::shared_fact`,
      edge_id: `${prefix}::edge::shared_edge`,
      reference: { ref_type: "artifact", ref_id: `${prefix}::artifact::shared_artifact` },
      documentation_url: "https://example.test/artifact:shared_artifact",
    })
    expect(trace.manifest.result).toMatchObject({
      artifact_id: `${prefix}::artifact::shared_artifact`,
      node_ref: `node:${prefix}::node::shared_fact`,
      typed_ref: { ref_type: "node", ref_id: `${prefix}::node::shared_fact` },
      verification_ref: `node:${prefix}::node::shared_fact`,
      documentation_url: "https://example.test/node:shared_fact",
      ordinary_text: "artifact:shared_artifact",
    })
    expect(trace.metrics).toMatchObject({
      result_node_id: `${prefix}::node::shared_fact`,
      evidence_refs: [
        "node:run_schema_first::node::shared_fact",
        "artifact:run_schema_first::artifact::shared_artifact",
        `node:${prefix}::node::shared_fact`,
        `artifact:${prefix}::artifact::shared_artifact`,
      ],
      documentation_url: "https://example.test/artifact:shared_artifact",
    })
    expectValidScopedGraph(trace)
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("publishes each derived compatibility set through one atomic generation indirection", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-derived-generation-"))
  const previousFailure = process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP
  try {
    const first = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "derived-generation-case",
      sessionID: "ses_derived_generation",
      runID: "run_derived_first",
    })
    await writeClosedSegment(first, {
      runID: "run_derived_first",
      marker: "first",
      caseID: "derived-generation-case",
      terminal: { manifest: { status: "success", run_id: "run_derived_first" }, metrics: {} },
    })
    materializeTrace({ caseDir: first.logicalRoot })
    const oldState = await visibleDerivedState(first.logicalRoot)
    expect(oldState.current).toContain("generations/")
    for (const relative of ["trace.json", "manifest.json", "legacy-trace.json", "provenance-trace.json"])
      expect((await fs.lstat(path.join(first.logicalRoot, relative))).isSymbolicLink()).toBe(true)

    const second = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "derived-generation-case",
      sessionID: "ses_derived_generation",
      runID: "run_derived_second",
    })
    await writeClosedSegment(second, {
      runID: "run_derived_second",
      marker: "second",
      caseID: "derived-generation-case",
      terminal: { manifest: { status: "success", run_id: "run_derived_second" }, metrics: {} },
    })
    const oldTree = await treeHash(first.logicalRoot)

    for (const step of ["generation_ready", "root_links_ready", "current_swap_ready", "current_swapped"]) {
      process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP = step
      expect(() => materializeTrace({ caseDir: first.logicalRoot }), step).toThrow(
        `injected derived publication failure: ${step}`,
      )
      expect(await visibleDerivedState(first.logicalRoot), step).toEqual(oldState)
      expect(await treeHash(first.logicalRoot), step).toBe(oldTree)
    }

    delete process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP
    materializeTrace({ caseDir: first.logicalRoot })
    const newState = await visibleDerivedState(first.logicalRoot)
    expect(newState.current).not.toBe(oldState.current)
    expect(newState.hashes).not.toEqual(oldState.hashes)
    const session = JSON.parse(await fs.readFile(first.sessionFile, "utf8")) as any
    const trace = JSON.parse(await fs.readFile(path.join(first.logicalRoot, "trace.json"), "utf8")) as any
    expect(trace.manifest.session_generation).toBe(session.generation)
  } finally {
    if (previousFailure === undefined) delete process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP
    else process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP = previousFailure
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("cleans a first derived generation after copy and root-link failures", async () => {
  const previousFailure = process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP
  try {
    for (const step of ["generation_copying", "root_link_installing"]) {
      const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), `opencode-derived-first-${step}-`))
      try {
        const segment = openTraceSegment({
          rootDir: traceRoot,
          logicalCaseID: "first-generation-case",
          sessionID: "ses_first_generation",
          runID: `run_${step}`,
        })
        await writeClosedSegment(segment, {
          runID: `run_${step}`,
          marker: step,
          caseID: "first-generation-case",
          terminal: { manifest: { status: "success", run_id: `run_${step}` }, metrics: {} },
        })
        const before = await treeHash(segment.logicalRoot)
        process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP = step

        expect(() => materializeTrace({ caseDir: segment.logicalRoot })).toThrow(
          `injected derived publication failure: ${step}`,
        )
        expect(await treeHash(segment.logicalRoot)).toBe(before)
        expect(await fileExists(path.join(segment.logicalRoot, ".derived"))).toBe(false)
      } finally {
        await fs.rm(traceRoot, { recursive: true, force: true })
      }
    }
  } finally {
    if (previousFailure === undefined) delete process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP
    else process.env.OPENCODE_TRACE_DERIVED_FAIL_STEP = previousFailure
  }
})

test("publishes only a generation-matched session snapshot with trace.json as the commit marker", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-generation-publication-"))
  const readyFile = path.join(traceRoot, "stage.ready")
  const publicationReadyFile = path.join(traceRoot, "publication.ready")
  const publicationGateFile = path.join(traceRoot, "publication.gate")
  const script = path.join(traceRoot, "materialize.ts")
  try {
    const segment = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "generation-case",
      sessionID: "ses_generation",
      runID: "run_generation",
    })
    await writeClosedSegment(segment, { runID: "run_generation", marker: "generation", caseID: "generation-case" })
    const staleCommitMarker = "stale trace commit marker"
    await fs.writeFile(path.join(segment.logicalRoot, "trace.json"), staleCommitMarker)
    await fs.writeFile(
      script,
      [
        `import fs from "node:fs"`,
        `const mkdirSync = fs.mkdirSync.bind(fs)`,
        `let paused = false`,
        `fs.mkdirSync = ((directory, options) => {`,
        `  if (!paused && String(directory) === ${JSON.stringify(lockDirectory(traceRoot, globalManifestLockKey))} && fs.existsSync(${JSON.stringify(readyFile)})) {`,
        `    paused = true`,
        `    fs.writeFileSync(${JSON.stringify(publicationReadyFile)}, "ready")`,
        `    while (!fs.existsSync(${JSON.stringify(publicationGateFile)})) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2)`,
        `  }`,
        `  return mkdirSync(directory, options)`,
        `})`,
        `const { materializeTrace } = await import(${JSON.stringify(materializerModule)})`,
        `const result = materializeTrace({ caseDir: ${JSON.stringify(segment.logicalRoot)} })`,
        `process.stdout.write(JSON.stringify(result))`,
      ].join("\n"),
    )

    const child = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: { ...process.env, OPENCODE_TRACE_MATERIALIZER_STAGE_READY_FILE: readyFile },
      stdout: "pipe",
      stderr: "pipe",
    })
    expect(await waitForFilePresence(readyFile)).toBe(true)
    expect(await waitForFilePresence(publicationReadyFile)).toBe(true)
    expect(await fs.readFile(path.join(segment.logicalRoot, "trace.json"), "utf8")).toBe(staleCommitMarker)

    const release = acquireTraceSessionLock(traceRoot, segment.lockKey)
    const manifest = JSON.parse(await fs.readFile(segment.sessionFile, "utf8")) as any
    manifest.generation += 1
    manifest.updated_at = new Date(Date.parse(manifest.updated_at) + 1).toISOString()
    await fs.writeFile(segment.sessionFile, JSON.stringify(manifest, undefined, 2) + "\n")
    release()
    await fs.writeFile(publicationGateFile, "continue")

    const exitCode = await child.exited
    const stderr = await new Response(child.stderr).text()
    expect(exitCode, stderr).toBe(0)
    expect(stderr).toBe("")
    const output = JSON.parse(await new Response(child.stdout).text()) as any
    const trace = JSON.parse(await fs.readFile(output.traceFile, "utf8")) as any
    expect(trace.manifest.session_generation).toBe(manifest.generation)
    expect(trace.manifest.run_id).toBe("run_generation")
    expect(await fs.readFile(path.join(segment.logicalRoot, "trace.json"), "utf8")).not.toBe(staleCommitMarker)
  } finally {
    await fs.writeFile(publicationGateFile, "continue").catch(() => {})
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("aggregates rich terminal manifests and metrics across ordered segments", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-rich-unified-segments-"))
  try {
    const first = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "rich-unified-case",
      sessionID: "ses_rich_unified",
      runID: "run_rich_first",
    })
    await writeClosedSegment(first, {
      runID: "run_rich_first",
      marker: "first",
      caseID: "rich-unified-case",
      terminal: {
        manifest: {
          trace_version: "6.0",
          case_id: "rich-unified-case",
          run_id: "run_rich_first",
          session_id: "ses_rich_unified",
          status: "success",
          server_status: "success",
          process_status: "success",
          case_status: "success",
          subject_revision: "git:first",
          result: { first: true },
        },
        metrics: {
          spans: 1,
          events: 2,
          records: 2,
          dataflow_edges: 1,
          artifacts: 1,
          token_usage: { input: 3, output: 4, total: 7 },
          stream_summary: { tool_input_delta_events: 1 },
          trace_health: {
            skill_request_unresolved: 1,
            compaction_quality_flags: { missing_token_estimate: 1 },
            issues: [{ kind: "first_issue" }],
          },
        },
      },
    })
    const second = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "rich-unified-case",
      sessionID: "ses_rich_unified",
      runID: "run_rich_second",
    })
    await writeClosedSegment(second, {
      runID: "run_rich_second",
      marker: "second",
      caseID: "rich-unified-case",
      terminal: {
        manifest: {
          trace_version: "6.0",
          case_id: "rich-unified-case",
          run_id: "run_rich_second",
          session_id: "ses_rich_unified",
          status: "error",
          server_status: "cancelled",
          process_status: "cancelled",
          case_status: "error",
          shutdown_signal: "SIGTERM",
          shutdown_disposition: "interrupted_before_case_completion",
          subject_revision: "git:second",
          recovery_status: "producer_recovered",
          result: { second: true },
          error: { message: "latest failure" },
        },
        metrics: {
          spans: 2,
          events: 3,
          records: 2,
          dataflow_edges: 1,
          artifacts: 1,
          token_usage: { input: 5, output: 6, total: 11 },
          stream_summary: { tool_input_delta_events: 2 },
          trace_health: {
            skill_request_unresolved: 2,
            compaction_quality_flags: { missing_token_estimate: 2 },
            issues: [{ kind: "second_issue" }],
          },
        },
      },
    })

    const result = materializeTrace({ caseDir: first.logicalRoot })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8")) as any
    const provenance = JSON.parse(
      await fs.readFile(path.join(first.logicalRoot, "provenance-trace.json"), "utf8"),
    ) as any
    const legacy = JSON.parse(await fs.readFile(path.join(first.logicalRoot, "legacy-trace.json"), "utf8")) as any

    expect(trace.manifest).toMatchObject({
      case_id: "rich-unified-case",
      run_id: "run_rich_second",
      session_id: "ses_rich_unified",
      status: "error",
      server_status: "cancelled",
      process_status: "cancelled",
      case_status: "error",
      shutdown_signal: "SIGTERM",
      shutdown_disposition: "interrupted_before_case_completion",
      subject_revision: "git:second",
      recovery_status: "producer_recovered",
      result: { second: true },
      error: { message: "latest failure" },
    })
    expect(trace.metrics).toMatchObject({
      spans: 3,
      events: 5,
      records: trace.nodes.length,
      dataflow_edges: trace.edges.length,
      artifacts: trace.artifacts.length,
      token_usage: { input: 8, output: 10, total: 18 },
      stream_summary: { tool_input_delta_events: 3 },
      trace_health: {
        skill_request_unresolved: 3,
        compaction_quality_flags: { missing_token_estimate: 3 },
        issues: [{ kind: "first_issue" }, { kind: "second_issue" }],
      },
      aggregation: {
        mode: "session_segments_v1",
        terminal_manifest_run_id: "run_rich_second",
        numeric_metrics: "sum",
        graph_counts: "materialized_unique_entities",
      },
    })
    expect(provenance).toMatchObject({ manifest: trace.manifest, metrics: trace.metrics })
    expect(provenance.records).toEqual(trace.records)
    expect(provenance.dataflow_edges).toEqual(trace.dataflow_edges)
    expect(legacy).toMatchObject({ trace_version: "1.3", run_id: "run_rich_second", marker: "second" })
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("discovers an immutable legacy root as segment zero when opening and materializing a resumed run", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-legacy-segment-zero-"))
  const logicalRoot = path.join(traceRoot, "legacy-resume")
  const legacyEntries: CausalIRJournalEntry[] = []
  const legacy = new CausalIRStore({
    runID: "run_legacy",
    caseID: "legacy-resume",
    append: (entry) => legacyEntries.push(entry),
  })
  legacy.createNode({
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-14T00:00:00.000Z",
    time_ms: 0,
    status: "running",
    data: { run_id: "run_legacy", case_id: "legacy-resume" },
  })
  legacy.createNode({
    node_id: "legacy_fact",
    kind: "evidence.semantic_fact",
    component: "tool",
    timestamp: "2026-08-14T00:00:01.000Z",
    time_ms: 1,
    status: "success",
    data: { claim: "legacy fact" },
    artifact_refs: ["legacy_artifact"],
  })
  legacy.createArtifact({
    artifact_id: "legacy_artifact",
    hash: createHash("sha256").update("legacy artifact").digest("hex"),
    path: "artifacts/legacy.txt",
  })
  legacy.closeRuntime({
    format: "runtime_close",
    status: "success",
    closed_at: "2026-08-14T00:00:02.000Z",
    manifest: { case_id: "legacy-resume", run_id: "run_legacy", session_id: "ses_legacy_resume" },
  })

  try {
    await fs.mkdir(path.join(logicalRoot, "artifacts"), { recursive: true })
    await fs.writeFile(
      path.join(logicalRoot, "records.jsonl"),
      legacyEntries.map((entry) => JSON.stringify(entry)).join("\n") + "\n",
    )
    await fs.writeFile(path.join(logicalRoot, "artifacts", "legacy.txt"), "legacy artifact")
    await fs.writeFile(path.join(logicalRoot, "index.sqlite"), "legacy index bytes")
    const legacyHashes = {
      records: await sha256(path.join(logicalRoot, "records.jsonl")),
      artifact: await sha256(path.join(logicalRoot, "artifacts", "legacy.txt")),
      index: await sha256(path.join(logicalRoot, "index.sqlite")),
    }

    const resumed = openTraceSegment({
      rootDir: traceRoot,
      logicalCaseID: "legacy-resume",
      sessionID: "ses_legacy_resume",
      runID: "run_resumed",
    })
    const session = JSON.parse(await fs.readFile(resumed.sessionFile, "utf8")) as any
    expect(session.segments).toMatchObject([
      {
        segment_id: "legacy-root",
        run_id: "run_legacy",
        path: ".",
        records: "records.jsonl",
        status: "completed",
      },
      { run_id: "run_resumed", continuation_of: "run_legacy", status: "running" },
    ])
    expect(await sha256(path.join(logicalRoot, "records.jsonl"))).toBe(legacyHashes.records)
    expect(await sha256(path.join(logicalRoot, "artifacts", "legacy.txt"))).toBe(legacyHashes.artifact)
    expect(await sha256(path.join(logicalRoot, "index.sqlite"))).toBe(legacyHashes.index)

    await writeClosedSegment(resumed, { runID: "run_resumed", marker: "resumed", caseID: "legacy-resume" })
    const result = materializeTrace({ caseDir: logicalRoot })
    const trace = JSON.parse(await fs.readFile(result.traceFile, "utf8")) as any
    expect(
      trace.records
        .filter((item: any) => item.event_type === "evidence.semantic_fact")
        .map((item: any) => item.data.claim),
    ).toEqual(["legacy fact", "resumed fact"])
    expect(trace.edges.filter((edge: any) => edge.metadata?.provenance_type === "run.continuation")).toHaveLength(1)
    expect(await fs.readFile(path.join(logicalRoot, trace.artifacts[0].path), "utf8")).toBe("legacy artifact")
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})

test("direct finish materializes and publishes the logical root while runtime storage stays physical", async () => {
  const traceRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-segment-direct-finish-"))
  const script = path.join(traceRoot, "direct-finish.ts")
  try {
    await fs.writeFile(
      script,
      [
        `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
        `const trace = CaseTrace.configure({ caseID: "direct-finish-case", subjectRevision: "git:single-segment", environment: { startup_payload: "x".repeat(4096) } }) as any`,
        `CaseTrace.setSessionID("ses_direct_finish")`,
        `CaseTrace.responseOutput({ text: "direct finish response", response_role: "final_answer", visibility: "user_visible", is_final_for_case: true, finality_source: "explicit" })`,
        `CaseTrace.finish({ status: "success", result: { answer: "done" } })`,
        `process.stdout.write(trace.caseDir)`,
      ].join("\n"),
    )
    const child = Bun.spawn([process.execPath, script], {
      cwd: packageDir,
      env: {
        ...process.env,
        OPENCODE_CASE_TRACE: "1",
        OPENCODE_CASE_TRACE_DIR: traceRoot,
        OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "8",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const exitCode = await child.exited
    const physicalDir = await new Response(child.stdout).text()
    const publication = await new Response(child.stderr).text()
    const logicalRoot = path.join(traceRoot, "direct-finish-case")

    expect(exitCode).toBe(0)
    expect(physicalDir).toStartWith(path.join(logicalRoot, "segments") + path.sep)
    expect(publication).toContain(`directory: ${logicalRoot}`)
    expect(publication).toContain(`json: ${path.join(logicalRoot, "trace.json")}`)
    expect(publication).not.toContain(`directory: ${physicalDir}`)
    const journal = (await fs.readFile(path.join(physicalDir, "records.jsonl"), "utf8"))
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
    expect(journal[0]).toMatchObject({ operation: "node.created", data: { kind: "run.start" } })
    expect(journal.some((entry: any) => entry.operation === "artifact.created")).toBe(true)
    const trace = JSON.parse(await fs.readFile(path.join(logicalRoot, "trace.json"), "utf8")) as any
    const manifest = JSON.parse(await fs.readFile(path.join(logicalRoot, "manifest.json"), "utf8")) as any
    const provenance = JSON.parse(await fs.readFile(path.join(logicalRoot, "provenance-trace.json"), "utf8")) as any
    const legacy = JSON.parse(await fs.readFile(path.join(logicalRoot, "legacy-trace.json"), "utf8")) as any
    const segment = JSON.parse(await fs.readFile(path.join(physicalDir, "segment.json"), "utf8")) as any
    expect(trace.manifest).toMatchObject({
      status: "success",
      case_status: "success",
      session_id: "ses_direct_finish",
      subject_revision: "git:single-segment",
      collection_mode: "passive_sidecar",
      result: { answer: "done" },
    })
    expect(manifest).toEqual(trace.manifest)
    expect(trace.nodes.every((node: any) => !node.node_id.startsWith(`${segment.run_id}::node::`))).toBe(true)
    expect(trace.edges.every((edge: any) => !edge.edge_id.startsWith(`${segment.run_id}::edge::`))).toBe(true)
    expect(
      trace.artifacts.every((artifact: any) => !artifact.artifact_id.startsWith(`${segment.run_id}::artifact::`)),
    ).toBe(true)
    expect(provenance).toMatchObject({ manifest: trace.manifest, metrics: trace.metrics })
    expect(provenance.records).toEqual(trace.records)
    expect(legacy).toMatchObject({ trace_version: "1.3", run_id: segment.run_id, status: "success" })
    expect(await fs.readFile(path.join(physicalDir, "legacy-trace.json"), "utf8")).toBe(
      await fs.readFile(path.join(logicalRoot, "legacy-trace.json"), "utf8"),
    )
    for (const runtimeFile of ["records.jsonl", "events.jsonl", "raw-events.jsonl"]) {
      expect(await fileExists(path.join(logicalRoot, runtimeFile))).toBe(false)
      expect(await fileExists(path.join(physicalDir, runtimeFile))).toBe(true)
    }
    const session = JSON.parse(await fs.readFile(path.join(logicalRoot, "session.json"), "utf8")) as any
    expect(session.segments).toMatchObject([{ status: "completed" }])
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})
