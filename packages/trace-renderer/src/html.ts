import { createHash } from "node:crypto"
import fs from "fs"
import path from "path"
import {
  VIEWER_STRING_BUDGET,
  boundedViewerJoin,
  boundedViewerText,
  escapeViewerHtml,
  provenanceTraceHtmlChunks,
  safeArtifactRelativePath,
} from "./viewer"
import type {
  ProvenanceTraceView,
  TraceArtifact,
  TraceComponent,
  TraceFieldSummary,
  TraceSummary,
} from "opencode/observability/case-trace"

type RenderCaseTraceHtmlOptions = {
  artifactDir?: string
  artifactContents?: Map<string, string> | Record<string, string>
}

export type WriteProvenanceTraceHtmlOptions = {
  artifactSourceRoot?: string
  forceStreaming?: boolean
  maxChunkBytes?: number
  streamingThreshold?: number
}

export type TraceHtmlWriteStats = {
  mode: "inline" | "streaming"
  record_count: number
  chunk_count: number
  max_chunk_bytes: number
  max_generator_chunk_bytes: number
  max_temporary_buffer_bytes: number
  artifact_payloads_embedded: number
}

const escapeHtml = escapeViewerHtml

function jsonScript(input: unknown) {
  return JSON.stringify(boundedViewerText(input, VIEWER_STRING_BUDGET))
    .replaceAll("<", "\\u003c")
    .replaceAll(">", "\\u003e")
    .replaceAll("&", "\\u0026")
}

function formatMs(input: number | undefined) {
  if (!Number.isFinite(input)) return "0 ms"
  const value = Math.max(0, input ?? 0)
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  return `${Math.round(value)} ms`
}

function tokenTotal(input: TraceSummary["token_usage"] | undefined) {
  if (!input) return 0
  if (input.total) return input.total
  return (
    (input.input ?? 0) +
    (input.output ?? 0) +
    (input.reasoning ?? 0) +
    (input.cached_input ?? 0) +
    (input.cache_write ?? 0)
  )
}

const componentOrder: TraceComponent[] = [
  "run",
  "runtime",
  "prompt",
  "context",
  "llm",
  "processor",
  "tool",
  "skill",
  "task",
  "mcp",
  "plugin",
  "evaluation",
  "result",
  "trace",
]

const componentLabel: Record<TraceComponent, string> = {
  run: "任务入口",
  runtime: "任务循环",
  prompt: "Prompt/上下文",
  context: "上下文管理",
  llm: "模型调用",
  processor: "结果处理",
  tool: "工具调用",
  skill: "Skill 执行",
  task: "子任务 Agent",
  mcp: "MCP 执行",
  plugin: "插件",
  evaluation: "外部评测",
  result: "最终结果",
  trace: "Trace",
}

function componentRank(component: string) {
  const index = componentOrder.indexOf(component as TraceComponent)
  return index === -1 ? componentOrder.length : index
}

function componentClass(component: string) {
  return boundedViewerText(component, 80).replace(/[^a-z0-9_-]/gi, "-")
}

function preview(input: unknown, limit = 180): string {
  if (input === undefined || input === null) return ""
  if (typeof input === "string") return boundedViewerText(input, limit)
  if (typeof input === "number" || typeof input === "boolean") return boundedViewerText(input, limit)
  const summary = input as Partial<TraceFieldSummary>
  if (typeof summary.preview === "string") return boundedViewerText(summary.preview, limit)
  if (summary.value !== undefined) return boundedViewerText(summary.value, limit)
  return boundedViewerText(input, limit)
}

function artifactID(input: unknown) {
  if (!input || typeof input !== "object") return undefined
  const summary = input as Partial<TraceFieldSummary>
  return typeof summary.artifact_id === "string" ? boundedViewerText(summary.artifact_id, 520) : undefined
}

function writeAtomicChunks(target: string, chunks: Iterable<string>, maxChunkBytes: number) {
  const temporary = path.join(
    path.dirname(target),
    `.${path.basename(target)}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`,
  )
  let chunkCount = 0
  let largestChunk = 0
  let largestGeneratorChunk = 0
  let largestTemporaryBuffer = 0
  let fd: number | undefined
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fd = fs.openSync(temporary, "w")
    for (const chunk of chunks) {
      const chunkBytes = Buffer.byteLength(chunk, "utf8")
      if (chunkBytes > maxChunkBytes) {
        throw new RangeError(`HTML generator chunk ${chunkBytes} exceeds ${maxChunkBytes} bytes`)
      }
      largestGeneratorChunk = Math.max(largestGeneratorChunk, chunkBytes)
      const bytes = Buffer.from(chunk, "utf8")
      largestTemporaryBuffer = Math.max(largestTemporaryBuffer, bytes.byteLength)
      fs.writeSync(fd, bytes)
      chunkCount += 1
      largestChunk = Math.max(largestChunk, bytes.byteLength)
    }
    fs.closeSync(fd)
    fd = undefined
    fs.renameSync(temporary, target)
    return { chunkCount, largestChunk, largestGeneratorChunk, largestTemporaryBuffer }
  } catch (error) {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd)
      } catch {}
    }
    try {
      fs.unlinkSync(temporary)
    } catch {}
    throw error
  }
}

type AuthorizedArtifactTarget = {
  fd: number
  dev: number
  ino: number
}

function sameArtifactIdentity(left: fs.Stats, right: Pick<AuthorizedArtifactTarget, "dev" | "ino">) {
  return left.dev === right.dev && left.ino === right.ino
}

function closeAuthorizedArtifactTargets(targets: ReadonlyMap<string, AuthorizedArtifactTarget>) {
  for (const target of targets.values()) {
    try {
      fs.closeSync(target.fd)
    } catch {}
  }
}

