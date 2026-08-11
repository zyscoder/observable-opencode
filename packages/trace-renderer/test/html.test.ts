import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import nodeFs from "node:fs"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { renderCaseTraceHtml, writeProvenanceTraceHtmlFile } from "../src/html"
import { provenanceTraceHtmlChunks, renderProvenanceTraceHtml } from "../src/viewer"
import type { ProvenanceTraceView, TraceArtifact, TraceComponent, TraceSummary } from "opencode/observability/case-trace"

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

  test(
    "previews and streams a 32MiB single record without constructing the full record string",
    async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-single-record-budget-"))
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

      await fs.rm(dir, { recursive: true, force: true })
    },
    120_000,
  )

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
        const source = await fs.readFile(path.join(import.meta.dir, "../src/html.ts"), "utf8")

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
