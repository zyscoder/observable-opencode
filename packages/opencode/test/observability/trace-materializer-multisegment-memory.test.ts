import { expect, test } from "bun:test"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

const GIB = 1024 * 1024 * 1024
const MAX_RSS_BYTES = 256 * 1024 * 1024
const NODE_COUNT = 31_000
test("materializes a high-cardinality multi-segment 1 GiB journal below 256 MiB RSS", async () => {
  const traceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-trace-multisegment-memory-"))
  const { canonicalCausalIRNode, causalIRPayloadHash } = await import("@/observability/causal-ir")
  const { openTraceSegment } = await import("@/observability/trace-segment")
  const padding = "x".repeat(8 * 1024)

  try {
    const writeSegment = (runID: string) => {
      const segment = openTraceSegment({
        rootDir: traceRoot,
        logicalCaseID: "multi-segment-high-cardinality",
        sessionID: "ses_multi_segment_high_cardinality",
        runID,
      })
      const recordsFile = path.join(segment.segmentDir, "records.jsonl")
      const fd = fs.openSync(recordsFile, "w")
      let sequence = 0
      let casePayloadHash: string | undefined
      let retainedNode: any
      const append = (
        operation: "node.created" | "case.checkpointed" | "case.runtime_closed",
        recordType: string,
        entityID: string,
        data: unknown,
        previousPayloadHash?: string,
      ) => {
        const payloadHash = causalIRPayloadHash(data)
        fs.writeSync(
          fd,
          `${JSON.stringify({
            sequence: ++sequence,
            time: "2026-08-17T00:00:00.000Z",
            run_id: runID,
            case_id: "multi-segment-high-cardinality",
            operation,
            record_type: recordType,
            entity_id: entityID,
            ...(previousPayloadHash === undefined ? {} : { previous_payload_hash: previousPayloadHash }),
            data,
            payload_hash: payloadHash,
          })}\n`,
        )
        return payloadHash
      }

      const runStart = canonicalCausalIRNode(
        {
          node_id: "run_start",
          kind: "run.start",
          component: "run",
          timestamp: "2026-08-17T00:00:00.000Z",
          time_ms: 0,
          status: "running",
          data: { run_id: runID, case_id: "multi-segment-high-cardinality" },
        },
        { runID, caseID: "multi-segment-high-cardinality", sequence: 1 },
      )
      append("node.created", "run.start", runStart.node_id, runStart)

      for (let index = 0; index < NODE_COUNT; index++) {
        const suffix = String(index).padStart(6, "0")
        const node = canonicalCausalIRNode(
          {
            node_id: `node_${suffix}`,
            kind: "execution.observation",
            component: "tool",
            timestamp: "2026-08-17T00:00:01.000Z",
            time_ms: index + 1,
            status: "success",
            aliases: [`record:r_${suffix}`, `evidence:e_${suffix}`, `context_snapshot:c_${suffix}`],
            data: { index, padding },
          },
          { runID, caseID: "multi-segment-high-cardinality", sequence: sequence + 1 },
        )
        append("node.created", node.kind, node.node_id, node)
        if (index === NODE_COUNT - 1) retainedNode = node
      }

      const snapshot = {
        version: "1.0",
        runID,
        caseID: "multi-segment-high-cardinality",
        nodes: [runStart, retainedNode],
        edges: [],
        artifacts: [],
        diagnostics: [],
      }
      casePayloadHash = append("case.checkpointed", "checkpoint", "multi-segment-high-cardinality", {
        snapshot,
        data: { reason: "high_cardinality_fixture_compaction" },
        hash_state_replacement: "nodes_and_edges",
      })
      const referenceNode = canonicalCausalIRNode(
        {
          node_id: "alias_reference",
          kind: "response.output",
          component: "result",
          timestamp: "2026-08-17T00:00:02.000Z",
          time_ms: NODE_COUNT + 1,
          title: "alias ref",
          status: "success",
          input_refs: ["evidence:e_030999"],
          data: { text: "resolved from the retained high-cardinality alias" },
        },
        { runID, caseID: "multi-segment-high-cardinality", sequence: sequence + 1 },
      )
      append("node.created", referenceNode.kind, referenceNode.node_id, referenceNode)
      append(
        "case.runtime_closed",
        "runtime_close",
        "multi-segment-high-cardinality",
        {
          format: "runtime_close",
          status: "success",
          closed_at: "2026-08-17T00:00:03.000Z",
          manifest: {
            case_id: "multi-segment-high-cardinality",
            run_id: runID,
            session_id: "ses_multi_segment_high_cardinality",
          },
        },
        casePayloadHash,
      )
      fs.fsyncSync(fd)
      fs.closeSync(fd)
      expect(segment.finalize("completed")).toBe(true)
      return fs.statSync(recordsFile).size
    }

    const journalBytes = writeSegment("run_high_cardinality_1") + writeSegment("run_high_cardinality_2")
    expect(journalBytes).toBeGreaterThanOrEqual(GIB)

    const child = Bun.spawn(
      [process.execPath, "--expose-gc", "test/fixture/trace-materializer-multisegment-memory-child.ts"],
      {
        cwd: path.join(import.meta.dir, "../.."),
        env: {
          ...process.env,
          OPENCODE_TRACE_MATERIALIZER_MULTI_MEMORY_DIR: path.join(traceRoot, "multi-segment-high-cardinality"),
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
    const match = stdout.match(/TRACE_MATERIALIZER_MULTI_MEMORY (\{[^\n]+\})/)
    process.stdout.write(`${stdout.match(/TRACE_MATERIALIZER_PHASE [^\n]+/g)?.join("\n") ?? ""}\n`)
    expect(match, `${stdout}\n${stderr}`).not.toBeNull()
    const measurement = JSON.parse(match![1]!) as {
      journalBytes: number
      beforeRSS: number
      afterRSS: number
      maxRSS: number
    }
    console.info("trace-materializer-multisegment-memory-rss", JSON.stringify(measurement))
    expect(measurement.journalBytes).toBeGreaterThanOrEqual(GIB)
    expect(measurement.maxRSS).toBeLessThanOrEqual(MAX_RSS_BYTES)
    expect(exitCode, stderr).toBe(0)
  } finally {
    fs.rmSync(traceRoot, { recursive: true, force: true })
  }
}, 600_000)
