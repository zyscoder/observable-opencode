import { Cause, Effect, Exit, Layer, Schema } from "effect"
import * as Stream from "effect/Stream"
import { createHash } from "node:crypto"
import fs from "node:fs"
import path from "node:path"
import { Agent } from "../../../src/agent/agent"
import { Bus } from "../../../src/bus"
import { Config } from "../../../src/config/config"
import { Image } from "../../../src/image/image"
import { Permission } from "../../../src/permission"
import { Plugin } from "../../../src/plugin"
import type { Provider } from "../../../src/provider/provider"
import { ModelID, ProviderID } from "../../../src/provider/schema"
import { Session } from "../../../src/session/session"
import { LLM } from "../../../src/session/llm"
import { MessageV2 } from "../../../src/session/message-v2"
import { SessionProcessor } from "../../../src/session/processor"
import { MessageID, PartID, SessionID } from "../../../src/session/schema"
import { SessionStatus } from "../../../src/session/status"
import { SessionSummary } from "../../../src/session/summary"
import { Snapshot } from "../../../src/snapshot"
import { SyncEvent } from "../../../src/sync"
import { Tool } from "../../../src/tool/tool"
import { Truncate } from "../../../src/tool/truncate"
import * as Log from "@opencode-ai/core/util/log"

Date.now = () => 1_700_000_000_000
let partSequence = 0
Object.defineProperty(PartID, "ascending", {
  value: (given?: string) => PartID.make(given ?? `prt_passive_${String(++partSequence).padStart(3, "0")}`),
})
await Log.init({ print: false })
const workspace = process.env.OPENCODE_EQUIVALENCE_WORKSPACE ?? process.cwd()

class PassiveToolError extends Error {
  constructor() {
    super("passive benchmark failure")
    this.name = "PassiveToolError"
  }
}

const model: Provider.Model = {
  id: ModelID.make("passive-model"),
  providerID: ProviderID.make("passive-provider"),
  api: { id: "passive-model", url: "https://example.invalid", npm: "@ai-sdk/openai" },
  name: "Passive Model",
  capabilities: {
    temperature: false,
    reasoning: false,
    attachment: false,
    toolcall: true,
    input: { text: true, audio: false, image: false, video: false, pdf: false },
    output: { text: true, audio: false, image: false, video: false, pdf: false },
    interleaved: false,
  },
  cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
  limit: { context: 100_000, input: 100_000, output: 10_000 },
  status: "active",
  options: {},
  headers: {},
  release_date: "2026-01-01",
}

const agent: Agent.Info = {
  name: "build",
  mode: "primary",
  options: {},
  permission: [{ permission: "*", pattern: "*", action: "allow" }],
}

const parts = new Map<string, MessageV2.Part>()
const messages = new Map<string, MessageV2.Info>()
const sessionLayer = Layer.mock(Session.Service, {
  getPart: (input) => Effect.succeed(parts.get(input.partID)),
  updatePart: (part) =>
    Effect.sync(() => {
      parts.set(part.id, structuredClone(part))
      return part
    }),
  updateMessage: (message) =>
    Effect.sync(() => {
      messages.set(message.id, structuredClone(message))
      return message
    }),
})

let stream = Stream.empty as Stream.Stream<LLM.Event, unknown>
const llmLayer = Layer.mock(LLM.Service, {
  stream: () => stream,
})
const agentLayer = Layer.mock(Agent.Service, { get: () => Effect.succeed(agent) })
const configLayer = Layer.mock(Config.Service, { get: () => Effect.succeed({}) })
const dependencies = Layer.mergeAll(
  sessionLayer,
  configLayer,
  Layer.mock(Bus.Service, {}),
  Layer.mock(Snapshot.Service, { track: () => Effect.succeed(undefined) }),
  agentLayer,
  llmLayer,
  Layer.mock(Permission.Service, {}),
  Layer.mock(Plugin.Service, {}),
  Layer.mock(Image.Service, {}),
  Layer.mock(SessionSummary.Service, {}),
  Layer.mock(SessionStatus.Service, {}),
  Layer.mock(SyncEvent.Service, { run: () => Effect.void }),
)
const layer = Layer.mergeAll(
  dependencies,
  Truncate.defaultLayer,
  SessionProcessor.layer.pipe(Layer.provideMerge(dependencies)),
)

