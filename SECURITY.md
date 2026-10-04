# Security Policy

## Reporting a vulnerability

Please report suspected security vulnerabilities privately through the repository's GitHub Security Advisories feature. Do not open a public issue, post exploit details, or disclose a vulnerability publicly before a fix or mitigation has been coordinated.

Include the affected component and version or commit, a clear description, reproduction steps or a proof of concept when safe, the potential impact, and any suggested mitigation. Please avoid including real credentials, personal data, or production data.

The hosted AMW service is proprietary and is not in this repository. Do not include hosted-service secrets or customer data in a report. If the repository owner publishes a separate reporting route for the hosted service, use that route for hosted-only issues.

## Response expectations

AMW is maintained by a solo founder and is pre-revenue, so response times may vary. The maintainer aims to:

- Acknowledge a report within 5 business days
- Provide an initial assessment or request for more information within 10 business days
- Give an update at least every 10 business days while the report is being investigated
- Coordinate disclosure timing after a fix or mitigation is available

These are targets, not guarantees. Reports that require substantial investigation or coordination may take longer.

## Scope

This policy covers security issues in the public repository, including:

- The protocol and normative material in `spec/`
- The verifier
- The Python and TypeScript SDKs
- Published integrations and examples
- The Fair Source gateway in `gateway/`
- Build, test, and release configuration committed to the repository

Examples of issues worth reporting include permit or scope bypasses, receipt signature or verification flaws, authentication or authorization bypasses, unsafe handling of secrets, and vulnerabilities that enable an irreversible tool call without the intended authorization.

Reports about general bugs, feature requests, exposed test data, or unsupported deployments belong in the normal issue tracker unless they create a security risk.

## No bounty

AMW currently offers no security bounty or other monetary reward. We appreciate responsible reports and will credit reporters when they request credit and it is safe to do so.

## Safe harbor

Good-faith research that follows this policy, avoids privacy violations and service disruption, and stops when a vulnerability is confirmed is welcome. Please do not access, modify, or exfiltrate data that does not belong to you.
