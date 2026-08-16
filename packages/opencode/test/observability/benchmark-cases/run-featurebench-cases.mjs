#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process"
import crypto from "node:crypto"
import fs from "node:fs"
import { request as httpRequest } from "node:http"
import { request as httpsRequest } from "node:https"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { assertTraceSemanticCategories as assertSemanticCategories } from "../semantic-categories.mjs"

const rootDir = path.dirname(fileURLToPath(import.meta.url))
const manifestFile = path.join(rootDir, "open-source-benchmarks.json")

function sha256(value) {
  return crypto
    .createHash("sha256")
    .update(String(value ?? ""))
    .digest("hex")
}

function parseArgs(argv) {
  const args = {
    binary: "",
    datasetRows: "/private/tmp/featurebench-lite-rows.json",
    out: "/private/tmp/observable-opencode-featurebench-run",
    cases: [],
    config: "",
    dryRun: false,
    requestTimeoutMs: 2 * 60 * 60 * 1000,
    semanticProbe: "",
  }
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]
    if (arg === "--binary") args.binary = argv[++index]
    else if (arg === "--dataset-rows") args.datasetRows = argv[++index]
    else if (arg === "--out") args.out = argv[++index]
    else if (arg === "--case") args.cases.push(argv[++index])
    else if (arg === "--config") args.config = argv[++index]
    else if (arg === "--request-timeout-ms") args.requestTimeoutMs = Number(argv[++index])
    else if (arg === "--semantic-probe") args.semanticProbe = argv[++index]
    else if (arg === "--dry-run") args.dryRun = true
    else if (arg === "--help" || arg === "-h") args.help = true
    else throw new Error(`unknown argument: ${arg}`)
  }
  return args
}

function usage() {
  return [
    "Usage: node run-featurebench-cases.mjs --binary <observable-binary> --dataset-rows <rows.json> [options]",
    "       node run-featurebench-cases.mjs --semantic-probe <trace.json>",
    "",
    "The runner uses FeatureBench's official problem statement and masked repository setup, then sends",
    "the request through opencode serve -> session -> HTTP message. It never includes patch/test_patch",
    "content in the agent prompt.",
  ].join("\n")
}

const FEATUREBENCH_SEMANTIC_CATEGORIES = ["artifact", "edge", "lifecycle", "node", "response", "tool"]

export function assertTraceSemanticCategories(traceFile, required = FEATUREBENCH_SEMANTIC_CATEGORIES) {
  return assertSemanticCategories(path.resolve(traceFile), required)
}

export function selectFeatureBenchRows(payload, sourceManifest) {
  const rows = (Array.isArray(payload?.rows) ? payload.rows : [])
    .map((item) => item?.row)
    .filter((item) => item && typeof item === "object")
  const byID = new Map(rows.map((row) => [row.instance_id, row]))
  return sourceManifest.instances.map((expected) => {
    const row = byID.get(expected.instance_id)
    if (!row) throw new Error(`FeatureBench instance missing from dataset rows: ${expected.instance_id}`)
    if (row.repo !== expected.repo) throw new Error(`repository mismatch for ${expected.instance_id}`)
    if (row.base_commit !== expected.base_commit) throw new Error(`base commit mismatch for ${expected.instance_id}`)
    const actualHash = sha256(row.problem_statement)
    if (actualHash !== expected.problem_statement_sha256) {
      throw new Error(`problem statement hash mismatch for ${expected.instance_id}`)
    }
    return { ...row, benchmark_manifest: expected }
  })
}

export function buildAgentPrompt(row, repoDir) {
  const repositoryRoot = path.resolve(repoDir)
  const officialTask = String(row.problem_statement ?? "").replaceAll("/testbed/", `${repositoryRoot}/`)
  return [
    officialTask,
    "",
    "## Local execution adapter",
    `The current open-source repository root is ${repositoryRoot}. Work only in this repository.`,
    "Inspect the codebase, implement the complete requested feature, and run the most relevant available tests.",
  ].join("\n")
}

