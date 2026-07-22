import { createHash } from "node:crypto"

export type ClaimAtomizationStatus = "atomic" | "group_required" | "invalid_fragment"

export type AtomizedResponseClaim = {
  key: string
  text: string
  raw_text: string
  canonical_text?: string
  claim_format: "factual_claim" | "table_fact"
  table_cells?: string[]
  table_subject?: string
  table_values?: string[]
  claim_group_id: string
  claim_index: number
  claim_count: number
  source_byte_range: [number, number]
  previous_claim_key?: string
  next_claim_key?: string
  atomization_status: ClaimAtomizationStatus
  atomization_reason: string
}

type ClaimSpan = {
  start: number
  end: number
  barrier_epoch: number
  table_cells?: string[]
}

type ClaimSourceView = {
  originalText: string
  scanEnd: number
}

type ClaimToken = {
  type: "TEXT" | "PROTECTED_TEXT" | "SOFT_BREAK" | "HARD_BREAK"
  start: number
  end: number
  table_cells?: string[]
}

type ClaimCandidate = Omit<
  AtomizedResponseClaim,
  "claim_index" | "claim_count" | "previous_claim_key" | "next_claim_key"
>

type MarkdownBlockToken = {
  type: string
  raw: string
  items?: MarkdownBlockToken[]
  tokens?: MarkdownBlockToken[]
  header?: MarkdownTableCell[]
  rows?: MarkdownTableCell[][]
}

type MarkdownTableCell = {
  text: string
}

export type MarkdownBlockLexer = (source: string, options: { gfm: true }) => MarkdownBlockToken[]

export function atomizeResponseClaimsWithLexer(input: unknown, lexer: MarkdownBlockLexer): AtomizedResponseClaim[] {
  const source = createClaimSourceView(input, 8000)
  const tokens = tokenizeClaimSource(source, lexer)
  const spans = mergeClaimContinuations(source, segmentClaimTokens(source, tokens))
  const candidates = spans.flatMap((span) => toFactualCandidate(source, span)).slice(0, 50)
  const claimCount = candidates.length

  return candidates.map((candidate, index) => ({
    ...candidate,
    claim_index: index + 1,
    claim_count: claimCount,
    previous_claim_key: candidates[index - 1]?.key,
    next_claim_key: candidates[index + 1]?.key,
  }))
}

function createClaimSourceView(input: unknown, limit: number): ClaimSourceView {
  const originalText = normalizeClaimSource(input)
  let scanEnd = Math.min(originalText.length, limit)
  if (
    scanEnd > 0 &&
    scanEnd < originalText.length &&
    isHighSurrogate(originalText.charCodeAt(scanEnd - 1)) &&
    isLowSurrogate(originalText.charCodeAt(scanEnd))
  ) {
    scanEnd--
  }
  if (
    scanEnd > 0 &&
    scanEnd < originalText.length &&
    originalText[scanEnd - 1] === "\r" &&
    originalText[scanEnd] === "\n"
  ) {
    scanEnd--
  }
  return { originalText, scanEnd }
}

function isHighSurrogate(value: number) {
  return value >= 0xd800 && value <= 0xdbff
}

function isLowSurrogate(value: number) {
  return value >= 0xdc00 && value <= 0xdfff
}

function normalizeClaimSource(input: unknown): string {
  if (typeof input === "string") return input
  if (input === undefined || input === null) return ""
  try {
    const serialized = JSON.stringify(input)
    if (typeof serialized === "string") return serialized
  } catch {
    // Fall through to preserve a stable source for unsupported values.
  }
  try {
    return String(input)
  } catch {
    return "[unserializable response]"
  }
}

function stringPreview(input: unknown, limit = 400) {
  if (typeof input === "string") return input.slice(0, limit)
  if (input === undefined || input === null) return ""
  try {
    const serialized = JSON.stringify(input)
    if (typeof serialized === "string") return serialized.slice(0, limit)
  } catch {}
  try {
    return String(input).slice(0, limit)
  } catch {}
  return "[unserializable response]".slice(0, limit)
}

function tokenizeClaimSource(source: ClaimSourceView, lexer: MarkdownBlockLexer): ClaimToken[] {
  return lexMarkdownBlocks(source, lexer)
}

