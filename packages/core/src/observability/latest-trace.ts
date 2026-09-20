import { randomUUID } from "node:crypto"
import type { LLMEvent } from "@opencode-ai/llm"
import type { CausalEdgeLike } from "./causal-ir"
import { TraceRuntime, type TraceHandle } from "./trace-runtime"

const truthy = new Set(["1", "true", "yes", "on"])

function enabled() {
  const value = process.env.OPENCODE_CASE_TRACE
  return value !== undefined && truthy.has(value.toLowerCase())
}

function preview(value: unknown, limit = 512): Record<string, unknown> {
  if (typeof value === "string") {
    return {
      type: "string",
      length: value.length,
      preview: value.length > limit ? `${value.slice(0, limit)}...[truncated]` : value,
    }
  }
  if (value === null || typeof value !== "object") return { value }
  if (Array.isArray(value)) return { type: "array", length: value.length }
  return { type: "object", keys: Object.keys(value).slice(0, 64) }
}

function toolComponent(name: string) {
  if (name === "skill") return "skill" as const
  if (name === "task" || name === "subagent" || name === "subtask") return "task" as const
  if (name.startsWith("mcp_") || name.startsWith("mcp.") || name.includes("/mcp/")) return "mcp" as const
  if (name.startsWith("plugin_") || name.startsWith("plugin.")) return "plugin" as const
  return "tool" as const
}

export function openLatestTrace(input: { sessionID: string; step: number; agent?: string; model?: string }) {
  if (!enabled()) return undefined
  const caseID = process.env.OPENCODE_CASE_ID || `session-${input.sessionID}`
  const runID = `${input.sessionID}-${Date.now()}-${input.step}-${randomUUID().slice(0, 8)}`
  try {
    const trace = TraceRuntime.open({
      caseID,
      sessionID: input.sessionID,
      runID,
    })
    trace.record({
      operation: "task.loop",
      component: "task",
      node_id: `task_loop_${trace.runID}`,
      data: {
        session_id: input.sessionID,
        step: input.step,
        ...(input.agent ? { agent: input.agent } : {}),
        ...(input.model ? { model: input.model } : {}),
      },
    })
    return trace
  } catch {
    return undefined
  }
}

export function recordPromptTrace(input: {
  sessionID: string
  messageID: string
  delivery: string
  prompt: unknown
}) {
  const trace = openLatestTrace({ sessionID: input.sessionID, step: 0 })
  if (!trace) return
  const artifact = trace.recordArtifact({
    value: input.prompt,
    mediaType: "application/json",
    previewLimit: 4_000,
  })
  recordLatestTrace(trace, {
    operation: "prompt.assembly",
    component: "prompt",
    data: {
      session_id: input.sessionID,
      message_id: input.messageID,
      delivery: input.delivery,
      prompt_shape:
        input.prompt && typeof input.prompt === "object" && !Array.isArray(input.prompt)
          ? Object.keys(input.prompt).slice(0, 64)
          : { type: typeof input.prompt },
      ...(artifact ? { prompt_artifact_id: artifact.artifact_id } : {}),
    },
  })
  trace.close("completed")
}

export function recordLatestTrace(trace: TraceHandle | undefined, input: Parameters<TraceHandle["record"]>[0]) {
  try {
    trace?.record(input)
  } catch {
    // Trace collection is a passive sidecar and must never affect the agent.
  }
}

export function recordLatestEdge(trace: TraceHandle | undefined, edge: CausalEdgeLike) {
  try {
    trace?.edge(edge)
  } catch {
    // Trace collection is a passive sidecar and must never affect the agent.
  }
}

export function recordLLMEvent(trace: TraceHandle | undefined, event: LLMEvent) {
  if (!trace) return
  switch (event.type) {
    case "text-start":
      recordLatestTrace(trace, { operation: "response.output", component: "result", data: { block_id: event.id } })
      return
    case "text-end":
      recordLatestTrace(trace, { operation: "response.output", component: "result", data: { block_id: event.id, phase: "end" } })
      return
    case "reasoning-end":
      recordLatestTrace(trace, {
        operation: "decision",
        component: "processor",
        data: { block_id: event.id, phase: "reasoning_end" },
      })
      return
    case "tool-call":
      {
        const component = toolComponent(event.name)
        const artifact = trace.recordArtifact({
          value: event.input,
          mediaType: "application/json",
          previewLimit: 4_000,
        })
      recordLatestTrace(trace, {
        operation: "tool.call",
        component,
        node_id: `tool_call_${event.id}`,
        data: {
          call_id: event.id,
          tool: event.name,
          input: preview(event.input),
          provider_executed: event.providerExecuted === true,
          selection_phase: "model_action",
          ...(artifact ? { input_artifact_id: artifact.artifact_id } : {}),
        },
      })
      recordLatestEdge(trace, {
        edge_id: `llm_to_tool_${event.id}`,
        from: { type: "node", id: `llm_call_${trace.runID}` },
        to: { type: "node", id: `tool_call_${event.id}` },
        relation: "called",
        label: `model selected ${event.name}`,
      })
      }
      return
    case "tool-result":
      {
        const component = toolComponent(event.name)
        const artifact = trace.recordArtifact({
          value: event.result,
          mediaType: "application/json",
          previewLimit: 4_000,
        })
      recordLatestTrace(trace, {
        operation: "tool.result",
        component,
        node_id: `tool_result_${event.id}`,
        data: {
          call_id: event.id,
          tool: event.name,
          result: preview(event.result),
          provider_executed: event.providerExecuted === true,
          ...(artifact ? { result_artifact_id: artifact.artifact_id } : {}),
        },
      })
      recordLatestEdge(trace, {
        edge_id: `tool_result_for_${event.id}`,
        from: { type: "node", id: `tool_call_${event.id}` },
        to: { type: "node", id: `tool_result_${event.id}` },
        relation: "returned_by",
        label: `${event.name} returned a result`,
      })
      }
      return
    case "tool-error":
      {
        const component = toolComponent(event.name)
      recordLatestTrace(trace, {
        operation: "tool.error",
        component,
        node_id: `tool_error_${event.id}`,
        data: { call_id: event.id, tool: event.name, message: event.message },
      })
      }
      return
    case "step-finish":
    case "finish":
      recordLatestTrace(trace, {
        operation: "llm.turn",
        component: "llm",
        data: {
          phase: "finish",
          reason: event.reason,
          usage: event.usage,
        },
      })
      return
    case "provider-error":
      recordLatestTrace(trace, {
        operation: "llm.call",
        component: "llm",
        data: {
          phase: "provider_error",
          message: event.message,
          classification: event.classification,
          retryable: event.retryable,
        },
      })
      return
    default:
      return
  }
}
