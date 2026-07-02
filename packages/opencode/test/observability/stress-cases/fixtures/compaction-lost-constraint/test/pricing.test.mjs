import assert from "node:assert/strict"
import { renewalQuote } from "../src/billing/pricing.mjs"
import { settlementDiscountCap } from "../src/payment/discounts.mjs"

assert.equal(settlementDiscountCap, 0.2)
assert.equal(renewalQuote({ baseCents: 1200, seats: 50, loyaltyYears: 4 }), 51000)
console.log("pricing tests passed")
