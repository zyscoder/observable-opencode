import { Context, Effect, Layer } from "effect"

import { InstanceState } from "@/effect/instance-state"

import PROMPT_ANTHROPIC from "./prompt/anthropic.txt"
import PROMPT_DEFAULT from "./prompt/default.txt"
import PROMPT_BEAST from "./prompt/beast.txt"
import PROMPT_GEMINI from "./prompt/gemini.txt"
import PROMPT_GPT from "./prompt/gpt.txt"
import PROMPT_KIMI from "./prompt/kimi.txt"

import PROMPT_CODEX from "./prompt/codex.txt"
import PROMPT_TRINITY from "./prompt/trinity.txt"
import type { Provider } from "@/provider/provider"
import type { Agent } from "@/agent/agent"
import { Permission } from "@/permission"
import { Skill } from "@/skill"
import { buildSkillCatalogExposure } from "@/skill/catalog-observability"
import { CaseTrace } from "@/observability/case-trace"

export function provider(model: Provider.Model) {
  if (model.api.id.includes("gpt-4") || model.api.id.includes("o1") || model.api.id.includes("o3"))
    return [PROMPT_BEAST]
  if (model.api.id.includes("gpt")) {
    if (model.api.id.includes("codex")) {
      return [PROMPT_CODEX]
    }
    return [PROMPT_GPT]
  }
  if (model.api.id.includes("gemini-")) return [PROMPT_GEMINI]
  if (model.api.id.includes("claude")) return [PROMPT_ANTHROPIC]
  if (model.api.id.toLowerCase().includes("trinity")) return [PROMPT_TRINITY]
  if (model.api.id.toLowerCase().includes("kimi")) return [PROMPT_KIMI]
  return [PROMPT_DEFAULT]
}

export type SkillTraceContext = {
  session_id: string
  message_id: string
  step: number
  provider_id: string
  model_id: string
  skill_tool_available: boolean
  source_refs?: string[]
  catalog_node_ref?: string
}

export interface Interface {
  readonly environment: (model: Provider.Model) => Effect.Effect<string[]>
  readonly skills: (agent: Agent.Info, trace?: SkillTraceContext) => Effect.Effect<string | undefined>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SystemPrompt") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const skill = yield* Skill.Service

    return Service.of({
      environment: Effect.fn("SystemPrompt.environment")(function* (model: Provider.Model) {
        const ctx = yield* InstanceState.context
        return [
          [
            `You are powered by the model named ${model.api.id}. The exact model ID is ${model.providerID}/${model.api.id}`,
            `Here is some useful information about the environment you are running in:`,
            `<env>`,
            `  Working directory: ${ctx.directory}`,
            `  Workspace root folder: ${ctx.worktree}`,
            `  Is directory a git repo: ${ctx.project.vcs === "git" ? "yes" : "no"}`,
            `  Platform: ${process.platform}`,
            `  Today's date: ${new Date().toDateString()}`,
            `</env>`,
          ].join("\n"),
        ]
      }),

      skills: Effect.fn("SystemPrompt.skills")(function* (agent: Agent.Info, trace?: SkillTraceContext) {
        const disabled = Permission.disabled(["skill"], agent.permission).has("skill")
        if (disabled && !trace) return
        const list = disabled ? [] : yield* skill.available(agent)
        if (trace) {
          const catalog = yield* skill.catalog()
          const permissionActions = Object.fromEntries(
            catalog.selected.map((item) => [
              item.name,
              disabled ? "deny" : Permission.evaluate("skill", item.name, agent.permission).action,
            ]),
          )
          const exposure = buildSkillCatalogExposure({
            snapshot: catalog,
            permissionActions,
            exposedNames: list.map((item) => item.name),
            skillToolAvailable: trace.skill_tool_available && !disabled,
          })
          const catalogNode = CaseTrace.node({
            kind: "skill.catalog.exposed",
            component: "skill",
            title: "Skill catalog exposed to model",
            status: "success",
            source_refs: trace.source_refs,
            data: {
              ...trace,
              agent: agent.name,
              ...exposure,
            },
          })
          if (catalogNode) trace.catalog_node_ref = `node:${catalogNode.node_id}`
        }
        if (disabled) return

        return [
          "Skills provide specialized instructions and workflows for specific tasks.",
          "Use the skill tool to load a skill when a task matches its description.",
          // the agents seem to ingest the information about skills a bit better if we present a more verbose
          // version of them here and a less verbose version in tool description, rather than vice versa.
          Skill.fmt(list, { verbose: true }),
        ].join("\n")
      }),
    })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(Skill.defaultLayer))

export * as SystemPrompt from "./system"
