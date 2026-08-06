import * as Tool from "./tool"
import DESCRIPTION from "./task.txt"
import { Session } from "@/session/session"
import { SessionID, MessageID } from "../session/schema"
import { MessageV2 } from "../session/message-v2"
import { Agent } from "../agent/agent"
import { deriveSubagentSessionPermission } from "../agent/subagent-permissions"
import type { SessionPrompt } from "../session/prompt"
import { Config } from "@/config/config"
import { Effect, Exit, Schema } from "effect"
import { EffectBridge } from "@/effect/bridge"
import { CaseTrace } from "@/observability/case-trace"

export interface TaskPromptOps {
  cancel(sessionID: SessionID): Effect.Effect<void>
  resolvePromptParts(template: string): Effect.Effect<SessionPrompt.PromptInput["parts"]>
  prompt(input: SessionPrompt.PromptInput): Effect.Effect<MessageV2.WithParts>
}

const id = "task"

export const Parameters = Schema.Struct({
  description: Schema.String.annotate({ description: "A short (3-5 words) description of the task" }),
  prompt: Schema.String.annotate({ description: "The task for the agent to perform" }),
  subagent_type: Schema.String.annotate({ description: "The type of specialized agent to use for this task" }),
  task_id: Schema.optional(Schema.String).annotate({
    description:
      "This should only be set if you mean to resume a previous task (you can pass a prior task_id and the task will continue the same subagent session as before instead of creating a fresh one)",
  }),
  command: Schema.optional(Schema.String).annotate({ description: "The command that triggered this task" }),
})