function lexMarkdownBlocks(source: ClaimSourceView, lexer: MarkdownBlockLexer): ClaimToken[] {
  const tokens: ClaimToken[] = []
  let cursor = 0

  try {
    const blocks = lexer(source.originalText.slice(0, source.scanEnd), { gfm: true })
    for (const block of blocks) {
      if (!block || typeof block.raw !== "string" || !block.raw) {
        tokens.push({ type: "HARD_BREAK", start: cursor, end: source.scanEnd })
        return tokens
      }
      const range = mapMarkedRawToSource(block.raw, source, cursor)
      if (!range) {
        tokens.push({ type: "HARD_BREAK", start: cursor, end: source.scanEnd })
        return tokens
      }
      adaptMarkdownBlock(source, block, range[0], range[1], tokens)
      cursor = range[1]
    }
  } catch {
    tokens.push({ type: "HARD_BREAK", start: cursor, end: source.scanEnd })
    return tokens
  }

  if (cursor < source.scanEnd) tokens.push({ type: "HARD_BREAK", start: cursor, end: source.scanEnd })
  return tokens
}

function mapMarkedRawToSource(raw: string, source: ClaimSourceView, cursor: number, limit = source.scanEnd) {
  let rawCursor = 0
  let sourceCursor = cursor

  while (rawCursor < raw.length) {
    if (sourceCursor >= limit) return undefined
    if (raw[rawCursor] === "\n") {
      if (source.originalText[sourceCursor] === "\n") {
        rawCursor++
        sourceCursor++
        continue
      }
      if (
        sourceCursor + 1 < limit &&
        source.originalText[sourceCursor] === "\r" &&
        source.originalText[sourceCursor + 1] === "\n"
      ) {
        rawCursor++
        sourceCursor += 2
        continue
      }
      return undefined
    }
    if (raw[rawCursor] !== source.originalText[sourceCursor]) return undefined
    rawCursor++
    sourceCursor++
  }

  return [cursor, sourceCursor] as [number, number]
}

function adaptMarkdownBlock(
  source: ClaimSourceView,
  block: MarkdownBlockToken,
  start: number,
  end: number,
  tokens: ClaimToken[],
) {
  if (block.type === "paragraph") {
    tokenizeParagraphRange(source, start, end, tokens)
    return
  }
  if (block.type === "list") {
    tokenizeMarkdownList(source, block, start, end, tokens)
    return
  }
  if (block.type === "table") {
    tokenizeMarkdownTable(source, block, start, end, tokens)
    return
  }
  tokens.push({ type: "HARD_BREAK", start, end })
}

function tokenizeMarkdownList(
  source: ClaimSourceView,
  block: MarkdownBlockToken,
  start: number,
  end: number,
  tokens: ClaimToken[],
) {
  if (!Array.isArray(block.items)) {
    tokens.push({ type: "HARD_BREAK", start, end })
    return
  }

  tokens.push({ type: "HARD_BREAK", start, end: start })
  let cursor = start
  for (const item of block.items) {
    const itemRange = mapNestedRawRange(source, item?.raw, cursor, end)
    if (!itemRange) {
      tokens.push({ type: "HARD_BREAK", start: cursor, end })
      return
    }
    if (cursor < itemRange.start) tokens.push({ type: "HARD_BREAK", start: cursor, end: itemRange.start })
    tokens.push({ type: "HARD_BREAK", start: itemRange.start, end: itemRange.start })
    tokenizeMarkdownListItem(source, item, itemRange.start, itemRange.end, tokens)
    tokens.push({ type: "HARD_BREAK", start: itemRange.end, end: itemRange.end })
    cursor = itemRange.end
  }
  if (cursor < end) tokens.push({ type: "HARD_BREAK", start: cursor, end })
  tokens.push({ type: "HARD_BREAK", start: end, end })
}

