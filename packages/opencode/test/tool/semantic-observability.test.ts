import { describe, expect, test } from "bun:test"
import { classifyShellOperation, inferVerificationStatus } from "../../src/tool/tool"
import { captureRepositorySnapshot, repositorySnapshotDelta } from "../../src/observability/repository-snapshot"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"
import { execFileSync } from "node:child_process"

describe("tool semantic observability", () => {
  test("keeps passive tool behavior identical for success and error paths", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-passive-tool-equivalence-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(root, "passive-tool-equivalence.ts")
    const toolModule = pathToFileURL(path.join(packageDir, "src/tool/tool.ts")).href
    const truncateModule = pathToFileURL(path.join(packageDir, "src/tool/truncate.ts")).href
    const agentModule = pathToFileURL(path.join(packageDir, "src/agent/agent.ts")).href
    const schemaModule = pathToFileURL(path.join(packageDir, "src/session/schema.ts")).href
    const effectModule = pathToFileURL(path.join(packageDir, "node_modules/effect/dist/index.js")).href

    fs.writeFileSync(
      script,
      [
        `import { Effect, Layer, ManagedRuntime, Schema } from ${JSON.stringify(effectModule)}`,
        `import { Tool } from ${JSON.stringify(toolModule)}`,
        `import { Truncate } from ${JSON.stringify(truncateModule)}`,
        `import { Agent } from ${JSON.stringify(agentModule)}`,
        `import { MessageID, SessionID } from ${JSON.stringify(schemaModule)}`,
        `class PassiveToolError extends Error { constructor() { super("passive benchmark failure"); this.name = "PassiveToolError" } }`,
        `const runtime = ManagedRuntime.make(Layer.mergeAll(Truncate.defaultLayer, Agent.defaultLayer))`,
        `const inputs = []`,
        `const callbackCounts = { metadata: 0, ask: 0 }`,
        `const agentVisibleMessages = []`,
        `const context = {`,
        `  sessionID: SessionID.descending(),`,
        `  messageID: MessageID.ascending(),`,
        `  agent: "build",`,
        `  abort: new AbortController().signal,`,
        `  callID: "passive-call",`,
        `  messages: [{ info: { role: "user" }, parts: [{ type: "text", text: "run passive benchmark" }] }],`,
        `  metadata(input) { callbackCounts.metadata++; agentVisibleMessages.push({ role: "tool", kind: "metadata", input }); return Effect.void },`,
        `  ask(input) { callbackCounts.ask++; agentVisibleMessages.push({ role: "tool", kind: "permission", input }); return Effect.void },`,
        `}`,
        `const info = await runtime.runPromise(Tool.define("passive-benchmark", Effect.succeed({`,
        `  description: "exercise passive tracing",`,
        `  parameters: Schema.Struct({ scenario: Schema.Union([Schema.Literal("success"), Schema.Literal("error")]), payload: Schema.String }),`,
        `  execute(args, ctx) {`,
        `    return Effect.gen(function* () {`,
        `      inputs.push(args)`,
        `      yield* ctx.metadata({ title: "passive callback", metadata: { scenario: args.scenario } })`,
        `      yield* ctx.ask({ permission: "read", patterns: [args.payload], always: [] })`,
        `      if (args.scenario === "error") return yield* Effect.fail(new PassiveToolError())`,
        `      return { title: "passive success", output: "stable tool output", metadata: { scenario: args.scenario, truncated: false } }`,
        `    })`,
        `  },`,
        `})))`,
        `const tool = await Effect.runPromise(info.init())`,
        `const outcomes = []`,
        `for (const args of [{ scenario: "success", payload: "fixed-input" }, { scenario: "error", payload: "fixed-input" }]) {`,
        `  try {`,
        `    const result = await Effect.runPromise(tool.execute(args, context))`,
        `    outcomes.push({ scenario: args.scenario, result })`,
        `    agentVisibleMessages.push({ role: "tool", kind: "result", content: result.output })`,
        `  } catch (error) {`,
        `    outcomes.push({ scenario: args.scenario, error: { class: error?.constructor?.name, type: error?.name, message: error?.message } })`,
        `    agentVisibleMessages.push({ role: "tool", kind: "error", content: error?.message })`,
        `  }`,
        `}`,
        `await runtime.dispose()`,
        `process.stdout.write(JSON.stringify({ inputs, outcomes, callbackCounts, agentVisibleMessages }))`,
      ].join("\n"),
    )

    const run = async (enabled: boolean) => {
      const traceRoot = path.join(root, enabled ? "enabled" : "disabled")
      const proc = Bun.spawn([process.execPath, script], {
        cwd: packageDir,
        env: {
          ...process.env,
          OPENCODE_CASE_TRACE: enabled ? "1" : "0",
          OPENCODE_CASE_ID: "passive-tool-equivalence",
          OPENCODE_CASE_TRACE_DIR: traceRoot,
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const code = await proc.exited
      const stderr = await new Response(proc.stderr).text()
      const stdout = await new Response(proc.stdout).text()
      expect({ code, stderr }).toEqual({ code: 0, stderr: "" })
      return { code, stderr, result: JSON.parse(stdout), traceRoot }
    }

    const disabled = await run(false)
    const enabled = await run(true)

    expect(disabled.code).toBe(0)
    expect(enabled.code).toBe(0)
    expect(disabled.stderr).toBe("")
    expect(enabled.stderr).toBe("")
    expect(enabled.result).toEqual(disabled.result)
    expect(disabled.result.inputs).toEqual([
      { scenario: "success", payload: "fixed-input" },
      { scenario: "error", payload: "fixed-input" },
    ])
    expect(disabled.result.outcomes[0]).toEqual({
      scenario: "success",
      result: {
        title: "passive success",
        output: "stable tool output",
        metadata: { scenario: "success", truncated: false },
      },
    })
    expect(disabled.result.outcomes[1]).toEqual({
      scenario: "error",
      error: {
        class: "PassiveToolError",
        type: "PassiveToolError",
        message: "passive benchmark failure",
      },
    })
    expect(disabled.result.callbackCounts).toEqual({ metadata: 2, ask: 2 })
    expect(disabled.result.agentVisibleMessages).toEqual([
      {
        role: "tool",
        kind: "metadata",
        input: { title: "passive callback", metadata: { scenario: "success" } },
      },
      {
        role: "tool",
        kind: "permission",
        input: { permission: "read", patterns: ["fixed-input"], always: [] },
      },
      { role: "tool", kind: "result", content: "stable tool output" },
      {
        role: "tool",
        kind: "metadata",
        input: { title: "passive callback", metadata: { scenario: "error" } },
      },
      {
        role: "tool",
        kind: "permission",
        input: { permission: "read", patterns: ["fixed-input"], always: [] },
      },
      { role: "tool", kind: "error", content: "passive benchmark failure" },
    ])
    expect(fs.existsSync(path.join(disabled.traceRoot, "passive-tool-equivalence"))).toBe(false)
    expect(fs.existsSync(path.join(enabled.traceRoot, "passive-tool-equivalence", "records.jsonl"))).toBe(true)
  })

  test("classifies shell commands by their actual semantic operation", () => {
    expect(classifyShellOperation("python3 -m pytest tests/test_api.py -q")).toBe("verification")
    expect(classifyShellOperation("/tmp/venv/bin/pytest tests/test_api.py -q")).toBe("verification")
    expect(classifyShellOperation("cd /tmp/repo && /tmp/venv/bin/pytest tests/test_api.py -q | tail -20")).toBe(
      "verification",
    )
    expect(classifyShellOperation("python3 -m pip install pytest")).toBe("environment_setup")
    expect(classifyShellOperation("git show HEAD~1:src/api.py")).toBe("code_inspection")
    expect(classifyShellOperation("python3 - <<'PY'\nopen('src/api.py', 'w').write('changed')\nPY")).toBe(
      "repository_change",
    )
    expect(classifyShellOperation("python3 scripts/generate.py")).toBe("general_execution")
    expect(
      classifyShellOperation(
        'npx mocha test/unit/adapters/http.js --timeout 10000 --grep "decompression|content-encoding" 2>&1 | head -60',
      ),
    ).toBe("verification")
  })

  test("does not treat a pipeline's trailing command exit code as the pytest result", () => {
    const command = "/tmp/venv/bin/pytest tests/test_api.py -q 2>&1 | tail -20"

    expect(inferVerificationStatus(command, 0, "2 failed, 10 passed in 0.20s", undefined)).toBe("failed")
    expect(inferVerificationStatus(command, 0, "12 passed in 0.20s", undefined)).toBe("passed")
    expect(inferVerificationStatus(command, 0, "collected tests/test_api.py", undefined)).toBe("unknown")
    expect(
      inferVerificationStatus(
        'npx mocha test/unit/adapters/http.js --grep "decompression|content-encoding" 2>&1 | head -60',
        0,
        "AxiosError decompression suite",
        undefined,
      ),
    ).toBe("unknown")
  })

  test("detects an actual repository mutation without changing repository state", () => {
    const repo = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-repository-snapshot-"))
    execFileSync("git", ["init", "-q", "-b", "benchmark"], { cwd: repo })
    execFileSync("git", ["config", "user.email", "trace@example.com"], { cwd: repo })
    execFileSync("git", ["config", "user.name", "Trace Test"], { cwd: repo })
    fs.mkdirSync(path.join(repo, "src"))
    fs.writeFileSync(path.join(repo, "src/api.py"), "value = 1\n")
    execFileSync("git", ["add", "."], { cwd: repo })
    execFileSync("git", ["commit", "-q", "-m", "baseline"], { cwd: repo })

    const before = captureRepositorySnapshot(repo)
    fs.writeFileSync(path.join(repo, "src/api.py"), "value = 2\n")
    const after = captureRepositorySnapshot(repo)
    const delta = repositorySnapshotDelta(before, after)

    expect(delta.changed).toBe(true)
    expect(delta.files).toContain("src/api.py")
    expect(delta.diff).toContain("+value = 2")
    expect(delta.before_fingerprint).not.toBe(delta.after_fingerprint)
    expect(execFileSync("git", ["status", "--porcelain"], { cwd: repo, encoding: "utf8" })).toContain("src/api.py")
  })
})
