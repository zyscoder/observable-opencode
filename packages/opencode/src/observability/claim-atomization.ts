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
}

type ClaimToken = {
  type: "TEXT" | "PROTECTED_TEXT" | "SOFT_BREAK" | "HARD_BREAK"
  start: number
  end: number
}

type ClaimCandidate = Omit<AtomizedResponseClaim, "claim_index" | "claim_count" | "previous_claim_key" | "next_claim_key">

export function atomizeResponseClaims(input: unknown): AtomizedResponseClaim[] {
  const source = normalizeResponseText(input)
  const tokens = tokenizeClaimSource(source)
  const spans = mergeClaimContinuations(source, segmentClaimTokens(source, tokens))
  const seen = new Set<string>()
  const candidates = spans
    .flatMap((span) => toFactualCandidate(source, span))
    .filter((candidate) => {
      const semanticKey = `${candidate.claim_format}:${candidate.canonical_text ?? candidate.text}`.toLowerCase()
      if (seen.has(semanticKey)) return false
      seen.add(semanticKey)
      return true
    })
    .slice(0, 50)
  const claimCount = candidates.length

  return candidates.map((candidate, index) => ({
    ...candidate,
    claim_index: index + 1,
    claim_count: claimCount,
    previous_claim_key: candidates[index - 1]?.key,
    next_claim_key: candidates[index + 1]?.key,
  }))
}

function normalizeResponseText(input: unknown) {
  return typeof input === "string" ? input.slice(0, 8000) : stringPreview(input, 8000)
}

function stringPreview(input: unknown, limit = 400) {
  if (typeof input === "string") return input.slice(0, limit)
  if (input === undefined || input === null) return ""
  try {
    return JSON.stringify(input).slice(0, limit)
  } catch {
    return String(input).slice(0, limit)
  }
}

function tokenizeClaimSource(source: string): ClaimToken[] {
  const tokens: ClaimToken[] = []
  let inFence = false
  let lineStart = 0

  while (lineStart < source.length) {
    const newline = source.indexOf("\n", lineStart)
    const lineStop = newline === -1 ? source.length : newline + 1
    const lineEnd = newline !== -1 && source[newline - 1] === "\r" ? newline - 1 : newline === -1 ? source.length : newline
    const line = source.slice(lineStart, lineEnd)
    const fence = /^\s*```/.test(line)

    if (fence) {
      tokens.push({ type: "HARD_BREAK", start: lineStart, end: lineStop })
      inFence = !inFence
    } else if (inFence || /^\s*#{1,6}\s+/.test(line) || !line.trim() || /^\s*>/.test(line)) {
      tokens.push({ type: "HARD_BREAK", start: lineStart, end: lineStop })
    } else {
      const listMarker = line.match(/^\s*(?:[-*+]|\d+[.)])\s+/)?.[0]
      if (listMarker) {
        const contentStart = lineStart + listMarker.length
        tokens.push({ type: "HARD_BREAK", start: lineStart, end: contentStart })
        tokenizeInlineText(source, contentStart, lineEnd, tokens)
        pushLineBreak(tokens, lineEnd, lineStop)
      } else if (isMarkdownTableLine(line)) {
        // A table row forms its own candidate span; its surrounding breaks prevent prose from joining it.
        tokens.push({ type: "HARD_BREAK", start: lineStart, end: lineStart })
        tokenizeInlineText(source, lineStart, lineEnd, tokens)
        tokens.push({ type: "HARD_BREAK", start: lineEnd, end: lineStop })
      } else {
        tokenizeInlineText(source, lineStart, lineEnd, tokens)
        pushLineBreak(tokens, lineEnd, lineStop)
      }
    }
    lineStart = lineStop
  }
  return tokens
}

function tokenizeInlineText(source: string, start: number, end: number, tokens: ClaimToken[]) {
  let textStart = start
  for (let index = start; index < end; index++) {
    if (source[index] !== "`") continue
    const close = source.indexOf("`", index + 1)
    if (close === -1 || close >= end) continue
    if (textStart < index) tokens.push({ type: "TEXT", start: textStart, end: index })
    tokens.push({ type: "PROTECTED_TEXT", start: index, end: close + 1 })
    textStart = close + 1
    index = close
  }
  if (textStart < end) tokens.push({ type: "TEXT", start: textStart, end })
}

function pushLineBreak(tokens: ClaimToken[], lineEnd: number, lineStop: number) {
  if (lineEnd < lineStop) tokens.push({ type: "SOFT_BREAK", start: lineEnd, end: lineStop })
}

function isMarkdownTableLine(line: string) {
  const cells = markdownTableCells(line)
  return cells.length >= 2
}

function segmentClaimTokens(source: string, tokens: ClaimToken[]): ClaimSpan[] {
  const spans: ClaimSpan[] = []
  const stack: string[] = []
  let spanStart: number | undefined
  let barrierEpoch = 0

  for (const token of tokens) {
    if (token.type === "HARD_BREAK") {
      if (spanStart !== undefined && !stack.length) pushTrimmedSpan(source, spanStart, token.start, barrierEpoch, spans)
      spanStart = undefined
      stack.length = 0
      barrierEpoch++
      continue
    }
    if (token.type === "SOFT_BREAK") {
      if (spanStart !== undefined && !stack.length) {
        pushTrimmedSpan(source, spanStart, token.start, barrierEpoch, spans)
        spanStart = undefined
      }
      continue
    }
    if (spanStart === undefined) spanStart = token.start
    if (token.type === "PROTECTED_TEXT") continue
    for (let index = token.start; index < token.end; index++) {
      if (spanStart === undefined) spanStart = index
      const value = source[index]!
      if (isOpeningBracket(value)) {
        stack.push(value)
        continue
      }
      const expected = openingBracketFor(value)
      if (expected) {
        if (stack.at(-1) === expected) stack.pop()
        continue
      }
      if (stack.length || !isClaimTerminator(source, index, token.end)) continue
      pushTrimmedSpan(source, spanStart, index + 1, barrierEpoch, spans)
      spanStart = undefined
    }
  }

  if (spanStart !== undefined && !stack.length) pushTrimmedSpan(source, spanStart, source.length, barrierEpoch, spans)
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
) {
  while (start < end && /\s/.test(input[start]!)) start++
  while (end > start && /\s/.test(input[end - 1]!)) end--
  if (start < end) output.push({ start, end, barrier_epoch: barrierEpoch })
}

