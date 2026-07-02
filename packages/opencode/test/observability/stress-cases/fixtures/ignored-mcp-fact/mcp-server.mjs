import readline from "node:readline"

const facts = {
  "discount-policy": {
    subject: "renewalQuote",
    predicate: "discount_cap",
    value: "15%",
    path: "docs/current-requirement.md",
    line_start: 3,
    line_end: 3,
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
      serverInfo: { name: "syntheticFacts", version: "1.0.0" },
    })
    return
  }
  if (msg.method === "notifications/initialized") return
  if (msg.method === "tools/list") {
    send(msg.id, {
      tools: [
        {
          name: "repo_fact",
          description: "Return structured facts about the repository",
          inputSchema: {
            type: "object",
            properties: { topic: { type: "string", enum: Object.keys(facts) } },
            required: ["topic"],
          },
        },
      ],
    })
    return
  }
  if (msg.method === "tools/call") {
    const topic = msg.params?.arguments?.topic
    const fact = facts[topic]
    if (!fact) return error(msg.id, -32602, `unknown topic: ${topic}`)
    send(msg.id, { content: [{ type: "text", text: JSON.stringify(fact) }], isError: false })
    return
  }
  error(msg.id, -32601, `unknown method: ${msg.method}`)
})
