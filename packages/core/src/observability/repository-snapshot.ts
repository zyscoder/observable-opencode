import fs from "node:fs"
import path from "node:path"
import { createHash } from "node:crypto"
import { execFileSync } from "node:child_process"

type StatusEntry = {
  code: string
  path: string
}

type FileMetadata = {
  kind: "file" | "directory" | "symlink" | "other" | "missing"
  size?: number
  mode?: number
  mtime_ms?: number
}

export type RepositorySnapshot = {
  available: boolean
  content_mode?: "index_only"
  root?: string
  head?: string
  status?: string
  fingerprint?: string
  file_hashes?: Record<string, string>
  file_metadata?: Record<string, FileMetadata>
  entries?: StatusEntry[]
}

export type RepositorySnapshotDelta = {
  changed: boolean
  files: string[]
  diff?: string
  before_fingerprint?: string
  after_fingerprint?: string
  status_before?: string
  status_after?: string
}

const MAX_GIT_OUTPUT = 32 * 1024 * 1024
const MAX_UNTRACKED_TEXT = 256 * 1024

function git(cwd: string, args: string[]) {
  try {
    return execFileSync("git", ["-C", cwd, ...args], {
      encoding: "utf8",
      timeout: 15_000,
      maxBuffer: MAX_GIT_OUTPUT,
      stdio: ["ignore", "pipe", "ignore"],
    }).trimEnd()
  } catch {
    return undefined
  }
}

function parseStatus(raw: string): StatusEntry[] {
  const parts = raw.split("\0")
  const output: StatusEntry[] = []
  for (let index = 0; index < parts.length; index++) {
    const item = parts[index]
    if (!item || item.length < 4) continue
    const code = item.slice(0, 2)
    const file = item.slice(3)
    if (!file) continue
    output.push({ code, path: file })
    if (/[RC]/.test(code) && parts[index + 1]) index++
  }
  return output
}

function fileHash(root: string, file: string) {
  const target = path.resolve(root, file)
  const relative = path.relative(root, target)
  if (relative.startsWith("..") || path.isAbsolute(relative)) return "outside_repository"
  try {
    const stat = fs.lstatSync(target)
    if (stat.isSymbolicLink()) {
      const targetDigest = createHash("sha256").update(fs.readlinkSync(target)).digest("hex")
      return `symlink_sha256:${targetDigest}`
    }
    if (!stat.isFile()) return `non_file:${stat.mode}`
    const digest = createHash("sha256")
    const fd = fs.openSync(target, "r")
    const buffer = Buffer.allocUnsafe(64 * 1024)
    try {
      let bytesRead = 0
      do {
        bytesRead = fs.readSync(fd, buffer, 0, buffer.byteLength, null)
        if (bytesRead) digest.update(buffer.subarray(0, bytesRead))
      } while (bytesRead)
    } finally {
      fs.closeSync(fd)
    }
    return digest.digest("hex")
  } catch {
    return "deleted"
  }
}

function fileMetadata(root: string, file: string): FileMetadata {
  const target = path.resolve(root, file)
  const relative = path.relative(root, target)
  if (relative.startsWith("..") || path.isAbsolute(relative)) return { kind: "missing" }
  try {
    const stat = fs.lstatSync(target)
    const common = {
      size: stat.size,
      mode: stat.mode,
      mtime_ms: stat.mtimeMs,
    }
    if (stat.isSymbolicLink()) return { ...common, kind: "symlink" }
    if (stat.isFile()) return { ...common, kind: "file" }
    if (stat.isDirectory()) return { ...common, kind: "directory" }
    return { ...common, kind: "other" }
  } catch {
    return { kind: "missing" }
  }
}

function untrackedDiff(root: string, entries: StatusEntry[], files: string[]) {
  const output: string[] = []
  const untracked = new Set(entries.filter((item) => item.code === "??").map((item) => item.path))
  for (const file of files) {
    if (!untracked.has(file)) continue
    const target = path.resolve(root, file)
    try {
      const stat = fs.statSync(target)
      if (!stat.isFile() || stat.size > MAX_UNTRACKED_TEXT) {
        output.push(`diff --git a/${file} b/${file}\nnew untracked file (${stat.size} bytes)`)
        continue
      }
      const content = fs.readFileSync(target, "utf8")
      output.push(
        [`diff --git a/${file} b/${file}`, "new file mode 100644", "--- /dev/null", `+++ b/${file}`, content]
          .join("\n")
          .trimEnd(),
      )
    } catch {
      // The file disappeared between status collection and artifact capture.
    }
  }
  return output.join("\n")
}

export function captureRepositorySnapshot(cwd: string): RepositorySnapshot {
  const root = git(cwd, ["rev-parse", "--show-toplevel"])
  if (!root) return { available: false }
  const head = git(root, ["rev-parse", "HEAD"]) ?? "unborn"
  const rawStatus = git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
  if (rawStatus === undefined) return { available: false, root, head }
  const entries = parseStatus(rawStatus)
  const fileHashes = Object.fromEntries(entries.map((item) => [item.path, fileHash(root, item.path)]))
  const fileMetadataIndex = Object.fromEntries(entries.map((item) => [item.path, fileMetadata(root, item.path)]))
  const fingerprint = createHash("sha256").update(JSON.stringify({ head, fileHashes })).digest("hex")
  return {
    available: true,
    content_mode: "index_only",
    root,
    head,
    status: entries.map((item) => `${item.code} ${item.path}`).join("\n"),
    fingerprint,
    file_hashes: fileHashes,
    file_metadata: fileMetadataIndex,
    entries,
  }
}

export function repositorySnapshotDelta(
  before: RepositorySnapshot | undefined,
  after: RepositorySnapshot | undefined,
): RepositorySnapshotDelta {
  if (!before?.available || !after?.available || before.root !== after.root) return { changed: false, files: [] }
  const beforeHashes = before.file_hashes ?? {}
  const afterHashes = after.file_hashes ?? {}
  const files = Array.from(new Set([...Object.keys(beforeHashes), ...Object.keys(afterHashes)]))
    .filter((file) => beforeHashes[file] !== afterHashes[file])
    .sort()
  if (!files.length && before.fingerprint === after.fingerprint) return { changed: false, files: [] }
  const tracked = files.length ? git(after.root!, ["diff", "--no-ext-diff", "--binary", "HEAD", "--", ...files]) : ""
  const untracked = untrackedDiff(after.root!, after.entries ?? [], files)
  const diff = [tracked, untracked].filter(Boolean).join("\n")
  return {
    changed: true,
    files,
    diff: diff || undefined,
    before_fingerprint: before.fingerprint,
    after_fingerprint: after.fingerprint,
    status_before: before.status,
    status_after: after.status,
  }
}
