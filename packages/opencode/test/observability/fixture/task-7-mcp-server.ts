import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js"
import { z } from "zod"

const server = new McpServer({ name: "task-7-local", version: "1.0.0" })
server.registerTool(
  "echo",
  {
    description: "Return a deterministic local fact",
    inputSchema: { value: z.string() },
  },
  async ({ value }) => ({
    content: [{ type: "text", text: `local-mcp:${value}` }],
  }),
)

await server.connect(new StdioServerTransport())
