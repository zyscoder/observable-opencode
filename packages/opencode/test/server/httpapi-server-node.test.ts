import { describe, expect, test } from "bun:test"
import type { Server } from "node:http"

describe("node HTTP API server", () => {
  test("does not terminate long-running agent requests after Node's default five-minute timeout", async () => {
    const module = (await import("../../src/server/httpapi-server.node")) as unknown as {
      createHttpServer?: () => Server
    }

    expect(module.createHttpServer).toBeFunction()
    const server = module.createHttpServer!()
    try {
      expect(server.requestTimeout).toBe(0)
    } finally {
      server.close()
    }
  })
})
