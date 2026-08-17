import { Database } from "bun:sqlite"
import fs from "node:fs/promises"
import { createServer } from "node:http"
import path from "node:path"

const packageDir = path.resolve(import.meta.dir, "../../..")
const cli = path.join(packageDir, "src", "index.ts")
const mcpServer = path.join(import.meta.dir, "task-7-mcp-server.ts")

type Completion =
  | { type: "text"; text: string; inputTokens?: number; outputTokens?: number }
  | { type: "tool"; name: string; callID: string; input: unknown }

type PersistedSession = {
  id: string
  parent_id: string | null
  title: string
  directory: string
}

type PersistedRow = {
  id: string
  session_id: string
  message_id?: string
  data: string
  time_created: number
  time_updated: number
}

function sse(completion: Completion) {
  const line = (value: unknown) => `data: ${JSON.stringify(value)}\n\n`
  const start = line({
    id: "chatcmpl-task-7",
    object: "chat.completion.chunk",
    choices: [{ delta: { role: "assistant" } }],
  })
  if (completion.type === "tool") {
    return [
      start,
      line({
        id: "chatcmpl-task-7",
        object: "chat.completion.chunk",
        choices: [
          {
            delta: {
              tool_calls: [
                {
                  index: 0,
                  id: completion.callID,
                  type: "function",
                  function: { name: completion.name, arguments: "" },
                },
              ],
            },
          },
        ],
      }),
      line({
        id: "chatcmpl-task-7",
        object: "chat.completion.chunk",
        choices: [
          {
            delta: {
              tool_calls: [
                {
                  index: 0,
                  function: { arguments: JSON.stringify(completion.input) },
                },
              ],
            },
          },
        ],
      }),
      line({
        id: "chatcmpl-task-7",
        object: "chat.completion.chunk",
        choices: [{ delta: {}, finish_reason: "tool_calls" }],
        usage: { prompt_tokens: 64, completion_tokens: 16, total_tokens: 80 },
      }),
      "data: [DONE]\n\n",
    ].join("")
  }
  const inputTokens = completion.inputTokens ?? 64
  const outputTokens = completion.outputTokens ?? 16
  return [
    start,
    line({
      id: "chatcmpl-task-7",
      object: "chat.completion.chunk",
      choices: [{ delta: { content: completion.text } }],
    }),
    line({
      id: "chatcmpl-task-7",
      object: "chat.completion.chunk",
      choices: [{ delta: {}, finish_reason: "stop" }],
      usage: {
        prompt_tokens: inputTokens,
        completion_tokens: outputTokens,
        total_tokens: inputTokens + outputTokens,
      },
    }),
    "data: [DONE]\n\n",
  ].join("")
}

function latestUserText(body: Record<string, unknown>) {
  const messages = Array.isArray(body.messages) ? body.messages : []
  const user = messages.findLast(
    (message): message is { role: string; content?: unknown } =>
      typeof message === "object" && message !== null && "role" in message && message.role === "user",
  )
  if (typeof user?.content === "string") return user.content
  if (!Array.isArray(user?.content)) return ""
  return user.content
    .map((part) => {
      if (typeof part === "string") return part
      if (typeof part !== "object" || part === null || !("text" in part)) return ""
      return typeof part.text === "string" ? part.text : ""
    })
    .join("\n")
}

