---
title: Credits
section: Concepts
order: 4
description: Credits meter governed Calls against a wallet and Permit budget.
---

# Credits

Credits are the internal metering unit for a governed Call. They are held in a wallet and limited by the Permit's `max_credits` value. They are not settlement, invoices, or payment rails.

The tool catalog reports the configured price through `annotations.creditsPerCall`. Before a Call runs, the gateway checks and reserves that price against the Permit budget. The reservation uses an atomic guarded update, including for concurrent Calls. A denied Call is not charged.

For the configured upstream MCP tool, a timeout or disconnect after the durable dispatch checkpoint can leave a charged `delivery_uncertain` outcome. The gateway cannot infer that the tool did nothing. Later reconciliation can add a separate refund ledger entry when supported evidence and operator settings allow it. The original Receipt still records the charge at signing time.

Inspect [Calls and request keys](/docs/calls-and-request-keys/) for retry behavior, [Receipts](/docs/receipts/) for signed cost evidence, and [Limits](/docs/limits/) before using Credits to represent a business cost.