export function featureBenchSubjectRevision(row) {
  return [
    "featurebench",
    row.instance_id,
    row.base_commit,
    "mask",
    sha256(row.patch),
  ].join(":")
}

export function writeNoninteractiveBenchmarkConfig(configRoot) {
  const configFile = path.join(path.resolve(configRoot), "opencode", "opencode.json")
  fs.mkdirSync(path.dirname(configFile), { recursive: true })
  fs.writeFileSync(
    configFile,
    JSON.stringify(
      {
        $schema: "https://opencode.ai/config.json",
        permission: {
          external_directory: "deny",
        },
      },
      null,
      2,
    ) + "\n",
  )
  return configFile
}

export function binarySupportsSubjectRevision(binaryPath) {
  return fs
    .readFileSync(path.resolve(binaryPath))
    .includes(Buffer.from("OPENCODE_TRACE_SUBJECT_REVISION"))
}

export function officialEvaluationStatus({ dockerAvailable }) {
  if (!dockerAvailable) {
    return {
      status: "not_run",
      reason: "docker_unavailable_on_host",
      evaluator: "FeatureBench official harness",
    }
  }
  return {
    status: "pending",
    reason: "prediction_ready_for_official_harness",
    evaluator: "FeatureBench official harness",
  }
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: options.env ?? process.env,
    input: options.input,
    encoding: "utf8",
    stdio: options.input === undefined ? "pipe" : ["pipe", "pipe", "pipe"],
    maxBuffer: 64 * 1024 * 1024,
  })
  if (result.status !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} failed (${result.status}): ${result.stderr || result.stdout || result.error}`,
    )
  }
  return result.stdout
}

export function sealMaskedRepositoryHistory(repoDir) {
  fs.rmSync(path.join(repoDir, ".git"), { recursive: true, force: true })
  run("git", ["init", "-q", "-b", "benchmark"], { cwd: repoDir })
  run("git", ["config", "user.email", "observable-opencode@localhost"], { cwd: repoDir })
  run("git", ["config", "user.name", "observable-opencode benchmark adapter"], { cwd: repoDir })
  run("git", ["add", "-A"], { cwd: repoDir })
  run("git", ["commit", "--allow-empty", "-m", "FeatureBench masked baseline"], { cwd: repoDir })
}

function prepareRepository(row, outDir) {
  const repoDir = path.join(outDir, "repos", row.instance_id)
  fs.rmSync(repoDir, { recursive: true, force: true })
  fs.mkdirSync(path.dirname(repoDir), { recursive: true })
  run("git", ["clone", "--filter=blob:none", "--no-checkout", `https://github.com/${row.repo}.git`, repoDir])
  run("git", ["checkout", "--detach", row.base_commit], { cwd: repoDir })
  if (String(row.patch ?? "").trim()) {
    run("git", ["apply", "--whitespace=nowarn", "-"], { cwd: repoDir, input: row.patch })
  }
  for (const testFile of Array.isArray(row.FAIL_TO_PASS) ? row.FAIL_TO_PASS : []) {
    fs.rmSync(path.join(repoDir, String(testFile).replace(/^\/testbed\//, "")), { force: true })
  }
  sealMaskedRepositoryHistory(repoDir)
  return repoDir
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export async function waitForGeneratedFile(file, options = {}) {
  const timeoutMs = options.timeoutMs ?? 60_000
  const intervalMs = options.intervalMs ?? 100
  const deadline = Date.now() + timeoutMs
  while (Date.now() <= deadline) {
    if (fs.existsSync(file)) return file
    await wait(intervalMs)
  }
  throw new Error(`timed out after ${timeoutMs} ms waiting for generated file: ${file}`)
}

function signalExitCode(signal) {
  return signal === "SIGINT" ? 130 : signal === "SIGTERM" ? 143 : 129
}

export const DEFAULT_CHILD_EXIT_TIMEOUT_MS = 5 * 60_000

function waitForChildExit(child, timeoutMs) {
  if (child.exitCode !== null) return Promise.resolve(child.exitCode)
  return new Promise((resolve, reject) => {
    const onExit = (code) => {
      clearTimeout(timer)
      resolve(code ?? child.exitCode ?? 0)
    }
    const timer = setTimeout(() => {
      child.off?.("exit", onExit)
      reject(new Error(`timed out after ${timeoutMs} ms waiting for observable-opencode child exit`))
    }, timeoutMs)
    child.once("exit", onExit)
  })
}

export function createRunnerSignalLifecycle(options) {
  const child = options.child
  const traceFile = options.traceFile
  const traceTimeoutMs = options.traceTimeoutMs ?? 60_000
  const childExitTimeoutMs = options.childExitTimeoutMs ?? DEFAULT_CHILD_EXIT_TIMEOUT_MS
  const handlers = new Map()
  let forwardedSignal
  let forwarding

  const forward = (signal) => {
    if (forwarding) return forwarding
    forwardedSignal = signal
    forwarding = (async () => {
      if (child.exitCode === null) signalRunnerChild(child, signal)
      const exitCode = await waitForChildExit(child, childExitTimeoutMs)
      await waitForGeneratedFile(traceFile, { timeoutMs: traceTimeoutMs })
      return { signal, exitCode, traceFile }
    })()
    return forwarding
  }

  const dispose = () => {
    for (const [signal, handler] of handlers) process.off(signal, handler)
    handlers.clear()
  }

  if (options.installProcessHandlers !== false) {
    for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
      const handler = () => void forward(signal)
      handlers.set(signal, handler)
      process.once(signal, handler)
    }
  }

  return {
    forward,
    dispose,
    get signal() {
      return forwardedSignal
    },
    get pending() {
      return forwarding
    },
  }
}

export function signalRunnerChild(child, signal, options = {}) {
  const platform = options.platform ?? process.platform
  const killProcessGroup = options.killProcessGroup ?? process.kill.bind(process)
  if (platform !== "win32" && Number.isInteger(child.pid) && child.pid > 0) {
    try {
      killProcessGroup(-child.pid, signal)
      return "process_group"
    } catch {}
  }
  child.kill(signal)
  return "child"
}

class RunnerSignalError extends Error {
  constructor(signal) {
    super(`benchmark runner interrupted by ${signal}`)
    this.name = "RunnerSignalError"
    this.signal = signal
    this.exitCode = signalExitCode(signal)
  }
}

function attachProcessLogs(child, resultDir) {
  const stdout = fs.createWriteStream(path.join(resultDir, "server.stdout.log"))
  const stderr = fs.createWriteStream(path.join(resultDir, "server.stderr.log"))
  child.stdout.pipe(stdout, { end: false })
  child.stderr.pipe(stderr, { end: false })
  return async () => {
    child.stdout.unpipe(stdout)
    child.stderr.unpipe(stderr)
    await Promise.all([new Promise((resolve) => stdout.end(resolve)), new Promise((resolve) => stderr.end(resolve))])
  }
}

async function waitForServer(child) {
  let buffer = ""
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out waiting for server; output: ${buffer}`)), 120000)
    child.stdout.on("data", (chunk) => {
      buffer += chunk.toString()
      const match = buffer.match(/listening on http:\/\/127\.0\.0\.1:(\d+)/)
      if (!match) return
      clearTimeout(timer)
      resolve(Number(match[1]))
    })
    child.stderr.on("data", (chunk) => {
      buffer += chunk.toString()
    })
    child.on("exit", (code) => {
      clearTimeout(timer)
      reject(new Error(`server exited before listening: ${code}; output: ${buffer}`))
    })
  })
}

async function stopServer(child) {
  if (child.exitCode !== null) return
  signalRunnerChild(child, "SIGINT")
  for (let attempt = 0; attempt < 80; attempt++) {
    if (child.exitCode !== null) return
    await wait(250)
  }
  signalRunnerChild(child, "SIGTERM")
  for (let attempt = 0; attempt < 40; attempt++) {
    if (child.exitCode !== null) return
    await wait(250)
  }
}

export function postJson(url, body, timeoutMs) {
  const target = new URL(url)
  const payload = JSON.stringify(body)
  const request = target.protocol === "https:" ? httpsRequest : httpRequest
  return new Promise((resolve, reject) => {
    const client = request(
      target,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        },
      },
      (response) => {
        response.setEncoding("utf8")
        let text = ""
        response.on("data", (chunk) => {
          text += chunk
        })
        response.on("end", () => {
          const status = response.statusCode ?? 0
          if (status < 200 || status >= 300) {
            reject(new Error(`HTTP ${status} from ${url}: ${text}`))
            return
          }
          try {
            resolve(text ? JSON.parse(text) : {})
          } catch (error) {
            reject(error)
          }
        })
      },
    )
    client.setTimeout(timeoutMs, () => {
      client.destroy(new Error(`HTTP request timed out after ${timeoutMs} ms: ${url}`))
    })
    client.on("error", reject)
    client.end(payload)
  })
}

function hasDocker() {
  return spawnSync("docker", ["version"], { stdio: "ignore" }).status === 0
}

async function runOneCase(row, args, sourceManifest) {
  const outDir = path.resolve(args.out)
  const repoDir = prepareRepository(row, outDir)
  const tracesDir = path.join(outDir, "traces")
  const homeDir = path.join(outDir, "home", row.instance_id)
  const configDir = args.config ? path.resolve(args.config) : path.join(outDir, "config", row.instance_id)
  const dataDir = path.join(outDir, "data", row.instance_id)
  const cacheDir = path.join(outDir, "cache", row.instance_id)
  const resultDir = path.join(outDir, "benchmark-results", row.instance_id)
  for (const directory of [tracesDir, homeDir, configDir, dataDir, cacheDir, resultDir]) {
    fs.mkdirSync(directory, { recursive: true })
  }
  if (!args.config) writeNoninteractiveBenchmarkConfig(configDir)
  const prompt = buildAgentPrompt(row, repoDir)
  const traceFile = path.join(tracesDir, row.instance_id, "trace.json")
  const child = spawn(path.resolve(args.binary), ["serve", "--hostname", "127.0.0.1", "--port", "0"], {
    cwd: repoDir,
    env: {
      ...process.env,
      HOME: homeDir,
      XDG_CONFIG_HOME: configDir,
      XDG_DATA_HOME: dataDir,
      XDG_CACHE_HOME: cacheDir,
      OPENCODE_CASE_TRACE: "1",
      OPENCODE_CASE_TRACE_DIR: tracesDir,
      OPENCODE_CASE_ID: row.instance_id,
      OPENCODE_BENCHMARK_SOURCE: sourceManifest.source.dataset,
      OPENCODE_BENCHMARK_SPLIT: sourceManifest.source.split,
      OPENCODE_BENCHMARK_INSTANCE_ID: row.instance_id,
      OPENCODE_TRACE_SUBJECT_REVISION: featureBenchSubjectRevision(row),
    },
    stdio: ["ignore", "pipe", "pipe"],
    detached: process.platform !== "win32",
  })
  const signalLifecycle = createRunnerSignalLifecycle({ child, traceFile })
  const closeProcessLogs = attachProcessLogs(child, resultDir)

  let requestError
  try {
    const port = await waitForServer(child)
    const directory = encodeURIComponent(repoDir)
    const session = await postJson(
      `http://127.0.0.1:${port}/session?directory=${directory}`,
      { title: row.instance_id },
      args.requestTimeoutMs,
    )
    await postJson(
      `http://127.0.0.1:${port}/session/${session.id}/message?directory=${directory}`,
      {
        model: {
          providerID: "deepseek",
          modelID: process.env.OPENCODE_BENCHMARK_MODEL ?? "deepseek-v4-pro",
        },
        agent: "build",
        parts: [{ type: "text", text: prompt }],
      },
      args.requestTimeoutMs,
    )
  } catch (error) {
    requestError = error
  } finally {
    if (signalLifecycle.pending) await signalLifecycle.pending
    else await stopServer(child)
    signalLifecycle.dispose()
    await closeProcessLogs()
  }

  if (signalLifecycle.signal) throw new RunnerSignalError(signalLifecycle.signal)
  try {
    await waitForGeneratedFile(traceFile)
  } catch (error) {
    throw requestError ?? error
  }
  const traceSemanticCategories = assertTraceSemanticCategories(traceFile)
  const modelPatch = run("git", ["diff", "--binary", "HEAD"], { cwd: repoDir })
  const evaluation = officialEvaluationStatus({ dockerAvailable: hasDocker() })
  const result = {
    benchmark: sourceManifest.source,
    instance: row.benchmark_manifest,
    prompt_sha256: sha256(prompt),
    mask_patch_sha256: sha256(row.patch),
    test_patch_sha256: sha256(row.test_patch),
    request_status: requestError ? "error" : "completed",
    request_error: requestError ? String(requestError.stack ?? requestError) : undefined,
    model_patch_sha256: sha256(modelPatch),
    model_patch_length: modelPatch.length,
    model_patch: modelPatch,
    trace_file: path.relative(outDir, traceFile),
    trace_semantic_categories: traceSemanticCategories,
    evaluation,
  }
  fs.writeFileSync(path.join(resultDir, "result.json"), JSON.stringify(result, null, 2) + "\n")
  if (requestError) throw requestError
  return result
}

