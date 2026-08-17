import { spawn, spawnSync } from "node:child_process"
import fs from "node:fs"
import { createServer } from "node:http"
import path from "node:path"

const packageDir = path.resolve(import.meta.dirname, "../../..")
const cli = path.join(packageDir, "src", "index.ts")

function sse(value) {
  const line = (item) => `data: ${JSON.stringify(item)}\n\n`
  const start = line({
    id: "chatcmpl-task-7-runner",
    object: "chat.completion.chunk",
    choices: [{ delta: { role: "assistant" } }],
  })
  if (value.type === "tool") {
    return [
      start,
      line({
        id: "chatcmpl-task-7-runner",
        object: "chat.completion.chunk",
        choices: [{
          delta: {
            tool_calls: [{
              index: 0,
              id: "call_task7_runner_write",
              type: "function",
              function: { name: "write", arguments: "" },
            }],
          },
        }],
      }),
      line({
        id: "chatcmpl-task-7-runner",
        object: "chat.completion.chunk",
        choices: [{
          delta: {
            tool_calls: [{
              index: 0,
              function: { arguments: JSON.stringify(value.input) },
            }],
          },
        }],
      }),
      line({
        id: "chatcmpl-task-7-runner",
        object: "chat.completion.chunk",
        choices: [{ delta: {}, finish_reason: "tool_calls" }],
        usage: { prompt_tokens: 64, completion_tokens: 16, total_tokens: 80 },
      }),
      "data: [DONE]\n\n",
    ].join("")
  }
  return [
    start,
    line({
      id: "chatcmpl-task-7-runner",
      object: "chat.completion.chunk",
      choices: [{ delta: { content: value.text } }],
    }),
    line({
      id: "chatcmpl-task-7-runner",
      object: "chat.completion.chunk",
      choices: [{ delta: {}, finish_reason: "stop" }],
      usage: { prompt_tokens: 64, completion_tokens: 16, total_tokens: 80 },
    }),
    "data: [DONE]\n\n",
  ].join("")
}

export async function startTask7RunnerModel(outputFile) {
  const server = createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(Buffer.from(chunk))
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}")
    const serialized = JSON.stringify(body)
    const value = serialized.includes("call_task7_runner_write")
      ? { type: "text", text: "TASK7_RUNNER_WORKFLOW_COMPLETE" }
      : {
          type: "tool",
          input: {
            filePath: outputFile,
            content: "task-7-production-runner-output\n",
          },
        }
    response.writeHead(200, {
      "content-type": "text/event-stream",
      connection: "close",
    })
    response.end(sse(value))
  })
  await new Promise((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const address = server.address()
  if (!address || typeof address === "string") throw new Error("Task 7 runner model did not bind")
  return {
    url: `http://127.0.0.1:${address.port}/v1`,
    close: async () => {
      server.closeAllConnections?.()
      await new Promise((resolve) => server.close(resolve))
    },
  }
}

export function writeTask7RunnerConfig(configRoot, baseURL) {
  const configFile = path.join(configRoot, "opencode", "opencode.json")
  fs.mkdirSync(path.dirname(configFile), { recursive: true })
  fs.writeFileSync(
    configFile,
    JSON.stringify({
      share: "disabled",
      permission: { "*": "allow" },
      provider: {
        task7: {
          name: "Task 7 runner loopback",
          id: "task7",
          env: [],
          npm: "@ai-sdk/openai-compatible",
          models: {
            "test-model": {
              id: "test-model",
              name: "Task 7 runner model",
              attachment: false,
              reasoning: false,
              temperature: false,
              tool_call: true,
              release_date: "2026-01-01",
              limit: { context: 8_192, output: 1_024 },
              cost: { input: 0, output: 0 },
              options: {},
            },
          },
          options: { apiKey: "task-7-local", baseURL },
        },
      },
    }, null, 2) + "\n",
  )
  return configFile
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'\\''`)}'`
}

export function writeTask7ObservableBinary(directory) {
  const binary = path.join(directory, "observable-opencode")
  fs.mkdirSync(directory, { recursive: true })
  fs.writeFileSync(
    binary,
    [
      "#!/bin/sh",
      "# OPENCODE_TRACE_SUBJECT_REVISION",
      `exec ${shellQuote(process.execPath)} ${shellQuote(cli)} "$@"`,
      "",
    ].join("\n"),
  )
  fs.chmodSync(binary, 0o755)
  return binary
}

export function runTask7Process(command, args, options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: ["ignore", "pipe", "pipe"],
    })
    let stdout = ""
    let stderr = ""
    child.stdout.on("data", (chunk) => (stdout += chunk))
    child.stderr.on("data", (chunk) => (stderr += chunk))
    child.on("close", (status, signal) => resolve({ status, signal, stdout, stderr }))
  })
}

function git(cwd, args) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" })
  if (result.status !== 0) throw new Error(result.stderr || result.stdout)
  return result.stdout.trim()
}

export function createTask7BenchmarkRepository(directory) {
  fs.mkdirSync(directory, { recursive: true })
  git(directory, ["init", "-q", "-b", "benchmark"])
  git(directory, ["config", "user.email", "task7@localhost"])
  git(directory, ["config", "user.name", "Task 7"])
  fs.writeFileSync(path.join(directory, "README.md"), "# Task 7 benchmark fixture\n")
  git(directory, ["add", "-A"])
  git(directory, ["commit", "-q", "-m", "Task 7 baseline"])
  return git(directory, ["rev-parse", "HEAD"])
}
