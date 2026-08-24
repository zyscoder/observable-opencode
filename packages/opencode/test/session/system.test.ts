import { describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import type { Agent } from "../../src/agent/agent"
import { NamedError } from "@opencode-ai/core/util/error"
import { Skill } from "../../src/skill"
import { Permission } from "../../src/permission"
import { SystemPrompt } from "../../src/session/system"
import { testEffect } from "../lib/effect"
import { buildSkillCatalogSnapshot } from "../../src/skill/catalog-observability"
import { CaseTrace } from "../../src/observability/case-trace"
import fs from "fs/promises"
import os from "os"
import path from "path"

const skills: Skill.Info[] = [
  {
    name: "zeta-skill",
    description: "Zeta skill.",
    location: "/tmp/zeta-skill/SKILL.md",
    content: "# zeta-skill",
  },
  {
    name: "alpha-skill",
    description: "Alpha skill.",
    location: "/tmp/alpha-skill/SKILL.md",
    content: "# alpha-skill",
  },
  {
    name: "middle-skill",
    description: "Middle skill.",
    location: "/tmp/middle-skill/SKILL.md",
    content: "# middle-skill",
  },
  {
    name: "manual-skill",
    location: "/tmp/manual-skill/SKILL.md",
    content: "# manual-skill",
  },
]

const build: Agent.Info = {
  name: "build",
  mode: "primary",
  permission: Permission.fromConfig({ "*": "allow" }),
  options: {},
}

const it = testEffect(
  SystemPrompt.layer.pipe(
    Layer.provide(
      Layer.succeed(
        Skill.Service,
        Skill.Service.of({
          get: (name) => Effect.succeed(skills.find((skill) => skill.name === name)),
          all: () => Effect.succeed(skills),
          dirs: () => Effect.succeed([]),
          available: () => Effect.succeed(skills),
          catalog: () =>
            Effect.succeed(
              buildSkillCatalogSnapshot({
                candidates: skills.map((skill) => ({
                  name: skill.name,
                  description: skill.description,
                  location: skill.location,
                  status: "loaded" as const,
                })),
                selectedLocations: Object.fromEntries(skills.map((skill) => [skill.name, skill.location])),
              }),
            ),
        }),
      ),
    ),
  ),
)

const itWithoutCatalog = testEffect(
  SystemPrompt.layer.pipe(
    Layer.provide(
      Layer.succeed(
        Skill.Service,
        Skill.Service.of({
          get: (name) => Effect.succeed(skills.find((skill) => skill.name === name)),
          all: () => Effect.succeed(skills),
          dirs: () => Effect.succeed([]),
          available: () => Effect.succeed(skills),
          catalog: () => Effect.die("observability catalog must not be read without trace context"),
        }),
      ),
    ),
  ),
)

describe("session.system", () => {
  itWithoutCatalog.effect("does not read the observability catalog when tracing is absent", () =>
    Effect.gen(function* () {
      const prompt = yield* SystemPrompt.Service
      const output = yield* prompt.skills(build)

      expect(output).toContain("<name>alpha-skill</name>")
    }),
  )

  it.effect("skills output is sorted by name and stable across calls", () =>
    Effect.gen(function* () {
      const prompt = yield* SystemPrompt.Service
      const first = yield* prompt.skills(build)
      const second = yield* prompt.skills(build)
      const output = first ?? (yield* Effect.fail(new NamedError.Unknown({ message: "missing skills output" })))

      expect(first).toBe(second)

      const alpha = output.indexOf("<name>alpha-skill</name>")
      const middle = output.indexOf("<name>middle-skill</name>")
      const zeta = output.indexOf("<name>zeta-skill</name>")

      expect(alpha).toBeGreaterThan(-1)
      expect(middle).toBeGreaterThan(alpha)
      expect(zeta).toBeGreaterThan(middle)
      expect(output).not.toContain("manual-skill")
    }),
  )

  it.effect("returns the passive Skill catalog trace ref to prompt assembly", () =>
    Effect.acquireUseRelease(
      Effect.promise(async () => {
        const dir = await fs.mkdtemp(path.join(os.tmpdir(), "opencode-skill-catalog-trace-"))
        const previous = {
          enabled: process.env.OPENCODE_CASE_TRACE,
          caseID: process.env.OPENCODE_CASE_ID,
          dir: process.env.OPENCODE_CASE_TRACE_DIR,
        }
        process.env.OPENCODE_CASE_TRACE = "1"
        process.env.OPENCODE_CASE_ID = "skill-catalog-trace-test"
        process.env.OPENCODE_CASE_TRACE_DIR = dir
        CaseTrace.configure({ input: { prompt: "feature migration" } })
        return { dir, previous }
      }),
      ({ dir }) =>
        Effect.gen(function* () {
          const prompt = yield* SystemPrompt.Service
          const trace: Parameters<typeof prompt.skills>[1] & { catalog_node_ref?: string } = {
            session_id: "ses_skill_catalog",
            message_id: "msg_skill_catalog",
            step: 1,
            provider_id: "test-provider",
            model_id: "test-model",
            skill_tool_available: true,
          }

          yield* prompt.skills(build, trace)

          expect(trace.catalog_node_ref).toStartWith("node:")
          CaseTrace.finishAll({ status: "success" })
          const materialized = yield* Effect.promise(() =>
            fs.readFile(path.join(dir, "skill-catalog-trace-test", "trace.json"), "utf8"),
          )
          const record = JSON.parse(materialized).records.find(
            (item: { event_type?: string }) => item.event_type === "skill.catalog.exposed",
          )
          expect(record).toBeDefined()
          expect(record.data.agent).toBe("build")
          expect(record.data.skill_tool_available).toBe(true)
        }),
      ({ dir, previous }) =>
        Effect.promise(async () => {
          CaseTrace.finishAll({ status: "success" })
          if (previous.enabled === undefined) delete process.env.OPENCODE_CASE_TRACE
          else process.env.OPENCODE_CASE_TRACE = previous.enabled
          if (previous.caseID === undefined) delete process.env.OPENCODE_CASE_ID
          else process.env.OPENCODE_CASE_ID = previous.caseID
          if (previous.dir === undefined) delete process.env.OPENCODE_CASE_TRACE_DIR
          else process.env.OPENCODE_CASE_TRACE_DIR = previous.dir
          await fs.rm(dir, { recursive: true, force: true })
        }),
    ),
  )
})
