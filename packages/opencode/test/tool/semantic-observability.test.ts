import { describe, expect, test } from "bun:test"
import { classifyShellOperation, inferVerificationStatus } from "../../src/tool/tool"
import { captureRepositorySnapshot, repositorySnapshotDelta } from "../../src/observability/repository-snapshot"
import { createTask7ProductionHarness } from "../observability/fixture/task-7-production-harness"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { execFileSync } from "node:child_process"

describe("tool semantic observability", () => {
  test("keeps production Agent behavior identical with tracing enabled and disabled", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-passive-production-equivalence-"))

    const normalize = (value: unknown, runRoot: string): unknown => {
      if (Array.isArray(value)) return value.map((item) => normalize(item, runRoot))
      if (value && typeof value === "object") {
        return Object.fromEntries(
          Object.entries(value)
            .filter(([key]) => !["id", "sessionID", "messageID", "time"].includes(key))
            .map(([key, item]) => [key, normalize(item, runRoot)]),
        )
      }
      if (typeof value !== "string") return value
      return value
        .replaceAll(runRoot, "<run-root>")
        .replaceAll(runRoot.replace(/^\/+/, ""), "<run-root>")
        .replace(/ses_[A-Za-z0-9]+/g, "<session-id>")
        .replace(/msg_[A-Za-z0-9]+/g, "<message-id>")
        .replace(/prt_[A-Za-z0-9]+/g, "<part-id>")
    }

    const stripTracePublication = (value: string) =>
      value.replace(/\[observable-opencode\] Session trace saved\n(?:  .+\n)+/g, "")

    const run = async (name: string, tracing: boolean) => {
      const runRoot = path.join(root, name)
      fs.mkdirSync(runRoot, { recursive: true })
      const harness = await createTask7ProductionHarness(runRoot)
      try {
        const result = await harness.run({
          tracing,
          caseID: "passive-production-equivalence",
          message: "TASK7_ROOT_WORKFLOW execute the deterministic production workflow",
        })
        const sessions = harness.persistedSessions()
        const sessionNames = new Map(sessions.map((session, index) => [session.id, `session-${index + 1}`]))
        const sessionRows = sessions.map((session) => ({
          name: sessionNames.get(session.id),
          parent: session.parent_id ? sessionNames.get(session.parent_id) : null,
          title: session.title,
          directory: normalize(session.directory, runRoot),
        }))
        const messages = sessions.flatMap((session) =>
          harness.persistedMessages(session.id).map((row) => ({
            session: sessionNames.get(session.id),
            data: normalize(JSON.parse(row.data), runRoot),
          })),
        )
        const parts = sessions.flatMap((session) =>
          harness.persistedParts(session.id).map((row) => ({
            session: sessionNames.get(session.id),
            data: normalize(JSON.parse(row.data), runRoot),
          })),
        )
        const toolCalls = parts
          .filter((row) => (row.data as any).type === "tool")
          .map((row) => ({ session: row.session, ...(row.data as any) }))
        const file = fs.readFileSync(path.join(harness.projectDir, "agent-output.txt"))
        return {
          result: {
            exitCode: result.exitCode,
            stdout: result.stdout,
            stderr: normalize(stripTracePublication(result.stderr), runRoot),
          },
          sessionRows,
          messages,
          parts,
          toolCalls,
          agentVisibleRequests: normalize(harness.requests, runRoot),
          fileHash: Bun.CryptoHasher.hash("sha256", file, "hex"),
          traceRoot: harness.traceRoot,
          rawStderr: result.stderr,
        }
      } finally {
        await harness.dispose()
      }
    }

    try {
      const disabled = await run("disabled", false)
      const enabled = await run("enabled", true)

      expect(enabled.result).toEqual(disabled.result)
      expect(enabled.sessionRows).toEqual(disabled.sessionRows)
      expect(enabled.messages).toEqual(disabled.messages)
      expect(enabled.parts).toEqual(disabled.parts)
      expect(enabled.toolCalls).toEqual(disabled.toolCalls)
      expect(enabled.agentVisibleRequests).toEqual(disabled.agentVisibleRequests)
      expect(enabled.fileHash).toBe(disabled.fileHash)
      expect(enabled.result.exitCode).toBe(0)
      expect(enabled.rawStderr).toContain("[observable-opencode] Session trace saved")
      expect(disabled.rawStderr).not.toContain("[observable-opencode] Session trace saved")
      expect(fs.existsSync(path.join(disabled.traceRoot, "passive-production-equivalence"))).toBe(false)
      expect(fs.existsSync(path.join(enabled.traceRoot, "passive-production-equivalence", "trace.json"))).toBe(true)
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  }, 1_200_000)

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
