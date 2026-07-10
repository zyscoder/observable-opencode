#!/usr/bin/env node
import { spawn } from "node:child_process"
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { analyzeTraceDirectory } from "./analyze-trace-sufficiency.mjs"
import { loadCases } from "./lib/stress-review.mjs"

const rootDir = path.dirname(fileURLToPath(import.meta.url))
const defaultOut = "/tmp/observable-opencode-stress-run"

function parseArgs(argv) {
  const args = {
    binary: "",
    out: defaultOut,
    cases: [],
    dryRun: false,
    config: "",
  }
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]
    if (arg === "--binary") args.binary = argv[++index]
    else if (arg === "--out") args.out = argv[++index]
    else if (arg === "--case") args.cases.push(argv[++index])
    else if (arg === "--config") args.config = argv[++index]
    else if (arg === "--dry-run") args.dryRun = true
    else if (arg === "--help" || arg === "-h") args.help = true
    else throw new Error(`unknown argument: ${arg}`)
  }
  return args
}

function usage() {
  return [
    "Usage: node run-stress-cases.mjs --binary <observable-binary> [--out /tmp/run] [--case case-id] [--dry-run]",
    "",
    "Environment:",
    "  DEEPSEEK_API_KEY             required for real runs",
    "  OPENCODE_STRESS_MODEL        defaults to deepseek-v4-pro",
  ].join("\n")
}

function selectedCases(args) {
  const cases = loadCases(rootDir)
  if (!args.cases.length) return cases
  const wanted = new Set(args.cases)
  const selected = cases.filter((item) => wanted.has(item.case_id))
  const missing = [...wanted].filter((id) => !selected.some((item) => item.case_id === id))
  if (missing.length) throw new Error(`unknown case id(s): ${missing.join(", ")}`)
  return selected
}

function copyFixture(caseDef, outDir) {
  const source = path.join(rootDir, caseDef.fixture_dir)
  const target = path.join(outDir, "repos", caseDef.case_id)
  fs.rmSync(target, { recursive: true, force: true })
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.cpSync(source, target, { recursive: true })
  return target
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function waitForServer(child) {
  let buffer = ""
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out waiting for server; output: ${buffer}`)), 30000)
    child.stdout.on("data", (chunk) => {
      buffer += chunk.toString()
      const match = buffer.match(/listening on http:\/\/127\.0\.0\.1:(\d+)/)
      if (match) {
        clearTimeout(timer)
        resolve(Number(match[1]))
      }
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
  child.kill("SIGINT")
  for (let attempt = 0; attempt < 30; attempt++) {
    if (child.exitCode !== null) return
    await wait(250)
  }
  child.kill("SIGTERM")
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  })
  const text = await response.text()
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${url}: ${text}`)
  return text ? JSON.parse(text) : {}
}

async function runOneCase(caseDef, args) {
  const outDir = path.resolve(args.out)
  const repoDir = copyFixture(caseDef, outDir)
  const tracesDir = path.join(outDir, "traces")
  const homeDir = path.join(outDir, "home", caseDef.case_id)
  const configDir = args.config ? path.resolve(args.config) : path.join(outDir, "config", caseDef.case_id)
  const dataDir = path.join(outDir, "data", caseDef.case_id)
  const cacheDir = path.join(outDir, "cache", caseDef.case_id)
  fs.mkdirSync(tracesDir, { recursive: true })
  fs.mkdirSync(configDir, { recursive: true })
  fs.mkdirSync(homeDir, { recursive: true })
  fs.mkdirSync(dataDir, { recursive: true })
  fs.mkdirSync(cacheDir, { recursive: true })

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
      OPENCODE_CASE_ID: caseDef.case_id,
      ...(caseDef.env ?? {}),
    },
    stdio: ["ignore", "pipe", "pipe"],
  })

  try {
    const port = await waitForServer(child)
    const directory = encodeURIComponent(repoDir)
    const session = await postJson(`http://127.0.0.1:${port}/session?directory=${directory}`, {
      title: caseDef.case_id,
    })
    await postJson(`http://127.0.0.1:${port}/session/${session.id}/message?directory=${directory}`, {
      model: {
        providerID: "deepseek",
        modelID: process.env.OPENCODE_STRESS_MODEL ?? "deepseek-v4-pro",
      },
      agent: "build",
      parts: [{ type: "text", text: caseDef.prompt }],
    })
  } finally {
    await stopServer(child)
  }

  const traceFile = path.join(tracesDir, caseDef.case_id, "trace.json")
  if (!fs.existsSync(traceFile)) throw new Error(`trace.json was not generated for ${caseDef.case_id}`)
}

export async function runStressCases(args) {
  const cases = selectedCases(args)
  if (args.dryRun) {
    for (const item of cases) console.log(`${item.case_id}\t${item.category}\t${item.fixture_dir}`)
    return []
  }
  if (!args.binary) throw new Error("--binary is required for real runs")
  if (!process.env.DEEPSEEK_API_KEY) throw new Error("DEEPSEEK_API_KEY is required for real runs")
  for (const item of cases) {
    console.log(`[stress] running ${item.case_id}`)
    await runOneCase(item, args)
  }
  const reportsDir = path.join(path.resolve(args.out), "reports")
  return analyzeTraceDirectory({
    casesFile: path.join(rootDir, "cases.json"),
    tracesDir: path.join(path.resolve(args.out), "traces"),
    outDir: reportsDir,
    caseIDs: cases.map((item) => item.case_id),
  })
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const args = parseArgs(process.argv.slice(2))
  if (args.help) {
    console.log(usage())
    process.exit(0)
  }
  runStressCases(args).catch((error) => {
    console.error(error?.stack ?? String(error))
    process.exit(1)
  })
}
