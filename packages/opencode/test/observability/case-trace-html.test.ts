import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import nodeFs from "node:fs"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { renderCaseTraceHtml, writeProvenanceTraceHtmlFile } from "@/observability/case-trace-html"
import { normalizeTemporalReferences, writeJsonDocumentAtomic } from "@/observability/causal-ir"
import { captureRepositorySnapshot } from "@/observability/repository-snapshot"
import { provenanceTraceHtmlChunks, renderProvenanceTraceHtml } from "@/observability/causal-trace-viewer"
import {
  sanitizeTraceJson,
  sanitizeTraceJsonStringChunks,
  sanitizeTraceJsonValue,
} from "@/observability/case-trace"
import type { ProvenanceTraceView, TraceArtifact, TraceComponent, TraceSummary } from "@/observability/case-trace"

process.env.OPENCODE_CASE_TRACE_QUIET = "1"

function occurrences(haystack: string, needle: string) {
  return haystack.split(needle).length - 1
}

function artifactSnapshotPath(content: string) {
  return `artifacts/render-snapshots/sha256/${createHash("sha256").update(content).digest("hex")}`
}

function section(html: string, id: string) {
  const start = html.indexOf(`<section id="${id}"`)
  if (start === -1) return ""
  const end = html.indexOf("</section>", start)
  return end === -1 ? html.slice(start) : html.slice(start, end + "</section>".length)
}

function largeLegacyTrace(recordCount: number, artifact: TraceArtifact): TraceSummary {
  return {
    trace_version: "1.3",
    case_id: "streaming-html-large",
    run_id: "run_streaming_html_large",
    started_at: "2026-07-31T00:00:00.000Z",
    ended_at: "2026-07-31T00:00:01.000Z",
    duration_ms: 1000,
    status: "success",
    environment: {},
    token_usage: {},
    spans: [],
    events: Array.from({ length: recordCount }, (_, index) => ({
      event_id: `event_${index}`,
      component: "context",
      event_type: `context.chunk.${index}`,
      timestamp: "2026-07-31T00:00:00.000Z",
      time_ms: index,
      data: {
        payload: {
          type: "text",
          length: artifact.length,
          hash: artifact.hash,
          preview: artifact.preview,
          artifact_id: artifact.artifact_id,
          payload_ref: artifact.artifact_id,
        },
      },
    })),
    artifacts: [artifact],
    errors: [],
  }
}

function provenanceTrace(
  records: ProvenanceTraceView["records"],
  artifacts: TraceArtifact[] = [],
  dataflowEdges: ProvenanceTraceView["dataflow_edges"] = [],
): ProvenanceTraceView {
  return {
    trace_version: "6.0",
    manifest: {
      trace_version: "6.0",
      case_id: "task6-adversarial",
      run_id: "run_task6_adversarial",
      started_at: "2026-07-31T00:00:00.000Z",
      duration_ms: 1000,
      status: "success",
      server_status: "success",
      process_status: "success",
      case_status: "success",
      collection_mode: "passive_sidecar",
      behavior_impact: "none",
      environment: {},
      token_usage: {},
      files: {},
    },
    records,
    dataflow_edges: dataflowEdges,
    artifacts,
    metrics: {
      spans: 0,
      events: records.length,
      records: records.length,
      dataflow_edges: dataflowEdges.length,
      artifacts: artifacts.length,
      token_usage: {},
      trace_health: { issues: [], compaction_quality_flags: {} },
    },
  } as unknown as ProvenanceTraceView
}