export async function runFeatureBenchCases(args) {
  if (args.semanticProbe) {
    const categories = assertTraceSemanticCategories(args.semanticProbe)
    console.log(`[featurebench] semantic categories: ${categories.join(", ")}`)
    return categories
  }
  const sourceManifest = JSON.parse(fs.readFileSync(manifestFile, "utf8"))
  const rowsPayload = JSON.parse(fs.readFileSync(path.resolve(args.datasetRows), "utf8"))
  let rows = selectFeatureBenchRows(rowsPayload, sourceManifest)
  if (args.cases.length) {
    const wanted = new Set(args.cases)
    rows = rows.filter((row) => wanted.has(row.instance_id))
    const missing = [...wanted].filter((id) => !rows.some((row) => row.instance_id === id))
    if (missing.length) throw new Error(`unknown FeatureBench case id(s): ${missing.join(", ")}`)
  }
  if (args.dryRun) {
    for (const row of rows) console.log(`${row.instance_id}\t${row.repo}\t${row.base_commit}`)
    return []
  }
  if (!args.binary) throw new Error("--binary is required")
  if (!binarySupportsSubjectRevision(args.binary)) {
    throw new Error(
      "observable-opencode binary does not support OPENCODE_TRACE_SUBJECT_REVISION; use a newer build before running a benchmark",
    )
  }
  if (!process.env.DEEPSEEK_API_KEY) throw new Error("DEEPSEEK_API_KEY is required")
  const results = []
  for (const row of rows) {
    console.log(`[featurebench] running ${row.instance_id}`)
    results.push(await runOneCase(row, args, sourceManifest))
  }
  return results
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const args = parseArgs(process.argv.slice(2))
  if (args.help) {
    console.log(usage())
    process.exit(0)
  }
  runFeatureBenchCases(args).catch((error) => {
    console.error(error?.stack ?? String(error))
    process.exit(error instanceof RunnerSignalError ? error.exitCode : 1)
  })
}
