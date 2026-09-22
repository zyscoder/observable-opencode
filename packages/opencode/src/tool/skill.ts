import path from "path"
import { Effect, Schema } from "effect"
import { Ripgrep } from "@opencode-ai/core/ripgrep"
import { Skill } from "../skill"
import * as Tool from "./tool"
import DESCRIPTION from "./skill.txt"
import {
  activeLatestTrace,
  recordLatestEdge,
  recordLatestTrace,
} from "@opencode-ai/core/observability/latest-trace"

export const Parameters = Schema.Struct({
  name: Schema.String.annotate({ description: "The name of the skill from available_skills" }),
})

export const SkillTool = Tool.define(
  "skill",
  Effect.gen(function* () {
    const skill = yield* Skill.Service
    const ripgrep = yield* Ripgrep.Service

    return {
      description: DESCRIPTION,
      parameters: Parameters,
      execute: (params: Schema.Schema.Type<typeof Parameters>, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const info = yield* skill
            .require(params.name)
            .pipe(Effect.catchTag("Skill.NotFoundError", (error) => Effect.die(new Error(error.message))))

          yield* ctx.ask({
            permission: "skill",
            patterns: [params.name],
            always: [params.name],
            metadata: {},
          })

          const dir = path.dirname(info.location)
          const base = dir
          const files = yield* ripgrep.find({
            cwd: dir,
            pattern: "!**/SKILL.md",
            hidden: true,
            follow: false,
            signal: ctx.abort,
            limit: 10,
          })
          const trace = activeLatestTrace(ctx.sessionID)
          const traceRunID = trace?.runID ?? ctx.sessionID
          const skillArtifact = trace?.recordArtifact({
            value: {
              name: info.name,
              location: info.location,
              base_directory: base,
              content: info.content,
              sampled_files: files.map((file) => path.resolve(dir, file.path)),
            },
            mediaType: "application/json",
            previewLimit: 6_000,
          })
          recordLatestTrace(trace, {
            operation: "skill.load",
            component: "skill",
            node_id: `skill_load_${traceRunID}`,
            data: {
              skill_name: info.name,
              location: info.location,
              base_directory: base,
              content_characters: info.content.length,
              sampled_file_count: files.length,
              selected_by_call_id: ctx.callID,
              skill_artifact_id: skillArtifact?.artifact_id,
            },
          })
          recordLatestEdge(trace, {
            edge_id: `tool_to_skill_load_${traceRunID}`,
            from: { type: "node", id: `tool_exec_${traceRunID}` },
            to: { type: "node", id: `skill_load_${traceRunID}` },
            relation: "produced",
            label: `skill tool loaded ${info.name} into the agent context`,
          })

          return {
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
              files.map((file) => `<file>${path.resolve(dir, file.path)}</file>`).join("\n"),
              "</skill_files>",
              "</skill_content>",
            ].join("\n"),
            metadata: {
              name: info.name,
              dir,
            },
          }
        }).pipe(Effect.orDie),
    }
  }),
)
