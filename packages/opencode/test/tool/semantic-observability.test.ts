import { describe, expect, test } from "bun:test"
import { classifyShellOperation, inferVerificationStatus } from "../../src/tool/tool"
import { captureRepositorySnapshot, repositorySnapshotDelta } from "../../src/observability/repository-snapshot"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { execFileSync } from "node:child_process"

describe("tool semantic observability", () => {
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