function tokenizeMarkdownListItem(
  source: ClaimSourceView,
  item: MarkdownBlockToken,
  start: number,
  end: number,
  tokens: ClaimToken[],
) {
  if (!Array.isArray(item.tokens)) {
    tokens.push({ type: "HARD_BREAK", start, end })
    return
  }

  let cursor = start
  for (const child of item.tokens) {
    const childRange = mapNestedRawRange(source, child?.raw, cursor, end)
    if (!childRange) {
      tokens.push({ type: "HARD_BREAK", start: cursor, end })
      return
    }
    if (cursor < childRange.start) tokens.push({ type: "HARD_BREAK", start: cursor, end: childRange.start })
    if (child.type === "paragraph" || child.type === "text") {
      tokenizeListParagraphRange(source, childRange.start, childRange.end, tokens)
    } else if (child.type === "list") {
      tokenizeMarkdownList(source, child, childRange.start, childRange.end, tokens)
    } else {
      tokens.push({ type: "HARD_BREAK", start: childRange.start, end: childRange.end })
    }
    cursor = childRange.end
  }
  if (cursor < end) tokens.push({ type: "HARD_BREAK", start: cursor, end })
}

function tokenizeListParagraphRange(source: ClaimSourceView, start: number, end: number, tokens: ClaimToken[]) {
  let lineStart = start
  let firstLine = true
  while (lineStart < end) {
    const newline = source.originalText.indexOf("\n", lineStart)
    const hasNewline = newline !== -1 && newline < end
    const lineStop = hasNewline ? newline + 1 : end
    const lineEnd = hasNewline && source.originalText[newline - 1] === "\r" ? newline - 1 : hasNewline ? newline : end
    if (!firstLine && lineStart < lineEnd && !/[ \t]/.test(source.originalText[lineStart]!)) {
      tokens.push({ type: "HARD_BREAK", start: lineStart, end: lineStart })
    }
    tokenizeInlineText(source, lineStart, lineEnd, tokens)
    pushLineBreak(tokens, lineEnd, lineStop)
    lineStart = lineStop
    firstLine = false
  }
}

function mapNestedRawRange(source: ClaimSourceView, raw: unknown, cursor: number, end: number) {
  if (typeof raw !== "string" || !raw) return undefined
  const rawLines = normalizedPhysicalLines(raw)
  let lineStart = cursor
  while (lineStart < end) {
    const range = mapNestedRawLinesAt(source, rawLines, lineStart, end)
    if (range) return range
    const line = sourcePhysicalLine(source.originalText, lineStart, end)
    if (line.stop <= lineStart) break
    lineStart = line.stop
  }
  return undefined
}

type NormalizedPhysicalLine = {
  text: string
  hasNewline: boolean
}

function normalizedPhysicalLines(raw: string): NormalizedPhysicalLine[] {
  const lines: NormalizedPhysicalLine[] = []
  let start = 0
  while (start < raw.length) {
    const newline = raw.indexOf("\n", start)
    if (newline === -1) {
      lines.push({ text: raw.slice(start), hasNewline: false })
      break
    }
    lines.push({ text: raw.slice(start, newline), hasNewline: true })
    start = newline + 1
  }
  return lines
}

function sourcePhysicalLine(input: string, start: number, limit: number) {
  const newline = input.indexOf("\n", start)
  const hasNewline = newline !== -1 && newline < limit
  const stop = hasNewline ? newline + 1 : limit
  const end = hasNewline && input[newline - 1] === "\r" ? newline - 1 : hasNewline ? newline : limit
  return { start, end, stop, hasNewline }
}

function mapNestedRawLinesAt(
  source: ClaimSourceView,
  rawLines: NormalizedPhysicalLine[],
  sourceStart: number,
  limit: number,
) {
  let lineStart = sourceStart
  let rangeStart: number | undefined
  let rangeEnd = sourceStart

  for (const rawLine of rawLines) {
    if (lineStart >= limit) return undefined
    const sourceLine = sourcePhysicalLine(source.originalText, lineStart, limit)
    const contentStart = nestedLineContentStart(source.originalText, sourceLine.start, sourceLine.end, rawLine.text)
    if (contentStart === undefined) return undefined
    if (rangeStart === undefined) rangeStart = contentStart
    if (rawLine.hasNewline) {
      if (!sourceLine.hasNewline) return undefined
      rangeEnd = sourceLine.stop
      lineStart = sourceLine.stop
    } else {
      rangeEnd = sourceLine.end
    }
  }

  return rangeStart === undefined ? undefined : { start: rangeStart, end: rangeEnd }
}

