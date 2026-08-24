import { describe, expect, test } from "bun:test"

import {
  buildSkillCatalogExposure,
  buildSkillCatalogSnapshot,
  classifySkillLocation,
  type SkillCatalogCandidate,
} from "../../src/skill/catalog-observability"

describe("skill catalog observability", () => {
  test("classifies compatible and native skill locations without changing them", () => {
    expect(classifySkillLocation("<built-in>")).toEqual({ family: "built_in", scope: "runtime" })
    expect(classifySkillLocation("/repo/.opencode/skills/migrate/SKILL.md")).toEqual({
      family: "opencode",
      scope: "project",
    })
    expect(classifySkillLocation("/home/dev/.config/opencode/skills/migrate/SKILL.md")).toEqual({
      family: "opencode",
      scope: "global",
    })
    expect(classifySkillLocation("/repo/.claude/skills/migrate/SKILL.md")).toEqual({
      family: "claude",
      scope: "project",
    })
    expect(classifySkillLocation("/home/dev/.agents/skills/migrate/SKILL.md")).toEqual({
      family: "agents",
      scope: "external",
    })
  })

  test("retains candidates, parse failures, conflicts, and the existing selected winner", () => {
    const candidates: SkillCatalogCandidate[] = [
      {
        name: "migration-check",
        description: "Use for feature migrations.",
        location: "/repo/.claude/skills/migration-check/SKILL.md",
        status: "loaded",
      },
      {
        name: "migration-check",
        description: "Validate migration consistency.",
        location: "/repo/.opencode/skills/migration-check/SKILL.md",
        status: "loaded",
      },
      {
        name: "",
        location: "/repo/.opencode/skills/broken/SKILL.md",
        status: "parse_failed",
        error: "frontmatter is invalid",
      },
    ]

    const snapshot = buildSkillCatalogSnapshot({
      candidates,
      selectedLocations: {
        "migration-check": "/repo/.claude/skills/migration-check/SKILL.md",
      },
    })

    expect(snapshot.candidates.map((item) => item.location)).toEqual([
      "/repo/.claude/skills/migration-check/SKILL.md",
      "/repo/.opencode/skills/broken/SKILL.md",
      "/repo/.opencode/skills/migration-check/SKILL.md",
    ])
    expect(snapshot.conflicts).toEqual([
      {
        name: "migration-check",
        candidate_locations: [
          "/repo/.claude/skills/migration-check/SKILL.md",
          "/repo/.opencode/skills/migration-check/SKILL.md",
        ],
        selected_location: "/repo/.claude/skills/migration-check/SKILL.md",
        resolution: "observed_runtime_winner",
      },
    ])
    expect(snapshot.selected).toEqual([
      {
        name: "migration-check",
        location: "/repo/.claude/skills/migration-check/SKILL.md",
      },
    ])
    expect(snapshot.parse_failures).toEqual([
      {
        location: "/repo/.opencode/skills/broken/SKILL.md",
        error: "frontmatter is invalid",
      },
    ])
  })

  test("projects permission decisions and the exact entries exposed to the model", () => {
    const snapshot = buildSkillCatalogSnapshot({
      candidates: [
        {
          name: "migration-check",
          description: "Use for feature migration consistency checks.",
          location: "/repo/.opencode/skills/migration-check/SKILL.md",
          status: "loaded",
        },
        {
          name: "private-check",
          description: "Internal only.",
          location: "/repo/.opencode/skills/private-check/SKILL.md",
          status: "loaded",
        },
      ],
      selectedLocations: {
        "migration-check": "/repo/.opencode/skills/migration-check/SKILL.md",
        "private-check": "/repo/.opencode/skills/private-check/SKILL.md",
      },
    })

    const exposure = buildSkillCatalogExposure({
      snapshot,
      permissionActions: { "migration-check": "allow", "private-check": "deny" },
      exposedNames: ["migration-check"],
      skillToolAvailable: true,
    })

    expect(exposure.permission_evaluations).toEqual([
      { name: "migration-check", action: "allow" },
      { name: "private-check", action: "deny" },
    ])
    expect(exposure.exposed).toEqual([
      {
        name: "migration-check",
        description: "Use for feature migration consistency checks.",
        location: "/repo/.opencode/skills/migration-check/SKILL.md",
      },
    ])
    expect(exposure.skill_tool_available).toBe(true)
  })
})