function chooseCompletion(body: Record<string, unknown>, projectDir: string): Completion {
  const serialized = JSON.stringify(body)
  const userText = latestUserText(body)
  if (serialized.includes("Generate a title for this conversation")) {
    return { type: "text", text: "Task 7 production session" }
  }
  if (serialized.includes("Output exactly the Markdown structure shown inside <template>")) {
    return {
      type: "text",
      text: [
        "## Goal",
        "- Complete the Task 7 production workflow.",
        "## Constraints & Preferences",
        "- Preserve local deterministic evidence.",
        "## Progress",
        "### Done",
        "- Write, Skill, MCP, and subagent calls completed.",
        "### In Progress",
        "- Continue after compaction.",
        "### Blocked",
        "- (none)",
        "## Key Decisions",
        "- Keep production persistence authoritative.",
        "## Next Steps",
        "- Return ROOT_WORKFLOW_COMPLETE.",
        "## Critical Context",
        "- local-mcp:trace-fact",
        "## Relevant Files",
        "- agent-output.txt: deterministic tool output.",
      ].join("\n"),
    }
  }
  if (userText.includes("Continue if you have next steps")) {
    return { type: "text", text: "ROOT_WORKFLOW_COMPLETE" }
  }
  if (userText.includes("TASK7_SUBAGENT_WORK")) {
    return { type: "text", text: "SUBAGENT_WORKFLOW_COMPLETE" }
  }
  if (userText.includes("TASK7_RESUME_WORKFLOW")) {
    return { type: "text", text: "RESUME_WORKFLOW_COMPLETE" }
  }
  if (userText.includes("TASK7_ROOT_WORKFLOW")) {
    if (!serialized.includes("call_task7_write")) {
      return {
        type: "tool",
        name: "write",
        callID: "call_task7_write",
        input: {
          filePath: path.join(projectDir, "agent-output.txt"),
          content: "task-7-production-output\n",
        },
      }
    }
    if (!serialized.includes("call_task7_skill")) {
      return {
        type: "tool",
        name: "skill",
        callID: "call_task7_skill",
        input: { name: "task-7-production" },
      }
    }
    if (!serialized.includes("call_task7_mcp")) {
      return {
        type: "tool",
        name: "local_echo",
        callID: "call_task7_mcp",
        input: { value: "trace-fact" },
      }
    }
    if (!serialized.includes("call_task7_task")) {
      return {
        type: "tool",
        name: "task",
        callID: "call_task7_task",
        input: {
          description: "Task 7 child workflow",
          prompt: "TASK7_SUBAGENT_WORK return the deterministic child result",
          subagent_type: "general",
        },
      }
    }
    return { type: "text", text: "ROOT_WORKFLOW_PRE_COMPACTION", inputTokens: 3_600, outputTokens: 32 }
  }
  return { type: "text", text: "TASK7_DEFAULT_RESPONSE" }
}

async function startLoopback(projectDir: string) {
  const requests: Record<string, unknown>[] = []
  const server = createServer(async (request, response) => {
    const chunks: Buffer[] = []
    for await (const chunk of request) chunks.push(Buffer.from(chunk))
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}") as Record<string, unknown>
    requests.push(body)
    response.writeHead(200, {
      "content-type": "text/event-stream",
      connection: "close",
    })
    response.end(sse(chooseCompletion(body, projectDir)))
  })
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const address = server.address()
  if (!address || typeof address === "string") throw new Error("Task 7 loopback server did not bind")
  return {
    url: `http://127.0.0.1:${address.port}/v1`,
    requests,
    close: async () => {
      server.closeAllConnections?.()
      await new Promise<void>((resolve) => server.close(() => resolve()))
    },
  }
}

function environment(root: string) {
  return {
    ...Object.fromEntries(
      Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
    ),
    HOME: path.join(root, "home"),
    XDG_CONFIG_HOME: path.join(root, "config"),
    XDG_DATA_HOME: path.join(root, "data"),
    XDG_CACHE_HOME: path.join(root, "cache"),
    XDG_STATE_HOME: path.join(root, "state"),
    OPENCODE_DISABLE_AUTOUPDATE: "1",
    OPENCODE_DISABLE_CHANNEL_DB: "1",
    OPENCODE_DB: path.join(root, "data", "opencode", "opencode.db"),
    OPENCODE_DISABLE_DEFAULT_PLUGINS: "1",
    OPENCODE_DISABLE_EXTERNAL_SKILLS: "1",
    OPENCODE_DISABLE_LSP_DOWNLOAD: "1",
    OPENCODE_DISABLE_MODELS_FETCH: "1",
  }
}