function nestedLineContentStart(input: string, start: number, end: number, rawLine: string) {
  const candidates = [start]
  let indentEnd = start
  while (indentEnd < end && /[ \t]/.test(input[indentEnd]!)) {
    indentEnd++
    candidates.push(indentEnd)
  }

  const marker = input.slice(indentEnd, end).match(/^(?:[-+*]|\d+[.)])[ \t]+/)
  if (marker) candidates.push(indentEnd + marker[0].length)

  for (const candidate of candidates) {
    if (candidate + rawLine.length !== end) continue
    if (input.slice(candidate, end) === rawLine) return candidate
  }
  return undefined
}

function tokenizeMarkdownTable(
  source: ClaimSourceView,
  block: MarkdownBlockToken,
  start: number,
  end: number,
  tokens: ClaimToken[],
) {
  if (!Array.isArray(block.header) || block.header.length < 2 || !Array.isArray(block.rows)) {
    tokens.push({ type: "HARD_BREAK", start, end })
    return
  }

  tokens.push({ type: "HARD_BREAK", start, end: start })
  const lines: Array<{ start: number; end: number; stop: number }> = []
  let lineStart = start
  while (lineStart < end) {
    const newline = source.originalText.indexOf("\n", lineStart)
    const hasNewline = newline !== -1 && newline < end
    const lineStop = hasNewline ? newline + 1 : end
    const lineEnd = hasNewline && source.originalText[newline - 1] === "\r" ? newline - 1 : hasNewline ? newline : end
    lines.push({ start: lineStart, end: lineEnd, stop: lineStop })
    lineStart = lineStop
  }

  if (lines.length < block.rows.length + 2) {
    tokens.push({ type: "HARD_BREAK", start, end })
    return
  }

  tokens.push({ type: "HARD_BREAK", start: lines[0]!.start, end: lines[1]!.stop })
  block.rows.forEach((row, index) => {
    const line = lines[index + 2]!
    const cells = markedTableRowCells(row, block.header!.length, source.originalText.slice(line.start, line.end))
    if (!cells) {
      tokens.push({ type: "HARD_BREAK", start: line.start, end: line.stop })
      return
    }
    tokens.push({ type: "HARD_BREAK", start: line.start, end: line.start })
    tokens.push({ type: "PROTECTED_TEXT", start: line.start, end: line.end, table_cells: cells })
    tokens.push({ type: "HARD_BREAK", start: line.end, end: line.stop })
  })
  const consumedLines = block.rows.length + 2
  if (consumedLines < lines.length) {
    tokens.push({ type: "HARD_BREAK", start: lines[consumedLines]!.start, end })
  }
  tokens.push({ type: "HARD_BREAK", start: end, end })
}

function markedTableRowCells(row: MarkdownTableCell[], expectedCount: number, rawLine: string) {
  if (!Array.isArray(row) || row.some((cell) => !cell || typeof cell.text !== "string")) return undefined
  const structured = row.map((cell) => cell.text)
  if (
    structured.length !== expectedCount ||
    structured.some((cell) => unclosedBacktickRun(cell)) ||
    hasUnescapedPipeInCodeSpan(rawLine)
  )
    return undefined
  return structured.map(normalizeMarkedTableCell)
}

function hasUnescapedPipeInCodeSpan(input: string) {
  for (let index = 0; index < input.length; index++) {
    if (input[index] !== "`" || isBackslashEscaped(input, index)) continue
    const delimiterLength = backtickRunLengthInText(input, index)
    const close = matchingBacktickRunInText(input, index + delimiterLength, delimiterLength)
    if (close === -1) continue
    for (let content = index + delimiterLength; content < close; content++) {
      if (input[content] === "|" && !isBackslashEscaped(input, content)) return true
    }
    index = close + delimiterLength - 1
  }
  return false
}

function backtickRunLengthInText(input: string, start: number) {
  let end = start
  while (input[end] === "`") end++
  return end - start
}

function matchingBacktickRunInText(input: string, start: number, length: number) {
  for (let index = start; index < input.length; index++) {
    if (input[index] !== "`" || isBackslashEscaped(input, index)) continue
    const candidateLength = backtickRunLengthInText(input, index)
    if (candidateLength === length) return index
    index += candidateLength - 1
  }
  return -1
}

function isBackslashEscaped(input: string, index: number) {
  let slashCount = 0
  while (index > 0 && input[--index] === "\\") slashCount++
  return slashCount % 2 === 1
}

