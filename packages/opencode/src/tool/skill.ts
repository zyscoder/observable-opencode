import path from "path"
import { pathToFileURL } from "url"
import { Effect, Schema } from "effect"
import * as Stream from "effect/Stream"
import { Ripgrep } from "../file/ripgrep"
import { Skill } from "../skill"
import * as Tool from "./tool"
import DESCRIPTION from "./skill.txt"
import { CaseTrace, type ActiveSpan } from "@/observability/case-trace"

export const Parameters = Schema.Struct({
  name: Schema.String.annotate({ description: "The name of the skill from available_skills" }),
})

export const SkillTool = Tool.define(
  "skill",
  Effect.gen(function* () {
    const skill = yield* Skill.Service
    const rg = yield* Ripgrep.Service

    return {
      description: DESCRIPTION,
      parameters: Parameters,
      execute: (params: Schema.Schema.Type<typeof Parameters>, ctx: Tool.Context) => {
        let traceSpan: ActiveSpan | undefined
        return Effect.gen(function* () {
          traceSpan = CaseTrace.get()?.startSpan({
            component: "skill",
            operation: "load",
            name: params.name,
            input: {
              name: params.name,
              sessionID: ctx.sessionID,
              messageID: ctx.messageID,
              callID: ctx.callID,
            },
          })
          const info = yield* skill.get(params.name)
          if (!info) {
            const all = yield* skill.all()
            const available = all.map((item) => item.name).join(", ")
            throw new Error(`Skill "${params.name}" not found. Available skills: ${available || "none"}`)
          }

          yield* ctx.ask({
            permission: "skill",
            patterns: [params.name],
            always: [params.name],
            metadata: {},
          })
          traceSpan?.event({
            event_type: "permission.accepted",
            data: {
              name: params.name,
            },
          })

          const dir = path.dirname(info.location)
          const base = pathToFileURL(dir).href
          const limit = 10
          const files = yield* rg.files({ cwd: dir, follow: false, hidden: true, signal: ctx.abort }).pipe(
            Stream.filter((file) => !file.includes("SKILL.md")),
            Stream.map((file) => path.resolve(dir, file)),
            Stream.take(limit),
            Stream.runCollect,
            Effect.map((chunk) => [...chunk].map((file) => `<file>${file}</file>`).join("\n")),
          )

          const result = {
            title: `Loaded skill: ${info.name}`,
            output: [
              `<skill_content name="${info.name}">`,
              `# Skill: ${info.name}`,
              "",
              info.content.trim(),
              "",
              `Base directory for this skill: ${base}`,
              "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.",
              "Note: file list is sampled.",
              "",
              "<skill_files>",
              files,
              "</skill_files>",
              "</skill_content>",
            ].join("\n"),
            metadata: {
              name: info.name,
              dir,
            },
          }
          traceSpan?.end({
            output: {
              name: info.name,
              dir,
              sampled_files: files,
              output: CaseTrace.summarizeText(result.output),
            },
          })
          return result
        }).pipe(
          Effect.tapError((error) =>
            Effect.sync(() =>
              traceSpan?.end({
                status: "error",
                error,
              }),
            ),
          ),
          Effect.orDie,
        )
      },
    }
  }),
)
