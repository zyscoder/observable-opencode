import { expect, test } from "bun:test"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { CausalIRStore } from "@/observability/causal-ir"

const GIB = 1024 * 1024 * 1024
const MAX_RSS_BYTES = 256 * 1024 * 1024
const FINAL_ARTIFACT_PAYLOAD = "final sparse artifact"
const childMode = process.env.OPENCODE_TRACE_MATERIALIZER_MEMORY_CHILD === "1"

if (childMode) {
  test("materializer memory measurement child", async () => {
    const { materializeTrace } = await import("@/observability/trace-materializer")
    const caseDir = process.env.OPENCODE_TRACE_MATERIALIZER_MEMORY_DIR!
    const phase = (name: string) =>
      process.stdout.write(
        `\nTRACE_MATERIALIZER_PHASE ${JSON.stringify({ phase: name, journalBytes: fs.statSync(path.join(caseDir, "records.jsonl")).size, rss: process.memoryUsage().rss, maxRSS: process.resourceUsage().maxRSS })}\n`,
      )

    Bun.gc(true)
    phase("child_ready")
    const result = materializeTrace({ caseDir })
    Bun.gc(true)
    const materializerMaxRSS = process.resourceUsage().maxRSS
    phase("materializer_gc_complete")
    const { loadRenderableTrace } = await import("../../../trace-renderer/src/load")
    const loaded = loadRenderableTrace(result.traceFile)
    Bun.gc(true)
    phase("renderer_loaded")

    expect(result.completeness).toBe("complete")
    expect(loaded.source).toBe("trace.json")
    expect(loaded.incomplete).toBe(false)
    expect(loaded.trace.artifacts).toHaveLength(1)
    expect(loaded.trace.artifacts[0]).toEqual(
      expect.objectContaining({ artifact_id: "bounded_artifact", payload: FINAL_ARTIFACT_PAYLOAD }),
    )
    const finalMaxRSS = process.resourceUsage().maxRSS
    process.stdout.write(
      `\nTRACE_MATERIALIZER_MEMORY ${JSON.stringify({ materializerMaxRSS, finalMaxRSS, maxRSS: Math.max(materializerMaxRSS, finalMaxRSS) })}\n`,
    )
  }, 600_000)
} else {
  test("materializes a sparse 1 GiB journal below the 256 MiB peak RSS bound", async () => {
    const caseDir = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-trace-materializer-memory-"))
    const recordsFile = path.join(caseDir, "records.jsonl")
    const fd = fs.openSync(recordsFile, "w")

    try {
      const store = new CausalIRStore({
        runID: "run_materializer_memory",
        caseID: "case-materializer-memory",
        append(entry) {
          fs.writeSync(fd, `${JSON.stringify(entry)}\n`)
        },
      })
      store.createNode({
        node_id: "run_start",
        kind: "run.start",
        component: "run",
        timestamp: "2026-08-14T12:00:00.000Z",
        time_ms: 0,
        status: "running",
        data: { run_id: "run_materializer_memory", case_id: "case-materializer-memory" },
      })
      const artifact = {
        artifact_id: "bounded_artifact",
        hash: "sha256:bounded-artifact",
        path: "artifacts/bounded.txt",
        payload: "x".repeat(4 * 1024 * 1024),
      }
      store.createArtifact(artifact)
      while (fs.fstatSync(fd).size < GIB) store.reuseArtifact(artifact)
      store.reuseArtifact({
        ...artifact,
        hash: "sha256:final-sparse-artifact",
        payload: FINAL_ARTIFACT_PAYLOAD,
      })
      store.closeRuntime({
        format: "runtime_close",
        status: "success",
        closed_at: "2026-08-14T12:00:01.000Z",
        manifest: { case_id: "case-materializer-memory", run_id: "run_materializer_memory" },
      })
      fs.fsyncSync(fd)
      fs.closeSync(fd)

      expect(fs.statSync(recordsFile).size).toBeGreaterThanOrEqual(GIB)
      const child = Bun.spawn(
        [
          process.execPath,
          "--expose-gc",
          "test",
          "test/observability/trace-materializer-memory.test.ts",
          "--timeout",
          "600000",
        ],
        {
          cwd: path.join(import.meta.dir, "../.."),
          env: {
            ...process.env,
            OPENCODE_TRACE_MATERIALIZER_MEMORY_CHILD: "1",
            OPENCODE_TRACE_MATERIALIZER_MEMORY_DIR: caseDir,
            OPENCODE_TRACE_MATERIALIZER_MEMORY_PHASES: "1",
          },
          stdout: "pipe",
          stderr: "pipe",
        },
      )
      const [exitCode, stdout, stderr] = await Promise.all([
        child.exited,
        new Response(child.stdout).text(),
        new Response(child.stderr).text(),
      ])
      const match = stdout.match(/TRACE_MATERIALIZER_MEMORY (\{[^\n]+\})/)
      process.stdout.write(`${stdout.match(/TRACE_MATERIALIZER_PHASE [^\n]+/g)?.join("\n") ?? ""}\n`)

      expect(match, `${stdout}\n${stderr}`).not.toBeNull()
      const measurement = JSON.parse(match![1]!) as {
        materializerMaxRSS: number
        finalMaxRSS: number
        maxRSS: number
      }
      expect(measurement.maxRSS).toBe(Math.max(measurement.materializerMaxRSS, measurement.finalMaxRSS))
      console.info("trace-materializer-memory-rss", JSON.stringify(measurement))
      expect(measurement.maxRSS).toBeLessThanOrEqual(MAX_RSS_BYTES)
      expect(exitCode, stderr).toBe(0)
    } finally {
      try {
        fs.closeSync(fd)
      } catch {}
      fs.rmSync(caseDir, { recursive: true, force: true })
    }
  }, 600_000)
}
