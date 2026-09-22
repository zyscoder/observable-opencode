import { PermissionV1 } from "@opencode-ai/core/v1/permission"
import { Effect, Schema } from "effect"
import { SessionV1 } from "@opencode-ai/core/v1/session"
import type { JSONSchema7 } from "@ai-sdk/provider"
import type { MessageV2 } from "../session/message-v2"
import type { Permission } from "../permission"
import type { SessionID, MessageID } from "../session/schema"
import * as Truncate from "./truncate"
import { Agent } from "@/agent/agent"
import {
  closeLatestTrace,
  openLatestTrace,
  recordLatestEdge,
  recordLatestTrace,
} from "@opencode-ai/core/observability/latest-trace"
import {
  classifyToolIntent,
  inferVerificationStatus,
  semanticComponent,
  semanticToolFields,
} from "@/observability/latest-trace-semantics"

interface Metadata {
  [key: string]: any
}

// TODO: remove this hack
export type DynamicDescription = (agent: Agent.Info) => Effect.Effect<string>

/**
 * Raised when the LLM calls a tool with arguments that fail the parameter
 * schema. This is the canonical "rewrite the input" tool error: the typed
 * error class makes it matchable upstream, and its `message` getter produces
 * the model-facing prose that the AI SDK feeds back as the tool result.
 */
export class InvalidArgumentsError extends Schema.TaggedErrorClass<InvalidArgumentsError>()(
  "ToolInvalidArgumentsError",
  {
    tool: Schema.String,
    detail: Schema.String,
  },
) {
  override get message() {
    return `The ${this.tool} tool was called with invalid arguments: ${this.detail}.\nPlease rewrite the input so it satisfies the expected schema.`
  }
}

export type Context<M extends Metadata = Metadata> = {
  sessionID: SessionID
  messageID: MessageID
  agent: string
  abort: AbortSignal
  callID?: string
  extra?: { [key: string]: unknown }
  messages: SessionV1.WithParts[]
  metadata(input: { title?: string; metadata?: M }): Effect.Effect<void>
  ask(input: Omit<PermissionV1.Request, "id" | "sessionID" | "tool">): Effect.Effect<void>
}

export interface ExecuteResult<M extends Metadata = Metadata> {
  title: string
  metadata: M
  output: string
  attachments?: Omit<SessionV1.FilePart, "id" | "sessionID" | "messageID">[]
}

export interface Def<
  Parameters extends Schema.Decoder<unknown> = Schema.Decoder<unknown>,
  M extends Metadata = Metadata,
> {
  id: string
  description: string
  parameters: Parameters
  jsonSchema?: JSONSchema7
  execute(args: Schema.Schema.Type<Parameters>, ctx: Context): Effect.Effect<ExecuteResult<M>>
  formatValidationError?(error: unknown): string
}
export type DefWithoutID<
  Parameters extends Schema.Decoder<unknown> = Schema.Decoder<unknown>,
  M extends Metadata = Metadata,
> = Omit<Def<Parameters, M>, "id">

export interface Info<
  Parameters extends Schema.Decoder<unknown> = Schema.Decoder<unknown>,
  M extends Metadata = Metadata,
> {
  id: string
  init: () => Effect.Effect<DefWithoutID<Parameters, M>>
}

type Init<Parameters extends Schema.Decoder<unknown>, M extends Metadata> =
  | DefWithoutID<Parameters, M>
  | (() => Effect.Effect<DefWithoutID<Parameters, M>>)

export type InferParameters<T> =
  T extends Info<infer P, any>
    ? Schema.Schema.Type<P>
    : T extends Effect.Effect<Info<infer P, any>, any, any>
      ? Schema.Schema.Type<P>
      : never
export type InferMetadata<T> =
  T extends Info<any, infer M> ? M : T extends Effect.Effect<Info<any, infer M>, any, any> ? M : never

export type InferDef<T> =
  T extends Info<infer P, infer M>
    ? Def<P, M>
    : T extends Effect.Effect<Info<infer P, infer M>, any, any>
      ? Def<P, M>
      : never

