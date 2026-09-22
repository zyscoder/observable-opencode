import { expect, test } from "bun:test"
import {
  classifyToolIntent,
  inferVerificationStatus,
  isVerificationCommand,
  semanticComponent,
} from "@/observability/latest-trace-semantics"

test("classifies repository tools by the action they actually perform", () => {
  expect(classifyToolIntent("read", { filePath: "src/main.ts" })).toMatchObject({
    operation: "code_inspection",
    purpose: "read repository context",
  })
  expect(classifyToolIntent("edit", { filePath: "src/main.ts" })).toMatchObject({
    operation: "repository_change",
    purpose: "modify repository files",
  })
  expect(classifyToolIntent("bash", { command: "bun test" })).toMatchObject({
    operation: "verification",
    purpose: "run verification command",
  })
})

test("recognizes verification commands and reports their observed status", () => {
  expect(isVerificationCommand("bun test packages/core/test/observability/latest-trace.test.ts")).toBe(true)
  expect(inferVerificationStatus("bun test", 0, "12 pass", "")).toBe("passed")
  expect(inferVerificationStatus("bun test", 1, "", "1 fail")).toBe("failed")
})

test("keeps MCP and Skill components distinct from ordinary tools", () => {
  expect(semanticComponent("mcp:github:search")).toBe("mcp")
  expect(semanticComponent("skill")).toBe("skill")
  expect(semanticComponent("read")).toBe("tool")
})
