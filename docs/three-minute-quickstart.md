# Three-minute quickstart

Get a demo key without email, install the Python SDK, and verify a published
receipt. Use Bash with `curl`, `jq`, and Python 3.11 or newer in an activated
virtual environment. You do not need to clone the repository for these steps.

## Get a demo key and install

```bash
AMW_DEMO_KEY=$(curl -X POST https://api.agentmiddleware.dev/v1/demo/keys -fsS | jq -er '.api_key | strings | select(length > 0)') &&
export AMW_API_KEY="$AMW_DEMO_KEY" &&
pip install amw-sdk
```

The endpoint accepts a request without a body or email and returns `api_key`.
The commands capture that value without printing it; installation proceeds only
if extraction succeeds. Keep the key private and do not paste it into issues or
chat. Demo keys are limited to the demo sandbox.

This repository does not yet include a retry example that runs with only
`AMW_API_KEY`. The existing Stripe refund example needs `STRIPE_SECRET_KEY` and
gateway dependencies, so it is not part of this quickstart. The commands above
set up the SDK; they do not execute a tool or create a new receipt.

## Verify the published sample offline

Inspect the [sample proof](https://agentmiddleware.dev/proof), or download its
existing receipt and public key snapshot and run the verifier:

```bash
pip install 'amw-sdk[verify]'
curl -fsSLo receipt.json https://www.agentmiddleware.dev/proof/receipt.json
curl -fsSLo trust-keys.json https://www.agentmiddleware.dev/proof/trust-keys.json
amw verify --bundle receipt.json --keys trust-keys.json --expect-issuer https://api.thisisatest.tech
```

Successful verification reports `VERIFIED`. After the downloads, verification
runs offline and needs no API key. This historical, self-issued receipt checks
against the supplied public key snapshot. It does not establish independent
issuer trust or prove a downstream effect.

AMW permits at most one debit per accepted idempotency key on the configured
upstream path. A receipt records gateway evidence, with the limits described on
the sample proof page.

See [dependency health](https://api.agentmiddleware.dev/health/dependencies) and
the [OpenAPI schema](https://api.agentmiddleware.dev/openapi.json). For local
development, optionally [clone the source](https://github.com/agentmiddleware/agent-middleware).