function unclosedBacktickRun(input: string) {
  let delimiter = 0
  let delimiterStart = -1
  for (let index = 0; index < input.length; index++) {
    if (input[index] !== "`") continue
    let end = index + 1
    while (input[end] === "`") end++
    const length = end - index
    if (!delimiter) {
      delimiter = length
      delimiterStart = index
    } else if (delimiter === length) {
      delimiter = 0
      delimiterStart = -1
    }
    index = end - 1
  }
  if (!delimiter) return undefined
  return { length: delimiter, start: delimiterStart }
}

function normalizeMarkedTableCell(input: string) {
  return input.trim().replace(/\*\*/g, "").replace(/`/g, "").trim()
}

function tokenizeParagraphRange(source: ClaimSourceView, start: number, end: number, tokens: ClaimToken[]) {
  let lineStart = start
  while (lineStart < end) {
    const newline = source.originalText.indexOf("\n", lineStart)
    const hasNewline = newline !== -1 && newline < end
    const lineStop = hasNewline ? newline + 1 : end
    const lineEnd = hasNewline && source.originalText[newline - 1] === "\r" ? newline - 1 : hasNewline ? newline : end
    tokenizeInlineText(source, lineStart, lineEnd, tokens)
    pushLineBreak(tokens, lineEnd, lineStop)
    lineStart = lineStop
  }
}

function tokenizeInlineText(source: ClaimSourceView, start: number, end: number, tokens: ClaimToken[]) {
  let textStart = start
  for (let index = start; index < end; index++) {
    if (source.originalText[index] !== "`") continue
    const delimiterLength = backtickRunLength(source, index, end)
    const close = matchingBacktickRun(source, index + delimiterLength, end, delimiterLength)
    if (close === -1) {
      if (textStart < index) tokens.push({ type: "TEXT", start: textStart, end: index })
      tokens.push({ type: "PROTECTED_TEXT", start: index, end })
      return
    }
    if (textStart < index) tokens.push({ type: "TEXT", start: textStart, end: index })
    tokens.push({ type: "PROTECTED_TEXT", start: index, end: close + delimiterLength })
    textStart = close + delimiterLength
    index = close + delimiterLength - 1
  }
  if (textStart < end) tokens.push({ type: "TEXT", start: textStart, end })
}

function backtickRunLength(source: ClaimSourceView, start: number, end: number) {
  let index = start
  while (index < end && source.originalText[index] === "`") index++
  return index - start
}

function matchingBacktickRun(source: ClaimSourceView, start: number, end: number, length: number) {
  for (let index = start; index < end; index++) {
    if (source.originalText[index] !== "`") continue
    const candidateLength = backtickRunLength(source, index, end)
    if (candidateLength === length) return index
    index += candidateLength - 1
  }
  return -1
}

function pushLineBreak(tokens: ClaimToken[], lineEnd: number, lineStop: number) {
  if (lineEnd < lineStop) tokens.push({ type: "SOFT_BREAK", start: lineEnd, end: lineStop })
}

function segmentClaimTokens(source: ClaimSourceView, tokens: ClaimToken[]): ClaimSpan[] {
  const spans: ClaimSpan[] = []
  const stack: string[] = []
  let spanStart: number | undefined
  let tableCells: string[] | undefined
  let barrierEpoch = 0

  for (const token of tokens) {
    if (token.type === "HARD_BREAK") {
      if (spanStart !== undefined && !stack.length) {
        pushTrimmedSpan(source.originalText, spanStart, token.start, barrierEpoch, spans, tableCells)
      }
      spanStart = undefined
      tableCells = undefined
      stack.length = 0
      barrierEpoch++
      continue
    }
    if (token.type === "SOFT_BREAK") {
      if (spanStart !== undefined && !stack.length) {
        pushTrimmedSpan(source.originalText, spanStart, token.start, barrierEpoch, spans, tableCells)
        spanStart = undefined
        tableCells = undefined
      }
      continue
    }
    if (spanStart === undefined) spanStart = token.start
    if (token.type === "PROTECTED_TEXT") {
      tableCells = token.table_cells ?? tableCells
      continue
    }
    for (let index = token.start; index < token.end; index++) {
      if (spanStart === undefined) spanStart = index
      const value = source.originalText[index]!
      if (isOpeningBracket(value)) {
        stack.push(value)
        continue
      }
      const expected = openingBracketFor(value)
      if (expected) {
        if (stack.at(-1) === expected) stack.pop()
        continue
      }
      if (stack.length || !isClaimTerminator(source.originalText, index, token.end)) continue
      pushTrimmedSpan(source.originalText, spanStart, index + 1, barrierEpoch, spans, tableCells)
      spanStart = undefined
      tableCells = undefined
    }
  }

  if (spanStart !== undefined && !stack.length) {
    pushTrimmedSpan(source.originalText, spanStart, source.scanEnd, barrierEpoch, spans, tableCells)
  }
  return spans
}

