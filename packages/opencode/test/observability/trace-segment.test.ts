import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { CausalIRStore, type CausalIRJournalEntry } from "@/observability/causal-ir"
import { materializeTrace } from "@/observability/trace-materializer"
import { openTraceSegment } from "@/observability/trace-segment"

const packageDir = path.resolve(import.meta.dir, "../..")
const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
const segmentModule = pathToFileURL(path.join(packageDir, "src/observability/trace-segment.ts")).href

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
    expect(await fileExists(lockDirectory(traceRoot, "session:ses_concurrent_first"))).toBe(false)
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
  const lock = lockDirectory(traceRoot, `session:${sessionID}`)
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
  const lock = lockDirectory(traceRoot, `session:${sessionID}`)
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
  const original =
    JSON.stringify(
      {
        schema_version: "1.0",
        logical_case_id: "atomic-failure",
        session_id: "ses_atomic_failure",
        created_at: "2026-08-15T00:00:00.000Z",
        updated_at: "2026-08-15T00:00:00.000Z",
        segments: [],
      },
      undefined,
      2,
    ) + "\n"
  try {
    await fs.mkdir(path.join(logicalRoot, "segments"), { recursive: true })
    await fs.writeFile(sessionDestination, original)
    await fs.chmod(logicalRoot, 0o555)

    expect(() =>
      openTraceSegment({
        rootDir: traceRoot,
        logicalCaseID: "atomic-failure",
        sessionID: "ses_atomic_failure",
        runID: "run_atomic_failure",
      }),
    ).toThrow()
    await fs.chmod(logicalRoot, 0o755)

    expect(await fs.readFile(sessionDestination, "utf8")).toBe(original)
    expect(await fs.readdir(path.join(logicalRoot, "segments"))).toEqual([])
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

async function writeClosedSegment(
  segment: ReturnType<typeof openTraceSegment>,
  input: {
    runID: string
    marker: string
    caseID?: string
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
  store.createNode({
    node_id: "shared_fact",
    kind: "evidence.semantic_fact",
    component: "tool",
    timestamp: "2026-08-15T00:00:01.000Z",
    time_ms: 1,
    status: "success",
    data: { claim: `${input.marker} fact` },
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
      JSON.stringify({ trace_version: "1.3", run_id: input.runID, marker: input.marker }),
    )
  }
  expect(segment.finalize(input.terminal?.manifest.status === "error" ? "failed" : "completed")).toBe(true)
}

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
    expect(trace.nodes).toHaveLength(4)
    expect(new Set(trace.nodes.map((node: any) => node.node_id)).size).toBe(4)
    expect(new Set(trace.nodes.map((node: any) => node.scope.run_id))).toEqual(new Set(["run_first", "run_second"]))

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