function authorizedArtifactTargets(artifacts: readonly TraceArtifact[], caseRoot: string | undefined) {
  const allowed = new Map<string, AuthorizedArtifactTarget>()
  if (!caseRoot) return allowed
  try {
    const realCaseRoot = fs.realpathSync(caseRoot)
    const artifactRoot = fs.realpathSync(path.resolve(caseRoot, "artifacts"))
    const artifactRootRelative = path.relative(realCaseRoot, artifactRoot)
    if (!artifactRootRelative || artifactRootRelative.startsWith("..") || path.isAbsolute(artifactRootRelative)) return allowed
    for (const artifact of artifacts) {
      const relativePath = safeArtifactRelativePath(artifact.path)
      if (!relativePath || allowed.has(relativePath)) continue
      let fd: number | undefined
      try {
        const lexicalTarget = path.resolve(caseRoot, relativePath)
        fd = fs.openSync(lexicalTarget, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW)
        const descriptor = fs.fstatSync(fd)
        if (!descriptor.isFile()) continue
        const realTarget = fs.realpathSync(lexicalTarget)
        const relative = path.relative(artifactRoot, realTarget)
        if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) continue
        if (!sameArtifactIdentity(fs.statSync(realTarget), descriptor)) continue
        allowed.set(relativePath, {
          fd,
          dev: descriptor.dev,
          ino: descriptor.ino,
        })
        fd = undefined
      } catch {
      } finally {
        if (fd !== undefined) {
          try {
            fs.closeSync(fd)
          } catch {}
        }
      }
    }
  } catch {}
  return allowed
}

function fsyncDirectory(directory: string) {
  let fd: number | undefined
  try {
    fd = fs.openSync(directory, fs.constants.O_RDONLY)
    fs.fsyncSync(fd)
  } finally {
    if (fd !== undefined) fs.closeSync(fd)
  }
}

function descriptorDigest(fd: number) {
  const digest = createHash("sha256")
  const buffer = Buffer.allocUnsafe(64 * 1024)
  let position = 0
  while (true) {
    const count = fs.readSync(fd, buffer, 0, buffer.length, position)
    if (!count) break
    digest.update(buffer.subarray(0, count))
    position += count
  }
  return { digest: digest.digest("hex"), length: position }
}

function validatePublishedSnapshot(target: string, expectedDigest: string, expectedLength: number) {
  let fd: number | undefined
  try {
    fd = fs.openSync(target, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW)
    const stats = fs.fstatSync(fd)
    if (!stats.isFile() || stats.size !== expectedLength) return false
    return descriptorDigest(fd).digest === expectedDigest
  } catch {
    return false
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd)
      } catch {}
    }
  }
}

function cleanupSnapshotDirectories(outputRoot: string) {
  const snapshotRoot = path.resolve(outputRoot, "artifacts", "render-snapshots", "sha256")
  try {
    fs.rmdirSync(snapshotRoot)
  } catch {}
  try {
    fs.rmdirSync(path.dirname(snapshotRoot))
  } catch {}
}

function ensureRendererDirectory(directory: string) {
  try {
    const stats = fs.lstatSync(directory)
    if (stats.isSymbolicLink() || !stats.isDirectory()) throw new Error(`${directory}: renderer snapshot path is not a directory`)
    return
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
  }
  fs.mkdirSync(directory)
  const stats = fs.lstatSync(directory)
  if (stats.isSymbolicLink() || !stats.isDirectory()) throw new Error(`${directory}: renderer snapshot path is not a directory`)
}

function prepareSnapshotRoot(outputRoot: string) {
  fs.mkdirSync(outputRoot, { recursive: true })
  const root = path.resolve(outputRoot)
  ensureRendererDirectory(root)
  const snapshotRoot = ["artifacts", "render-snapshots", "sha256"].reduce((directory, segment) => {
    const next = path.join(directory, segment)
    ensureRendererDirectory(next)
    return next
  }, root)
  const relative = path.relative(fs.realpathSync(root), fs.realpathSync(snapshotRoot))
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative))
    throw new Error(`${snapshotRoot}: renderer snapshot path escapes output directory`)
  return snapshotRoot
}

type PublishedArtifactSnapshot = {
  path: string
  created: boolean
}

function publishArtifactSnapshot(
  outputRoot: string,
  snapshotRoot: string,
  target: AuthorizedArtifactTarget,
): PublishedArtifactSnapshot | undefined {
  const temporary = path.join(snapshotRoot, `.snapshot.${process.pid}.${Math.random().toString(16).substring(2)}.tmp`)
  let temporaryFd: number | undefined
  let published: string | undefined
  let created = false
  try {
    temporaryFd = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600)
    const digest = createHash("sha256")
    const buffer = Buffer.allocUnsafe(64 * 1024)
    let sourcePosition = 0
    while (true) {
      const count = fs.readSync(target.fd, buffer, 0, buffer.length, sourcePosition)
      if (!count) break
      digest.update(buffer.subarray(0, count))
      let written = 0
      while (written < count) written += fs.writeSync(temporaryFd, buffer, written, count - written)
      sourcePosition += count
    }
    fs.fchmodSync(temporaryFd, 0o444)
    fs.fsyncSync(temporaryFd)
    fs.closeSync(temporaryFd)
    temporaryFd = undefined

    const contentDigest = digest.digest("hex")
    const relativeSnapshot = `artifacts/render-snapshots/sha256/${contentDigest}`
    published = path.join(snapshotRoot, contentDigest)
    try {
      fs.linkSync(temporary, published)
      created = true
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error
      if (!validatePublishedSnapshot(published, contentDigest, sourcePosition)) return undefined
    }
    fs.unlinkSync(temporary)
    fsyncDirectory(snapshotRoot)
    return { path: relativeSnapshot, created }
  } catch {
    if (created && published) {
      try {
        fs.unlinkSync(published)
      } catch {}
      cleanupSnapshotDirectories(outputRoot)
    }
    return undefined
  } finally {
    if (temporaryFd !== undefined) {
      try {
        fs.closeSync(temporaryFd)
      } catch {}
    }
    try {
      fs.unlinkSync(temporary)
    } catch {}
  }
}

function publishArtifactSnapshots(
  outputRoot: string | undefined,
  targets: ReadonlyMap<string, AuthorizedArtifactTarget>,
) {
  const snapshots = new Map<string, string>()
  const created: string[] = []
  if (!outputRoot) return { snapshots, created }
  const snapshotRoot = targets.size ? prepareSnapshotRoot(outputRoot) : undefined
  for (const [relativePath, target] of targets) {
    const snapshot = publishArtifactSnapshot(outputRoot, snapshotRoot!, target)
    if (!snapshot) continue
    snapshots.set(relativePath, snapshot.path)
    if (snapshot.created) created.push(snapshot.path)
  }
  return { snapshots, created }
}

