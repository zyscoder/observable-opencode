import { createHash } from "node:crypto"
import fs from "node:fs"
import path from "node:path"

export type TraceArtifactRef = {
  artifact_id: string
  path: string
  media_type: string
  bytes: number
  sha256: string
  preview?: string
}

export type TraceArtifactInput = {
  segmentDir: string
  value: unknown
  mediaType?: string
  previewLimit?: number
}

function serialize(value: unknown) {
  if (typeof value === "string") return value
  return JSON.stringify(value, null, 2) ?? String(value)
}

export function writeTraceArtifact(input: TraceArtifactInput): TraceArtifactRef {
  const content = serialize(input.value)
  const bytes = Buffer.byteLength(content, "utf8")
  const sha256 = createHash("sha256").update(content).digest("hex")
  const artifactID = `artifact_${sha256.slice(0, 20)}`
  const artifactDir = path.join(input.segmentDir, "artifacts")
  const artifactPath = path.join(artifactDir, `${artifactID}.txt`)
  fs.mkdirSync(artifactDir, { recursive: true })
  if (!fs.existsSync(artifactPath)) fs.writeFileSync(artifactPath, content, "utf8")
  const limit = Math.max(0, input.previewLimit ?? 2_000)
  return {
    artifact_id: artifactID,
    path: path.relative(input.segmentDir, artifactPath).split(path.sep).join("/"),
    media_type: input.mediaType ?? "text/plain",
    bytes,
    sha256,
    ...(limit > 0 ? { preview: content.slice(0, limit) } : {}),
  }
}