const result = await Effect.runPromise(
  Effect.gen(function* () {
    const sessionID = SessionID.make("ses_passive_projection")
    const user: MessageV2.User = {
      id: MessageID.make("msg_passive_user"),
      sessionID,
      role: "user",
      time: { created: Date.now() },
      agent: "build",
      model: { providerID: model.providerID, modelID: model.id },
    }
    const assistant: MessageV2.Assistant = {
      id: MessageID.make("msg_passive_assistant"),
      sessionID,
      role: "assistant",
      parentID: user.id,
      mode: "build",
      agent: "build",
      path: { cwd: "/passive", root: "/passive" },
      cost: 0,
      tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      modelID: model.id,
      providerID: model.providerID,
      time: { created: Date.now() },
    }
    const processor = yield* SessionProcessor.Service
    const handle = yield* processor.create({ assistantMessage: assistant, sessionID, model })
    const inputs: unknown[] = []
    const metadataCallbacks: unknown[] = []
    const askCallbacks: unknown[] = []
    const errorOracles: unknown[] = []
    const parameters = Schema.Struct({
      scenario: Schema.Union([Schema.Literal("success"), Schema.Literal("error"), Schema.Literal("pre-aborted")]),
      payload: Schema.String,
    })
    const info = yield* Tool.define<typeof parameters, { scenario: string }, never>(
      "passive-benchmark",
      Effect.succeed({
        description: "exercise passive tracing through production projection",
        parameters,
        execute: (args, ctx) =>
          Effect.gen(function* () {
            inputs.push(args)
            yield* ctx.metadata({ title: `passive ${args.scenario}`, metadata: { callback: args.scenario } })
            yield* ctx.ask({ permission: "read", patterns: [args.payload], always: [], metadata: {} })
            if (args.scenario === "error") return yield* Effect.die(new PassiveToolError())
            if (args.scenario === "pre-aborted" && ctx.abort.aborted) {
              return yield* Effect.die(new DOMException("Passive tool pre-aborted", "AbortError"))
            }
            fs.writeFileSync(path.join(workspace, "agent-output.txt"), "stable agent file\n")
            return {
              title: "passive success",
              output: "stable tool output",
              metadata: { scenario: args.scenario },
              attachments: [
                {
                  type: "file" as const,
                  mime: "text/plain",
                  filename: "passive.txt",
                  url: "data:text/plain;base64,cGFzc2l2ZQ==",
                },
              ],
            }
          }),
      }),
    )
    const tool = yield* info.init()
    const scenarios = ["success", "error", "pre-aborted"] as const

    for (const scenario of scenarios) {
      const callID = `passive-${scenario}`
      const args = { scenario, payload: "fixed-input" }
      const abort = new AbortController()
      if (scenario === "pre-aborted") abort.abort()
      const context: Tool.Context = {
        sessionID,
        messageID: assistant.id,
        agent: agent.name,
        abort: abort.signal,
        callID,
        messages: [],
        metadata: (value) => {
          metadataCallbacks.push({ scenario, value })
          return handle
            .updateToolCall(callID, (part) => ({
              ...part,
              state: {
                status: "running",
                input: args,
                title: value.title,
                metadata: value.metadata,
                time: { start: Date.now() },
              },
            }))
            .pipe(Effect.asVoid)
        },
        ask: (value) =>
          Effect.sync(() => {
            askCallbacks.push({ scenario, value })
          }),
      }
      stream = Stream.concat(
        Stream.fromIterable([{ type: "tool-input-start", id: callID, toolName: info.id } as LLM.Event]),
        Stream.fromEffect(
          Effect.gen(function* () {
            const exit = yield* Effect.exit(tool.execute(args, context))
            if (Exit.isFailure(exit)) {
              const error = Cause.squash(exit.cause)
              errorOracles.push({
                scenario,
                constructorName: error instanceof Error ? error.constructor.name : typeof error,
                name: error instanceof Error ? error.name : undefined,
                isPassiveToolError: error instanceof PassiveToolError,
                hasPassiveToolErrorPrototype: Object.getPrototypeOf(error) === PassiveToolError.prototype,
                message: error instanceof Error ? error.message : String(error),
              })
              return {
                type: "tool-error",
                toolCallId: callID,
                toolName: info.id,
                error,
              } as LLM.Event
            }
            return {
              type: "tool-result",
              toolCallId: callID,
              toolName: info.id,
              input: args,
              output: {
                ...exit.value,
                attachments: exit.value.attachments?.map((attachment, index) => ({
                  ...attachment,
                  id: `attachment-${scenario}-${index}`,
                  sessionID,
                  messageID: assistant.id,
                })),
              },
            } as LLM.Event
          }),
        ),
      )
      yield* handle.process({
        user,
        sessionID,
        model,
        agent,
        system: [],
        messages: [{ role: "user", content: "run passive benchmark" }],
        tools: {},
      })
    }

    const toolParts = Array.from(parts.values()).filter((part): part is MessageV2.ToolPart => part.type === "tool")
    const projectedToolParts = toolParts.map((part) => ({
      callID: part.callID,
      tool: part.tool,
      state: {
        ...part.state,
        ...(part.state.status === "completed"
          ? {
              attachments: part.state.attachments?.map((attachment) => ({
                type: attachment.type,
                mime: attachment.mime,
                filename: attachment.filename,
                url: attachment.url,
              })),
            }
          : {}),
      },
    }))
    const agentVisibleMessages = yield* Effect.promise(() =>
      MessageV2.toModelMessages(
        [
          {
            info: user,
            parts: [
              {
                id: PartID.make("prt_passive_user"),
                messageID: user.id,
                sessionID,
                type: "text",
                text: "run passive benchmark",
              },
            ],
          },
          { info: assistant, parts: toolParts },
        ],
        model,
      ),
    )
    const fileHashes = Object.fromEntries(
      fs
        .readdirSync(workspace)
        .sort()
        .map((file) => [file, createHash("sha256").update(fs.readFileSync(path.join(workspace, file))).digest("hex")]),
    )
    const sessionRows = {
      messages: [...messages.values()].map((message) => ({
        id: message.id,
        sessionID: message.sessionID,
        role: message.role,
        ...("parentID" in message ? { parentID: message.parentID } : {}),
        ...("finish" in message ? { finish: message.finish } : {}),
      })),
      parts: toolParts.map((part) => ({
        id: part.id,
        messageID: part.messageID,
        sessionID: part.sessionID,
        type: part.type,
        callID: part.callID,
        tool: part.tool,
        status: part.state.status,
      })),
    }
    const toolCalls = projectedToolParts.map((part) => ({
      callID: part.callID,
      tool: part.tool,
      input: part.state.input,
    }))

    return {
      fileHashes,
      sessionRows,
      toolCalls,
      inputs,
      callbackCounts: { metadata: metadataCallbacks.length, ask: askCallbacks.length },
      metadataCallbacks,
      askCallbacks,
      errorOracles,
      projectedToolParts,
      agentVisibleMessages,
    }
  }).pipe(Effect.provide(layer), Effect.scoped),
)

process.stdout.write(JSON.stringify(result))
