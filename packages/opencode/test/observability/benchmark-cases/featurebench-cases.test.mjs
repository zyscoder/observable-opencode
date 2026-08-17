import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import crypto from "node:crypto"
import fs from "node:fs"
import { createServer } from "node:http"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import * as featureBenchRunner from "./run-featurebench-cases.mjs"
import manifest from "./open-source-benchmarks.json" with { type: "json" }
import {
  createTask7BenchmarkRepository,
  runTask7Process,
  startTask7RunnerModel,
  writeTask7ObservableBinary,
  writeTask7RunnerConfig,
} from "../fixture/task-7-runner-harness.mjs"

const { buildAgentPrompt, officialEvaluationStatus, selectFeatureBenchRows } = featureBenchRunner

function git(cwd, args) {
  return spawnSync("git", args, { cwd, encoding: "utf8" })
}

test("source manifest pins real FeatureBench instances without embedding benchmark patches", () => {
  assert.equal(manifest.source.dataset, "LiberCoders/FeatureBench")
  assert.equal(manifest.source.split, "lite")
  assert.equal(manifest.source.license, "MIT")
  assert.equal(manifest.instances.length, 3)
  assert.ok(manifest.instances.every((item) => item.repo.includes("/")))
  assert.ok(manifest.instances.every((item) => /^[a-f0-9]{40}$/.test(item.base_commit)))
  assert.ok(manifest.instances.every((item) => /^[a-f0-9]{64}$/.test(item.problem_statement_sha256)))
  assert.ok(manifest.instances.every((item) => !("patch" in item)))
  assert.ok(manifest.instances.every((item) => !("test_patch" in item)))
  assert.ok(manifest.instances.every((item) => !("gold_patch" in item)))
  assert.doesNotMatch(JSON.stringify(manifest), /questions\.json|requirement_understanding\.json/)
})

test("adapter selects exact pinned rows and rejects problem statement drift", () => {
  const instance = manifest.instances[0]
  const row = {
    ...instance,
    problem_statement: "Pinned open-source feature request",
    patch: "MASK_PATCH_CONTENT",
    test_patch: "OFFICIAL_TEST_PATCH",
    FAIL_TO_PASS: instance.fail_to_pass,
    PASS_TO_PASS: instance.pass_to_pass,
  }
  const matchingManifest = {
    ...manifest,
    instances: [
      {
        ...instance,
        problem_statement_sha256: crypto.createHash("sha256").update(row.problem_statement).digest("hex"),
      },
    ],
  }

  const selected = selectFeatureBenchRows({ rows: [{ row }] }, matchingManifest)

  assert.equal(selected.length, 1)
  assert.equal(selected[0].instance_id, instance.instance_id)
  assert.throws(
    () => selectFeatureBenchRows({ rows: [{ row: { ...row, problem_statement: "drifted" } }] }, matchingManifest),
    /problem statement hash mismatch/,
  )
})

test("agent prompt contains the official task but never exposes mask or test patches", () => {
  const row = {
    problem_statement: "Implement the complete feature in the current open-source repository.",
    patch: "SECRET_MASK_PATCH_SHOULD_NOT_BE_IN_PROMPT",
    test_patch: "SECRET_TEST_PATCH_SHOULD_NOT_BE_IN_PROMPT",
  }

  const prompt = buildAgentPrompt(row, "/tmp/featurebench/repo")

  assert.match(prompt, /Implement the complete feature/)
  assert.match(prompt, /\/tmp\/featurebench\/repo/)
  assert.doesNotMatch(prompt, /SECRET_MASK_PATCH/)
  assert.doesNotMatch(prompt, /SECRET_TEST_PATCH/)
  assert.doesNotMatch(prompt, /questions\.json/)
})

test("trace subject revision binds the exact masked FeatureBench baseline", () => {
  const row = {
    instance_id: "owner__repo.abc.case.def.lv1",
    base_commit: "a".repeat(40),
    patch: "masked source patch\n",
  }

  const revision = featureBenchRunner.featureBenchSubjectRevision(row)

  assert.equal(
    revision,
    `featurebench:${row.instance_id}:${row.base_commit}:mask:${crypto
      .createHash("sha256")
      .update(row.patch)
      .digest("hex")}`,
  )
})

test("noninteractive benchmark config denies external directory approval deadlocks", () => {
  const configRoot = fs.mkdtempSync(path.join(os.tmpdir(), "featurebench-config-"))

  const configFile = featureBenchRunner.writeNoninteractiveBenchmarkConfig(configRoot)

  assert.equal(configFile, path.join(configRoot, "opencode", "opencode.json"))
  assert.deepEqual(JSON.parse(fs.readFileSync(configFile, "utf8")), {
    $schema: "https://opencode.ai/config.json",
    permission: {
      external_directory: "deny",
    },
  })
})