function rollbackPublishedArtifactSnapshots(outputRoot: string, snapshots: readonly string[]) {
  const snapshotRoot = path.resolve(outputRoot, "artifacts", "render-snapshots", "sha256")
  for (const snapshot of snapshots) {
    try {
      fs.unlinkSync(path.join(snapshotRoot, path.basename(snapshot)))
    } catch {}
  }
  cleanupSnapshotDirectories(outputRoot)
}

export function writeProvenanceTraceHtmlFile(
  target: string,
  trace: ProvenanceTraceView,
  options: WriteProvenanceTraceHtmlOptions = {},
): TraceHtmlWriteStats {
  const maxChunkBytes = Math.max(1024, Math.floor(options.maxChunkBytes ?? 256 * 1024))
  const streamingThreshold = Math.max(1, Math.floor(options.streamingThreshold ?? 5000))
  const streaming = options.forceStreaming === true || trace.records.length >= streamingThreshold
  const outputRoot = path.dirname(target)
  const authorized = authorizedArtifactTargets(trace.artifacts, options.artifactSourceRoot ?? outputRoot)
  let artifactSnapshotPaths: ReadonlyMap<string, string>
  let createdSnapshots: string[]
  try {
    const published = publishArtifactSnapshots(outputRoot, authorized)
    artifactSnapshotPaths = published.snapshots
    createdSnapshots = published.created
  } finally {
    closeAuthorizedArtifactTargets(authorized)
  }
  const chunks = provenanceTraceHtmlChunks(trace, { maxChunkBytes, artifactSnapshotPaths })
  let written: ReturnType<typeof writeAtomicChunks>
  try {
    written = writeAtomicChunks(target, chunks, maxChunkBytes)
  } catch (error) {
    rollbackPublishedArtifactSnapshots(outputRoot, createdSnapshots)
    throw error
  }
  return {
    mode: streaming ? "streaming" : "inline",
    record_count: trace.records.length,
    chunk_count: written.chunkCount,
    max_chunk_bytes: written.largestChunk,
    max_generator_chunk_bytes: written.largestGeneratorChunk,
    max_temporary_buffer_bytes: written.largestTemporaryBuffer,
    artifact_payloads_embedded: 0,
  }
}

function collectArtifactContents(
  trace: TraceSummary,
  options: RenderCaseTraceHtmlOptions,
  allowedArtifactTargets: ReadonlyMap<string, AuthorizedArtifactTarget>,
  artifactSnapshotPaths: ReadonlyMap<string, string>,
) {
  const contents = new Map<string, string>()
  // Large traces keep artifact bodies authoritative on disk. Embedding them in
  // trace.html creates another full payload collection during finalization.
  if (trace.events.length + trace.spans.length >= 5000) return contents
  const artifactsByID = new Map<string, AuthorizedArtifactTarget>()
  for (const artifact of trace.artifacts ?? []) {
    const id = boundedViewerText(artifact.artifact_id, 520)
    const relativePath = safeArtifactRelativePath(artifact.path)
    const target = relativePath ? allowedArtifactTargets.get(relativePath) : undefined
    if (id && relativePath && target && artifactSnapshotPaths.has(relativePath)) artifactsByID.set(id, target)
  }
  if (options.artifactContents instanceof Map) {
    for (const [key, value] of options.artifactContents) {
      const id = boundedViewerText(key, 520)
      if (artifactsByID.has(id)) contents.set(id, boundedViewerText(value, VIEWER_STRING_BUDGET))
    }
  } else if (options.artifactContents) {
    for (const key in options.artifactContents) {
      if (!Object.prototype.hasOwnProperty.call(options.artifactContents, key)) continue
      const id = boundedViewerText(key, 520)
      if (artifactsByID.has(id))
        contents.set(id, boundedViewerText(options.artifactContents[key], VIEWER_STRING_BUDGET))
    }
  }
  if (!options.artifactDir) return contents
  const buffer = Buffer.allocUnsafe(VIEWER_STRING_BUDGET * 4)
  for (const [id, target] of artifactsByID) {
    if (contents.has(id)) continue
    try {
      const count = fs.readSync(target.fd, buffer, 0, buffer.length, 0)
      contents.set(id, boundedViewerText(buffer.toString("utf8", 0, count), VIEWER_STRING_BUDGET))
    } catch {}
  }
  return contents
}

function collectComponents(trace: TraceSummary) {
  const components = new Set<string>()
  for (const span of trace.spans) components.add(boundedViewerText(span.component, 80))
  for (const event of trace.events) components.add(boundedViewerText(event.component, 80))
  return [...components].sort((a, b) => componentRank(a) - componentRank(b) || a.localeCompare(b))
}

function componentMetrics(trace: TraceSummary) {
  return collectComponents(trace).map((component) => {
    const spans = trace.spans.filter((span) => boundedViewerText(span.component, 80) === component)
    const events = trace.events.filter((event) => boundedViewerText(event.component, 80) === component)
    return {
      component,
      spans: spans.length,
      events: events.length,
      duration: spans.reduce((sum, span) => sum + (span.duration_ms ?? 0), 0),
      tokens: spans.reduce((sum, span) => sum + tokenTotal(span.token_usage), 0),
    }
  })
}

type ProcessItem = {
  time: number
  component: TraceComponent
  kind: "span" | "event"
  title: string
  status?: string
  duration?: number
  input?: unknown
  output?: unknown
  data?: unknown
}

function processItems(trace: TraceSummary) {
  const items: ProcessItem[] = [
    ...trace.spans.map((span) => ({
      time: span.start_ms,
      component: boundedViewerText(span.component, 80) as TraceComponent,
      kind: "span" as const,
      title: boundedViewerText(span.name ?? span.operation, 520),
      status: boundedViewerText(span.status, 80),
      duration: span.duration_ms,
      input: span.input_summary,
      output: span.output_summary,
    })),
    ...trace.events.map((event) => ({
      time: event.time_ms,
      component: boundedViewerText(event.component, 80) as TraceComponent,
      kind: "event" as const,
      title: boundedViewerText(event.event_type, 520),
      data: event.data,
    })),
  ]
  return items.toSorted((a, b) => a.time - b.time)
}

type FlowEdge = {
  from: TraceComponent
  to: TraceComponent
  count: number
  samples: string[]
}

