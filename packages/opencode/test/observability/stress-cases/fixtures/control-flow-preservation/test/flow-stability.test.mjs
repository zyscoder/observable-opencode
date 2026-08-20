import assert from "node:assert/strict"
import { renewalQuote } from "../src/pricing.mjs"

assert.equal(renewalQuote({ baseCents: 1000, seats: 120, loyaltyYears: 1, region: "CN", enterprise: false }), 85000)
assert.equal(renewalQuote({ baseCents: 1000, seats: 120, loyaltyYears: 3, region: "CN", enterprise: true }), 85000)
assert.equal(renewalQuote({ baseCents: 500, seats: 8, loyaltyYears: 2, region: "NA", enterprise: false }), 3960)
assert.equal(renewalQuote({}), 0)
assert.equal(renewalQuote({ baseCents: -200, seats: 10 }), 0)

console.log("flow stability tests passed")
