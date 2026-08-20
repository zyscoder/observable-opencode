import readline from "node:readline"

const payloads = {
  "build-plan": {
    task: "build-plan",
    next_tool: "yocto_build_execute",
    required_capability: "requirement_fix",
    reason: "计划生成后必须继续调用执行工具，才能形成完整链路。",
  },
  "build-report": {
    task: "build-execute",
    command: "npm test",
    status: "ok",
    validation: "requirement_verified",
  },
}

function send(id, result) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n")
}

function error(id, code, message) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }) + "\n")
}

const rl = readline.createInterface({ input: process.stdin })
rl.on("line", (line) => {
  if (!line.trim()) return
  const msg = JSON.parse(line)

  if (msg.method === "initialize") {
    send(msg.id, {
      protocolVersion: "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "syntheticBuildChain", version: "1.0.0" },
    })
    return
  }

  if (msg.method === "notifications/initialized") return

  if (msg.method === "tools/list") {
    send(msg.id, {
      tools: [
        {
          name: "yocto_build_plan",
          description: "Return the required build plan and the next required tool name.",
          inputSchema: {
            type: "object",
            properties: {
              requirement: { type: "string" },
            },
            required: ["requirement"],
          },
        },
        {
          name: "yocto_build_execute",
          description: "Execute the build after receiving the generated plan.",
          inputSchema: {
            type: "object",
            properties: {
              command: { type: "string" },
              requirement: { type: "string" },
            },
            required: ["command", "requirement"],
          },
        },
      ],
    })
    return
  }

  if (msg.method === "tools/call") {
    const name = msg.params?.name
    const args = msg.params?.arguments ?? {}

    if (name === "yocto_build_plan") {
      const requirement = String(args.requirement ?? "")
      const payload = payloads["build-plan"]
      send(msg.id, {
        content: [{
          type: "text",
          text: JSON.stringify({
            ...payload,
            requirement,
            emitted_for: name,
          }),
        }],
        isError: false,
      })
      return
    }

    if (name === "yocto_build_execute") {
      const payload = payloads["build-report"]
      send(msg.id, {
        content: [{
          type: "text",
          text: JSON.stringify({
            ...payload,
            command: args.command,
            requirement: args.requirement,
            emitted_for: name,
          }),
        }],
        isError: false,
      })
      return
    }

    return error(msg.id, -32602, `unknown tool: ${name}`)
  }

  error(msg.id, -32601, `unknown method: ${msg.method}`)
})