test("runner rejects binaries that cannot bind trace subject revisions", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "featurebench-binary-capability-"))
  const currentBinary = path.join(directory, "current-opencode")
  const legacyBinary = path.join(directory, "legacy-opencode")
  fs.writeFileSync(currentBinary, "binary\0OPENCODE_TRACE_SUBJECT_REVISION\0payload")
  fs.writeFileSync(legacyBinary, "binary\0legacy-trace-only\0payload")

  assert.equal(featureBenchRunner.binarySupportsSubjectRevision(currentBinary), true)
  assert.equal(featureBenchRunner.binarySupportsSubjectRevision(legacyBinary), false)
})

test("official evaluation is explicitly not run when Docker is unavailable", () => {
  assert.deepEqual(officialEvaluationStatus({ dockerAvailable: false }), {
    status: "not_run",
    reason: "docker_unavailable_on_host",
    evaluator: "FeatureBench official harness",
  })
})

test("HTTP case requests do not inherit global fetch's hidden response-header timeout", async () => {
  assert.equal(typeof featureBenchRunner.postJson, "function")
  const server = createServer((_request, response) => {
    setTimeout(() => {
      response.writeHead(200, {
        "content-type": "application/json",
        connection: "close",
      })
      response.end('{"ok":true}')
    }, 20)
  })
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  const address = server.address()
  assert.ok(address && typeof address === "object")

  try {
    const result = await featureBenchRunner.postJson(
      `http://127.0.0.1:${address.port}/long-agent-turn`,
      { prompt: "run a long case" },
      1000,
    )
    assert.deepEqual(result, { ok: true })
  } finally {
    server.close()
    server.closeAllConnections?.()
    assert.equal(server.listening, false)
  }

  const originalFetch = globalThis.fetch
  let fetchCalls = 0
  globalThis.fetch = async () => {
    fetchCalls += 1
    throw new TypeError("simulated fetch transport timeout")
  }
  try {
    await assert.rejects(
      featureBenchRunner.postJson(
        "http://127.0.0.1:1/no-global-fetch",
        { prompt: "do not use fetch" },
        100,
      ),
    )
    assert.equal(fetchCalls, 0)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("runner waits for trace finalization after the server process exits", async () => {
  assert.equal(typeof featureBenchRunner.waitForGeneratedFile, "function")
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "featurebench-trace-finalize-"))
  const traceFile = path.join(directory, "trace.json")
  setTimeout(() => fs.writeFileSync(traceFile, '{"status":"success"}\n'), 30)

  const result = await featureBenchRunner.waitForGeneratedFile(traceFile, { timeoutMs: 1000, intervalMs: 10 })

  assert.equal(result, traceFile)
  assert.equal(JSON.parse(fs.readFileSync(traceFile, "utf8")).status, "success")
})

test("FeatureBench runner validates semantics from an actual production case", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "featurebench-production-runner-"))
  const sourceRepo = path.join(directory, "source-repo")
  const out = path.join(directory, "out")
  const config = path.join(directory, "config")
  const instanceID = "task7__local.production.case"
  const repo = "task7/local"
  const problemStatement = "Create task-7-runner-output.txt with deterministic production evidence."
  const baseCommit = createTask7BenchmarkRepository(sourceRepo)
  const outputFile = path.join(out, "repos", instanceID, "task-7-runner-output.txt")
  const model = await startTask7RunnerModel(outputFile)
  try {
    const localManifest = {
      source: { dataset: "Task7/LocalFeatureBench", split: "fixture", license: "MIT" },
      instances: [{
        instance_id: instanceID,
        repo,
        base_commit: baseCommit,
        problem_statement_sha256: crypto.createHash("sha256").update(problemStatement).digest("hex"),
      }],
    }
    const rows = {
      rows: [{
        row: {
          instance_id: instanceID,
          repo,
          base_commit: baseCommit,
          problem_statement: problemStatement,
          patch: "",
          test_patch: "",
          FAIL_TO_PASS: [],
          PASS_TO_PASS: [],
        },
      }],
    }
    const manifestFile = path.join(directory, "manifest.json")
    const rowsFile = path.join(directory, "rows.json")
    fs.writeFileSync(manifestFile, JSON.stringify(localManifest))
    fs.writeFileSync(rowsFile, JSON.stringify(rows))
    writeTask7RunnerConfig(config, model.url)
    const binary = writeTask7ObservableBinary(path.join(directory, "bin"))

    const result = await runTask7Process(
      process.execPath,
      [
        path.join(import.meta.dirname, "run-featurebench-cases.mjs"),
        "--binary", binary,
        "--dataset-rows", rowsFile,
        "--manifest", manifestFile,
        "--out", out,
        "--config", config,
        "--case", instanceID,
        "--skip-docker",
      ],
      {
        env: {
          ...process.env,
          DEEPSEEK_API_KEY: "task-7-local",
          OPENCODE_BENCHMARK_PROVIDER: "task7",
          OPENCODE_BENCHMARK_MODEL: "test-model",
          OPENCODE_DB: "opencode.db",
          OPENCODE_DISABLE_AUTOUPDATE: "1",
          OPENCODE_DISABLE_DEFAULT_PLUGINS: "1",
          OPENCODE_DISABLE_LSP_DOWNLOAD: "1",
          OPENCODE_DISABLE_MODELS_FETCH: "1",
          GIT_ALLOW_PROTOCOL: "file",
          GIT_CONFIG_COUNT: "1",
          GIT_CONFIG_KEY_0: `url.file://${sourceRepo}.insteadOf`,
          GIT_CONFIG_VALUE_0: `https://github.com/${repo}.git`,
        },
      },
    )

    assert.equal(result.status, 0, result.stderr)
    assert.equal(fs.readFileSync(outputFile, "utf8"), "task-7-production-runner-output\n")
    const benchmarkResult = JSON.parse(
      fs.readFileSync(path.join(out, "benchmark-results", instanceID, "result.json"), "utf8"),
    )
    assert.equal(benchmarkResult.request_status, "completed")
    for (const category of ["artifact", "edge", "lifecycle", "node", "response", "tool"]) {
      assert.ok(benchmarkResult.trace_semantic_categories.includes(category), category)
    }
    assert.equal(benchmarkResult.evaluation.reason, "docker_disabled_by_option")
  } finally {
    await model.close()
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

test("runner allows large traces enough time to finalize after a signal", () => {
  assert.equal(featureBenchRunner.DEFAULT_CHILD_EXIT_TIMEOUT_MS, 5 * 60_000)
})

test("runner forwards a parent signal once and waits for child trace publication", async () => {
  assert.equal(typeof featureBenchRunner.createRunnerSignalLifecycle, "function")
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "featurebench-signal-forward-"))
  const traceFile = path.join(directory, "trace.json")
  const forwarded = []
  const listeners = new Map()
  const child = {
    exitCode: null,
    kill(signal) {
      forwarded.push(signal)
      setTimeout(() => {
        fs.writeFileSync(traceFile, JSON.stringify({ manifest: { status: "cancelled", shutdown_signal: signal } }))
        child.exitCode = signal === "SIGTERM" ? 143 : 130
        listeners.get("exit")?.(child.exitCode)
      }, 30)
      return true
    },
    once(event, listener) {
      listeners.set(event, listener)
      return child
    },
    off(event, listener) {
      if (listeners.get(event) === listener) listeners.delete(event)
      return child
    },
  }

  const lifecycle = featureBenchRunner.createRunnerSignalLifecycle({
    child,
    traceFile,
    traceTimeoutMs: 1000,
    installProcessHandlers: false,
  })
  const first = lifecycle.forward("SIGTERM")
  const second = lifecycle.forward("SIGTERM")
  const result = await first

  assert.strictEqual(first, second)
  assert.deepEqual(forwarded, ["SIGTERM"])
  assert.equal(result.signal, "SIGTERM")
  assert.equal(result.traceFile, traceFile)
  assert.equal(result.exitCode, 143)
  assert.equal(JSON.parse(fs.readFileSync(traceFile, "utf8")).manifest.status, "cancelled")
  lifecycle.dispose()
})

