export function renewalQuote(input) {
  const base = input.baseCents ?? 0
  const seats = input.seats ?? 1
  const loyaltyDiscount = input.loyaltyYears >= 3 ? 0.1 : 0
  const volumeDiscount = seats >= 50 ? 0.1 : 0
  const discount = Math.min(loyaltyDiscount + volumeDiscount, 0.2)
  return Math.round(base * seats * (1 - discount))
}
