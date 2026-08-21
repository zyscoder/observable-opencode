import assert from "node:assert/strict"
import { renewalQuote } from "../src/pricing.mjs"

assert.equal(renewalQuote({ baseCents: 1000, seats: 50, loyaltyYears: 4, region: "CN", enterprise: false }), 42500)
assert.equal(renewalQuote({ baseCents: 1000, seats: 49, loyaltyYears: 4, region: "EU", enterprise: false }), 43120)
assert.equal(renewalQuote({ baseCents: 1000, seats: 50, loyaltyYears: 1, region: "NA", enterprise: false }), 44500)
assert.equal(renewalQuote({ baseCents: 1000, seats: 10, loyaltyYears: 1, region: "EU", enterprise: true }), 9700)

console.log("pricing tests passed")