function addEdge(edges: Map<string, FlowEdge>, from: TraceComponent, to: TraceComponent, sample: string) {
  const boundedFrom = boundedViewerText(from, 80) as TraceComponent
  const boundedTo = boundedViewerText(to, 80) as TraceComponent
  if (boundedFrom === boundedTo) return
  const key = boundedViewerJoin([boundedFrom, boundedTo], "->", 180)
  const edge = edges.get(key) ?? { from: boundedFrom, to: boundedTo, count: 0, samples: [] }
  edge.count += 1
  if (edge.samples.length < 3 && sample) edge.samples.push(boundedViewerText(sample, 1200))
  edges.set(key, edge)
}

function flowEdges(trace: TraceSummary) {
  const edges = new Map<string, FlowEdge>()
  const points = [
    ...trace.spans.map((span) => ({
      time: span.start_ms,
      component: boundedViewerText(span.component, 80) as TraceComponent,
      label: boundedViewerJoin([boundedViewerText(span.name ?? span.operation, 520), "start"], ":", 530),
    })),
    ...trace.events.map((event) => ({
      time: event.time_ms,
      component: boundedViewerText(event.component, 80) as TraceComponent,
      label: boundedViewerText(event.event_type, 530),
    })),
    ...trace.spans.map((span) => ({
      time: span.end_ms ?? span.start_ms,
      component: boundedViewerText(span.component, 80) as TraceComponent,
      label: boundedViewerJoin([boundedViewerText(span.name ?? span.operation, 520), "end"], ":", 530),
    })),
  ].toSorted((a, b) => a.time - b.time)

  let previous: (typeof points)[number] | undefined
  for (const point of points) {
    if (previous)
      addEdge(edges, previous.component, point.component, boundedViewerJoin([previous.label, point.label], " -> ", 1200))
    previous = point
  }

  const spanByID = new Map(trace.spans.map((span) => [boundedViewerText(span.span_id, 520), span]))
  for (const span of trace.spans) {
    if (!span.parent_span_id) continue
    const parent = spanByID.get(boundedViewerText(span.parent_span_id, 520))
    if (parent)
      addEdge(
        edges,
        parent.component,
        span.component,
        boundedViewerJoin(
          ["parent", boundedViewerText(parent.name ?? parent.operation, 520), boundedViewerText(span.name ?? span.operation, 520)],
          " -> ",
          1200,
        ),
      )
  }

  return [...edges.values()].sort((a, b) => b.count - a.count || componentRank(a.from) - componentRank(b.from))
}

function renderSummary(input: unknown) {
  const text = preview(input, 260)
  return text ? `<code>${escapeHtml(text)}</code>` : `<span class="muted">-</span>`
}

function renderArtifactDetails(input: unknown, artifactContents: Map<string, string>) {
  const id = artifactID(input)
  if (!id) return ""
  const content = artifactContents.get(id)
  if (content === undefined) return `<div class="artifact-missing">完整内容未嵌入：<code>${escapeHtml(id)}</code></div>`
  return `<details class="artifact-inline">
    <summary>查看完整内容 <code>${escapeHtml(id)}</code></summary>
    <pre>${escapeHtml(content)}</pre>
  </details>`
}

function renderIoCell(label: string, input: unknown, artifactContents: Map<string, string>) {
  return `<div class="io-cell">
    <div class="io-label">${escapeHtml(label)}</div>
    <div class="io-scroll">${renderSummary(input)}</div>
    ${renderArtifactDetails(input, artifactContents)}
  </div>`
}

function renderAgentProcess(trace: TraceSummary, artifactContents: Map<string, string>) {
  const items = processItems(trace)
  if (!items.length) return `<div class="empty">没有流程事件。</div>`
  return `<div class="process-scroll"><div class="process">
    ${items
      .map(
        (item) => `<div class="step">
          <div class="step-time">${escapeHtml(formatMs(item.time))}</div>
          <div class="step-dot ${escapeHtml(componentClass(item.component))}"></div>
          <div class="step-body">
            <div class="step-head">
              <span class="pill ${escapeHtml(componentClass(item.component))}">${escapeHtml(componentLabel[item.component] ?? item.component)}</span>
              <strong>${escapeHtml(item.title)}</strong>
              ${item.status ? `<span class="status">${escapeHtml(item.status)}</span>` : ""}
              ${item.duration !== undefined ? `<span class="muted">${escapeHtml(formatMs(item.duration))}</span>` : ""}
            </div>
            <div class="step-io">
              ${item.input !== undefined ? renderIoCell("输入", item.input, artifactContents) : ""}
              ${item.output !== undefined ? renderIoCell("输出", item.output, artifactContents) : ""}
              ${item.data !== undefined ? renderIoCell("事件", item.data, artifactContents) : ""}
            </div>
          </div>
        </div>`,
      )
      .join("")}
  </div></div>`
}

function renderArtifacts(
  trace: TraceSummary,
  artifactContents: Map<string, string>,
  artifactSnapshotPaths: ReadonlyMap<string, string>,
) {
  const artifacts = trace.artifacts ?? []
  if (!artifacts.length) return `<div class="empty">没有大文本 artifact。</div>`
  return `<div class="artifacts">
      ${artifacts
      .map((artifact: TraceArtifact) => {
        const id = boundedViewerText(artifact.artifact_id, 520)
        const content = artifactContents.get(id)
        const safePath = safeArtifactRelativePath(artifact.path)
        const snapshotPath = safePath ? artifactSnapshotPaths.get(safePath) : undefined
        return `<details class="artifact-row">
          <summary>
            <span class="pill trace">${escapeHtml(artifact.kind)}</span>
            <strong>${escapeHtml(artifact.label ?? id)}</strong>
            <code>${escapeHtml(id)}</code>
            <span class="muted">${artifact.length} chars</span>
          </summary>
          <div class="artifact-meta">
            <span>path: ${snapshotPath ? `<a href="${escapeHtml(snapshotPath)}"><code>${escapeHtml(snapshotPath)}</code></a>` : `<span class="artifact-missing" title="Artifact unavailable">Artifact unavailable</span>`}</span>
            <span>hash: <code>${escapeHtml(artifact.hash)}</code></span>
          </div>
          ${
            content === undefined
              ? `<div class="artifact-missing">完整内容未嵌入。</div>`
              : `<pre>${escapeHtml(content)}</pre>`
          }
        </details>`
      })
      .join("")}
  </div>`
}