function isOpeningBracket(value: string) {
  return value === "(" || value === "（" || value === "[" || value === "［" || value === "{" || value === "｛"
}

function openingBracketFor(value: string) {
  return new Map([
    [")", "("],
    ["）", "（"],
    ["]", "["],
    ["］", "［"],
    ["}", "{"],
    ["｝", "｛"],
  ]).get(value)
}

function isClaimTerminator(source: string, index: number, end: number) {
  const value = source[index]!
  if (!/[。！？.!?；;]/.test(value)) return false
  if (value === "!" && source[index + 1] === "=") return false
  if (value !== ".") return true
  const previous = source[index - 1] ?? ""
  const next = index + 1 < end ? source[index + 1]! : ""
  return !(/[A-Za-z0-9_$]/.test(previous) && /[A-Za-z0-9_$]/.test(next))
}

function pushTrimmedSpan(
  input: string,
  start: number,
  end: number,
  barrierEpoch: number,
  output: ClaimSpan[],
  tableCells?: string[],
) {
  while (start < end && /\s/.test(input[start]!)) start++
  while (end > start && /\s/.test(input[end - 1]!)) end--
  if (start < end) output.push({ start, end, barrier_epoch: barrierEpoch, table_cells: tableCells })
}

function mergeClaimContinuations(source: ClaimSourceView, input: ClaimSpan[]) {
  const output: ClaimSpan[] = []
  for (const span of input) {
    if (/^[,，、:：)）\]］}｝]/.test(source.originalText.slice(span.start, span.end))) {
      const previous = output.at(-1)
      if (previous && previous.barrier_epoch === span.barrier_epoch) {
        output[output.length - 1] = { ...previous, end: span.end }
      }
      continue
    }
    output.push(span)
  }
  return output
}

