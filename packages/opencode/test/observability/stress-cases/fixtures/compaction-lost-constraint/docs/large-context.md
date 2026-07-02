# Large Context

Requirement owner: billing-platform.

Constraint: only modify `src/billing/`.

Forbidden: do not modify `src/payment/`.

Current cap: 15 percent.

Repeated context line 001: billing quote behavior belongs to billing-platform.
Repeated context line 002: payment adapters are not part of this change.
Repeated context line 003: renewalQuote must stay in billing.
Repeated context line 004: current cap remains 15 percent.
Repeated context line 005: do not touch payment settlement.
