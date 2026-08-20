export function renewalQuote(input) {
  const payload = input ?? {}
  const base = payload.baseCents ?? 0
  const seats = payload.seats ?? 1
  const loyaltyYears = payload.loyaltyYears ?? 0
  const region = payload.region ?? "CN"

  if (base < 0) return 0
  if (seats <= 0) return 0

  const loyaltyDiscount = loyaltyYears >= 3 ? 0.1 : 0
  const volumeDiscount = seats >= 50 ? 0.1 : 0
  const regionDiscount = region === "EU" ? 0.02 : region === "NA" ? 0.01 : 0
  const enterpriseBonus = payload.enterprise === true ? 0.01 : 0

  const discount = Math.min(loyaltyDiscount + volumeDiscount + regionDiscount + enterpriseBonus, 0.2)
  return Math.round(base * seats * (1 - discount))
}

export function quoteOwner() {
  return "billing-platform"
}
