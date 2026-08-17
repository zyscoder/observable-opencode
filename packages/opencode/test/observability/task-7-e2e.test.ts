import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { CausalIRStore, type CausalIRJournalEntry } from "@/observability/causal-ir"

const packageDir = path.resolve(import.meta.dir, "../..")
const fixture = path.join(import.meta.dir, "fixture", "task-7-trace-process.ts")
const cli = path.join(packageDir, "src", "index.ts")

async function waitForJson(file: string) {
  for (let attempt = 0; attempt < 500; attempt += 1) {
    try {
      return JSON.parse(await fs.readFile(file, "utf8")) as any
    } catch {
      await Bun.sleep(10)
    }
  }
  throw new Error(`timed out waiting for ${file}`)
}

async function exists(file: string) {
  return fs
    .access(file)
    .then(() => true)
    .catch(() => false)
}

async function sha256(file: string) {
  return createHash("sha256")
    .update(await fs.readFile(file))
    .digest("hex")
}

async function directoryHashes(directory: string) {
  if (!(await exists(directory))) return []
  const files: string[] = []
  const visit = async (current: string) => {
    for (const entry of await fs.readdir(current, { withFileTypes: true })) {
      const file = path.join(current, entry.name)
      if (entry.isDirectory()) await visit(file)
      if (entry.isFile()) files.push(file)
    }
  }
  await visit(directory)
  files.sort()
  return Promise.all(files.map(async (file) => [path.relative(directory, file), await sha256(file)] as const))
}

async function finalize(caseDir: string, env: Record<string, string> = {}) {
  const child = Bun.spawn([process.execPath, cli, "trace-finalize", caseDir], {
    cwd: packageDir,
    env: {
      ...process.env,
      OPENCODE_CASE_TRACE: "0",
      ...env,
    },
    stdout: "pipe",
    stderr: "pipe",
  })
  const exitCode = await child.exited
  return {
    exitCode,
    stdout: await new Response(child.stdout).text(),
    stderr: await new Response(child.stderr).text(),
  }
}