function toFactualCandidate(source: ClaimSourceView, span: ClaimSpan): ClaimCandidate[] {
  const rawText = source.originalText.slice(span.start, span.end)
  const normalized = rawText.replace(/\s+/g, " ").trim()
  if (isBrokenClaimFragment(normalized) || isNonFactualResponseClaim(normalized)) return []
  const textLength = normalized.replace(/\s/g, "").length
  const hasFactSignal = /\d|[/\\][\w.-]+|[A-Za-z_$][\w$]*\(|[A-Za-z_$][\w$]*\.[A-Za-z_$]/.test(normalized)
  const hasAtomicVerdict =
    /^(?:修改|修复|实现)?(?:全部|所有)?(?:测试|检查|验证|用例)?(?:均|都|已)?(?:通过|失败|成功|完成)[。.!?]?$/.test(
      normalized,
    )
  if (textLength < 6 && !hasFactSignal && !hasAtomicVerdict) return []

  const tableFact = span.table_cells ? markdownTableFactClaim(span.table_cells) : undefined
  const text = tableFact?.text ?? normalized
  const canonicalText = tableFact?.canonical_text
  const semanticStatement = canonicalText ?? text
  const semanticHash = stableHash(semanticStatement)
  const byteRange: [number, number] = [
    Buffer.byteLength(source.originalText.slice(0, span.start)),
    Buffer.byteLength(source.originalText.slice(0, span.end)),
  ]

  return [
    {
      key: `claim_${stableHash(
        `${tableFact?.claim_format ?? "factual_claim"}:${semanticStatement}:${byteRange[0]}:${byteRange[1]}`,
      ).slice(0, 12)}`,
      text,
      raw_text: rawText,
      canonical_text: canonicalText,
      claim_format: tableFact?.claim_format ?? "factual_claim",
      table_cells: tableFact?.table_cells,
      table_subject: tableFact?.table_subject,
      table_values: tableFact?.table_values,
      claim_group_id: `claim_group_${semanticHash.slice(0, 12)}`,
      source_byte_range: byteRange,
      atomization_status: "atomic",
      atomization_reason: "complete_merged_statement",
    },
  ]
}

function stableHash(input: string) {
  return createHash("sha256").update(input).digest("hex")
}

export function isNonFactualResponseClaim(input: unknown) {
  const text = typeof input === "string" ? input : stringPreview(input)
  if (/__TRACE_PROTECTED_\d+__/.test(text)) return true
  if (
    /^\s*[+-]\s+/.test(text) &&
    (/(?:\b(?:const|let|var|return|import|export)\b|[{};=]|=>)/.test(text) ||
      /^\s*[+-]\s*(?:async\s+)?function\s+\w+\s*\(/.test(text))
  )
    return true
  const normalized = text
    .trim()
    .replace(/^#+\s*/, "")
    .replace(/\*\*/g, "")
    .replace(/^[-*]\s*/, "")
    .replace(/^["'`*_]+|["'`*_]+$/g, "")
    .replace(/[。.!?；;:：]+$/g, "")
    .trim()
    .toLowerCase()
  if (!normalized) return true
  if (/^\d+[.)]?$/.test(normalized)) return true
  if (/^(好的|可以|下面|因此|总结|结论)$/.test(normalized)) return true
  if (/^(summary|here'?s the summary|final summary|result summary)$/.test(normalized)) return true
  if (
    /^(冲突点|冲突总结|修复完成|完成|最终答案|最终结果|根因分析|问题定位|验证结果|npm test 结果|改动说明|变更摘要|执行结果|实现结果|设计约束|修改文件|额外通用性检查|额外通用性检查结果)$/.test(
      normalized,
    )
  )
    return true
  if (
    /^(goal|constraints?\s*&?\s*preferences?|progress|done|in progress|blocked|key decisions|next steps|critical context|relevant files)$/.test(
      normalized,
    )
  )
    return true
  if (
    /^(?:[一二三四五六七八九十]+[、.]\s*)?(需求影响分析报告|资料交叉比对|一致性结论|依据来源|使用的上下文资料|上下文资料|压缩链路验证汇总)$/.test(
      normalized,
    )
  )
    return true
  if (
    /^(?:[一二三四五六七八九十]+[、.]\s*)?(各来源的关键结论对比|关键结论对比|子\s*agent\s*独立总结|子 agent 独立总结|是否需要改动|一致性判断)$/.test(
      normalized,
    )
  )
    return true
  if (/^计算推导(?:\s*[（(].*[）)])?$/.test(normalized)) return true
  if (/^(?:(?:mcp\s*)?返回的事实|mcp facts?|facts?|修改点|改动点|变更点|changes?|changed files?)$/i.test(normalized))
    return true
  if (
    /^(design constraints?|design constraints honored|verification results?|implementation summary|change summary)$/.test(
      normalized,
    )
  )
    return true
  if (/^[\w\s-]+存在不一致$/.test(normalized)) return true
  if (/^(no further steps needed|nothing else needed|no next steps needed)$/.test(normalized)) return true
  if (/^(以下是|下面是|这里是).*(总结|结论|报告)$/.test(normalized)) return true
  return false
}

function markdownTableFactClaim(cells: string[]) {
  if (cells.length < 2) return undefined
  const subject = cells[0]?.trim()
  if (!subject) return undefined
  const values = cells
    .slice(1)
    .map((cell) => cell.trim())
    .filter((cell) => cell && !/^[-—]+$/.test(cell))
  if (!values.length) return undefined
  const canonicalText = `${subject}: ${values.join(" | ")}`
  return {
    text: canonicalText,
    canonical_text: canonicalText,
    claim_format: "table_fact" as const,
    table_cells: cells,
    table_subject: subject,
    table_values: values,
  }
}

export function isBrokenClaimFragment(input: unknown) {
  const text = typeof input === "string" ? input.trim() : stringPreview(input, 200).trim()
  if (!text) return false
  if (/^\d+[%)]?[。.!?；;]?$/.test(text)) return true
  if (/^(?:\.\d+|[A-Za-z0-9_$-]+\.)$/.test(text)) return true
  if (/^(?:mjs|ts|tsx|js|jsx|json|md|yaml|yml|go|rs|py|java|cc|cpp|h|hpp)\b[。.!?；;)]?$/i.test(text)) return true
  if (/^[,，。.!?；;:：)\]]+$/.test(text)) return true
  return false
}
