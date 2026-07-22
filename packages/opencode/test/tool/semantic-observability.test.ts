import { describe, expect, test } from "bun:test"
import { classifyShellOperation, inferVerificationStatus } from "../../src/tool/tool"
import { captureRepositorySnapshot, repositorySnapshotDelta } from "../../src/observability/repository-snapshot"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { execFileSync } from "node:child_process"

describe("tool semantic observability", () => {
  test("keeps production-projected passive tool behavior identical across tracing boundaries", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-passive-tool-equivalence-"))
    const packageDir = path.resolve(import.meta.dir, "../..")
    const script = path.join(import.meta.dir, "fixture", "passive-tool-projection.ts")
    const invalidRoot = path.join(root, "invalid-root")

    try {
      fs.writeFileSync(invalidRoot, "not a directory")
      const run = async (enabled: boolean, traceRoot: string) => {
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
        return { result: JSON.parse(stdout), traceRoot }
      }

      const disabled = await run(false, path.join(root, "disabled"))
      const enabled = await run(true, path.join(root, "enabled"))
      const invalid = await run(true, invalidRoot)

      expect(enabled.result).toEqual(disabled.result)
      expect(invalid.result).toEqual(disabled.result)
      expect(disabled.result.errorOracles).toEqual([
        {
          scenario: "error",
          constructorName: "PassiveToolError",
          name: "PassiveToolError",
          isPassiveToolError: true,
          hasPassiveToolErrorPrototype: true,
          message: "passive benchmark failure",
        },
        {
          scenario: "pre-aborted",
          constructorName: "DOMException",
          name: "AbortError",
          isPassiveToolError: false,
          hasPassiveToolErrorPrototype: false,
          message: "Passive tool pre-aborted",
        },
      ])
      expect(disabled.result.inputs).toEqual([
        { scenario: "success", payload: "fixed-input" },
        { scenario: "error", payload: "fixed-input" },
        { scenario: "pre-aborted", payload: "fixed-input" },
      ])
      expect(disabled.result.callbackCounts).toEqual({ metadata: 3, ask: 3 })
      expect(disabled.result.metadataCallbacks).toEqual([
        {
          scenario: "success",
          value: { title: "passive success", metadata: { callback: "success" } },
        },
        {
          scenario: "error",
          value: { title: "passive error", metadata: { callback: "error" } },
        },
        {
          scenario: "pre-aborted",
          value: { title: "passive pre-aborted", metadata: { callback: "pre-aborted" } },
        },
      ])
      expect(disabled.result.askCallbacks).toEqual(
        ["success", "error", "pre-aborted"].map((scenario) => ({
          scenario,
          value: { permission: "read", patterns: ["fixed-input"], always: [], metadata: {} },
        })),
      )
      expect(disabled.result.projectedToolParts).toEqual([
        {
          callID: "passive-success",
          tool: "passive-benchmark",
          state: {
            status: "completed",
            input: { scenario: "success", payload: "fixed-input" },
            output: "stable tool output",
            metadata: { scenario: "success", truncated: false },
            title: "passive success",
            time: { start: 1_700_000_000_000, end: 1_700_000_000_000 },
            attachments: [
              {
                type: "file",
                mime: "text/plain",
                filename: "passive.txt",
                url: "data:text/plain;base64,cGFzc2l2ZQ==",
              },
            ],
          },
        },
        {
          callID: "passive-error",
          tool: "passive-benchmark",
          state: {
            status: "error",
            input: { scenario: "error", payload: "fixed-input" },
            error: "passive benchmark failure",
            time: { start: 1_700_000_000_000, end: 1_700_000_000_000 },
          },
        },
        {
          callID: "passive-pre-aborted",
          tool: "passive-benchmark",
          state: {
            status: "error",
            input: { scenario: "pre-aborted", payload: "fixed-input" },
            error: "Passive tool pre-aborted",
            time: { start: 1_700_000_000_000, end: 1_700_000_000_000 },
          },
        },
      ])
      expect(disabled.result.agentVisibleMessages).toEqual([
        { role: "user", content: [{ type: "text", text: "run passive benchmark" }] },
        {
          role: "assistant",
          content: [
            {
              type: "tool-call",
              toolCallId: "passive-success",
              toolName: "passive-benchmark",
              input: { scenario: "success", payload: "fixed-input" },
            },
            {
              type: "tool-call",
              toolCallId: "passive-error",
              toolName: "passive-benchmark",
              input: { scenario: "error", payload: "fixed-input" },
            },
            {
              type: "tool-call",
              toolCallId: "passive-pre-aborted",
              toolName: "passive-benchmark",
              input: { scenario: "pre-aborted", payload: "fixed-input" },
            },
          ],
        },
        {
          role: "tool",
          content: [
            {
              type: "tool-result",
              toolCallId: "passive-success",
              toolName: "passive-benchmark",
              output: {
                type: "content",
                value: [
                  { type: "text", text: "stable tool output" },
                  { type: "media", mediaType: "text/plain", data: "cGFzc2l2ZQ==" },
                ],
              },
            },
            {
              type: "tool-result",
              toolCallId: "passive-error",
              toolName: "passive-benchmark",
              output: { type: "error-text", value: "passive benchmark failure" },
            },
            {
              type: "tool-result",
              toolCallId: "passive-pre-aborted",
              toolName: "passive-benchmark",
              output: { type: "error-text", value: "Passive tool pre-aborted" },
            },
          ],
        },
      ])
      expect(fs.existsSync(path.join(disabled.traceRoot, "passive-tool-equivalence"))).toBe(false)
      expect(fs.existsSync(path.join(enabled.traceRoot, "passive-tool-equivalence", "records.jsonl"))).toBe(true)
      expect(fs.statSync(invalidRoot).isFile()).toBe(true)
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
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