test(
  "SIGKILL preserves durable segment bytes across failed materialization and retry",
  async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-task-7-resume-"))
    const readyFile = path.join(root, "killed.ready.json")
    const requestFile = path.join(root, "resumed.request.json")
    const common = {
      ...process.env,
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_CASE_TRACE_DIR: root,
      OPENCODE_CASE_ID: "task-7-resume",
      OPENCODE_CASE_TRACE_QUIET: "1",
      OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "128",
      TASK7_TRACE_SESSION_ID: "ses_task_7_resume",
    }

    try {
      const killed = Bun.spawn([process.execPath, fixture, "hold"], {
        cwd: packageDir,
        env: {
          ...common,
          OPENCODE_RUN_ID: "run_task_7_killed",
          TASK7_TRACE_READY_FILE: readyFile,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const ready = await waitForJson(readyFile)
      killed.kill("SIGKILL")
      expect(await killed.exited).not.toBe(0)
      expect(await new Response(killed.stderr).text()).toBe("")

      const firstJournalHash = await sha256(ready.recordsFile)
      const firstIndexHash = await sha256(path.join(ready.segmentDir, "index.sqlite"))
      const firstArtifactHashes = await directoryHashes(path.join(ready.segmentDir, "artifacts"))
      expect(firstArtifactHashes.length).toBeGreaterThan(0)

      const resumed = Bun.spawn([process.execPath, fixture, "close"], {
        cwd: packageDir,
        env: {
          ...common,
          OPENCODE_RUN_ID: "run_task_7_resumed",
          TASK7_TRACE_REQUEST_FILE: requestFile,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      expect(await resumed.exited).toBe(0)
      expect(await new Response(resumed.stderr).text()).toBe("")
      const request = await waitForJson(requestFile)
      const caseDir = request.caseDir as string

      expect(await sha256(ready.recordsFile)).toBe(firstJournalHash)
      expect(await sha256(path.join(ready.segmentDir, "index.sqlite"))).toBe(firstIndexHash)
      expect(await directoryHashes(path.join(ready.segmentDir, "artifacts"))).toEqual(firstArtifactHashes)

      const blocker = path.join(root, "materializer-blocker")
      await fs.writeFile(blocker, "not a directory")
      const failed = await finalize(caseDir, {
        OPENCODE_TRACE_MATERIALIZER_STAGE_READY_FILE: path.join(blocker, "ready"),
      })
      expect(failed.exitCode).not.toBe(0)
      expect(await exists(path.join(caseDir, "trace.json"))).toBe(false)
      expect(await sha256(ready.recordsFile)).toBe(firstJournalHash)

      const retried = await finalize(caseDir)
      expect(retried.exitCode, retried.stderr).toBe(0)
      expect(retried.stdout).toContain(`trace: ${path.join(caseDir, "trace.json")}`)

      const session = JSON.parse(await fs.readFile(path.join(caseDir, "session.json"), "utf8")) as any
      const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any
      expect(session.session_id).toBe("ses_task_7_resume")
      expect(session.segments.map((segment: any) => segment.status)).toEqual(["interrupted_unfinalized", "completed"])
      expect(session.segments[1].continuation_of).toBe(session.segments[0].run_id)
      expect(trace.edges.some((edge: any) => edge.normalized_relation === "continued_from")).toBe(true)
      expect(trace.artifacts.length).toBeGreaterThan(0)
      expect(await sha256(ready.recordsFile)).toBe(firstJournalHash)
      expect(await sha256(path.join(ready.segmentDir, "index.sqlite"))).toBe(firstIndexHash)
      expect(await directoryHashes(path.join(ready.segmentDir, "artifacts"))).toEqual(firstArtifactHashes)
    } finally {
      await fs.rm(root, { recursive: true, force: true })
    }
  },
  60_000,
)

test("legacy flat journal materializes without changing its durable bytes", async () => {
  const caseDir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-task-7-legacy-"))
  const entries: CausalIRJournalEntry[] = []
  const store = new CausalIRStore({
    runID: "run_task_7_legacy",
    caseID: "task-7-legacy",
    append: (entry) => entries.push(entry),
  })
  store.createNode({
    node_id: "run_start",
    kind: "run.start",
    component: "run",
    timestamp: "2026-08-16T00:00:00.000Z",
    time_ms: 0,
    status: "running",
    data: { run_id: "run_task_7_legacy", case_id: "task-7-legacy" },
  })
  store.createNode({
    node_id: "legacy_response",
    kind: "response.output",
    component: "result",
    timestamp: "2026-08-16T00:00:01.000Z",
    time_ms: 1,
    status: "success",
    data: { text: "legacy recovery" },
  })
  store.closeRuntime({
    format: "runtime_close",
    status: "success",
    closed_at: "2026-08-16T00:00:02.000Z",
    manifest: { case_id: "task-7-legacy", run_id: "run_task_7_legacy" },
  })

  try {
    await fs.writeFile(path.join(caseDir, "records.jsonl"), entries.map((entry) => JSON.stringify(entry)).join("\n") + "\n")
    const before = await sha256(path.join(caseDir, "records.jsonl"))
    const result = await finalize(caseDir)
    const trace = JSON.parse(await fs.readFile(path.join(caseDir, "trace.json"), "utf8")) as any

    expect(result.exitCode, result.stderr).toBe(0)
    expect(await sha256(path.join(caseDir, "records.jsonl"))).toBe(before)
    expect(trace.manifest.status).toBe("success")
    expect(trace.nodes.some((node: any) => node.kind === "response.output")).toBe(true)
    expect(await exists(path.join(caseDir, "session.json"))).toBe(false)
  } finally {
    await fs.rm(caseDir, { recursive: true, force: true })
  }
})
