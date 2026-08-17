import { expect, test } from "bun:test"
import { spawn as spawnPty } from "bun-pty"
import fs from "node:fs/promises"
import { createServer } from "node:http"
import os from "node:os"
import path from "node:path"

const packageDir = path.resolve(import.meta.dir, "../../..")
const cli = path.join(packageDir, "src", "index.ts")

function completion(text: string) {
  const line = (value: unknown) => `data: ${JSON.stringify(value)}\n\n`
  return [
    line({
      id: "chatcmpl-task-7-tui",
      object: "chat.completion.chunk",
      choices: [{ delta: { role: "assistant" } }],
    }),
    line({
      id: "chatcmpl-task-7-tui",
      object: "chat.completion.chunk",
      choices: [{ delta: { content: text } }],
    }),
    line({
      id: "chatcmpl-task-7-tui",
      object: "chat.completion.chunk",
      choices: [{ delta: {}, finish_reason: "stop" }],
      usage: { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 },
    }),
    "data: [DONE]\n\n",
  ].join("")
}

async function listenLoopback() {
  let hits = 0
  const server = createServer(async (request, response) => {
    hits += 1
    const chunks: Buffer[] = []
    for await (const chunk of request) chunks.push(Buffer.from(chunk))
    const body = Buffer.concat(chunks).toString("utf8")
    const title = body.includes("Generate a title for this conversation")
    response.writeHead(200, {
      "content-type": "text/event-stream",
      connection: "close",
    })
    response.end(completion(title ? "Task 7 TUI" : "production TUI response"))
  })
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const address = server.address()
  if (!address || typeof address === "string") throw new Error("loopback server did not bind a TCP port")
  return {
    url: `http://127.0.0.1:${address.port}/v1`,
    get hits() {
      return hits
    },
    close: async () => {
      server.closeAllConnections?.()
      await new Promise<void>((resolve) => server.close(() => resolve()))
    },
  }
}

async function filesNamed(root: string, name: string) {
  const found: string[] = []
  const visit = async (directory: string) => {
    for (const entry of await fs.readdir(directory, { withFileTypes: true }).catch(() => [])) {
      const file = path.join(directory, entry.name)
      if (entry.isDirectory()) await visit(file)
      if (entry.isFile() && entry.name === name) found.push(file)
    }
  }
  await visit(root)
  return found
}

async function waitForFile(file: string) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    try {
      await fs.access(file)
      return
    } catch {
      await Bun.sleep(25)
    }
  }
}

async function waitForDurableResponse(traceRoot: string, diagnostic: () => string) {
  for (let attempt = 0; attempt < 1_200; attempt += 1) {
    for (const journal of await filesNamed(traceRoot, "records.jsonl")) {
      const text = await fs.readFile(journal, "utf8").catch(() => "")
      if (text.includes("response.output")) return path.dirname(path.dirname(path.dirname(journal)))
    }
    await Bun.sleep(25)
  }
  throw new Error(`timed out waiting for a production TUI response journal under ${traceRoot}; ${diagnostic()}`)
}

function waitForExit(proc: ReturnType<typeof spawnPty>) {
  return new Promise<{ exitCode: number; signal?: number | string }>((resolve) => {
    proc.onExit(resolve)
  })
}

test("production TUI materializes trace.json after normal exit, SIGINT, SIGTERM, and SIGHUP", async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-task-7-production-tui-"))
    const server = await listenLoopback()
    try {
      for (const scenario of [
        { mode: "sigint", signal: "SIGINT", exitCode: 130 },
        { mode: "sigterm", signal: "SIGTERM", exitCode: 143 },
      { mode: "sighup", signal: "SIGHUP", exitCode: 129 },
        { mode: "normal", input: "\u0004", exitCode: 0 },
      ] as const) {
        const scenarioRoot = path.join(root, scenario.mode)
        const project = path.join(scenarioRoot, "project")
        const traceRoot = path.join(scenarioRoot, "traces")
        const caseID = `task-7-production-tui-${scenario.mode}`
        const caseDir = path.join(traceRoot, caseID)
        await fs.mkdir(project, { recursive: true })
        await fs.writeFile(
          path.join(project, "opencode.json"),
          JSON.stringify({
            provider: {
              task7: {
                name: "Task 7 loopback",
                id: "task7",
                env: [],
                npm: "@ai-sdk/openai-compatible",
                models: {
                  "test-model": {
                    id: "test-model",
                    name: "Task 7 test model",
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
                options: { apiKey: "task-7-local", baseURL: server.url },
              },
            },
          }),
        )

        const output: string[] = []
        const proc = spawnPty(
          process.execPath,
          [cli, "--pure", project, "--model", "task7/test-model", "--prompt", `TUI ${scenario.mode}`],
          {
            name: "xterm-256color",
            cols: 100,
            rows: 30,
            cwd: packageDir,
            env: {
              ...Object.fromEntries(
                Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
              ),
              HOME: path.join(scenarioRoot, "home"),
              XDG_CONFIG_HOME: path.join(scenarioRoot, "config"),
              XDG_DATA_HOME: path.join(scenarioRoot, "data"),
              XDG_CACHE_HOME: path.join(scenarioRoot, "cache"),
              OPENCODE_CASE_TRACE: "1",
              OPENCODE_CASE_TRACE_DIR: traceRoot,
              OPENCODE_CASE_ID: caseID,
              OPENCODE_CASE_TRACE_QUIET: "",
            },
          },
        )
        proc.onData((data) => output.push(data))
        const exited = waitForExit(proc)
        try {
          const durableCaseDir = await waitForDurableResponse(
            traceRoot,
            () => `provider hits=${server.hits}; PTY output=${output.join("")}`,
          )
          expect(durableCaseDir).toBe(caseDir)
          if ("signal" in scenario) process.kill(proc.pid, scenario.signal)
          else proc.write(scenario.input)
          const result = await Promise.race([
            exited,
            Bun.sleep(30_000).then(() => {
              throw new Error(`production TUI did not exit for ${scenario.mode}`)
            }),
          ])
          const traceFile = path.join(caseDir, "trace.json")
          await waitForFile(traceFile)
          const traceText = await fs.readFile(traceFile, "utf8").catch((error) => {
            const tail = output.join("").slice(-4_000)
            throw new Error(
              `trace publication failed for ${scenario.mode}; exit=${JSON.stringify(result)}; terminal tail=${tail}`,
              { cause: error },
            )
          })
          const trace = JSON.parse(traceText) as any
          const session = JSON.parse(await fs.readFile(path.join(caseDir, "session.json"), "utf8")) as any

          expect(result.exitCode).toBe(scenario.exitCode)
          expect(output.join("")).toContain("Session trace saved")
          expect(session.segments).toHaveLength(1)
        expect(session.segments[0].status).toBe("signal" in scenario ? "cancelled" : "completed")
        expect(trace.manifest.status).toBe("signal" in scenario ? "cancelled" : "success")
        if ("signal" in scenario) {
          expect(trace.manifest).toMatchObject({
            server_status: "cancelled",
            process_status: "cancelled",
            shutdown_signal: scenario.signal,
          })
        }
          expect(trace.records.some((record: any) => record.event_type === "response.output")).toBe(true)
        } finally {
          try {
            process.kill(proc.pid, "SIGKILL")
          } catch {}
          await Promise.race([exited, Bun.sleep(2_000)]).catch(() => undefined)
        }
      }
    } finally {
      await server.close()
      await fs.rm(root, { recursive: true, force: true })
    }
}, 1_200_000)