export async function createTask7ProductionHarness(root: string) {
  const projectDir = path.join(root, "project")
  const traceRoot = path.join(root, "traces")
  await fs.mkdir(path.join(projectDir, ".opencode", "skills", "task-7-production"), { recursive: true })
  await fs.writeFile(
    path.join(projectDir, ".opencode", "skills", "task-7-production", "SKILL.md"),
    [
      "---",
      "name: task-7-production",
      "description: Deterministic production Skill for Task 7 E2E.",
      "---",
      "Retain the local MCP fact and continue the production workflow.",
      "",
    ].join("\n"),
  )
  const loopback = await startLoopback(projectDir)
  await fs.writeFile(
    path.join(projectDir, "opencode.json"),
    JSON.stringify(
      {
        share: "disabled",
        permission: { "*": "allow" },
        compaction: {
          auto: true,
          prune: false,
          reserved: 512,
          preserve_recent_tokens: 1_024,
          tail_turns: 1,
        },
        mcp: {
          local: {
            type: "local",
            command: [process.execPath, mcpServer],
            enabled: true,
          },
        },
        provider: {
          task7: {
            name: "Task 7 loopback",
            id: "task7",
            env: [],
            npm: "@ai-sdk/openai-compatible",
            models: {
              "test-model": {
                id: "test-model",
                name: "Task 7 production model",
                attachment: false,
                reasoning: false,
                temperature: false,
                tool_call: true,
                release_date: "2026-01-01",
                limit: { context: 4_096, output: 512 },
                cost: { input: 0, output: 0 },
                options: {},
              },
            },
            options: { apiKey: "task-7-local", baseURL: loopback.url },
          },
        },
      },
      null,
      2,
    ) + "\n",
  )

  const env = environment(root)
  const databaseFile = path.join(env.XDG_DATA_HOME, "opencode", "opencode.db")

  function query<T>(sql: string, values: string[] = []) {
    const database = new Database(databaseFile, { readonly: true })
    try {
      return database.query(sql).all(...values) as T[]
    } finally {
      database.close()
    }
  }

  return {
    projectDir,
    traceRoot,
    databaseFile,
    requests: loopback.requests,
    async run(input: { tracing: boolean; caseID: string; message: string; sessionID?: string }) {
      const args = [
        cli,
        "--pure",
        "run",
        "--dir",
        projectDir,
        "--model",
        "task7/test-model",
        "--dangerously-skip-permissions",
      ]
      if (input.sessionID) args.push("--session", input.sessionID)
      args.push(input.message)
      const child = Bun.spawn([process.execPath, ...args], {
        cwd: projectDir,
        env: {
          ...env,
          OPENCODE_CASE_TRACE: input.tracing ? "1" : "0",
          OPENCODE_CASE_TRACE_DIR: traceRoot,
          OPENCODE_CASE_ID: input.caseID,
          OPENCODE_CASE_TRACE_QUIET: "",
        },
        stdout: "pipe",
        stderr: "pipe",
      })
      const exitCode = await child.exited
      return {
        exitCode,
        stdout: await new Response(child.stdout).text(),
        stderr: await new Response(child.stderr).text(),
      }
    },
    persistedSessions() {
      return query<PersistedSession>(
        "select id, parent_id, title, directory from session order by time_created, id",
      )
    },
    persistedMessages(sessionID: string) {
      return query<PersistedRow>(
        "select id, session_id, data, time_created, time_updated from message where session_id = ? order by time_created, id",
        [sessionID],
      )
    },
    persistedParts(sessionID: string) {
      return query<PersistedRow>(
        "select id, session_id, message_id, data, time_created, time_updated from part where session_id = ? order by time_created, id",
        [sessionID],
      )
    },
    dispose: () => loopback.close(),
  }
}