function renderFlow(trace: TraceSummary) {
  const metrics = componentMetrics(trace)
  const edges = flowEdges(trace)
  if (!metrics.length) return `<div class="empty">没有组件流转数据。</div>`
  return `<div class="flow">
    <div class="flow-nodes">
      ${metrics
        .map(
          (item) => `<div class="flow-node ${escapeHtml(componentClass(item.component))}">
            <div class="node-title">${escapeHtml(componentLabel[item.component as TraceComponent] ?? item.component)}</div>
            <div class="node-sub"><code>${escapeHtml(item.component)}</code></div>
            <div class="node-metrics">
              <span>${item.spans} span</span>
              <span>${item.events} event</span>
              <span>${escapeHtml(formatMs(item.duration))}</span>
              ${item.tokens ? `<span>${item.tokens} token</span>` : ""}
            </div>
          </div>`,
        )
        .join("")}
    </div>
    <div class="flow-edges">
      <h3>跨组件流转</h3>
      ${
        edges.length
          ? edges
              .map(
                (edge) => `<div class="edge">
                  <span class="pill ${escapeHtml(componentClass(edge.from))}">${escapeHtml(edge.from)}</span>
                  <span class="arrow">→</span>
                  <span class="pill ${escapeHtml(componentClass(edge.to))}">${escapeHtml(edge.to)}</span>
                  <span class="edge-count">${edge.count} 次</span>
                  <span class="edge-sample">${escapeHtml(boundedViewerJoin(edge.samples, "；"))}</span>
                </div>`,
              )
              .join("")
          : `<div class="empty">没有跨组件跳转。</div>`
      }
    </div>
  </div>`
}

function renderContextSnapshots(trace: TraceSummary, artifactContents: Map<string, string>) {
  const snapshots = trace.context_snapshots ?? []
  if (!snapshots.length) return `<div class="empty">没有 LLM 上下文快照。</div>`
  return `<div class="process">
    ${snapshots
      .map(
        (snapshot) => `<div class="step">
          <div class="step-time">${escapeHtml(snapshot.phase)}</div>
          <div class="step-dot context"></div>
          <div class="step-body">
            <div class="step-head">
              <span class="pill context">context</span>
              <strong>${escapeHtml(snapshot.snapshot_id)}</strong>
              <span class="muted">${escapeHtml(boundedViewerJoin([snapshot.provider_id, snapshot.model_id], "/") || "-")}</span>
              <span class="muted">${snapshot.message_count ?? 0} messages</span>
              <span class="muted">${snapshot.tool_count ?? 0} tools</span>
            </div>
            <div class="step-io">
              ${snapshot.system ? renderIoCell("System", snapshot.system, artifactContents) : ""}
              ${snapshot.messages ? renderIoCell("Messages", snapshot.messages, artifactContents) : ""}
              ${snapshot.tools ? renderIoCell("Tools", snapshot.tools, artifactContents) : ""}
            </div>
          </div>
        </div>`,
      )
      .join("")}
  </div>`
}

function renderSourceRecords(trace: TraceSummary, artifactContents: Map<string, string>) {
  const decisions = trace.semantic_decisions ?? []
  const edges = trace.dataflow_edges ?? []
  const responseSegments = trace.response_segments ?? []
  if (!decisions.length && !edges.length && !responseSegments.length) return `<div class="empty">没有响应和来源记录。</div>`

  return `<div>
    ${
      decisions.length
        ? `<h3>Decisions</h3><table>
          <thead><tr><th>ID</th><th>组件</th><th>类型</th><th>意图</th><th>动作</th><th>理由</th><th>来源</th></tr></thead>
          <tbody>
            ${decisions
              .map(
                (item) => `<tr>
                  <td><code>${escapeHtml(item.decision_id)}</code></td>
                  <td>${escapeHtml(item.component)}</td>
                  <td>${escapeHtml(item.decision_type)}</td>
                  <td>${escapeHtml(item.intent ?? "")}</td>
                  <td>${escapeHtml(item.chosen_action ?? "")}</td>
                  <td>${renderSummary(item.rationale)}</td>
                  <td>${escapeHtml(boundedViewerJoin(item.source_refs ?? []))}</td>
                </tr>`,
              )
              .join("")}
          </tbody>
        </table>`
        : ""
    }
    ${
      edges.length
        ? `<h3>Dataflow Edges</h3><table>
          <thead><tr><th>关系</th><th>From</th><th>To</th><th>说明</th></tr></thead>
          <tbody>
            ${edges
              .map(
                (edge) => `<tr>
                  <td>${escapeHtml(edge.relation)}</td>
                  <td><code>${escapeHtml(edge.from.type)}:${escapeHtml(edge.from.id)}</code></td>
                  <td><code>${escapeHtml(edge.to.type)}:${escapeHtml(edge.to.id)}</code></td>
                  <td>${escapeHtml(edge.label ?? "")}</td>
                </tr>`,
              )
              .join("")}
          </tbody>
        </table>`
        : ""
    }
    ${
      responseSegments.length
        ? `<h3>Response Output</h3><div class="process">
          ${responseSegments
            .map(
              (item) => `<div class="io-cell">
                <div class="io-label"><code>${escapeHtml(item.segment_id)}</code></div>
                ${renderIoCell("Text", item.text, artifactContents)}
                <div class="muted">source refs: ${escapeHtml(boundedViewerJoin(item.source_refs ?? []) || "-")}</div>
              </div>`,
            )
            .join("")}
        </div>`
        : ""
    }
  </div>`
}

