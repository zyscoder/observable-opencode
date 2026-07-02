import { describe, expect, test } from "bun:test"
import { shouldRunStartupJsonMigration } from "@/cli/startup-migration"

describe("startup json migration gate", () => {
  test("skips cold-start migration when no sqlite marker and no legacy storage exist", () => {
    expect(shouldRunStartupJsonMigration({ markerExists: false, legacyStorageExists: false })).toBe(false)
  })

  test("runs migration only for unmigrated legacy storage", () => {
    expect(shouldRunStartupJsonMigration({ markerExists: false, legacyStorageExists: true })).toBe(true)
    expect(shouldRunStartupJsonMigration({ markerExists: true, legacyStorageExists: true })).toBe(false)
  })
})