function wrap<Parameters extends Schema.Decoder<unknown>, Result extends Metadata>(
  id: string,
  init: Init<Parameters, Result>,
  truncate: Truncate.Interface,
  agents: Agent.Interface,
) {
  return () =>
    Effect.gen(function* () {
      const toolInfo = typeof init === "function" ? { ...(yield* init()) } : { ...init }
      // Compile the parser closure once per tool init; `decodeUnknownEffect`
      // allocates a new closure per call, so hoisting avoids re-closing it for
      // every LLM tool invocation.
      const decode = Schema.decodeUnknownEffect(toolInfo.parameters)
      const execute = toolInfo.execute
      toolInfo.execute = (args, ctx) => {
        const attrs = {
          "tool.name": id,
          "session.id": ctx.sessionID,
          "message.id": ctx.messageID,
          ...(ctx.callID ? { "tool.call_id": ctx.callID } : {}),
        }
        const trace = openLatestTrace({
          sessionID: ctx.sessionID,
          step: Date.now(),
          agent: ctx.agent,
        })
        const traceRunID = trace?.runID ?? ctx.sessionID
        const inputArtifact = trace?.recordArtifact({
          value: args,
          mediaType: "application/json",
          previewLimit: 4_000,
        })
        const intent = classifyToolIntent(id, args)
        recordLatestTrace(trace, {
          operation: "tool.call",
          component: semanticComponent(id),
          node_id: `tool_exec_${traceRunID}`,
          data: {
            tool: id,
            call_id: ctx.callID,
            phase: "execution",
            selection_phase: "runtime_dispatch",
            intent: intent.purpose,
            operation_kind: intent.operation,
            input_shape: args && typeof args === "object" ? Object.keys(args as object) : typeof args,
            ...(inputArtifact ? { input_artifact_id: inputArtifact.artifact_id } : {}),
          },
        })
        recordLatestTrace(trace, {
          operation: "decision",
          component: semanticComponent(id),
          node_id: `decision_tool_${traceRunID}`,
          data: {
            decision_id: `decision_tool_${traceRunID}`,
            decision_type: "runtime_tool_dispatch",
            chosen_action: id,
            intent: intent.purpose,
            rationale: "The model emitted this tool call and the runtime dispatched it to the registered implementation.",
            call_id: ctx.callID,
            input_artifact_id: inputArtifact?.artifact_id,
          },
        })
        recordLatestEdge(trace, {
          edge_id: `decision_to_tool_${traceRunID}`,
          from: { type: "node", id: `decision_tool_${traceRunID}` },
          to: { type: "node", id: `tool_exec_${traceRunID}` },
          relation: "selected_by",
          label: `runtime dispatched ${id} after model tool selection`,
        })
        return Effect.gen(function* () {
          const decoded = yield* decode(args).pipe(
            Effect.mapError(
              (error) =>
                new InvalidArgumentsError({
                  tool: id,
                  detail: toolInfo.formatValidationError ? toolInfo.formatValidationError(error) : String(error),
                }),
            ),
          )
          const result = yield* execute(decoded as Schema.Schema.Type<Parameters>, ctx)
          const finalResult =
            result.metadata.truncated !== undefined
              ? result
              : yield* Effect.gen(function* () {
                  const agent = yield* agents.get(ctx.agent)
                  const truncated = yield* truncate.output(result.output, {}, agent)
                  return {
                    ...result,
                    output: truncated.content,
                    metadata: {
                      ...result.metadata,
                      truncated: truncated.truncated,
                      ...(truncated.truncated && { outputPath: truncated.outputPath }),
                    },
                  }
                })
          const outputArtifact = trace?.recordArtifact({
            value: finalResult.output,
            mediaType: "text/plain",
            previewLimit: 4_000,
          })
          const metadataArtifact = trace?.recordArtifact({
            value: finalResult.metadata,
            mediaType: "application/json",
            previewLimit: 4_000,
          })
          const semantic = semanticToolFields(id, args, finalResult.metadata)
          const metadata = finalResult.metadata as Record<string, unknown>
          const semanticNodeID = `tool_semantic_${traceRunID}`
          const semanticArtifact = trace?.recordArtifact({
            value: {
              tool: id,
              args,
              output: finalResult.output,
              metadata: finalResult.metadata,
              semantic,
            },
            mediaType: "application/json",
            previewLimit: 6_000,
          })
          recordLatestTrace(trace, {
            operation: "tool.result",
            component: semanticComponent(id),
            node_id: `tool_exec_result_${traceRunID}`,
            data: {
              tool: id,
              call_id: ctx.callID,
              title: finalResult.title,
              truncated: finalResult.metadata.truncated === true,
              ...(outputArtifact ? { result_artifact_id: outputArtifact.artifact_id } : {}),
              ...(metadataArtifact ? { metadata_artifact_id: metadataArtifact.artifact_id } : {}),
            },
          })
          if (semantic.operation === "verification") {
            recordLatestTrace(trace, {
              operation: "verification",
              component: "tool",
              node_id: semanticNodeID,
              data: {
                tool: id,
                command: semantic.command,
                purpose: semantic.purpose,
                status: inferVerificationStatus(
                  semantic.command ?? "",
                  typeof metadata.exit === "number" ? metadata.exit : undefined,
                  metadata.output ?? finalResult.output,
                  metadata.stderr,
                ),
                exit_code: typeof metadata.exit === "number" ? metadata.exit : undefined,
                stdout: metadata.output ?? finalResult.output,
                stderr: metadata.stderr,
                result_artifact_id: outputArtifact?.artifact_id,
                semantic_artifact_id: semanticArtifact?.artifact_id,
              },
            })
          } else if (semantic.operation === "repository_change") {
            recordLatestTrace(trace, {
              operation: "change",
              component: "tool",
              node_id: semanticNodeID,
              data: {
                tool: id,
                intent: semantic.purpose,
                files: semantic.file_paths,
                diff_artifact_id: metadataArtifact?.artifact_id,
                verification_status: "not_observed_at_tool_boundary",
                semantic_artifact_id: semanticArtifact?.artifact_id,
              },
            })
          } else if (semantic.operation === "code_inspection") {
            recordLatestTrace(trace, {
              operation: "observation",
              component: "tool",
              node_id: semanticNodeID,
              data: {
                tool: id,
                observation_type: "repository_read",
                files: semantic.file_paths,
                read_purpose: semantic.read_purpose,
                read_reason: semantic.read_reason,
                used_by_decision: null,
                semantic_availability: "tool_contract_does_not_expose_downstream_decision_use",
                result_artifact_id: outputArtifact?.artifact_id,
                semantic_artifact_id: semanticArtifact?.artifact_id,
              },
            })
          } else {
            recordLatestTrace(trace, {
              operation: "observation",
              component: semanticComponent(id),
              node_id: semanticNodeID,
              data: {
                tool: id,
                observation_type: "tool_output",
                purpose: semantic.purpose,
                result_artifact_id: outputArtifact?.artifact_id,
                semantic_artifact_id: semanticArtifact?.artifact_id,
              },
            })
          }
          if (semantic.operation === "code_inspection" || semantic.operation === "verification") {
            const evidenceNodeID = `tool_evidence_${traceRunID}`
            recordLatestTrace(trace, {
              operation: "evidence.semantic_fact",
              component: "result",
              node_id: evidenceNodeID,
              data: {
                source_node_id: semanticNodeID,
                evidence_type: semantic.operation === "verification" ? "verification_result" : "repository_observation",
                tool: id,
                artifact_id: outputArtifact?.artifact_id,
                reliability: "observed_tool_output",
              },
            })
            recordLatestEdge(trace, {
              edge_id: `semantic_to_evidence_${traceRunID}`,
              from: { type: "node", id: semanticNodeID },
              to: { type: "node", id: evidenceNodeID },
              relation: "derived_from",
              label: "semantic tool result was preserved as an evidence fact",
            })
          }
          recordLatestEdge(trace, {
            edge_id: `tool_result_to_semantic_${traceRunID}`,
            from: { type: "node", id: `tool_exec_result_${traceRunID}` },
            to: { type: "node", id: semanticNodeID },
            relation: "produced",
            label: `${id} result was projected into semantic provenance`,
          })
          recordLatestEdge(trace, {
            edge_id: `tool_exec_result_for_${traceRunID}`,
            from: { type: "node", id: `tool_exec_${traceRunID}` },
            to: { type: "node", id: `tool_exec_result_${traceRunID}` },
            relation: "returned_by",
            label: `${id} execution returned a result`,
          })
          closeLatestTrace(ctx.sessionID, trace, "completed")
          return finalResult
        }).pipe(
          Effect.tapError((error) =>
            Effect.sync(() =>
              recordLatestTrace(trace, {
                operation: "tool.error",
                component: semanticComponent(id),
                node_id: `tool_exec_error_${traceRunID}`,
                data: {
                  tool: id,
                  call_id: ctx.callID,
                  message: String(error),
                  intent: intent.purpose,
                  operation_kind: intent.operation,
                },
              }),
            ),
          ),
          Effect.orDie,
          Effect.withSpan("Tool.execute", { attributes: attrs }),
          Effect.ensuring(Effect.sync(() => closeLatestTrace(ctx.sessionID, trace, "failed"))),
        )
      }
      return toolInfo
    })
}

export function define<
  Parameters extends Schema.Decoder<unknown>,
  Result extends Metadata,
  R,
  ID extends string = string,
>(
  id: ID,
  init: Effect.Effect<Init<Parameters, Result>, never, R>,
): Effect.Effect<Info<Parameters, Result>, never, R | Truncate.Service | Agent.Service> & { id: ID } {
  return Object.assign(
    Effect.gen(function* () {
      const resolved = yield* init
      const truncate = yield* Truncate.Service
      const agents = yield* Agent.Service
      return { id, init: wrap(id, resolved, truncate, agents) }
    }),
    { id },
  )
}

export function init<P extends Schema.Decoder<unknown>, M extends Metadata>(
  info: Info<P, M>,
): Effect.Effect<Def<P, M>> {
  return Effect.gen(function* () {
    const init = yield* info.init()
    return {
      ...init,
      id: info.id,
    }
  })
}

export * as Tool from "./tool"