function renderChangesAndVerification(trace: TraceSummary, artifactContents: Map<string, string>) {
  const verifications = trace.verification_records ?? []
  const changes = trace.change_records ?? []
  if (!verifications.length && !changes.length) return `<div class="empty">没有变更或验证记录。</div>`

  return `<div>
    ${
      verifications.length
        ? `<h3>Verification</h3><table>
          <thead><tr><th>ID</th><th>阶段</th><th>状态</th><th>命令</th><th>失败解析</th><th>输出</th></tr></thead>
          <tbody>
            ${verifications
              .map(
                (item) => `<tr>
                  <td><code>${escapeHtml(item.verification_id)}</code></td>
                  <td>${escapeHtml(item.stage ?? "unknown")}</td>
                  <td>${escapeHtml(item.status)}</td>
                  <td><code>${escapeHtml(item.command ?? "")}</code></td>
                  <td>${escapeHtml(
                    item.parsed_failures
                      .reduce(
                        (output, failure) =>
                          boundedViewerJoin(
                            [
                              output,
                              boundedViewerJoin(
                                [
                                  failure.file,
                                  failure.line,
                                  failure.message,
                                  failure.expected && `expected ${boundedViewerText(failure.expected, 260)}`,
                                  failure.actual && `actual ${boundedViewerText(failure.actual, 260)}`,
                                ],
                                " ",
                                720,
                              ),
                            ],
                            "; ",
                            2400,
                          ),
                        "",
                      ),
                  )}</td>
                  <td>
                    ${item.stdout ? renderArtifactDetails(item.stdout, artifactContents) || renderSummary(item.stdout) : ""}
                    ${item.stderr ? renderArtifactDetails(item.stderr, artifactContents) || renderSummary(item.stderr) : ""}
                  </td>
                </tr>`,
              )
              .join("")}
          </tbody>
        </table>`
        : ""
    }
    ${
      changes.length
        ? `<h3>Changes</h3><table>
          <thead><tr><th>ID</th><th>文件</th><th>意图</th><th>来源</th><th>Diff</th></tr></thead>
          <tbody>
            ${changes
              .map(
                (item) => `<tr>
                  <td><code>${escapeHtml(item.change_id)}</code></td>
                  <td>${escapeHtml(boundedViewerJoin(item.files))}</td>
                  <td>${escapeHtml(item.intent ?? "")}</td>
                  <td>${escapeHtml(boundedViewerJoin(item.source_refs ?? []))}</td>
                  <td>${item.diff ? renderArtifactDetails(item.diff, artifactContents) || renderSummary(item.diff) : ""}</td>
                </tr>`,
              )
              .join("")}
          </tbody>
        </table>`
        : ""
    }
  </div>`
}

function renderDesignRecords(trace: TraceSummary, artifactContents: Map<string, string>) {
  const records = trace.design_records ?? []
  if (!records.length) return `<div class="empty">没有方案设计记录。</div>`
  return `<div class="process">
    ${records
      .map(
        (item) => `<div class="io-cell">
          <div class="io-label">
            <code>${escapeHtml(item.design_id)}</code>
            ${item.source ? `<span class="muted">${escapeHtml(item.source)}</span>` : ""}
          </div>
          <div class="step-io">
            ${item.requirement_summary ? renderIoCell("Requirement", item.requirement_summary, artifactContents) : ""}
            ${item.existing_boundaries ? renderIoCell("Boundaries", item.existing_boundaries, artifactContents) : ""}
            ${item.design_constraints ? renderIoCell("Constraints", item.design_constraints, artifactContents) : ""}
            ${item.candidate_solutions ? renderIoCell("Candidates", item.candidate_solutions, artifactContents) : ""}
            ${item.selected_solution ? renderIoCell("Selected Solution", item.selected_solution, artifactContents) : ""}
            ${item.tradeoffs ? renderIoCell("Tradeoffs", item.tradeoffs, artifactContents) : ""}
            ${item.risks ? renderIoCell("Risks", item.risks, artifactContents) : ""}
            ${item.test_strategy ? renderIoCell("Test Strategy", item.test_strategy, artifactContents) : ""}
          </div>
          <div class="muted">source refs: ${escapeHtml(boundedViewerJoin(item.source_refs ?? []) || "-")}</div>
        </div>`,
      )
      .join("")}
  </div>`
}

function renderConstraints(trace: TraceSummary) {
  const constraints = trace.constraint_records ?? []
  if (!constraints.length) return `<div class="empty">没有约束记录。</div>`
  return `<table>
    <thead><tr><th>ID</th><th>来源</th><th>约束</th><th>状态</th><th>Source Refs</th></tr></thead>
    <tbody>
      ${constraints
        .map(
          (item) => `<tr>
            <td><code>${escapeHtml(item.constraint_id)}</code></td>
            <td>${escapeHtml(item.source)}</td>
            <td>${escapeHtml(item.constraint)}</td>
            <td>${escapeHtml(item.status)}</td>
            <td>${escapeHtml(boundedViewerJoin(item.source_refs ?? []))}</td>
          </tr>`,
        )
        .join("")}
    </tbody>
  </table>`
}

