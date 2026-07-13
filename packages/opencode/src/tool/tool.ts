import { Effect, Schema } from "effect"
import type { JSONSchema7 } from "@ai-sdk/provider"
import type { MessageV2 } from "../session/message-v2"
import type { Permission } from "../permission"
import type { SessionID, MessageID } from "../session/schema"
import * as Truncate from "./truncate"
import { Agent } from "@/agent/agent"
import { CaseTrace, type ActiveSpan } from "@/observability/case-trace"

interface Metadata {
  [key: string]: any
}

// TODO: remove this hack
export type DynamicDescription = (agent: Agent.Info) => Effect.Effect<string>

export type Context<M extends Metadata = Metadata> = {
  sessionID: SessionID
  messageID: MessageID
  agent: string
  abort: AbortSignal
  callID?: string
  extra?: { [key: string]: unknown }
  messages: MessageV2.WithParts[]
  metadata(input: { title?: string; metadata?: M }): Effect.Effect<void>
  ask(input: Omit<Permission.Request, "id" | "sessionID" | "tool">): Effect.Effect<void>
}

export interface ExecuteResult<M extends Metadata = Metadata> {
  title: string
  metadata: M
  output: string
  attachments?: Omit<MessageV2.FilePart, "id" | "sessionID" | "messageID">[]
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

function objectValue(input: unknown, key: string) {
  if (!input || typeof input !== "object") return undefined
  return (input as Record<string, unknown>)[key]
}

function stringValue(input: unknown, key: string) {
  const value = objectValue(input, key)
  return typeof value === "string" ? value : undefined
}

function numberValue(input: unknown, key: string) {
  const value = objectValue(input, key)
  return typeof value === "number" ? value : undefined
}

function isVerificationCommand(command: string | undefined) {
  return Boolean(
    command &&
      /(?:^|[;&|]\s*)(?:python\d*(?:\.\d+)?\s+-m\s+pytest|uv\s+run\s+(?:[^\s;&|]\/)?pytest|(?:[^\s;&|]*\/)?pytest|bun\s+test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+(?:run\s+)?test|go\s+test|cargo\s+test|node\s+[^;&|]*test|(?:[^\s;&|]*\/)?(?:jest|vitest|mocha)|xcodebuild\b)/i.test(
        command,
      ),
  )
}

export type VerificationStatus = "passed" | "failed" | "unknown"

export function inferVerificationStatus(
  command: string,
  exitCode: number | undefined,
  stdout: unknown,
  stderr: unknown,
): VerificationStatus {
  if (exitCode !== undefined && exitCode !== 0) return "failed"
  if (!command.includes("|")) return exitCode === 0 ? "passed" : "unknown"

  const output = `${typeof stdout === "string" ? stdout : ""}\n${typeof stderr === "string" ? stderr : ""}`
  if (
    /(?:^|[\s,])(?:[1-9]\d*)\s+(?:failed|errors?)\b|(?:^|\n)(?:FAILURES|ERRORS)(?:\n|$)|\btest result:\s*FAILED\b/i.test(
      output,
    )
  ) {
    return "failed"
  }
  if (/(?:^|[\s,])(?:[1-9]\d*)\s+passed\b|\btest result:\s*ok\b/i.test(output)) return "passed"
  return "unknown"
}

export type ShellOperationKind =
  | "verification"
  | "environment_setup"
  | "repository_change"
  | "code_inspection"
  | "general_execution"

export function classifyShellOperation(command: string): ShellOperationKind {
  const text = command.trim()
  if (
    /(?:^|[;&|]\s*)(?:python\d*(?:\.\d+)?\s+-m\s+pip\s+install|pip\d*\s+install|uv\s+(?:sync|pip\s+install)|poetry\s+install|npm\s+(?:install|ci)|pnpm\s+install|yarn\s+install|bun\s+install|apt(?:-get)?\s+install|brew\s+install)\b/i.test(
      text,
    )
  )
    return "environment_setup"
  if (isVerificationCommand(text)) return "verification"
  if (
    /(?:open\s*\([^\n]*,[^\n]*["'][wax+][^"']*["']|\.write_(?:text|bytes)\s*\(|\bsed\s+-[^\n]*i\b|\bperl\s+-[^\n]*i\b|\b(?:tee|cp|mv|rm|touch|mkdir|install)\b|\b(?:git\s+(?:apply|checkout|restore|reset|clean)|apply_patch|patch)\b|(?:^|[^<>])>{1,2}(?!&))/i.test(
      text,
    )
  )
    return "repository_change"
  if (
    /(?:^|[;&|]\s*)(?:git\s+(?:show|diff|status|log|grep|ls-files|rev-parse)|cat|rg|grep|find|fd|ls|pwd|head|tail|wc|stat|sed\s+-n|awk)\b/i.test(
      text,
    ) ||
    /open\s*\([^\n]*(?:["']r[bt]?["']|\)\.read)/i.test(text)
  )
    return "code_inspection"
  return "general_execution"
}

function toolIntent(id: string, args: unknown) {
  const command = stringValue(args, "command")
  if (id === "read") return "read repository context"
  if (id === "grep" || id === "glob") return "search repository context"
  if (id === "edit" || id === "write") return "modify repository files"
  if (command) {
    const operation = classifyShellOperation(command)
    if (operation === "verification") return "run verification command"
    if (operation === "environment_setup") return "prepare execution environment"
    if (operation === "repository_change") return "modify repository through shell"
    if (operation === "code_inspection") return "inspect repository through shell"
    return "run shell command"
  }
  return "execute tool"
}

function semanticToolResult(input: {
  id: string
  args: unknown
  ctx: Context
  spanID?: string
  result: ExecuteResult
}) {
  const command = stringValue(input.args, "command")
  const diff = stringValue(input.result.metadata, "diff")
  const filediff = objectValue(input.result.metadata, "filediff")
  const editFile =
    typeof filediff === "object" && filediff ? stringValue(filediff, "file") : stringValue(input.args, "filePath")

  if (command) {
    const operationKind = classifyShellOperation(command)
    const exitCode = numberValue(input.result.metadata, "exit")
    const stdout = objectValue(input.result.metadata, "output") ?? input.result.output
    const stderr = objectValue(input.result.metadata, "stderr")
    const verificationStatus = inferVerificationStatus(command, exitCode, stdout, stderr)
    const verification =
      operationKind === "verification"
        ? CaseTrace.verification({
            span_id: input.spanID,
            tool_call_id: input.ctx.callID,
            command,
            cwd: stringValue(input.args, "workdir"),
            purpose: stringValue(input.args, "description") ?? input.result.title,
            stage: "unknown",
            exit_code: exitCode,
            status: verificationStatus,
            stdout,
            stderr,
            metadata: {
              tool: input.id,
              operation_kind: operationKind,
              status_inference: command.includes("|") ? "pipeline_output_summary" : "process_exit_code",
              truncated: input.result.metadata.truncated,
            },
          })
        : undefined
    if (verification && input.spanID) {
      CaseTrace.edge({
        from: { type: "span", id: input.spanID, label: input.id },
        to: { type: "verification", id: verification.verification_id },
        relation: "tool_to_observation",
        label: "Tool output recorded as verification evidence",
      })
    }
    CaseTrace.observation({
      source: input.id,
      category: operationKind === "verification" ? "verification_output" : operationKind,
      summary: input.result.title,
      data: {
        operation_kind: operationKind,
        command,
        cwd: stringValue(input.args, "workdir"),
        exit_code: exitCode,
        output: stdout,
        stderr,
        metadata: input.result.metadata,
      },
      span_id: input.spanID,
      source_refs: verification
        ? [`verification:${verification.verification_id}`]
        : input.spanID
          ? [`span:${input.spanID}`]
          : undefined,
    })
  }

  if (input.id === "edit" || diff || filediff) {
    const files = [editFile].filter((item): item is string => Boolean(item))
    const change = CaseTrace.change({
      span_id: input.spanID,
      tool_call_id: input.ctx.callID,
      files,
      intent: `Apply ${input.id} tool result`,
      diff: diff ?? (typeof filediff === "object" && filediff ? stringValue(filediff, "patch") : undefined),
      metadata: {
        tool: input.id,
        title: input.result.title,
        additions: typeof filediff === "object" && filediff ? numberValue(filediff, "additions") : undefined,
        deletions: typeof filediff === "object" && filediff ? numberValue(filediff, "deletions") : undefined,
      },
    })
    if (change && input.spanID) {
      CaseTrace.edge({
        from: { type: "span", id: input.spanID, label: input.id },
        to: { type: "change", id: change.change_id },
        relation: "tool_to_change",
        label: "Tool result changed repository files",
      })
    }
    CaseTrace.observation({
      source: input.id,
      category: "repository_change",
      summary: input.result.title,
      data: {
        files,
        diff: diff ?? (typeof filediff === "object" && filediff ? stringValue(filediff, "patch") : undefined),
        metadata: input.result.metadata,
      },
      span_id: input.spanID,
      source_refs: change ? [`change:${change.change_id}`] : input.spanID ? [`span:${input.spanID}`] : undefined,
    })
  } else if (!command) {
    CaseTrace.observation({
      source: input.id,
      category: "tool_output",
      summary: input.result.title,
      data: {
        args: input.args,
        output: input.result.output,
        metadata: input.result.metadata,
      },
      span_id: input.spanID,
      source_refs: input.spanID ? [`span:${input.spanID}`] : undefined,
    })
  }
}

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
        let traceSpan: ActiveSpan | undefined
        return Effect.gen(function* () {
          traceSpan = CaseTrace.get()?.startSpan({
            component: "tool",
            operation: "execute",
            name: id,
            input: {
              args,
              sessionID: ctx.sessionID,
              messageID: ctx.messageID,
              agent: ctx.agent,
              callID: ctx.callID,
            },
            metadata: attrs,
          })
          const decision = CaseTrace.decision({
            span_id: traceSpan?.id,
            component: "tool",
            decision_type: "tool_execute",
            intent: toolIntent(id, args),
            chosen_action: id,
            rationale: stringValue(args, "description") ?? stringValue(args, "command") ?? id,
            metadata: {
              callID: ctx.callID,
              sessionID: ctx.sessionID,
              messageID: ctx.messageID,
              tool_description: toolInfo.description,
              args,
            },
          })
          if (decision && traceSpan) {
            CaseTrace.edge({
              from: { type: "decision", id: decision.decision_id },
              to: { type: "span", id: traceSpan.id, label: id },
              relation: "decision_to_tool",
              label: "Model selected tool execution",
            })
          }
          const decoded = yield* decode(args).pipe(
            Effect.mapError((error) =>
              toolInfo.formatValidationError
                ? new Error(toolInfo.formatValidationError(error), { cause: error })
                : new Error(
                    `The ${id} tool was called with invalid arguments: ${error}.\nPlease rewrite the input so it satisfies the expected schema.`,
                    { cause: error },
                  ),
            ),
          )
          const result = yield* execute(decoded as Schema.Schema.Type<Parameters>, ctx)
          if (result.metadata.truncated !== undefined) {
            semanticToolResult({ id, args, ctx, spanID: traceSpan?.id, result })
            traceSpan?.end({
              output: {
                title: result.title,
                metadata: result.metadata,
                output: CaseTrace.summarizeText(result.output),
                attachments: result.attachments?.length ?? 0,
              },
            })
            return result
          }
          const agent = yield* agents.get(ctx.agent)
          const truncated = yield* truncate.output(result.output, {}, agent)
          const finalResult = {
            ...result,
            output: truncated.content,
            metadata: {
              ...result.metadata,
              truncated: truncated.truncated,
              ...(truncated.truncated && { outputPath: truncated.outputPath }),
            },
          }
          semanticToolResult({ id, args, ctx, spanID: traceSpan?.id, result: finalResult })
          traceSpan?.end({
            output: {
              title: finalResult.title,
              metadata: finalResult.metadata,
              output: CaseTrace.summarizeText(finalResult.output),
              attachments: finalResult.attachments?.length ?? 0,
            },
          })
          return finalResult
        }).pipe(
          Effect.tapError((error) =>
            Effect.sync(() => {
              traceSpan?.end({
                status: "error",
                error,
              })
              CaseTrace.get()?.event({
                component: "tool",
                event_type: "execute.error",
                data: {
                  tool: id,
                  sessionID: ctx.sessionID,
                  messageID: ctx.messageID,
                  callID: ctx.callID,
                  error,
                },
              })
            }),
          ),
          Effect.orDie,
          Effect.withSpan("Tool.execute", { attributes: attrs }),
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