export const TaskTool = Tool.define(
  id,
  Effect.gen(function* () {
    const agent = yield* Agent.Service
    const config = yield* Config.Service
    const sessions = yield* Session.Service

    const run = Effect.fn("TaskTool.execute")(function* (
      params: Schema.Schema.Type<typeof Parameters>,
      ctx: Tool.Context,
    ) {
      const cfg = yield* config.get()

      if (!ctx.extra?.bypassAgentCheck) {
        yield* ctx.ask({
          permission: id,
          patterns: [params.subagent_type],
          always: ["*"],
          metadata: {
            description: params.description,
            subagent_type: params.subagent_type,
          },
        })
      }

      const next = yield* agent.get(params.subagent_type)
      if (!next) {
        return yield* Effect.fail(new Error(`Unknown agent type: ${params.subagent_type} is not a valid agent type`))
      }

      const taskID = params.task_id
      const session = taskID
        ? yield* sessions.get(SessionID.make(taskID)).pipe(Effect.catchCause(() => Effect.succeed(undefined)))
        : undefined
      const parent = yield* sessions.get(ctx.sessionID)
      const parentAgent = parent.agent
        ? yield* agent.get(parent.agent).pipe(Effect.catchCause(() => Effect.succeed(undefined)))
        : undefined
      const nextSession =
        session ??
        (yield* sessions.create({
          parentID: ctx.sessionID,
          title: params.description + ` (@${next.name} subagent)`,
          permission: [
            ...deriveSubagentSessionPermission({
              parentSessionPermission: parent.permission ?? [],
              parentAgent,
              subagent: next,
            }),
            ...(cfg.experimental?.primary_tools?.map((item) => ({
              pattern: "*",
              action: "allow" as const,
              permission: item,
            })) ?? []),
          ],
        }))
      CaseTrace.aliasSession(nextSession.id, ctx.sessionID)

      const msg = yield* Effect.sync(() => MessageV2.get({ sessionID: ctx.sessionID, messageID: ctx.messageID }))
      if (msg.info.role !== "assistant") return yield* Effect.fail(new Error("Not an assistant message"))

      const model = next.model ?? {
        modelID: msg.info.modelID,
        providerID: msg.info.providerID,
      }

      yield* ctx.metadata({
        title: params.description,
        metadata: {
          sessionId: nextSession.id,
          model,
        },
      })

      const ops = ctx.extra?.promptOps as TaskPromptOps
      if (!ops) return yield* Effect.fail(new Error("TaskTool requires promptOps in ctx.extra"))
      const runCancel = yield* EffectBridge.make()

      const messageID = MessageID.ascending()
      const cancel = ops.cancel(nextSession.id)

      function onAbort() {
        runCancel.fork(cancel)
      }

      return yield* Effect.acquireUseRelease(
        Effect.sync(() => {
          ctx.abort.addEventListener("abort", onAbort)
        }),
        () =>
          Effect.gen(function* () {
            const parts = yield* ops.resolvePromptParts(params.prompt)
            const subagentPromptNode = CaseTrace.promptAssembly({
              stage: "subagent_prompt",
              session_id: nextSession.id,
              message_id: messageID,
              agent: next.name,
              model,
              input: {
                parent_session_id: ctx.sessionID,
                parent_message_id: ctx.messageID,
                description: params.description,
                prompt: params.prompt,
                subagent_type: params.subagent_type,
              },
              output: {
                parts,
                part_count: parts.length,
                part_types: parts.map((part) => part.type),
              },
              metadata: {
                resumed: Boolean(taskID),
                command: params.command,
              },
            })
            if (subagentPromptNode) {
              CaseTrace.edge({
                from: { type: "session", id: ctx.sessionID, label: "parent_session" },
                to: { type: "prompt", id: subagentPromptNode.node_id, label: "subagent_prompt" },
                relation: "parent_to_subagent",
                label: "Parent agent delegated prompt context to subagent",
              })
            }
            const result = yield* ops.prompt({
              messageID,
              sessionID: nextSession.id,
              model: {
                modelID: model.modelID,
                providerID: model.providerID,
              },
              agent: next.name,
              tools: {
                ...(next.permission.some((rule) => rule.permission === "todowrite") ? {} : { todowrite: false }),
                ...(next.permission.some((rule) => rule.permission === id) ? {} : { task: false }),
                ...Object.fromEntries((cfg.experimental?.primary_tools ?? []).map((item) => [item, false])),
              },
              parts,
            })

            const output = {
              title: params.description,
              metadata: {
                sessionId: nextSession.id,
                childMessageId: result.info.id,
                model,
              },
              output: [
                `task_id: ${nextSession.id} (for resuming to continue this task if needed)`,
                "",
                "<task_result>",
                result.parts.findLast((item) => item.type === "text")?.text ?? "",
                "</task_result>",
              ].join("\n"),
            }
            if (subagentPromptNode) {
              CaseTrace.edge({
                from: { type: "session", id: nextSession.id, label: "child_session" },
                to: { type: "session", id: ctx.sessionID, label: "parent_session" },
                relation: "subagent_to_parent",
                label: "Subagent result was returned to the parent agent",
              })
              CaseTrace.edge({
                from: { type: "message", id: result.info.id, label: "child_final_message" },
                to: { type: "span", id: ctx.callID ?? nextSession.id, label: "parent_task" },
                relation: "returned_to",
                label: "Subagent final message returned to parent task call",
                metadata: {
                  parent_session_id: ctx.sessionID,
                  child_session_id: nextSession.id,
                },
              })
            }
            return output
          }),
        (_, exit) =>
          Effect.gen(function* () {
            if (Exit.hasInterrupts(exit)) yield* cancel
          }).pipe(
            Effect.ensuring(
              Effect.sync(() => {
                ctx.abort.removeEventListener("abort", onAbort)
              }),
            ),
          ),
      )
    })

    return {
      description: DESCRIPTION,
      parameters: Parameters,
      execute: (params: Schema.Schema.Type<typeof Parameters>, ctx: Tool.Context) => {
        const traceSpan = CaseTrace.startSpan({
          component: "task",
          operation: "subagent",
          name: params.subagent_type,
          input: {
            description: params.description,
            prompt: CaseTrace.summarizeText(params.prompt),
            subagent_type: params.subagent_type,
            task_id: params.task_id,
            command: params.command,
            parent_session_id: ctx.sessionID,
            message_id: ctx.messageID,
          },
        })
        return run(params, ctx).pipe(
          Effect.tap((result) =>
            Effect.sync(() => {
              traceSpan?.end({
                output: {
                  description: params.description,
                  subagent_type: params.subagent_type,
                  child_session_id: result.metadata.sessionId,
                  child_message_id: result.metadata.childMessageId,
                  task_id: result.metadata.sessionId,
                  model: result.metadata.model,
                  output: CaseTrace.summarizeText(result.output),
                },
              })
              CaseTrace.event({
                component: "task",
                event_type: "completed",
                data: {
                  description: params.description,
                  subagent_type: params.subagent_type,
                  task_id: result.metadata.sessionId,
                  model: result.metadata.model,
                  output: CaseTrace.summarizeText(result.output),
                },
              })
              CaseTrace.observation({
                source: "subagent",
                category: params.subagent_type,
                summary: result.output,
                data: {
                  description: params.description,
                  subagent_type: params.subagent_type,
                  child_session_id: result.metadata.sessionId,
                  model: result.metadata.model,
                  output: result.output,
                },
                span_id: traceSpan?.id,
                source_refs: traceSpan ? [`span:${traceSpan.id}`] : undefined,
                metadata: {
                  child_session_id: result.metadata.sessionId,
                },
              })
            }),
          ),
          Effect.tapError((error) =>
            Effect.sync(() => {
              traceSpan?.end({
                status: "error",
                error,
              })
              CaseTrace.event({
                component: "task",
                event_type: "failed",
                data: {
                  description: params.description,
                  subagent_type: params.subagent_type,
                  task_id: params.task_id,
                  error,
                },
              })
            }),
          ),
          Effect.orDie,
        )
      },
    }
  }),
)