export function renderCaseTraceHtml(trace: TraceSummary, options: RenderCaseTraceHtmlOptions = {}) {
  const allowedArtifactTargets = authorizedArtifactTargets(trace.artifacts ?? [], options.artifactDir)
  let artifactContents: Map<string, string>
  let artifactSnapshotPaths: ReadonlyMap<string, string>
  try {
    artifactSnapshotPaths = publishArtifactSnapshots(options.artifactDir, allowedArtifactTargets).snapshots
    artifactContents = collectArtifactContents(trace, options, allowedArtifactTargets, artifactSnapshotPaths)
  } finally {
    closeAuthorizedArtifactTargets(allowedArtifactTargets)
  }
  const spans = trace.spans.toSorted((a, b) => a.start_ms - b.start_ms)
  const maxEnd = Math.max(trace.duration_ms, ...spans.map((span) => span.end_ms ?? span.start_ms))
  const duration = Math.max(1, maxEnd)
  const tools = trace.spans.filter((span) => ["tool", "skill", "task", "mcp"].includes(span.component))
  const errors = trace.errors.length
    ? trace.errors
    : trace.spans.flatMap((span) =>
        span.error ? [{ ...span.error, span_id: span.span_id, component: span.component }] : [],
      )

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Opencode Case Trace - ${escapeHtml(trace.case_id)}</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --text: #18202a;
      --muted: #667085;
      --border: #d9dee7;
      --soft: #f9fafb;
      --run: #2563eb;
      --runtime: #0f766e;
      --prompt: #7c3aed;
      --context: #4f46e5;
      --llm: #be123c;
      --processor: #c2410c;
      --tool: #047857;
      --skill: #b45309;
      --task: #9333ea;
      --mcp: #0369a1;
      --plugin: #475467;
      --result: #15803d;
      --trace: #64748b;
      --other: #475467;
      --error: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }
    header {
      padding: 28px 32px 18px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }
    h1, h2, h3 { margin: 0; letter-spacing: 0; }
    h1 { font-size: 24px; }
    h2 { font-size: 16px; margin-bottom: 12px; }
    h3 { font-size: 13px; margin: 0 0 10px; color: #344054; }
    main { padding: 24px 32px 40px; }
    section {
      margin-bottom: 20px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
    }
    .meta {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
    }
    .cards, .flow-nodes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }
    .card, .flow-node {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfe;
    }
    .label, .muted, .node-sub { color: var(--muted); font-size: 12px; }
    .value { font-size: 18px; font-weight: 650; margin-top: 4px; word-break: break-word; }
    .node-title { font-size: 14px; font-weight: 700; }
    .node-metrics {
      margin-top: 10px;
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      color: #344054;
      font-size: 12px;
    }
    .flow-node { border-top: 4px solid var(--other); }
    .flow-node.run, .pill.run, .step-dot.run, .bar.run { border-color: var(--run); background-color: #eff6ff; }
    .flow-node.runtime, .pill.runtime, .step-dot.runtime, .bar.runtime { border-color: var(--runtime); background-color: #ecfdf5; }
    .flow-node.prompt, .pill.prompt, .step-dot.prompt, .bar.prompt { border-color: var(--prompt); background-color: #f5f3ff; }
    .flow-node.context, .pill.context, .step-dot.context, .bar.context { border-color: var(--context); background-color: #eef2ff; }
    .flow-node.llm, .pill.llm, .step-dot.llm, .bar.llm { border-color: var(--llm); background-color: #fff1f2; }
    .flow-node.processor, .pill.processor, .step-dot.processor, .bar.processor { border-color: var(--processor); background-color: #fff7ed; }
    .flow-node.tool, .pill.tool, .step-dot.tool, .bar.tool { border-color: var(--tool); background-color: #ecfdf5; }
    .flow-node.skill, .pill.skill, .step-dot.skill, .bar.skill { border-color: var(--skill); background-color: #fffbeb; }
    .flow-node.task, .pill.task, .step-dot.task, .bar.task { border-color: var(--task); background-color: #faf5ff; }
    .flow-node.mcp, .pill.mcp, .step-dot.mcp, .bar.mcp { border-color: var(--mcp); background-color: #f0f9ff; }
    .flow-node.plugin, .pill.plugin, .step-dot.plugin, .bar.plugin { border-color: var(--plugin); background-color: #f8fafc; }
    .flow-node.result, .pill.result, .step-dot.result, .bar.result { border-color: var(--result); background-color: #f0fdf4; }
    .flow-node.trace, .pill.trace, .step-dot.trace, .bar.trace { border-color: var(--trace); background-color: #f8fafc; }
    .flow-edges { margin-top: 14px; }
    .edge {
      display: grid;
      grid-template-columns: auto 18px auto 64px minmax(180px, 1fr);
      gap: 8px;
      align-items: center;
      padding: 8px 0;
      border-top: 1px solid #edf0f5;
      font-size: 12px;
    }
    .edge:first-of-type { border-top: 0; }
    .arrow { color: #98a2b3; font-weight: 700; text-align: center; }
    .edge-count { color: #344054; font-weight: 650; }
    .edge-sample { color: var(--muted); overflow-wrap: anywhere; }
    .process-scroll {
      max-height: min(78vh, 860px);
      overflow-y: auto;
      padding-right: 8px;
      overscroll-behavior: contain;
    }
    .process { position: relative; min-width: 0; }
    .step {
      display: grid;
      grid-template-columns: 72px 18px minmax(0, 1fr);
      gap: 10px;
      padding: 10px 0;
      border-top: 1px solid #edf0f5;
    }
    .step:first-child { border-top: 0; }
    .step-time { color: var(--muted); font-size: 12px; padding-top: 2px; text-align: right; }
    .step-dot {
      width: 12px;
      height: 12px;
      margin-top: 4px;
      border-radius: 999px;
      border: 3px solid var(--other);
      background: #ffffff;
    }
    .step-head {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      font-size: 13px;
    }
    .step-io {
      margin-top: 5px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      min-width: 0;
    }
    .io-cell {
      min-width: 0;
      border: 1px solid #edf0f5;
      border-radius: 6px;
      background: #fcfcfd;
      padding: 6px 8px;
    }
    .io-label {
      margin-bottom: 4px;
      color: #475467;
      font-size: 11px;
      font-weight: 650;
    }
    .io-scroll {
      max-width: 100%;
      overflow-x: auto;
      overflow-y: hidden;
      padding-bottom: 2px;
      white-space: nowrap;
    }
    .io-scroll code {
      display: inline-block;
      min-width: max-content;
      white-space: pre;
    }
    .timeline {
      position: relative;
      overflow-x: auto;
      padding-bottom: 6px;
    }
    .row {
      display: grid;
      grid-template-columns: 170px minmax(520px, 1fr) 86px;
      min-height: 30px;
      align-items: center;
      gap: 10px;
      border-top: 1px solid #edf0f5;
      font-size: 12px;
    }
    .row:first-child { border-top: 0; }
    .name {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: #344054;
    }
    .bar-wrap {
      position: relative;
      height: 16px;
      border-left: 1px solid #eef2f7;
      border-right: 1px solid #eef2f7;
    }
    .bar {
      position: absolute;
      top: 3px;
      height: 10px;
      min-width: 2px;
      border-radius: 3px;
      background: var(--other);
    }
    .bar.run { background: var(--run); }
    .bar.runtime { background: var(--runtime); }
    .bar.prompt { background: var(--prompt); }
    .bar.context { background: var(--context); }
    .bar.llm { background: var(--llm); }
    .bar.processor { background: var(--processor); }
    .bar.tool { background: var(--tool); }
    .bar.skill { background: var(--skill); }
    .bar.task { background: var(--task); }
    .bar.mcp { background: var(--mcp); }
    .bar.plugin { background: var(--plugin); }
    .bar.result { background: var(--result); }
    .bar.trace { background: var(--trace); }
    .bar.error { background: var(--error); }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      text-align: left;
      padding: 8px 10px;
      border-top: 1px solid #edf0f5;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      border-top: 0;
    }
    code, pre {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: #111827;
      color: #e5e7eb;
      border-radius: 8px;
      padding: 12px;
      max-height: 420px;
      overflow: auto;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 2px 8px;
      color: #344054;
      background: #ffffff;
      font-size: 12px;
      white-space: nowrap;
    }
    .status {
      border-radius: 999px;
      padding: 2px 7px;
      background: #f2f4f7;
      color: #344054;
      font-size: 12px;
    }
    .empty, .more { color: var(--muted); font-size: 13px; }
    details summary { cursor: pointer; color: #344054; font-weight: 650; }
    .artifact-inline {
      margin-top: 6px;
      border-top: 1px solid #edf0f5;
      padding-top: 6px;
    }
    .artifact-inline summary, .artifact-row summary {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      font-size: 12px;
    }
    .artifact-inline pre, .artifact-row pre {
      margin-top: 8px;
      max-height: 520px;
      white-space: pre;
      overflow: auto;
    }
    .artifact-row {
      border-top: 1px solid #edf0f5;
      padding: 10px 0;
    }
    .artifact-row:first-child { border-top: 0; }
    .artifact-meta {
      margin-top: 8px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }
    .artifact-missing {
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    @media (max-width: 760px) {
      header, main { padding-left: 16px; padding-right: 16px; }
      .edge { grid-template-columns: 1fr; }
      .step { grid-template-columns: 54px 14px minmax(0, 1fr); }
      .row { grid-template-columns: 140px minmax(360px, 1fr) 76px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Opencode Case Trace</h1>
    <div class="meta">
      <span>case_id: <code>${escapeHtml(trace.case_id)}</code></span>
      <span>run_id: <code>${escapeHtml(trace.run_id)}</code></span>
      <span>status: <strong>${escapeHtml(trace.status)}</strong></span>
      <span>started_at: ${escapeHtml(trace.started_at)}</span>
    </div>
  </header>
  <main>
    <section>
      <div class="cards">
        <div class="card"><div class="label">总耗时</div><div class="value">${escapeHtml(formatMs(trace.duration_ms))}</div></div>
        <div class="card"><div class="label">Span 数</div><div class="value">${trace.spans.length}</div></div>
        <div class="card"><div class="label">Event 数</div><div class="value">${trace.events.length}</div></div>
        <div class="card"><div class="label">Token 总量</div><div class="value">${tokenTotal(trace.token_usage)}</div></div>
        <div class="card"><div class="label">错误数</div><div class="value">${errors.length}</div></div>
      </div>
    </section>

    <section>
      <h2>组件数据流转</h2>
      ${renderFlow(trace)}
    </section>

    <section>
      <h2>Agent 运行流程</h2>
      ${renderAgentProcess(trace, artifactContents)}
    </section>

    <section>
      <h2>Response & Source Records</h2>
      ${renderSourceRecords(trace, artifactContents)}
    </section>

    <section>
      <h2>LLM Context</h2>
      ${renderContextSnapshots(trace, artifactContents)}
    </section>

    <section>
      <h2>Changes & Verification</h2>
      ${renderChangesAndVerification(trace, artifactContents)}
    </section>

    <section>
      <h2>Design Records</h2>
      ${renderDesignRecords(trace, artifactContents)}
    </section>

    <section>
      <h2>Constraints</h2>
      ${renderConstraints(trace)}
    </section>

    <section>
      <h2>Artifacts</h2>
      ${renderArtifacts(trace, artifactContents, artifactSnapshotPaths)}
    </section>

    <section>
      <h2>组件时间线</h2>
      ${
        spans.length
          ? `<div class="timeline">
          ${spans
            .map((span) => {
              const left = Math.max(0, Math.min(100, (span.start_ms / duration) * 100))
              const width = Math.max(0.3, (((span.end_ms ?? span.start_ms) - span.start_ms) / duration) * 100)
              const cls = span.error || span.status === "error" ? "error" : componentClass(span.component)
              return `<div class="row" title="${escapeHtml(span.operation)}">
                <div class="name"><span class="pill ${escapeHtml(componentClass(span.component))}">${escapeHtml(span.component)}</span> ${escapeHtml(span.name ?? span.operation)}</div>
                <div class="bar-wrap"><div class="bar ${escapeHtml(cls)}" style="left:${left}%;width:${width}%"></div></div>
                <div>${escapeHtml(formatMs(span.duration_ms))}</div>
              </div>`
            })
            .join("")}
        </div>`
          : `<div class="empty">没有 span 数据。</div>`
      }
    </section>

    <section>
      <h2>Token 汇总</h2>
      <table>
        <thead><tr><th>input</th><th>output</th><th>reasoning</th><th>cached_input</th><th>cache_write</th><th>total</th><th>cost</th></tr></thead>
        <tbody>
          <tr>
            <td>${trace.token_usage.input ?? 0}</td>
            <td>${trace.token_usage.output ?? 0}</td>
            <td>${trace.token_usage.reasoning ?? 0}</td>
            <td>${trace.token_usage.cached_input ?? 0}</td>
            <td>${trace.token_usage.cache_write ?? 0}</td>
            <td>${tokenTotal(trace.token_usage)}</td>
            <td>${trace.token_usage.cost ?? 0}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section>
      <h2>工具与外部调用</h2>
      ${
        tools.length
          ? `<table>
        <thead><tr><th>组件</th><th>名称</th><th>状态</th><th>耗时</th><th>输入摘要</th><th>输出摘要</th></tr></thead>
        <tbody>
          ${tools
            .map(
              (span) => `<tr>
                <td>${escapeHtml(span.component)}</td>
                <td><code>${escapeHtml(span.name ?? span.operation)}</code></td>
                <td>${escapeHtml(span.status)}</td>
                <td>${escapeHtml(formatMs(span.duration_ms))}</td>
                <td>${renderSummary(span.input_summary ?? {})}</td>
                <td>${renderSummary(span.output_summary ?? {})}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>`
          : `<div class="empty">没有工具、skill、task 或 MCP 调用。</div>`
      }
    </section>

    <section>
      <h2>错误</h2>
      ${
        errors.length
          ? `<table>
        <thead><tr><th>组件</th><th>Span</th><th>Message</th></tr></thead>
        <tbody>
          ${errors
            .map(
              (error) => `<tr>
                <td>${escapeHtml(error.component ?? "")}</td>
                <td><code>${escapeHtml(error.span_id ?? "")}</code></td>
                <td>${escapeHtml(error.message ?? error)}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>`
          : `<div class="empty">没有错误。</div>`
      }
    </section>

    <section>
      <details>
        <summary>原始 Trace JSON</summary>
        <pre id="raw"></pre>
      </details>
    </section>
  </main>
  <script>
    const tracePreview = ${jsonScript(trace)};
    document.getElementById("raw").textContent = tracePreview;
  </script>
</body>
</html>`
}
