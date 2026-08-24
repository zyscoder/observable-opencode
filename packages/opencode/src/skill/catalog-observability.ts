export type SkillSourceFamily = "built_in" | "opencode" | "claude" | "agents" | "configured"
export type SkillSourceScope = "runtime" | "global" | "project" | "external" | "configured"

export type SkillCatalogCandidate = {
  name: string
  description?: string
  location: string
  status: "loaded" | "parse_failed"
  error?: string
  source_family?: SkillSourceFamily
  source_scope?: SkillSourceScope
  content_hash?: string
}

export type SkillCatalogSnapshot = {
  candidates: SkillCatalogCandidate[]
  selected: Array<{ name: string; location: string }>
  conflicts: Array<{
    name: string
    candidate_locations: string[]
    selected_location: string
    resolution: "observed_runtime_winner"
  }>
  parse_failures: Array<{ location: string; error: string }>
}

export type SkillCatalogExposure = SkillCatalogSnapshot & {
  permission_evaluations: Array<{ name: string; action: string }>
  exposed: Array<{ name: string; description?: string; location: string }>
  skill_tool_available: boolean
}

export function classifySkillLocation(location: string): {
  family: SkillSourceFamily
  scope: SkillSourceScope
} {
  const normalized = location.replaceAll("\\", "/")
  if (normalized === "<built-in>") return { family: "built_in", scope: "runtime" }
  if (normalized.includes("/.config/opencode/")) return { family: "opencode", scope: "global" }
  if (normalized.includes("/.opencode/")) return { family: "opencode", scope: "project" }
  if (normalized.includes("/.claude/")) return { family: "claude", scope: "project" }
  if (normalized.includes("/.agents/")) return { family: "agents", scope: "external" }
  return { family: "configured", scope: "configured" }
}

function observedCandidate(candidate: SkillCatalogCandidate): SkillCatalogCandidate {
  const source = classifySkillLocation(candidate.location)
  return {
    ...candidate,
    source_family: candidate.source_family ?? source.family,
    source_scope: candidate.source_scope ?? source.scope,
  }
}

export function buildSkillCatalogSnapshot(input: {
  candidates: SkillCatalogCandidate[]
  selectedLocations: Record<string, string>
}): SkillCatalogSnapshot {
  const candidates = input.candidates
    .map(observedCandidate)
    .toSorted((a, b) => a.location.localeCompare(b.location) || a.name.localeCompare(b.name))
  const selected = Object.entries(input.selectedLocations)
    .map(([name, location]) => ({ name, location }))
    .toSorted((a, b) => a.name.localeCompare(b.name) || a.location.localeCompare(b.location))
  const grouped = new Map<string, string[]>()
  for (const candidate of candidates) {
    if (candidate.status !== "loaded" || !candidate.name) continue
    const locations = grouped.get(candidate.name) ?? []
    locations.push(candidate.location)
    grouped.set(candidate.name, locations)
  }
  const conflicts = [...grouped.entries()]
    .filter(([, locations]) => locations.length > 1)
    .map(([name, locations]) => ({
      name,
      candidate_locations: locations.toSorted(),
      selected_location: input.selectedLocations[name] ?? "",
      resolution: "observed_runtime_winner" as const,
    }))
    .toSorted((a, b) => a.name.localeCompare(b.name))
  const parse_failures = candidates
    .filter((candidate) => candidate.status === "parse_failed")
    .map((candidate) => ({ location: candidate.location, error: candidate.error ?? "unknown parse failure" }))

  return { candidates, selected, conflicts, parse_failures }
}

export function buildSkillCatalogExposure(input: {
  snapshot: SkillCatalogSnapshot
  permissionActions: Record<string, string>
  exposedNames: string[]
  skillToolAvailable: boolean
}): SkillCatalogExposure {
  const selectedByName = new Map(input.snapshot.selected.map((item) => [item.name, item]))
  const candidatesByLocation = new Map(input.snapshot.candidates.map((item) => [item.location, item]))
  const permission_evaluations = input.snapshot.selected
    .map((item) => ({ name: item.name, action: input.permissionActions[item.name] ?? "unknown" }))
    .toSorted((a, b) => a.name.localeCompare(b.name))
  const exposed = [...new Set(input.exposedNames)]
    .map((name) => {
      const selected = selectedByName.get(name)
      if (!selected) return undefined
      const candidate = candidatesByLocation.get(selected.location)
      return {
        name,
        description: candidate?.description,
        location: selected.location,
      }
    })
    .filter((item): item is NonNullable<typeof item> => item !== undefined)
    .toSorted((a, b) => a.name.localeCompare(b.name))

  return {
    ...input.snapshot,
    permission_evaluations,
    exposed,
    skill_tool_available: input.skillToolAvailable,
  }
}
