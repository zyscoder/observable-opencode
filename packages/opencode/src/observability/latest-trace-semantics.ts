export type ToolOperation =
  | "verification"
  | "environment_setup"
  | "repository_change"
  | "code_inspection"
  | "general_execution"

export type ToolIntent = {
  operation: ToolOperation
  purpose: string
  command?: string
}

function objectValue(input: unknown, key: string) {
  if (!input || typeof input !== "object") return undefined
  return (input as Record<string, unknown>)[key]
}

function stringValue(input: unknown, key: string) {
  const value = objectValue(input, key)
  return typeof value === "string" ? value : undefined
}

export function isVerificationCommand(command: string | undefined) {
  return Boolean(
    command &&
      /(?:^|[;&|]\s*)(?:python\d*(?:\.\d+)?\s+-m\s+pytest|uv\s+run\s+(?:[^\s;&|]+\/)?pytest|(?:[^\s;&|]*\/)?pytest|bun\s+(?:run\s+)?test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+(?:run\s+)?test|go\s+test|cargo\s+test|node\s+[^;&|]*test|(?:(?:npx|bunx|pnpm\s+exec|yarn\s+exec)\s+)?(?:[^\s;&|]*\/)?(?:jest|vitest|mocha)\b|xcodebuild\b|(?:make|cmake|bazel|gradle|mvn)\s+(?:check|test|verify|build)\b)/i.test(
        command,
      ),
  )
}

export function inferVerificationStatus(
  command: string,
  exitCode: number | undefined,
  stdout: unknown,
  stderr: unknown,
): "passed" | "failed" | "unknown" {
  if (exitCode !== undefined && exitCode !== 0) return "failed"
  if (exitCode === 0) return "passed"
  const output = `${typeof stdout === "string" ? stdout : ""}\n${typeof stderr === "string" ? stderr : ""}`
  if (/\b(?:failed|failure|error|errors|FAILURES|ERRORS)\b/i.test(output)) return "failed"
  if (/\b(?:passed|pass|ok|success|succeeded)\b/i.test(output)) return "passed"
  return command.includes("|") ? "unknown" : "unknown"
}

export function classifyShellOperation(command: string): ToolOperation {
  const text = command.trim()
  if (/(?:^|[;&|]\s*)(?:python\d*\s+-m\s+pip\s+install|pip\d*\s+install|uv\s+(?:sync|pip\s+install)|poetry\s+install|npm\s+(?:install|ci)|pnpm\s+install|yarn\s+install|bun\s+install|apt(?:-get)?\s+install|brew\s+install)\b/i.test(text))
    return "environment_setup"
  if (isVerificationCommand(text)) return "verification"
  if (/(?:\b(?:tee|cp|mv|rm|touch|mkdir|install)\b|\b(?:git\s+(?:apply|checkout|restore|reset|clean)|apply_patch|patch)\b|(?:^|[^<>])>{1,2}(?!&))/i.test(text))
    return "repository_change"
  if (/(?:^|[;&|]\s*)(?:git\s+(?:show|diff|status|log|grep|ls-files|rev-parse)|cat|rg|grep|find|fd|ls|pwd|head|tail|wc|stat|sed\s+-n|awk)\b/i.test(text))
    return "code_inspection"
  return "general_execution"
}

export function classifyToolIntent(id: string, args: unknown): ToolIntent {
  const command = stringValue(args, "command") ?? stringValue(args, "cmd")
  if (id === "read") return { operation: "code_inspection", purpose: "read repository context" }
  if (id === "grep" || id === "glob") return { operation: "code_inspection", purpose: "search repository context" }
  if (id === "edit" || id === "write" || id === "patch") {
    return { operation: "repository_change", purpose: "modify repository files" }
  }
  if (command) {
    const operation = classifyShellOperation(command)
    const purpose =
      operation === "verification"
        ? "run verification command"
        : operation === "environment_setup"
          ? "prepare execution environment"
          : operation === "repository_change"
            ? "modify repository through shell"
            : operation === "code_inspection"
              ? "inspect repository through shell"
              : "run shell command"
    return { operation, purpose, command }
  }
  return { operation: "general_execution", purpose: "execute tool" }
}

export function semanticComponent(id: string): "mcp" | "skill" | "task" | "tool" {
  if (id === "skill") return "skill"
  if (id === "task") return "task"
  if (id.startsWith("mcp:") || id.startsWith("mcp_") || id.startsWith("list_mcp_") || id.startsWith("read_mcp_"))
    return "mcp"
  return "tool"
}

export function filePathsFromToolInput(id: string, args: unknown, metadata?: unknown): string[] {
  const values = [
    stringValue(args, "filePath"),
    stringValue(args, "file_path"),
    stringValue(args, "path"),
    stringValue(args, "file"),
    stringValue(args, "workdir"),
    stringValue(metadata, "file"),
  ]
  return [...new Set(values.filter((value): value is string => Boolean(value)))]
}

export function semanticToolFields(id: string, args: unknown, metadata?: unknown) {
  const intent = classifyToolIntent(id, args)
  return {
    operation: intent.operation,
    purpose: intent.purpose,
    ...(intent.command ? { command: intent.command } : {}),
    file_paths: filePathsFromToolInput(id, args, metadata),
    read_purpose:
      intent.operation === "code_inspection"
        ? stringValue(args, "purpose") ?? stringValue(args, "reason") ?? null
        : undefined,
    read_reason:
      intent.operation === "code_inspection"
        ? stringValue(args, "reason") ?? stringValue(args, "purpose") ?? null
        : undefined,
  }
}