test("runner targets the detached child process group on posix", () => {
  assert.equal(typeof featureBenchRunner.signalRunnerChild, "function")
  const groupSignals = []
  const directSignals = []
  const child = {
    pid: 4242,
    exitCode: null,
    kill(signal) {
      directSignals.push(signal)
      return true
    },
  }

  const target = featureBenchRunner.signalRunnerChild(child, "SIGTERM", {
    platform: "darwin",
    killProcessGroup(pid, signal) {
      groupSignals.push([pid, signal])
    },
  })

  assert.equal(target, "process_group")
  assert.deepEqual(groupSignals, [[-4242, "SIGTERM"]])
  assert.deepEqual(directSignals, [])
})

test("masked repositories expose no parent commit or unreachable original implementation", () => {
  assert.equal(typeof featureBenchRunner.sealMaskedRepositoryHistory, "function")
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "featurebench-sealed-history-"))
  assert.equal(git(repo, ["init", "-q"]).status, 0)
  assert.equal(git(repo, ["config", "user.email", "benchmark@localhost"]).status, 0)
  assert.equal(git(repo, ["config", "user.name", "benchmark"]).status, 0)
  fs.writeFileSync(path.join(repo, "implementation.py"), "SECRET_ORIGINAL_IMPLEMENTATION = True\n")
  assert.equal(git(repo, ["add", "-A"]).status, 0)
  assert.equal(git(repo, ["commit", "-q", "-m", "original"]).status, 0)
  const originalCommit = git(repo, ["rev-parse", "HEAD"]).stdout.trim()
  fs.rmSync(path.join(repo, "implementation.py"))

  featureBenchRunner.sealMaskedRepositoryHistory(repo)

  assert.equal(git(repo, ["rev-list", "--count", "HEAD"]).stdout.trim(), "1")
  assert.notEqual(git(repo, ["cat-file", "-e", `${originalCommit}^{commit}`]).status, 0)
  assert.notEqual(git(repo, ["show", "HEAD~1:implementation.py"]).status, 0)
})