describe("case trace HTML artifact storage", () => {
  test("continues to render legacy small-trace fields and inline artifact drill-down", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-legacy-artifact-root-"))
    const payload = "legacy artifact body"
    const artifact: TraceArtifact = {
      artifact_id: "artifact_legacy",
      kind: "text",
      label: "legacy.payload",
      path: "artifacts/sha256/legacy.txt",
      length: payload.length,
      hash: "legacy",
      preview: payload,
      created_at: "2026-07-31T00:00:00.000Z",
      availability: "bundled",
    }
    const trace = largeLegacyTrace(1, artifact)
    trace.trace_version = "1.0"
    trace.case_id = "legacy-readable"

    try {
      await fs.mkdir(path.dirname(path.join(dir, artifact.path)), { recursive: true })
      await fs.writeFile(path.join(dir, artifact.path), payload)
      const html = renderCaseTraceHtml(trace, {
        artifactDir: dir,
        artifactContents: { [artifact.artifact_id]: payload },
      })

      expect(html).toContain("legacy-readable")
      expect(html).toContain("context.chunk.0")
      expect(html).toContain(payload)
      expect(html).toContain(`href="${artifactSnapshotPath(payload)}"`)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("keeps 5000+ artifact references without embedding duplicate payload copies", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-large-artifact-root-"))
    const payload = `authoritative-artifact:${"x".repeat(64 * 1024)}`
    const artifact: TraceArtifact = {
      artifact_id: "artifact_authoritative",
      kind: "text",
      label: "context.payload",
      path: "artifacts/sha256/authoritative.txt",
      length: payload.length,
      hash: "authoritative",
      preview: payload.slice(0, 64),
      created_at: "2026-07-31T00:00:00.000Z",
      occurrences: 5001,
      availability: "bundled",
    }
    const trace = largeLegacyTrace(5001, artifact)

    try {
      await fs.mkdir(path.dirname(path.join(dir, artifact.path)), { recursive: true })
      await fs.writeFile(path.join(dir, artifact.path), payload)
      const html = renderCaseTraceHtml(trace, {
        artifactDir: dir,
        artifactContents: new Map([[artifact.artifact_id, payload]]),
      })

      expect(occurrences(html, payload)).toBe(0)
      expect(html).toContain(`href="${artifactSnapshotPath(payload)}"`)
      expect(html).toContain("context.chunk.0")
      expect(html).toContain("context.chunk.5000")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("writes 5000+ provenance records atomically in bounded chunks", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-streaming-trace-html-"))
    const target = path.join(dir, "trace.html")
    const artifact: TraceArtifact = {
      artifact_id: "artifact_streamed",
      kind: "text",
      label: "tool.output",
      path: "artifacts/sha256/streamed.txt",
      length: 128 * 1024,
      hash: "streamed",
      preview: "streamed artifact preview",
      created_at: "2026-07-31T00:00:00.000Z",
      occurrences: 5001,
      availability: "bundled",
    }
    const trace = {
      trace_version: "6.0",
      manifest: {
        trace_version: "6.0",
        case_id: "streaming-provenance-large",
        run_id: "run_streaming_provenance_large",
        started_at: "2026-07-31T00:00:00.000Z",
        duration_ms: 1000,
        status: "success",
        server_status: "success",
        process_status: "success",
        case_status: "success",
        collection_mode: "passive_sidecar",
        behavior_impact: "none",
        environment: {},
        token_usage: {},
        files: {
          trace: "trace.json",
          legacy_trace: "legacy-trace.json",
          provenance_trace: "provenance-trace.json",
          trace_html: "trace.html",
          records: "records.jsonl",
          raw_events: "raw-events.jsonl",
          partial_latest: "partial/latest.json",
        },
      },
      records: Array.from({ length: 5001 }, (_, index) => ({
        record_id: `record_${index}`,
        component: "tool",
        event_type: "tool.result",
        timestamp: "2026-07-31T00:00:00.000Z",
        time_ms: index,
        title: `tool result ${index}`,
        status: "success",
        artifact_refs: [artifact.artifact_id],
        data: {
          output: {
            type: "text",
            length: artifact.length,
            hash: artifact.hash,
            preview: artifact.preview,
            artifact_id: artifact.artifact_id,
            payload_ref: artifact.artifact_id,
          },
        },
      })),
      dataflow_edges: [],
      artifacts: [artifact],
      metrics: {
        spans: 0,
        events: 5001,
        records: 5001,
        dataflow_edges: 0,
        artifacts: 1,
        token_usage: {},
        trace_health: { issues: [], compaction_quality_flags: {} },
      },
    } as unknown as ProvenanceTraceView

    try {
      await fs.mkdir(path.dirname(path.join(dir, artifact.path)), { recursive: true })
      await fs.writeFile(path.join(dir, artifact.path), "streamed artifact")
      const stats = writeProvenanceTraceHtmlFile(target, trace, {
        forceStreaming: true,
        maxChunkBytes: 64 * 1024,
      })
      const html = await fs.readFile(target, "utf8")

      expect(stats.mode).toBe("streaming")
      expect(stats.record_count).toBe(5001)
      expect(stats.max_chunk_bytes).toBeLessThanOrEqual(64 * 1024)
      expect(stats.chunk_count).toBeGreaterThan(1)
      expect(html).toContain("record_0")
      expect(html).toContain("record_5000")
      expect(html).toContain(`href="${artifactSnapshotPath("streamed artifact")}"`)
      expect(html).not.toContain("x".repeat(1024))
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("keeps inline and streaming viewers semantically reachable through the same sections", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-viewer-parity-"))
    const target = path.join(dir, "trace.html")
    const artifact: TraceArtifact = {
      artifact_id: "artifact_parity",
      kind: "text",
      label: "parity.output",
      path: "artifacts/sha256/parity.txt",
      length: 100,
      hash: "parity",
      preview: "parity artifact",
      created_at: "2026-07-31T00:00:00.000Z",
      availability: "bundled",
    }
    const baseRecord = {
      component: "runtime",
      timestamp: "2026-07-31T00:00:00.000Z",
      status: "success",
      source_refs: [],
      input_refs: [],
      output_refs: [],
    }
    const records = [
      {
        ...baseRecord,
        record_id: "decision_parity",
        event_type: "decision",
        time_ms: 1,
        title: "Choose implementation",
        data: { decision_type: "implementation", chosen_action: "edit source", rationale: "parity rationale" },
      },
      {
        ...baseRecord,
        record_id: "llm_turn_parity",
        event_type: "llm.turn",
        component: "llm",
        time_ms: 2,
        title: "DeepSeek turn",
        token_usage: { input: 12, output: 7, total: 19 },
        data: { provider_id: "deepseek", model_id: "deepseek-v4-pro", agent_role: "main", finish_reason: "tool-calls" },
      },
      {
        ...baseRecord,
        record_id: "lifecycle_parity",
        event_type: "agent.lifecycle",
        time_ms: 3,
        title: "Main lifecycle",
        data: { phase: "turn.completed", summary: "lifecycle parity summary" },
      },
      {
        ...baseRecord,
        record_id: "subagent_parity",
        event_type: "subagent.call",
        component: "task",
        time_ms: 4,
        title: "Architecture subagent",
        artifact_refs: [artifact.artifact_id],
        data: { input: "inspect architecture", output: "subagent parity result", child_trace_mode: "inline_same_trace" },
      },
      {
        ...baseRecord,
        record_id: "evidence_parity",
        event_type: "evidence.semantic_fact",
        component: "tool",
        time_ms: 5,
        title: "Repository evidence",
        artifact_refs: [artifact.artifact_id],
        data: { source: "tool", category: "repo_fact", summary: "pricing owns discounts" },
      },
      {
        ...baseRecord,
        record_id: "context_parity",
        event_type: "context.compaction",
        component: "context",
        time_ms: 6,
        title: "Context compaction",
        artifact_refs: [artifact.artifact_id],
        data: { algorithm: "head-tail-summary", input: "before context", output: "after context" },
      },
      {
        ...baseRecord,
        record_id: "claim_parity",
        event_type: "response.claim",
        component: "result",
        time_ms: 7,
        title: "Final claim",
        data: {
          claim_id: "claim_parity",
          text: "Pricing owns discounts.",
          support_level: "direct",
          direct_evidence_refs: ["evidence:evidence_parity"],
          matched_evidence_refs: ["evidence:evidence_parity"],
        },
      },
    ]
    const trace = {
      trace_version: "6.0",
      manifest: {
        trace_version: "6.0",
        case_id: "viewer-parity",
        run_id: "run_viewer_parity",
        started_at: "2026-07-31T00:00:00.000Z",
        duration_ms: 10,
        status: "success",
        server_status: "success",
        process_status: "success",
        case_status: "success",
        collection_mode: "passive_sidecar",
        behavior_impact: "none",
        environment: {},
        token_usage: {},
        files: {},
      },
      records,
      dataflow_edges: [
        {
          edge_id: "edge_parity",
          from: { type: "decision", id: "decision_parity" },
          to: { type: "response_claim", id: "claim_parity" },
          relation: "decision_to_claim",
          label: "parity causal edge",
        },
      ],
      artifacts: [artifact],
      metrics: {
        spans: 0,
        events: records.length,
        records: records.length,
        dataflow_edges: 1,
        artifacts: 1,
        token_usage: {},
        trace_health: { issues: [], compaction_quality_flags: {} },
      },
    } as unknown as ProvenanceTraceView

    try {
      const inline = renderProvenanceTraceHtml(trace)
      writeProvenanceTraceHtmlFile(target, trace, { forceStreaming: true, maxChunkBytes: 4096 })
      const streaming = await fs.readFile(target, "utf8")
      const inlineSections = [...inline.matchAll(/<section id="([^"]+)"/g)].map((match) => match[1])
      const streamingSections = [...streaming.matchAll(/<section id="([^"]+)"/g)].map((match) => match[1])

      expect(streaming).toBe(inline)
      expect(streamingSections).toEqual(inlineSections)
      for (const [sectionID, semanticToken] of [
        ["semantic-pipeline", "decision_parity"],
        ["llm-turns", "llm_turn_parity"],
        ["lifecycle", "lifecycle parity summary"],
        ["subagents", "subagent parity result"],
        ["claim-evidence-matrix", "Pricing owns discounts."],
        ["evidence-facts", "pricing owns discounts"],
        ["component-dataflow", "parity causal edge"],
        ["io-inspector", "before context"],
        ["context-compaction", "head-tail-summary"],
        ["artifacts", artifact.artifact_id],
      ]) {
        expect(section(inline, sectionID)).toContain(semanticToken)
        expect(section(streaming, sectionID)).toContain(semanticToken)
      }
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test(
    "uses the bounded writer for a real 5000-record trace and stores one authoritative payload",
    async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-streaming-trace-runtime-"))
      const packageDir = path.resolve(import.meta.dir, "../..")
      const script = path.join(dir, "large-runtime-trace.ts")
      const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href
      const marker = "task-6-authoritative-payload:"
      const payload = marker + "z".repeat(4096)

      try {
        await fs.writeFile(
          script,
          [
            `import fs from "node:fs"`,
            `import path from "node:path"`,
            `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
            `const payload = ${JSON.stringify(payload)}`,
            `const active = CaseTrace.configure() as any`,
            `for (let index = 0; index < 5001; index++) CaseTrace.node({ node_id: "task6_record_" + index, kind: "execution.observation", component: "tool", title: "task6 record " + index, data: { output: payload } })`,
            `const rssBefore = process.memoryUsage().rss`,
            `const finalizationStarted = performance.now()`,
            `CaseTrace.finish({ status: "success", result: { answer: "agent-visible-result" } })`,
            `fs.writeFileSync(path.join(active.caseDir, "task6-finalization-stats.json"), JSON.stringify({ rss_before: rssBefore, rss_after: process.memoryUsage().rss, finalization_ms: performance.now() - finalizationStarted }))`,
            `process.stdout.write("agent-visible-result")`,
          ].join("\n"),
        )

        const proc = Bun.spawn([process.execPath, script], {
          cwd: packageDir,
          env: {
            ...process.env,
            OPENCODE_CASE_TRACE: "1",
            OPENCODE_CASE_ID: "task6-large-runtime",
            OPENCODE_CASE_TRACE_DIR: dir,
            OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH: "96",
            OPENCODE_CASE_TRACE_PARTIAL_INTERVAL_MS: "3600000",
          },
          stdout: "pipe",
          stderr: "pipe",
        })
        const code = await proc.exited
        const stdout = await new Response(proc.stdout).text()
        const stderr = await new Response(proc.stderr).text()
        const caseDir = path.join(dir, "task6-large-runtime")
        const traceText = await fs.readFile(path.join(caseDir, "trace.json"), "utf8")
        const provenanceText = await fs.readFile(path.join(caseDir, "provenance-trace.json"), "utf8")
        const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
        const trace = JSON.parse(traceText) as any
        const finalizationStats = JSON.parse(
          await fs.readFile(path.join(caseDir, "task6-finalization-stats.json"), "utf8"),
        ) as { rss_before: number; rss_after: number; finalization_ms: number }
        const artifactDir = path.join(caseDir, "artifacts", "sha256")
        const artifactFiles = await fs.readdir(artifactDir)
        let authoritativeCopies = 0
        for (const file of artifactFiles) {
          const content = await fs.readFile(path.join(artifactDir, file), "utf8")
          if (content.includes(marker)) authoritativeCopies += 1
        }

        expect(code).toBe(0)
        expect(stderr).toBe("")
        expect(stdout).toBe("agent-visible-result")
        expect(trace.records.length).toBeGreaterThanOrEqual(5001)
        expect(authoritativeCopies).toBe(1)
        expect(traceText).not.toContain(payload)
        expect(provenanceText).not.toContain(payload)
        expect(html).not.toContain(payload)
        expect(html).toContain("Trace v6.0")
        expect(html).not.toContain("Records are streamed below in causal order.")
        expect(html).toContain("task6_record_0")
        expect(html).toContain("task6_record_5000")
        expect(finalizationStats.finalization_ms).toBeLessThan(60_000)
        expect(finalizationStats.rss_after).toBeLessThan(2 * 1024 * 1024 * 1024)
        console.info("task6-large-trace-stats", JSON.stringify(finalizationStats))
      } finally {
        await fs.rm(dir, { recursive: true, force: true })
      }
    },
    120_000,
  )

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    test(`keeps repeated ${signal} partial finalization byte-identical`, async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), `opencode-idempotent-${signal.toLowerCase()}-`))
      const packageDir = path.resolve(import.meta.dir, "../..")
      const script = path.join(dir, "idempotent-signal-trace.ts")
      const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

      try {
        await fs.writeFile(
          script,
          [
            `import fs from "node:fs"`,
            `import path from "node:path"`,
            `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
            `const trace = CaseTrace.configure({ input: { prompt: "idempotent signal" } }) as any`,
            `CaseTrace.node({ node_id: "before_signal", kind: "execution.observation", component: "runtime", title: "before signal" })`,
            `trace.flushForSignal(${JSON.stringify(signal)})`,
            `const firstTrace = fs.readFileSync(trace.traceFile)`,
            `const firstHtml = fs.readFileSync(trace.htmlFile)`,
            `const firstState = JSON.parse(firstTrace.toString("utf8")).manifest`,
            `trace.flushForSignal(${JSON.stringify(signal)})`,
            `const secondTrace = fs.readFileSync(trace.traceFile)`,
            `const secondHtml = fs.readFileSync(trace.htmlFile)`,
            `const secondState = JSON.parse(secondTrace.toString("utf8")).manifest`,
            `process.stdout.write(JSON.stringify({ trace_equal: firstTrace.equals(secondTrace), html_equal: firstHtml.equals(secondHtml), first_state: { status: firstState.status, server_status: firstState.server_status, process_status: firstState.process_status, case_status: firstState.case_status, shutdown_signal: firstState.shutdown_signal, shutdown_disposition: firstState.shutdown_disposition }, second_state: { status: secondState.status, server_status: secondState.server_status, process_status: secondState.process_status, case_status: secondState.case_status, shutdown_signal: secondState.shutdown_signal, shutdown_disposition: secondState.shutdown_disposition } }))`,
          ].join("\n"),
        )

        const proc = Bun.spawn([process.execPath, script], {
          cwd: packageDir,
          env: {
            ...process.env,
            OPENCODE_CASE_TRACE: "1",
            OPENCODE_CASE_ID: `task6-idempotent-${signal.toLowerCase()}`,
            OPENCODE_CASE_TRACE_DIR: dir,
          },
          stdout: "pipe",
          stderr: "pipe",
        })
        const code = await proc.exited
        const stdout = await new Response(proc.stdout).text()
        const stderr = await new Response(proc.stderr).text()

        expect(code).toBe(0)
        expect(stderr).toBe("")
        const result = JSON.parse(stdout)
        expect(result.trace_equal).toBe(true)
        expect(result.html_equal).toBe(true)
        expect(result.second_state).toEqual(result.first_state)
        expect(result.first_state).toMatchObject({
          status: "cancelled",
          server_status: "cancelled",
          process_status: "cancelled",
          case_status: "cancelled",
          shutdown_signal: signal,
          shutdown_disposition: "interrupted_before_case_completion",
        })
      } finally {
        await fs.rm(dir, { recursive: true, force: true })
      }
    })
  }

  test("keeps completed-case signal finalization stable across repeated calls", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-idempotent-completed-signal-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "idempotent-completed-signal.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import fs from "node:fs"`,
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure() as any`,
          `CaseTrace.responseOutput({ text: "Completed before shutdown.", metadata: { response_role: "final_answer", visibility: "user_visible", is_final_for_case: true, finality_source: "explicit" } })`,
          `CaseTrace.finish({ status: "success", result: { answer: "complete" } })`,
          `trace.flushForSignal("SIGTERM")`,
          `const first = fs.readFileSync(trace.traceFile)`,
          `trace.flushForSignal("SIGTERM")`,
          `const second = fs.readFileSync(trace.traceFile)`,
          `const manifest = JSON.parse(second.toString("utf8")).manifest`,
          `process.stdout.write(JSON.stringify({ equal: first.equals(second), status: manifest.status, server_status: manifest.server_status, process_status: manifest.process_status, case_status: manifest.case_status, signal: manifest.shutdown_signal, disposition: manifest.shutdown_disposition }))`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-idempotent-completed-signal",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stdout = await new Response(proc.stdout).text()
      const stderr = await new Response(proc.stderr).text()

      expect(code, stderr).toBe(0)
      expect(stderr).toBe("")
      expect(JSON.parse(stdout)).toEqual({
        equal: true,
        status: "success",
        server_status: "cancelled",
        process_status: "cancelled",
        case_status: "success",
        signal: "SIGTERM",
        disposition: "graceful_after_case_completion",
      })
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("writes large causal documents without constructing one full JSON buffer", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-streaming-causal-json-"))
    const target = path.join(dir, "trace.json")
    const document = {
      trace_version: "6.0",
      manifest: { case_id: "streaming-json", status: "success" },
      records: Array.from({ length: 5001 }, (_, index) => ({
        record_id: `json_record_${index}`,
        artifact_ref: "artifact_authoritative",
        preview: `record ${index}`,
      })),
      artifacts: [
        {
          artifact_id: "artifact_authoritative",
          path: "artifacts/sha256/authoritative.txt",
          length: 1024 * 1024,
        },
      ],
    }

    try {
      const stats = writeJsonDocumentAtomic(target, document, { maxChunkBytes: 32 * 1024 })
      const stored = await fs.readFile(target)

      expect(JSON.parse(stored.toString("utf8"))).toEqual(document)
      expect(stats.mode).toBe("streaming_json")
      expect(stats.max_chunk_bytes).toBeLessThanOrEqual(32 * 1024)
      expect(stats.chunk_count).toBeGreaterThan(1)
      expect(stats.top_level_array_items).toBe(5002)
      expect(stats.full_document_buffered).toBe(false)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("keeps streaming JSON sanitization deeply equal to the legacy whole-document sanitizer", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-streaming-json-sanitize-parity-"))
    const target = path.join(dir, "trace.json")
    const shared = { nested_secret: "visible", api_key: "must-redact", url: new URL("https://user:pass@example.com/x") }
    const document = {
      manifest: {
        token_usage: { input: 11, output: 7, total: 18 },
        access_token: "must-redact",
      },
      records: [
        { record_id: "one", data: shared },
        { record_id: "two", data: shared, bigint: 12n, missing: undefined },
      ],
      diagnostics: [new Error("sanitizer parity")],
    }

    try {
      writeJsonDocumentAtomic(target, document, {
        maxChunkBytes: 1024,
        sanitize: sanitizeTraceJsonValue,
        sanitizeStringChunks: sanitizeTraceJsonStringChunks,
        normalizeTemporalReferences: true,
      })
      const streamed = JSON.parse(await fs.readFile(target, "utf8"))
      const legacy = JSON.parse(JSON.stringify(sanitizeTraceJson(document)))

      expect(streamed).toEqual(legacy)
      expect(streamed.manifest.access_token).toBe("[REDACTED]")
      expect(streamed.records[0].data.api_key).toBe("[REDACTED]")
      expect(streamed.records[1].bigint).toBe("12")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("does not inspect or serialize fallback JSON after a hard-link succeeds", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-hard-link-no-fallback-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "hard-link-no-fallback.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import fs from "node:fs"`,
          `import path from "node:path"`,
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure() as any`,
          `const source = path.join(trace.caseDir, "hard-link-source.json")`,
          `const target = path.join(trace.caseDir, "hard-link-target.json")`,
          `fs.writeFileSync(source, "{\\"authoritative\\":true}")`,
          `const poison = new Proxy({}, { get() { throw new Error("fallback inspected") }, ownKeys() { throw new Error("fallback serialized") } })`,
          `const linked = trace.safeLinkOrWrite(source, target, poison)`,
          `process.stdout.write(JSON.stringify({ linked, same_inode: fs.statSync(source).ino === fs.statSync(target).ino, content: fs.readFileSync(target, "utf8") }))`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-hard-link-no-fallback",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stdout = await new Response(proc.stdout).text()
      const stderr = await new Response(proc.stderr).text()

      expect(code).toBe(0)
      expect(stderr).toBe("")
      expect(JSON.parse(stdout)).toEqual({
        linked: true,
        same_inode: true,
        content: '{"authoritative":true}',
      })
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("keeps repository snapshot metadata separate from file content", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-repository-snapshot-index-"))
    const marker = "repository-snapshot-content-marker:"
    const file = path.join(dir, "large-untracked.txt")

    try {
      const git = Bun.spawnSync(["git", "init", "-q", dir])
      expect(git.exitCode).toBe(0)
      await fs.writeFile(file, marker + "r".repeat(2 * 1024 * 1024))

      const snapshot = captureRepositorySnapshot(dir) as any
      const serialized = JSON.stringify(snapshot)

      expect(snapshot.available).toBe(true)
      expect(snapshot.content_mode).toBe("index_only")
      expect(snapshot.file_metadata["large-untracked.txt"]).toMatchObject({
        kind: "file",
        size: 2 * 1024 * 1024 + marker.length,
      })
      expect(snapshot.file_hashes["large-untracked.txt"]).toMatch(/^[a-f0-9]{64}$/)
      expect(serialized).not.toContain(marker)
      expect(serialized.length).toBeLessThan(16 * 1024)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("keeps Agent-visible prompt, message, and tool-result bytes unchanged", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-passive-byte-identity-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "passive-byte-identity.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const prompt = Buffer.from("prompt:\\u0000需求理解\\n", "utf8")`,
          `const message = Buffer.from(JSON.stringify({ role: "user", content: "implement exactly", order: 1 }), "utf8")`,
          `const toolResult = Buffer.from("tool-result:\\u0000ok\\n", "utf8")`,
          `CaseTrace.configure({ input: { prompt: prompt.toString("base64") } })`,
          `const span = CaseTrace.get()?.startSpan({ component: "tool", operation: "read", input: { message: message.toString("base64") } })`,
          `span?.end({ output: { bytes: toolResult.toString("base64") }, status: "success" })`,
          `CaseTrace.finish({ status: "success", result: { answer: "unchanged" } })`,
          `process.stdout.write(Buffer.concat([prompt, message, toolResult]))`,
        ].join("\n"),
      )

      const run = async (enabled: boolean) => {
        const proc = Bun.spawn([process.execPath, script], {
          cwd: packageDir,
          env: {
            ...process.env,
            OPENCODE_CASE_TRACE: enabled ? "1" : "0",
            OPENCODE_CASE_ID: enabled ? "task6-passive-enabled" : "task6-passive-disabled",
            OPENCODE_CASE_TRACE_DIR: dir,
          },
          stdout: "pipe",
          stderr: "pipe",
        })
        const code = await proc.exited
        const stdout = Buffer.from(await new Response(proc.stdout).arrayBuffer())
        const stderr = await new Response(proc.stderr).text()
        return {
          code,
          stdout,
          stderr,
          sha256: createHash("sha256").update(stdout).digest("hex"),
        }
      }

      const baseline = await run(false)
      const traced = await run(true)

      expect(baseline.code).toBe(0)
      expect(traced.code).toBe(0)
      expect(baseline.stderr).toBe("")
      expect(traced.stderr).toBe("")
      expect(traced.stdout).toEqual(baseline.stdout)
      expect(traced.sha256).toBe(baseline.sha256)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test(
    "bounds every HTML generator yield and writer buffer for 5001 one-kilobyte records",
    async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-html-record-chunks-"))
      const target = path.join(dir, "trace.html")
      const payload = `one-kilobyte-record:${"x".repeat(1024)}`
      const records = Array.from({ length: 5001 }, (_, index) => ({
        record_id: `bounded_record_${index}`,
        component: "tool" as const,
        event_type: "evidence.semantic_fact",
        timestamp: "2026-07-31T00:00:00.000Z",
        time_ms: index,
        title: `bounded record ${index}`,
        status: "success" as const,
        source_refs: [`tool_result:${index}`],
        data: { source: "tool", category: "repo_fact", summary: payload, output: payload },
      }))
      const trace = provenanceTrace(records)
      const maxChunkBytes = 4096

      try {
        const chunks = [...provenanceTraceHtmlChunks(trace, { maxChunkBytes })]
        const stats = writeProvenanceTraceHtmlFile(target, trace, {
          forceStreaming: true,
          maxChunkBytes,
        })
        const streaming = await fs.readFile(target, "utf8")

        expect(Math.max(...chunks.map((chunk) => Buffer.byteLength(chunk, "utf8")))).toBeLessThanOrEqual(
          maxChunkBytes,
        )
        expect(stats.max_generator_chunk_bytes).toBeLessThanOrEqual(maxChunkBytes)
        expect(stats.max_temporary_buffer_bytes).toBeLessThanOrEqual(maxChunkBytes)
        expect(streaming).toBe(chunks.join(""))
        expect(streaming).toBe(renderProvenanceTraceHtml(trace))
        expect(streaming).toContain("bounded_record_0")
        expect(streaming).toContain("bounded_record_5000")
        expect(section(streaming, "agent-flow")).toContain("one-kilobyte-record:")
        expect(section(streaming, "io-inspector")).toContain(payload)
        expect(section(streaming, "semantic-facts")).toContain(payload)
        expect(section(streaming, "evidence-facts")).toContain(payload)
      } finally {
        await fs.rm(dir, { recursive: true, force: true })
      }
    },
    120_000,
  )

  test("normalizes and sanitizes direct cyclic results with stable path markers", () => {
    const cycle: Record<string, unknown> = {
      answer: "cycle-safe-result",
      source_refs: ["recent_prompt", "node:durable"],
    }
    cycle.self = cycle

    const temporal = normalizeTemporalReferences(cycle)
    const sanitized = sanitizeTraceJson(cycle) as Record<string, unknown>

    expect(temporal.selectors).toEqual(["recent_prompt"])
    expect(temporal.value).toEqual({
      answer: "cycle-safe-result",
      source_refs: ["node:durable"],
      self: "[Circular:$]",
    })
    expect(sanitized).toEqual(temporal.value)
    expect(() => JSON.stringify(sanitized)).not.toThrow()
  })

  test("finishes a direct cyclic CaseTrace result without losing result semantics", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-direct-cycle-result-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "direct-cycle-result.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `CaseTrace.configure({ input: { prompt: "direct cycle" } })`,
          `const result: any = { answer: "direct-cycle-answer", source_refs: ["recent_prompt", "node:durable"] }`,
          `result.self = result`,
          `CaseTrace.finish({ status: "success", result })`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-direct-cycle-result",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stderr = await new Response(proc.stderr).text()
      const traceText = await fs.readFile(path.join(dir, "task6-direct-cycle-result", "trace.json"), "utf8")
      const trace = JSON.parse(traceText) as any

      expect(code).toBe(0)
      expect(stderr).toBe("")
      expect(trace.manifest.status).toBe("success")
      expect(trace.manifest.result.answer).toBe("direct-cycle-answer")
      expect(trace.manifest.result.source_refs).toEqual(["node:durable"])
      expect(trace.manifest.result.self).toBe("[Circular:$.manifest.result]")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("keeps a real SIGTERM cycle cancelled and preserves its persisted facts", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-sigterm-cycle-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "sigterm-cycle.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure({ input: { prompt: "cycle signal" } }) as any`,
          `CaseTrace.node({ node_id: "persisted_before_cycle_signal", kind: "execution.observation", component: "runtime", title: "persisted before cycle signal" })`,
          `const cycle: any = { marker: "cycle-result-before-sigterm", source_refs: ["recent_prompt", "node:persisted_before_cycle_signal"] }`,
          `cycle.self = cycle`,
          `trace.result = cycle`,
          `process.kill(process.pid, "SIGTERM")`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-sigterm-cycle",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stderr = await new Response(proc.stderr).text()
      const caseDir = path.join(dir, "task6-sigterm-cycle")
      const traceText = await fs.readFile(path.join(caseDir, "trace.json"), "utf8")
      const trace = JSON.parse(traceText) as any

      expect(code).toBe(143)
      expect(stderr).toBe("")
      expect(trace.manifest).toMatchObject({
        status: "cancelled",
        server_status: "cancelled",
        process_status: "cancelled",
        case_status: "cancelled",
        shutdown_signal: "SIGTERM",
      })
      expect(traceText).toContain("persisted_before_cycle_signal")
      expect(traceText).toContain("cycle-result-before-sigterm")
      expect(traceText).toMatch(/\[Circular:\$\.[^\]]+\]/)
      expect(await fs.readFile(path.join(caseDir, "trace.html"), "utf8")).toContain("case cancelled")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("guards cancelled signal facts before finalization errors and rejects later error finish", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-signal-error-guard-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "signal-error-guard.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import fs from "node:fs"`,
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure({ input: { prompt: "signal error guard" } }) as any`,
          `CaseTrace.node({ node_id: "persisted_before_signal_error", kind: "execution.observation", component: "runtime", title: "persisted before signal error" })`,
          `trace.evaluateConstraints = () => { throw new Error("forced finalization failure") }`,
          `trace.flushForSignal("SIGTERM")`,
          `trace.finish({ status: "error", error: new Error("must not overwrite signal") })`,
          `const stored = JSON.parse(fs.readFileSync(trace.traceFile, "utf8"))`,
          `process.stdout.write(JSON.stringify({ manifest: stored.manifest, has_fact: stored.records.some((record: any) => record.record_id === "persisted_before_signal_error") }))`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-signal-error-guard",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stderr = await new Response(proc.stderr).text()
      const result = JSON.parse(await new Response(proc.stdout).text())

      expect(code).toBe(0)
      expect(stderr).toBe("")
      expect(result.has_fact).toBe(true)
      expect(result.manifest).toMatchObject({
        status: "cancelled",
        server_status: "cancelled",
        process_status: "cancelled",
        case_status: "cancelled",
        shutdown_signal: "SIGTERM",
      })
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("preserves parent path semantics and JSON undefined rules in streaming output", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-streaming-json-paths-"))
    const target = path.join(dir, "trace.json")
    const document = {
      source_refs: ["recent_prompt", "node:root"],
      records: [
        {
          record_id: "reviewer-reproduction",
          source_refs: ["recent_prompt", "node:record"],
          omitted: undefined,
          values: [undefined, "kept"],
        },
      ],
      omitted: undefined,
    }

    try {
      writeJsonDocumentAtomic(target, document, {
        maxChunkBytes: 1024,
        sanitize: sanitizeTraceJsonValue,
        sanitizeStringChunks: sanitizeTraceJsonStringChunks,
        normalizeTemporalReferences: true,
      })
      const parsed = JSON.parse(await fs.readFile(target, "utf8"))

      expect(parsed).toEqual({
        source_refs: ["node:root"],
        records: [{ record_id: "reviewer-reproduction", source_refs: ["node:record"], values: [null, "kept"] }],
      })

      writeJsonDocumentAtomic(target, undefined, {
        sanitize: sanitizeTraceJsonValue,
        sanitizeStringChunks: sanitizeTraceJsonStringChunks,
        normalizeTemporalReferences: true,
      })
      expect(JSON.parse(await fs.readFile(target, "utf8"))).toBeNull()

      await fs.writeFile(target, '{"stable":true}')
      expect(() =>
        writeJsonDocumentAtomic(target, { broken: 1n }, { sanitize: (value) => value }),
      ).toThrow()
      expect(JSON.parse(await fs.readFile(target, "utf8"))).toEqual({ stable: true })
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("fails closed for traversal, absolute, and executable artifact paths", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-artifact-path-policy-"))
    const secret = path.join(path.dirname(dir), `${path.basename(dir)}-secret.txt`)
    const secretText = "private-artifact-secret-must-not-render"
    const unsafePaths = ["../" + path.basename(secret), secret, "javascript:alert(1)", "data:text/html,owned"]
    const artifacts = unsafePaths.map(
      (artifactPath, index): TraceArtifact => ({
        artifact_id: `unsafe_artifact_${index}`,
        kind: "text",
        label: `unsafe ${index}`,
        path: artifactPath,
        length: secretText.length,
        hash: `unsafe-${index}`,
        preview: `unsafe preview ${index}`,
        created_at: "2026-07-31T00:00:00.000Z",
        availability: "bundled",
      }),
    )

    try {
      await fs.writeFile(secret, secretText)
      const legacy = largeLegacyTrace(1, artifacts[0])
      const legacyHtml = renderCaseTraceHtml(legacy, { artifactDir: dir })
      const record = {
        record_id: "unsafe_artifact_record",
        component: "tool" as const,
        event_type: "tool.result",
        timestamp: "2026-07-31T00:00:00.000Z",
        time_ms: 1,
        artifact_refs: artifacts.map((artifact) => artifact.artifact_id),
        data: { output: "unsafe refs" },
      }
      const viewerHtml = renderProvenanceTraceHtml(provenanceTrace([record], artifacts))

      expect(legacyHtml).not.toContain(secretText)
      expect(legacyHtml).not.toContain(`href="${artifacts[0].path}"`)
      expect(viewerHtml).not.toContain('href="../')
      expect(viewerHtml).not.toContain('href="javascript:')
      expect(viewerHtml).not.toContain('href="data:')
      expect(viewerHtml).not.toContain(`href="${secret}"`)
      expect(occurrences(viewerHtml, "Artifact unavailable")).toBeGreaterThanOrEqual(unsafePaths.length)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
      await fs.rm(secret, { force: true })
    }
  })

  test("stores only an irreversible symlink target digest in index-only snapshots", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-snapshot-symlink-"))
    const privateRoot = await fs.mkdtemp(path.join(os.tmpdir(), "private-repository-target-"))
    const privateTarget = path.join(privateRoot, "closed-source-kernel-secret.bin")
    const link = path.join(dir, "private-link")

    try {
      expect(Bun.spawnSync(["git", "init", "-q", dir]).exitCode).toBe(0)
      await fs.writeFile(privateTarget, "private content")
      await fs.symlink(privateTarget, link)

      const snapshot = captureRepositorySnapshot(dir) as any
      const serialized = JSON.stringify(snapshot)

      expect(snapshot.file_hashes["private-link"]).toMatch(/^symlink_sha256:[a-f0-9]{64}$/)
      expect(snapshot.file_metadata["private-link"]).toMatchObject({ kind: "symlink" })
      expect(snapshot.file_metadata["private-link"]).not.toHaveProperty("symlink_target")
      expect(serialized).not.toContain(privateTarget)
      expect(serialized).not.toContain(privateRoot)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
      await fs.rm(privateRoot, { recursive: true, force: true })
    }
  })

  test("recovers a coherent cancelled partial and HTML after SIGTERM trace and first HTML write failures", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-sigterm-emergency-recovery-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "sigterm-emergency-recovery.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure({ input: { prompt: "emergency terminal recovery" } }) as any`,
          `CaseTrace.node({ node_id: "persisted_before_emergency", kind: "execution.observation", component: "runtime", title: "persisted before emergency" })`,
          `const originalSafeWrite = trace.safeWrite.bind(trace)`,
          `let terminalHtmlFailures = 0`,
          `trace.safeWrite = (target: string, content: unknown) => {`,
          `  if (target === trace.traceFile) return false`,
          `  if (target === trace.htmlFile && terminalHtmlFailures++ === 0) return false`,
          `  return originalSafeWrite(target, content)`,
          `}`,
          `process.kill(process.pid, "SIGTERM")`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-sigterm-emergency-recovery",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stderr = await new Response(proc.stderr).text()
      const caseDir = path.join(dir, "task6-sigterm-emergency-recovery")
      const partialText = await fs.readFile(path.join(caseDir, "partial/latest.json"), "utf8")
      const html = await fs.readFile(path.join(caseDir, "trace.html"), "utf8")
      const manifest = JSON.parse(await fs.readFile(path.join(caseDir, "manifest.json"), "utf8"))
      const partial = JSON.parse(partialText)

      expect(code).toBe(143)
      expect(stderr).toContain("[opencode-observability] terminal trace persistence failed")
      expect(stderr).toContain('"trace":false')
      expect(stderr).toContain('"canonical_removed":true')
      expect(partial.manifest).toMatchObject({
        status: "cancelled",
        process_status: "cancelled",
        shutdown_signal: "SIGTERM",
      })
      expect(manifest).toMatchObject({ status: "cancelled", shutdown_signal: "SIGTERM" })
      expect(partialText).toContain("persisted_before_emergency")
      expect(html).toContain("case cancelled")
      expect(html).toContain("persisted_before_emergency")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("reports an explicit terminal persistence failure when every critical SIGTERM path is unwritable", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-sigterm-all-unwritable-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "sigterm-all-unwritable.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure({ input: { prompt: "all terminal paths unwritable" } }) as any`,
          `CaseTrace.node({ node_id: "persisted_before_total_failure", kind: "execution.observation", component: "runtime", title: "persisted before total failure" })`,
          `const blocked = new Set([trace.traceFile, trace.partialFile, trace.manifestFile, trace.htmlFile])`,
          `const originalSafeWrite = trace.safeWrite.bind(trace)`,
          `trace.safeWrite = (target: string, content: unknown) => blocked.has(target) ? false : originalSafeWrite(target, content)`,
          `trace.safeLinkOrWrite = (_source: string, target: string) => blocked.has(target) ? false : false`,
          `process.kill(process.pid, "SIGTERM")`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-sigterm-all-unwritable",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stderr = await new Response(proc.stderr).text()

      expect(code).toBe(143)
      expect(stderr).toContain("[opencode-observability] terminal trace persistence failed")
      expect(stderr).toContain("task6-sigterm-all-unwritable")
      expect(stderr).toContain("SIGTERM")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test(
    "previews and streams a 32MiB single record without constructing the full record string",
    async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-single-record-budget-"))
      const jsonTarget = path.join(dir, "trace.json")
      const marker = "single-record-prefix:"
      const payload = marker + "x".repeat(32 * 1024 * 1024)
      const output = { payload, nested: { verdict: "preserved" } }
      const record = {
        record_id: "single_record_32mib",
        component: "tool" as const,
        event_type: "tool.result",
        timestamp: "2026-07-31T00:00:00.000Z",
        time_ms: 1,
        data: { output },
      }
      const trace = provenanceTrace([record])
      const originalStringify = JSON.stringify

      try {
        JSON.stringify = ((value: unknown, ...args: unknown[]) => {
          if (value === output) throw new Error("viewer attempted an unbounded stringify")
          return originalStringify(value, ...(args as []))
        }) as typeof JSON.stringify
        const html = renderProvenanceTraceHtml(trace)
        expect(section(html, "io-inspector")).toContain(marker)
        expect(html.length).toBeLessThan(2 * 1024 * 1024)
      } finally {
        JSON.stringify = originalStringify
      }

      try {
        const stats = writeJsonDocumentAtomic(jsonTarget, { records: [record] }, { maxChunkBytes: 4096 })
        const parsed = JSON.parse(await fs.readFile(jsonTarget, "utf8"))

        expect(stats.max_chunk_bytes).toBeLessThanOrEqual(4096)
        expect(stats.max_encoder_temporary_bytes).toBeLessThanOrEqual(4096)
        expect(parsed.records[0].data.output.payload.length).toBe(payload.length)
        expect(parsed.records[0].data.output.payload.startsWith(marker)).toBe(true)
        expect(parsed.records[0].data.output.nested.verdict).toBe("preserved")
      } finally {
        await fs.rm(dir, { recursive: true, force: true })
      }
    },
    120_000,
  )

  test("keeps whole-root self and cross-field cycle paths deeply equivalent to sanitizeTraceJson", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-whole-root-cycle-"))
    const target = path.join(dir, "trace.json")
    const root: Record<string, unknown> = { name: "root" }
    const child: Record<string, unknown> = { name: "child", parent: root }
    root.self = root
    root.left = child
    root.right = { child }

    try {
      const expected = sanitizeTraceJson(root)
      writeJsonDocumentAtomic(target, root, {
        maxChunkBytes: 1024,
        sanitize: sanitizeTraceJsonValue,
        sanitizeStringChunks: sanitizeTraceJsonStringChunks,
        normalizeTemporalReferences: true,
      })
      const parsed = JSON.parse(await fs.readFile(target, "utf8"))

      expect(parsed).toEqual(expected)
      expect(parsed.self).toBe("[Circular:$]")
      expect(parsed.left.parent).toBe("[Circular:$]")
      expect(parsed.right.child.parent).toBe("[Circular:$]")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("streams Unicode, controls, and lone surrogates byte-identically to JSON.stringify", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-streaming-json-unicode-"))
    const target = path.join(dir, "trace.json")
    const document = {
      text: "中文🙂 quotation: \" slash: \\",
      controls: "line\nnull\0tab\t",
      lone_high: "\ud800",
      lone_low: "\udfff",
      array: ["保留", undefined, "🙂"],
    }

    try {
      writeJsonDocumentAtomic(target, document, { maxChunkBytes: 1024 })
      expect(await fs.readFile(target, "utf8")).toBe(JSON.stringify(document))
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("authorizes artifact hrefs by the real case artifact root and rejects symlink escapes", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-artifact-realpath-"))
    const outsideDir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-artifact-outside-"))
    const artifactDir = path.join(dir, "artifacts/sha256")
    const target = path.join(dir, "trace.html")
    const validPath = "artifacts/sha256/valid.txt"
    const escapePath = "artifacts/sha256/escape.txt"
    const artifacts: TraceArtifact[] = [
      {
        artifact_id: "artifact_valid_realpath",
        kind: "text",
        label: "valid",
        path: validPath,
        length: 5,
        hash: "valid",
        preview: "valid",
        created_at: "2026-07-31T00:00:00.000Z",
        availability: "bundled",
      },
      {
        artifact_id: "artifact_symlink_escape",
        kind: "text",
        label: "escape",
        path: escapePath,
        length: 6,
        hash: "escape",
        preview: "escape",
        created_at: "2026-07-31T00:00:00.000Z",
        availability: "bundled",
      },
    ]
    const record = {
      record_id: "artifact_realpath_record",
      component: "tool" as const,
      event_type: "tool.result",
      timestamp: "2026-07-31T00:00:00.000Z",
      time_ms: 1,
      artifact_refs: artifacts.map((artifact) => artifact.artifact_id),
      data: { output: "artifact links" },
    }
    const trace = provenanceTrace([record], artifacts)

    try {
      await fs.mkdir(artifactDir, { recursive: true })
      await fs.writeFile(path.join(dir, validPath), "valid")
      const outside = path.join(outsideDir, "private.txt")
      await fs.writeFile(outside, "secret")
      await fs.symlink(outside, path.join(dir, escapePath))

      writeProvenanceTraceHtmlFile(target, trace, { forceStreaming: true, maxChunkBytes: 4096 })
      const html = await fs.readFile(target, "utf8")
      const pure = renderProvenanceTraceHtml(trace)

      expect(html).toContain(`href="${artifactSnapshotPath("valid")}"`)
      expect(html).not.toContain(`href="${escapePath}"`)
      expect(html).toContain("artifact_symlink_escape")
      expect(html).toContain("Artifact unavailable")
      expect(pure).not.toContain(`href="${validPath}"`)
      expect(pure).not.toContain(`href="${escapePath}"`)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
      await fs.rm(outsideDir, { recursive: true, force: true })
    }
  })

  test(
    "sanitizes production JSON incrementally without a whole-root clone or full-string replace",
    async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-production-sanitize-budget-"))
      const target = path.join(dir, "trace.json")
      const payload = `production-sanitize-prefix:${"x".repeat(8 * 1024 * 1024)}`
      const document = {
        manifest: { status: "success" },
        records: [{ record_id: "large-sanitize-record", data: { payload } }],
      }
      const originalReplace = String.prototype.replace
      let wholeRootCalls = 0
      let maxReplaceInput = 0
      let replaceWork = 0

      try {
        const replaceTrap = function (
          this: string,
          searchValue: string | RegExp,
          replaceValue: string | ((substring: string, ...args: unknown[]) => string),
        ) {
          const length = this.length
          maxReplaceInput = Math.max(maxReplaceInput, length)
          replaceWork += length
          if (length > 64 * 1024) throw new Error(`unbounded production sanitizer replace: ${length}`)
          return (originalReplace as Function).call(this, searchValue, replaceValue) as string
        }
        String.prototype.replace = replaceTrap as typeof String.prototype.replace

        const stats = writeJsonDocumentAtomic(target, document, {
          maxChunkBytes: 4096,
          sanitize: (value, key, valuePath) => {
            if (value === document) wholeRootCalls += 1
            return sanitizeTraceJsonValue(value, key, valuePath)
          },
          sanitizeStringChunks: sanitizeTraceJsonStringChunks,
          normalizeTemporalReferences: true,
        })
        const parsed = JSON.parse(await fs.readFile(target, "utf8"))

        expect(wholeRootCalls).toBe(0)
        expect(maxReplaceInput).toBeLessThanOrEqual(64 * 1024)
        expect(replaceWork).toBeLessThan(payload.length * 32)
        expect(stats.max_chunk_bytes).toBeLessThanOrEqual(4096)
        expect(stats.max_encoder_temporary_bytes).toBeLessThanOrEqual(4096)
        expect(parsed.records[0].data.payload).toBe(payload)
      } finally {
        String.prototype.replace = originalReplace
        await fs.rm(dir, { recursive: true, force: true })
      }
    },
    120_000,
  )

  test(
    "budgets a 32MiB execution observation title before HTML escaping",
    () => {
      const marker = "bounded-observation-title:<&>"
      const title = marker + "x".repeat(32 * 1024 * 1024)
      const trace = provenanceTrace([
        {
          record_id: "observation-title-budget",
          component: "runtime",
          event_type: "execution.observation",
          timestamp: "2026-07-31T00:00:00.000Z",
          time_ms: 1,
          title,
          data: { source: "review", category: "memory" },
        },
      ])
      const originalReplaceAll = String.prototype.replaceAll
      let maxEscapeInput = 0

      try {
        const replaceAllTrap = function (
          this: string,
          searchValue: string | RegExp,
          replaceValue: string | ((substring: string, ...args: unknown[]) => string),
        ) {
          const length = this.length
          maxEscapeInput = Math.max(maxEscapeInput, length)
          if (length > 64 * 1024) throw new Error(`unbounded viewer escape: ${length}`)
          return (originalReplaceAll as Function).call(this, searchValue, replaceValue) as string
        }
        String.prototype.replaceAll = replaceAllTrap as typeof String.prototype.replaceAll

        const html = renderProvenanceTraceHtml(trace)
        expect(html).toContain("bounded-observation-title:&lt;&amp;&gt;")
        expect(html).toContain("truncated")
        expect(html.length).toBeLessThan(2 * 1024 * 1024)
        expect(maxEscapeInput).toBeLessThanOrEqual(64 * 1024)
      } finally {
        String.prototype.replaceAll = originalReplaceAll
      }
    },
    120_000,
  )

  test("rejects legacy artifact symlink bodies and hrefs with or without caller-supplied contents", async () => {
    const caseRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-legacy-artifact-realpath-"))
    const outsideRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-legacy-artifact-private-"))
    const artifactPath = "artifacts/sha256/private-link.txt"
    const secret = "legacy-symlink-private-body"
    const supplied = "caller-supplied-private-body"
    const artifact: TraceArtifact = {
      artifact_id: "artifact_legacy_symlink_escape",
      kind: "text",
      label: "legacy symlink escape",
      path: artifactPath,
      length: secret.length,
      hash: "legacy-symlink",
      preview: "private preview",
      created_at: "2026-07-31T00:00:00.000Z",
      availability: "bundled",
    }
    const trace = largeLegacyTrace(1, artifact)

    try {
      await fs.mkdir(path.dirname(path.join(caseRoot, artifactPath)), { recursive: true })
      const outside = path.join(outsideRoot, "private.txt")
      await fs.writeFile(outside, secret)
      await fs.symlink(outside, path.join(caseRoot, artifactPath))

      const untrusted = renderCaseTraceHtml(trace, { artifactDir: caseRoot })
      const callerSupplied = renderCaseTraceHtml(trace, {
        artifactDir: caseRoot,
        artifactContents: { [artifact.artifact_id]: supplied },
      })

      for (const html of [untrusted, callerSupplied]) {
        expect(html).not.toContain(secret)
        expect(html).not.toContain(supplied)
        expect(html).not.toContain(`href="${artifactPath}"`)
        expect(html).toContain("Artifact unavailable")
      }
    } finally {
      await fs.rm(caseRoot, { recursive: true, force: true })
      await fs.rm(outsideRoot, { recursive: true, force: true })
    }
  })

  test("removes a stale running canonical and reports persistent terminal canonical write failure", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-stale-running-canonical-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "stale-running-canonical.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import fs from "node:fs"`,
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure({ input: { prompt: "stale canonical terminal failure" } }) as any`,
          `CaseTrace.node({ node_id: "terminal-fact", kind: "execution.observation", component: "runtime", title: "terminal fact" })`,
          `fs.writeFileSync(trace.traceFile, JSON.stringify({ manifest: { status: "running", process_status: "running" } }))`,
          `const originalSafeWrite = trace.safeWrite.bind(trace)`,
          `trace.safeWrite = (target: string, content: unknown) => target === trace.traceFile ? false : originalSafeWrite(target, content)`,
          `trace.flushForSignal("SIGTERM")`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-stale-running-canonical",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stderr = await new Response(proc.stderr).text()
      const caseDir = path.join(dir, "task6-stale-running-canonical")
      const canonical = await fs.readFile(path.join(caseDir, "trace.json"), "utf8").catch(() => undefined)
      const partial = JSON.parse(await fs.readFile(path.join(caseDir, "partial/latest.json"), "utf8"))

      expect(code, stderr).toBe(0)
      expect(canonical).toBeUndefined()
      expect(partial.manifest).toMatchObject({ status: "cancelled", shutdown_signal: "SIGTERM" })
      expect(stderr).toContain("[opencode-observability] terminal trace persistence failed")
      expect(stderr).toContain("task6-stale-running-canonical")
      expect(stderr).toContain("SIGTERM")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("matches whole-string sanitization for every boundary and random fragmentation", () => {
    const cases = [
      "url https://reader:url-password-secret@example.test/audit visible",
      "prefix https://example.test/a?visible=1&access_token=url-secret-value&after=ok suffix",
      "prefix https://example.test/a?token=query-secret&visible=ok suffix",
      String.raw`{"visible":"ok","token":"json-secret\\\"tail","after":"yes"}`,
      String.raw`{"apiKey":"camel-json-secret","after":"yes"}`,
      String.raw`{'api_key':'single-quoted\\'secret','after':'yes'}`,
      "prefix\nAuthorization: Bearer header-secret-value\nX-Trace: visible",
      `curl -H "Cookie: session=quoted-header-secret; Path=/" https://example.test`,
      `curl -H Cookie:session="embedded-cookie-secret" https://example.test`,
      "Cookie: session=cookie-secret; Path=/",
      `tool --api-key="command-secret-value" --mode safe`,
      `tool --token command-token-secret --mode safe`,
      "AUTHORIZATION=Bearer assignment-secret MODE=safe",
      "password:generic-colon-secret visible",
      "token sk-12345678secret visible",
      "Authorization result Bearer abcdefgh123456 visible",
      "requestError: Authorization: error-header-secret",
      String.raw`before😀 {"password":"秘密\\\"unicode-secret😀"} after`,
    ]
    const longInput = `begin token=${"v".repeat(2048)} end 😀 ${String.raw`{"api_key":"escaped\\\"value"}`}`
    const longOracle = sanitizeTraceJson(longInput) as string
    const originalReplace = String.prototype.replace
    let maxReplaceReceiver = 0

    try {
      const replaceTrap = function (
        this: string,
        searchValue: string | RegExp,
        replaceValue: string | ((substring: string, ...args: unknown[]) => string),
      ) {
        maxReplaceReceiver = Math.max(maxReplaceReceiver, this.length)
        if (this.length > 128) throw new Error(`unbounded stateful sanitizer receiver: ${this.length}`)
        return (originalReplace as Function).call(this, searchValue, replaceValue) as string
      }

      const oracles = cases.map((input) => sanitizeTraceJson(input) as string)
      String.prototype.replace = replaceTrap as typeof String.prototype.replace
      for (const [caseIndex, input] of cases.entries()) {
        for (let boundary = 0; boundary <= input.length; boundary++) {
          const fragments = [input.slice(0, boundary), input.slice(boundary)]
          const actual = Array.from(
            sanitizeTraceJsonStringChunks(fragments, "payload", ["records", String(caseIndex), "payload"], 7),
          ).join("")
          expect(actual, `case ${caseIndex}, boundary ${boundary}`).toBe(oracles[caseIndex])
        }
      }

      let seed = 0x6d2b79f5
      for (let attempt = 0; attempt < 64; attempt++) {
        const fragments: string[] = []
        for (let offset = 0; offset < longInput.length; ) {
          seed = Math.imul(seed ^ (seed >>> 15), 1 | seed)
          seed ^= seed + Math.imul(seed ^ (seed >>> 7), 61 | seed)
          const width = 1 + ((seed ^ (seed >>> 14)) >>> 0) % 31
          fragments.push(longInput.slice(offset, offset + width))
          offset += width
        }
        expect(
          Array.from(sanitizeTraceJsonStringChunks(fragments, "payload", ["records", "long", "payload"], 11)).join(""),
        ).toBe(longOracle)
      }
      expect(maxReplaceReceiver).toBeLessThanOrEqual(128)
    } finally {
      String.prototype.replace = originalReplace
    }
  })

  test("binds artifact reads and href authority to the validated file object", async () => {
    const caseRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-artifact-object-binding-"))
    const outsideRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-artifact-object-private-"))
    const artifactPath = "artifacts/sha256/object-binding.txt"
    const target = path.join(caseRoot, artifactPath)
    const displaced = `${target}.validated`
    const outside = path.join(outsideRoot, "private.txt")
    let replaced = false
    const artifact: TraceArtifact = {
      artifact_id: "artifact_object_binding",
      kind: "text",
      label: "object binding",
      path: artifactPath,
      length: 16,
      hash: "object-binding",
      preview: "original preview",
      created_at: "2026-07-31T00:00:00.000Z",
      availability: "bundled",
    }

    try {
      await fs.mkdir(path.dirname(target), { recursive: true })
      await fs.writeFile(target, "authorized-object")
      await fs.writeFile(outside, "replacement-private-secret")
      const artifactContents = new Proxy<Record<string, string>>(
        {},
        {
          ownKeys() {
          nodeFs.renameSync(target, displaced)
          nodeFs.symlinkSync(outside, target)
          replaced = true
            return []
          },
        },
      )

      const html = renderCaseTraceHtml(largeLegacyTrace(1, artifact), { artifactDir: caseRoot, artifactContents })

      expect(replaced).toBe(true)
      expect(html).not.toContain("replacement-private-secret")
      expect(html).not.toContain(`href="${artifactPath}"`)
      expect(html).toContain(`href="${artifactSnapshotPath("authorized-object")}"`)
      expect(html).toContain("authorized-object")
    } finally {
      await fs.rm(caseRoot, { recursive: true, force: true })
      await fs.rm(outsideRoot, { recursive: true, force: true })
    }
  })

  test("real production finish preserves root collection and representative record identities", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-finish-no-root-clone-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "finish-no-root-clone.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure({ input: { prompt: "finish identity" } }) as any`,
          `CaseTrace.node({ node_id: "finish-identity-record", kind: "execution.observation", component: "runtime", title: "identity" })`,
          `const owned = trace.causalIR as any`,
          `const originalOwnedFinalize = owned.finalize.bind(owned)`,
          `const originalStoreFinalize = owned.store.finalize.bind(owned.store)`,
          `let expected: any`,
          `let identities: any`,
          `owned.finalize = (value: any) => { expected = value; return originalOwnedFinalize(value) }`,
          `owned.store.finalize = (value: any) => { identities = { root: value === expected, records: value.nodes === expected.nodes, record: value.nodes[0] === expected.nodes[0] }; return originalStoreFinalize(value) }`,
          `trace.finish({ status: "success", result: { answer: "identity" } })`,
          `process.stdout.write(JSON.stringify(identities))`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-finish-no-root-clone",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stdout = await new Response(proc.stdout).text()
      const stderr = await new Response(proc.stderr).text()

      expect(code, stderr).toBe(0)
      expect(JSON.parse(stdout)).toEqual({ root: true, records: true, record: true })
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("removes stale canonical truth when emergency fallback and partial loading both fail", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-emergency-double-failure-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(dir, "emergency-double-failure.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import fs from "node:fs"`,
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure({ input: { prompt: "double emergency failure" } }) as any`,
          `fs.writeFileSync(trace.traceFile, JSON.stringify({ manifest: { status: "running", process_status: "running" } }))`,
          `fs.rmSync(trace.partialFile, { force: true })`,
          `const originalFinish = trace.finish.bind(trace)`,
          `trace.finish = () => { throw new Error("forced finish failure") }`,
          `trace.causalIRSummary = () => { throw new Error("forced fallback failure") }`,
          `trace.flushForSignal("SIGTERM")`,
          `trace.finished = true`,
          `trace.finish = originalFinish`,
        ].join("\n"),
      )
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: "1",
          OPENCODE_CASE_ID: "task6-emergency-double-failure",
          OPENCODE_CASE_TRACE_DIR: dir,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stderr = await new Response(proc.stderr).text()
      const canonical = await fs
        .readFile(path.join(dir, "task6-emergency-double-failure", "trace.json"), "utf8")
        .catch(() => undefined)

      expect(code, stderr).toBe(0)
      expect(canonical).toBeUndefined()
      expect(stderr).toContain("[opencode-observability] terminal trace persistence failed")
      expect(stderr).toContain('"canonical_removed":true')
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test(
    "budgets every legacy flow label before interpolation",
    async () => {
      const marker = "legacy-flow-label:<&>"
      const huge = marker + "x".repeat(32 * 1024 * 1024)
      const trace = largeLegacyTrace(0, {
        artifact_id: "unused",
        kind: "text",
        path: "artifacts/unused.txt",
        length: 0,
        hash: "unused",
        preview: "",
        created_at: "2026-07-31T00:00:00.000Z",
      })
      trace.artifacts = []
      trace.spans = [
        {
          span_id: huge,
          component: "runtime",
          operation: huge,
          name: huge,
          start_ms: 0,
          start_time: "2026-07-31T00:00:00.000Z",
          status: "success",
        },
        {
          span_id: "child",
          parent_span_id: huge,
          component: "tool",
          operation: "child",
          name: "child",
          start_ms: 1,
          start_time: "2026-07-31T00:00:00.001Z",
          status: "success",
        },
      ]
      const originalReplaceAll = String.prototype.replaceAll
      let maxEscapeReceiver = 0

      try {
        String.prototype.replaceAll = function (
          searchValue: string | RegExp,
          replaceValue: string | ((substring: string, ...args: unknown[]) => string),
        ) {
          maxEscapeReceiver = Math.max(maxEscapeReceiver, this.length)
          if (this.length > 64 * 1024) throw new Error(`unbounded legacy escape receiver: ${this.length}`)
          return (originalReplaceAll as Function).call(this, searchValue, replaceValue) as string
        }
        const html = renderCaseTraceHtml(trace)
        const source = await fs.readFile(path.join(packageDirForTest(), "src/observability/case-trace-html.ts"), "utf8")

        expect(html).toContain("legacy-flow-label:&lt;&amp;&gt;")
        expect(html.length).toBeLessThan(2 * 1024 * 1024)
        expect(maxEscapeReceiver).toBeLessThanOrEqual(64 * 1024)
        expect(source).not.toMatch(/`[^`]*\$\{(?:span|parent)\.(?:name|operation|span_id|parent_span_id)/)
      } finally {
        String.prototype.replaceAll = originalReplaceAll
      }
    },
    120_000,
  )

  test("uses one iterator-only redaction kernel for adjacent matches across a 2040-case corpus", () => {
    const corpus = Array.from({ length: 2040 }, (_, index) => {
      const input = `case-${index} token=token-${index};password=pass-${index},api_key=key-${index} &token=query-${index}&visible=ok`
      const expected = `case-${index} token=[REDACTED];password=[REDACTED],api_key=[REDACTED] &token=[REDACTED]&visible=ok`
      return { input, expected }
    })

    for (const [caseIndex, { input, expected }] of corpus.entries()) {
      let consumed = 0
      function* fragments() {
        yield ""
        for (let offset = 0; offset < input.length; ) {
          const width = 1 + ((caseIndex * 17 + offset * 13) % 9)
          const fragment = input.substring(offset, offset + width)
          consumed += fragment.length
          yield fragment
          yield ""
          offset += fragment.length
        }
        yield ""
      }

      const whole = sanitizeTraceJson(input)
      const streamed = Array.from(
        sanitizeTraceJsonStringChunks(fragments(), "payload", ["records", String(caseIndex), "payload"], 7),
      ).join("")

      expect(whole, `whole case ${caseIndex}`).toBe(expected)
      expect(streamed, `stream case ${caseIndex}`).toBe(expected)
      expect(consumed, `consumed case ${caseIndex}`).toBe(input.length)
    }
  })

  test("bounds every temporary substring while preserving a 4096+ character URL scheme", () => {
    const scheme = `a${"b".repeat(8192)}+v1`
    const input = `${scheme}://reader:scheme-secret@example.test/audit?token=query-secret&visible=ok`
    const originalSlice = String.prototype.slice
    let maxSliceResult = 0

    try {
      String.prototype.slice = function (start?: number, end?: number) {
        const result = originalSlice.call(this, start, end)
        maxSliceResult = Math.max(maxSliceResult, result.length)
        if (result.length > 2048) throw new Error(`unbounded matcher substring: ${result.length}`)
        return result
      }
      function* fragments() {
        for (let offset = 0; offset < input.length; offset += 13) {
          yield ""
          yield input.substring(offset, offset + 13)
        }
      }

      const whole = sanitizeTraceJson(input) as string
      const streamed = Array.from(sanitizeTraceJsonStringChunks(fragments(), "url", ["url"], 31)).join("")

      expect(streamed).toBe(whole)
      expect(streamed.startsWith(`${scheme}://[REDACTED]:[REDACTED]@example.test/audit`)).toBe(true)
      expect(streamed).toContain("?token=[REDACTED]&visible=ok")
      expect(streamed).not.toContain("scheme-secret")
      expect(streamed).not.toContain("query-secret")
      expect(maxSliceResult).toBeLessThanOrEqual(2048)
    } finally {
      String.prototype.slice = originalSlice
    }
  })

  test("opens and closes a duplicate artifact source path exactly once", async () => {
    const caseRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-duplicate-artifact-fd-"))
    const artifactPath = "artifacts/sha256/duplicate-source.txt"
    const source = path.join(caseRoot, artifactPath)
    const first: TraceArtifact = {
      artifact_id: "artifact_duplicate_first",
      kind: "text",
      label: "duplicate first",
      path: artifactPath,
      length: 22,
      hash: "duplicate-first",
      preview: "duplicate source body",
      created_at: "2026-07-31T00:00:00.000Z",
      availability: "bundled",
    }
    const second = { ...first, artifact_id: "artifact_duplicate_second", hash: "duplicate-second" }
    const trace = largeLegacyTrace(1, first)
    trace.artifacts = [first, second]
    const originalOpen = nodeFs.openSync
    const originalClose = nodeFs.closeSync
    const tracked = new Set<number>()
    let opened = 0
    let closed = 0

    try {
      await fs.mkdir(path.dirname(source), { recursive: true })
      await fs.writeFile(source, "duplicate source body")
      nodeFs.openSync = ((target: nodeFs.PathLike, flags: nodeFs.OpenMode, mode?: nodeFs.Mode) => {
        const fd = originalOpen.call(nodeFs, target, flags, mode)
        if (path.resolve(String(target)) === source) {
          opened += 1
          tracked.add(fd)
        }
        return fd
      }) as typeof nodeFs.openSync
      nodeFs.closeSync = ((fd: number) => {
        if (tracked.delete(fd)) closed += 1
        return originalClose.call(nodeFs, fd)
      }) as typeof nodeFs.closeSync

      const html = renderCaseTraceHtml(trace, { artifactDir: caseRoot })

      expect(html).toContain("artifact_duplicate_first")
      expect(html).toContain("artifact_duplicate_second")
      expect(opened).toBe(1)
      expect(closed).toBe(opened)
      expect(tracked.size).toBe(0)
    } finally {
      nodeFs.openSync = originalOpen
      nodeFs.closeSync = originalClose
      await fs.rm(caseRoot, { recursive: true, force: true })
    }
  })

  test("keeps renderer hrefs bound to an immutable content-addressed snapshot when the source is replaced", async () => {
    const caseRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-render-snapshot-binding-"))
    const artifactPath = "artifacts/sha256/mutable-source.txt"
    const source = path.join(caseRoot, artifactPath)
    const displaced = `${source}.validated`
    const htmlTarget = path.join(caseRoot, "trace.html")
    const originalBody = "immutable-authoritative-body"
    const replacementBody = "mutable-replacement-private-body"
    const artifact: TraceArtifact = {
      artifact_id: "artifact_snapshot_binding",
      kind: "text",
      label: "snapshot binding",
      path: artifactPath,
      length: originalBody.length,
      hash: "untrusted-reported-hash",
      preview: "snapshot binding",
      created_at: "2026-07-31T00:00:00.000Z",
      availability: "bundled",
    }
    const trace = provenanceTrace(
      [
        {
          record_id: "snapshot-binding-record",
          component: "tool" as const,
          event_type: "tool.result",
          timestamp: "2026-07-31T00:00:00.000Z",
          time_ms: 1,
          title: "snapshot binding",
          artifact_refs: [artifact.artifact_id],
          data: { output: "snapshot binding" },
        },
      ],
      [artifact],
    )
    let replaced = false
    const caseID = trace.manifest.case_id

    try {
      await fs.mkdir(path.dirname(source), { recursive: true })
      await fs.writeFile(source, originalBody)
      Object.defineProperty(trace.manifest, "case_id", {
        configurable: true,
        get() {
          if (!replaced) {
            nodeFs.renameSync(source, displaced)
            nodeFs.writeFileSync(source, replacementBody)
            replaced = true
          }
          return caseID
        },
      })

      writeProvenanceTraceHtmlFile(htmlTarget, trace, { forceStreaming: true, maxChunkBytes: 4096 })
      const html = await fs.readFile(htmlTarget, "utf8")
      const href = html.match(/href="(artifacts\/render-snapshots\/sha256\/[a-f0-9]{64})"/)?.[1]

      expect(replaced).toBe(true)
      expect(href).toBeDefined()
      expect(href).not.toBe(artifactPath)
      expect(await fs.readFile(path.join(caseRoot, href!), "utf8")).toBe(originalBody)
      expect(await fs.readFile(source, "utf8")).toBe(replacementBody)
      expect(html).not.toContain(replacementBody)
    } finally {
      await fs.rm(caseRoot, { recursive: true, force: true })
    }
  })

  test(
    "budgets 32MiB component and artifact identifiers before every lookup and sort",
    async () => {
      const caseRoot = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-viewer-container-key-budget-"))
      const huge = `container-key:<&>${"x".repeat(32 * 1024 * 1024)}`
      const artifactPath = "artifacts/sha256/container-key.txt"
      const artifact: TraceArtifact = {
        artifact_id: huge,
        kind: "text",
        label: huge,
        path: artifactPath,
        length: 4,
        hash: "container-key",
        preview: "body",
        created_at: "2026-07-31T00:00:00.000Z",
        availability: "bundled",
      }
      const legacy = largeLegacyTrace(0, artifact)
      legacy.spans = [
        {
          span_id: huge,
          component: huge as TraceComponent,
          operation: huge,
          name: huge,
          start_ms: 0,
          start_time: "2026-07-31T00:00:00.000Z",
          status: huge as any,
        },
      ]
      const modern = provenanceTrace(
        [
          {
            record_id: huge,
            component: huge as TraceComponent,
            event_type: "execution.observation",
            timestamp: "2026-07-31T00:00:00.000Z",
            time_ms: 1,
            title: huge,
            artifact_refs: [huge],
            data: { summary: huge },
          },
        ],
        [artifact],
      )
      const originalMapSet = Map.prototype.set
      const originalMapGet = Map.prototype.get
      const originalMapHas = Map.prototype.has
      const originalSetAdd = Set.prototype.add
      const originalSetHas = Set.prototype.has
      const originalLocaleCompare = String.prototype.localeCompare
      const rejectHugeKey = (value: unknown, operation: string) => {
        if (typeof value === "string" && value.length > 64 * 1024)
          throw new Error(`unbounded ${operation} key: ${value.length}`)
      }

      try {
        await fs.mkdir(path.dirname(path.join(caseRoot, artifactPath)), { recursive: true })
        await fs.writeFile(path.join(caseRoot, artifactPath), "body")
        Map.prototype.set = function (key: unknown, value: unknown) {
          rejectHugeKey(key, "Map.set")
          return originalMapSet.call(this, key, value)
        }
        Map.prototype.get = function (key: unknown) {
          rejectHugeKey(key, "Map.get")
          return originalMapGet.call(this, key)
        }
        Map.prototype.has = function (key: unknown) {
          rejectHugeKey(key, "Map.has")
          return originalMapHas.call(this, key)
        }
        Set.prototype.add = function (value: unknown) {
          rejectHugeKey(value, "Set.add")
          return originalSetAdd.call(this, value)
        }
        Set.prototype.has = function (value: unknown) {
          rejectHugeKey(value, "Set.has")
          return originalSetHas.call(this, value)
        }
        String.prototype.localeCompare = function (that: string, ...args: unknown[]) {
          rejectHugeKey(String(this), "localeCompare receiver")
          rejectHugeKey(that, "localeCompare argument")
          return (originalLocaleCompare as Function).call(this, that, ...args) as number
        }

        const legacyHtml = renderCaseTraceHtml(legacy, { artifactDir: caseRoot })
        const modernHtml = renderProvenanceTraceHtml(modern)

        expect(legacyHtml).toContain("container-key:&lt;&amp;&gt;")
        expect(modernHtml).toContain("container-key:&lt;&amp;&gt;")
        expect(legacyHtml.length).toBeLessThan(4 * 1024 * 1024)
        expect(modernHtml.length).toBeLessThan(4 * 1024 * 1024)
      } finally {
        Map.prototype.set = originalMapSet
        Map.prototype.get = originalMapGet
        Map.prototype.has = originalMapHas
        Set.prototype.add = originalSetAdd
        Set.prototype.has = originalSetHas
        String.prototype.localeCompare = originalLocaleCompare
        await fs.rm(caseRoot, { recursive: true, force: true })
      }
    },
    120_000,
  )
})

function packageDirForTest() {
  return path.resolve(import.meta.dir, "../..")
}
