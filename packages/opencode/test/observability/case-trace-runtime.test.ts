import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { normalizeTemporalReferences, writeJsonDocumentAtomic } from "@/observability/causal-ir"
import { sanitizeTraceJson, sanitizeTraceJsonStringChunks, sanitizeTraceJsonValue } from "@/observability/case-trace"
import { captureRepositorySnapshot } from "@/observability/repository-snapshot"

process.env.OPENCODE_CASE_TRACE_QUIET = "1"

describe("case trace runtime persistence", () => {
  test(
    "uses the bounded writer for a real 5000-record trace and stores one authoritative payload",
    async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-streaming-trace-runtime-"))
      const packageDir = packageDirForTest()
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
      const packageDir = packageDirForTest()
      const script = path.join(dir, "idempotent-signal-trace.ts")
      const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

      try {
        await fs.writeFile(
          script,
          [
            `import fs from "node:fs"`,
            `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
            `const trace = CaseTrace.configure({ input: { prompt: "idempotent signal" } }) as any`,
            `CaseTrace.node({ node_id: "before_signal", kind: "execution.observation", component: "runtime", title: "before signal" })`,
            `trace.flushForSignal(${JSON.stringify(signal)})`,
            `const firstTrace = fs.readFileSync(trace.traceFile)`,
            `const firstState = JSON.parse(firstTrace.toString("utf8")).manifest`,
            `trace.flushForSignal(${JSON.stringify(signal)})`,
            `const secondTrace = fs.readFileSync(trace.traceFile)`,
            `const secondState = JSON.parse(secondTrace.toString("utf8")).manifest`,
            `process.stdout.write(JSON.stringify({ trace_equal: firstTrace.equals(secondTrace), first_state: { status: firstState.status, server_status: firstState.server_status, process_status: firstState.process_status, case_status: firstState.case_status, shutdown_signal: firstState.shutdown_signal, shutdown_disposition: firstState.shutdown_disposition }, second_state: { status: secondState.status, server_status: secondState.server_status, process_status: secondState.process_status, case_status: secondState.case_status, shutdown_signal: secondState.shutdown_signal, shutdown_disposition: secondState.shutdown_disposition } }))`,
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
    const packageDir = packageDirForTest()
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

  test(
    "writes a 32MiB causal record in bounded chunks",
    async () => {
      const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-single-record-budget-"))
      const target = path.join(dir, "trace.json")
      const marker = "single-record-prefix:"
      const payload = marker + "x".repeat(32 * 1024 * 1024)

      try {
        const stats = writeJsonDocumentAtomic(
          target,
          {
            records: [
              {
                record_id: "single_record_32mib",
                component: "tool",
                event_type: "tool.result",
                timestamp: "2026-07-31T00:00:00.000Z",
                time_ms: 1,
                data: { output: { payload, nested: { verdict: "preserved" } } },
              },
            ],
          },
          { maxChunkBytes: 4096 },
        )
        const parsed = JSON.parse(await fs.readFile(target, "utf8"))

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
    const packageDir = packageDirForTest()
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
    const packageDir = packageDirForTest()
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

  test("discards late observer events and a signal after session finish", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-late-after-finish-"))
    const packageDir = packageDirForTest()
    const script = path.join(dir, "late-after-finish.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `CaseTrace.configure({ caseID: "late-after-finish", traceDir: ${JSON.stringify(dir)} })`,
          `CaseTrace.setSessionID("ses_finished")`,
          `CaseTrace.observation({ session_id: "ses_finished", source: "runtime", category: "before", summary: "before finish" })`,
          `CaseTrace.finishSession("ses_finished", { status: "success" })`,
          `CaseTrace.observation({ session_id: "ses_finished", source: "runtime", category: "late", summary: "must be discarded" })`,
          `process.stdout.write("agent-result-stable")`,
          `process.kill(process.pid, "SIGTERM")`,
        ].join("\n"),
      )

      const child = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: { ...process.env, OPENCODE_CASE_TRACE: "1" },
        stdout: "pipe",
        stderr: "pipe",
      })
      const [exitCode, stdout, stderr] = await Promise.all([
        child.exited,
        new Response(child.stdout).text(),
        new Response(child.stderr).text(),
      ])

      expect(exitCode).toBe(143)
      expect(stdout).toBe("agent-result-stable")
      expect(stderr).toBe("")
      const trace = await fs.readFile(path.join(dir, "late-after-finish", "trace.json"), "utf8")
      expect(trace).toContain("before finish")
      expect(trace).not.toContain("must be discarded")
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

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
    const packageDir = packageDirForTest()
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
    const packageDir = packageDirForTest()
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
      expect(await fs.access(path.join(caseDir, "trace.html")).then(() => true).catch(() => false)).toBe(false)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("guards cancelled signal facts before finalization errors and rejects later error finish", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-signal-error-guard-"))
    const packageDir = packageDirForTest()
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

  test("recovers a coherent cancelled partial after a SIGTERM trace write failure", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-sigterm-emergency-recovery-"))
    const packageDir = packageDirForTest()
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
          `trace.safeWrite = (target: string, content: unknown) => {`,
          `  if (target === trace.traceFile) return false`,
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
      expect(await fs.access(path.join(caseDir, "trace.html")).then(() => true).catch(() => false)).toBe(false)
    } finally {
      await fs.rm(dir, { recursive: true, force: true })
    }
  })

  test("reports an explicit terminal persistence failure when every critical SIGTERM path is unwritable", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-sigterm-all-unwritable-"))
    const packageDir = packageDirForTest()
    const script = path.join(dir, "sigterm-all-unwritable.ts")
    const traceModule = pathToFileURL(path.join(packageDir, "src/observability/case-trace.ts")).href

    try {
      await fs.writeFile(
        script,
        [
          `import { CaseTrace } from ${JSON.stringify(traceModule)}`,
          `const trace = CaseTrace.configure({ input: { prompt: "all terminal paths unwritable" } }) as any`,
          `CaseTrace.node({ node_id: "persisted_before_total_failure", kind: "execution.observation", component: "runtime", title: "persisted before total failure" })`,
          `const blocked = new Set([trace.traceFile, trace.partialFile, trace.manifestFile])`,
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

  test("removes a stale running canonical and reports persistent terminal canonical write failure", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-stale-running-canonical-"))
    const packageDir = packageDirForTest()
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

  test("real production finish preserves root collection and representative record identities", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-finish-no-root-clone-"))
    const packageDir = packageDirForTest()
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
    const packageDir = packageDirForTest()
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
          `const originalSafeWrite = trace.safeWrite.bind(trace)`,
          `trace.safeWrite = (target: string, content: unknown) => [trace.traceFile, trace.manifestFile, trace.partialFile].includes(target) ? false : originalSafeWrite(target, content)`,
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

})

function packageDirForTest() {
  return path.resolve(import.meta.dir, "../..")
}