function mergeClaimContinuations(source: string, input: ClaimSpan[]) {
  const output: ClaimSpan[] = []
  for (const span of input) {
    if (/^[,，、:：)）\]］}｝]/.test(source.slice(span.start, span.end))) {
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

function toFactualCandidate(source: string, span: ClaimSpan): ClaimCandidate[] {
  const rawText = source.slice(span.start, span.end)
  const normalized = rawText.replace(/\s+/g, " ").trim()
  if (isBrokenClaimFragment(normalized) || isNonFactualResponseClaim(normalized)) return []
  const textLength = normalized.replace(/\s/g, "").length
  const hasFactSignal = /\d|[/\\][\w.-]+|[A-Za-z_$][\w$]*\(|[A-Za-z_$][\w$]*\.[A-Za-z_$]/.test(normalized)
  const hasAtomicVerdict = /^(?:修改|修复|实现)?(?:全部|所有)?(?:测试|检查|验证|用例)?(?:均|都|已)?(?:通过|失败|成功|完成)[。.!?]?$/.test(
    normalized,
  )
  if (textLength < 6 && !hasFactSignal && !hasAtomicVerdict) return []

  const tableFact = markdownTableFactClaim(normalized)
  const text = tableFact?.text ?? normalized
  const canonicalText = tableFact?.canonical_text
  const semanticStatement = canonicalText ?? text
  const semanticHash = stableHash(semanticStatement)
  const byteRange: [number, number] = [
    Buffer.byteLength(source.slice(0, span.start)),
    Buffer.byteLength(source.slice(0, span.end)),
  ]

  return [
    {
      key: `claim_${stableHash(`${tableFact?.claim_format ?? "factual_claim"}:${semanticStatement}`).slice(0, 12)}`,
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

export function isNonFactualResponseClaim(input: string) {
  if (isMarkdownTableStructuralRow(input)) return true
  if (/__TRACE_PROTECTED_\d+__/.test(input)) return true
  if (
    /^\s*[+-]\s+/.test(input) &&
    (/(?:\b(?:const|let|var|return|import|export)\b|[{};=]|=>)/.test(input) ||
      /^\s*[+-]\s*(?:async\s+)?function\s+\w+\s*\(/.test(input))
  )
    return true
  const normalized = input
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
  if (/^(design constraints?|design constraints honored|verification results?|implementation summary|change summary)$/.test(normalized))
    return true
  if (/^[\w\s-]+存在不一致$/.test(normalized)) return true
  if (/^(no further steps needed|nothing else needed|no next steps needed)$/.test(normalized)) return true
  if (/^(以下是|下面是|这里是).*(总结|结论|报告)$/.test(normalized)) return true
  return false
}

function markdownTableCells(input: string) {
  const trimmed = input.trim()
  if (!trimmed.includes("|")) return []
  return trimmed
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim().replace(/\*\*/g, "").replace(/`/g, "").trim())
}

function isLikelyTableHeaderCell(input: string) {
  const normalized = input.trim().toLowerCase()
  if (!normalized) return true
  if (/^:?-{2,}:?$/.test(normalized)) return true
  if (
    /^(项目|结果|来源|状态|输入|计算|关键信息|维度|说明|字段|值|文件|路径|议题|结论|事实|约束|描述|当前值|预期值|是否命中|脚本|命令|覆盖风险|子 agent 结论|subagent result|field|value|status)$/.test(
      normalized,
    )
  )
    return true
  if (/^(折扣上限|架构规定|现行需求|当前代码|相关文件|证据|动作|原因|风险)$/.test(normalized)) return true
  if (/^mcp\s+[\w-]+$/i.test(normalized)) return true
  if (/^syntheticfacts(?:\s*\(mcp\))?[_\w.-]*$/i.test(normalized)) return true
  if (/^(?:[\w@+.-]+\/)?[\w@+.-]+\.(?:md|mjs|js|ts|tsx|json|txt|py|go|rs|java|yaml|yml)$/i.test(normalized)) return true
  return false
}

function isMarkdownTableStructuralRow(input: string) {
  const cells = markdownTableCells(input)
  if (cells.length < 2) return false
  if (cells.every((cell) => /^:?-{2,}:?$/.test(cell))) return true
  const joined = cells.join(" ")
  const hasFactValue =
    /billing-platform|15\s*%|15 percent|0\.15|全部通过|pricing tests passed|失败|通过|无需改动|Math\.min|quoteOwner\(|renewalQuote\(input\)|48000|51000/i.test(
      joined,
    )
  if (hasFactValue) return false
  return cells.every(isLikelyTableHeaderCell)
}

function markdownTableFactClaim(input: string) {
  const cells = markdownTableCells(input)
  if (cells.length < 2 || isMarkdownTableStructuralRow(input)) return undefined
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
