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
  input: { runID: string; marker: string; caseID?: string },
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
    artifact_refs: ["shared_artifact"],
  })
  store.createEdge({
    edge_id: "shared_edge",
    from: { type: "node", id: "run_start" },
    to: { type: "node", id: "shared_fact" },
    relation: "produced",
    eligible_for_attribution: true,
  })
  store.createArtifact({
    artifact_id: "shared_artifact",
    hash: createHash("sha256").update(input.marker).digest("hex"),
    path: "artifacts/fact.txt",
  })
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
  await fs.writeFile(
    path.join(segment.segmentDir, "records.jsonl"),
    entries.map((entry) => JSON.stringify(entry)).join("\n") + "\n",
  )
  await fs.mkdir(path.join(segment.segmentDir, "artifacts"), { recursive: true })
  await fs.writeFile(path.join(segment.segmentDir, "artifacts", "fact.txt"), `${input.marker} artifact`)
  expect(segment.finalize("completed")).toBe(true)
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
    expect(
      await Promise.all(
        trace.artifacts.map((artifact: any) => fs.readFile(path.join(first.logicalRoot, artifact.path), "utf8")),
      ),
    ).toEqual(["first artifact", "second artifact"])
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
        `const trace = CaseTrace.configure({ caseID: "direct-finish-case" }) as any`,
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
    const trace = JSON.parse(await fs.readFile(path.join(logicalRoot, "trace.json"), "utf8")) as any
    expect(trace.manifest).toMatchObject({ status: "success", case_status: "success", session_id: "ses_direct_finish" })
    const session = JSON.parse(await fs.readFile(path.join(logicalRoot, "session.json"), "utf8")) as any
    expect(session.segments).toMatchObject([{ status: "completed" }])
  } finally {
    await fs.rm(traceRoot, { recursive: true, force: true })
  }
})
