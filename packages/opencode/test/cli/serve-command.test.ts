import { describe, expect, test } from "bun:test"
import fs from "fs/promises"
import path from "path"

describe("serve command startup path", () => {
  test("does not bootstrap the full AppRuntime before listening", async () => {
    const servePath = path.resolve(import.meta.dir, "../../src/cli/cmd/serve.ts")
    const source = await fs.readFile(servePath, "utf8")

    expect(source).not.toContain("effectCmd")
    expect(source).not.toContain("AppRuntime")
    expect(source).toContain("resolveNetworkOptionsAsync")
  })
})
