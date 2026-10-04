# Licensing

This repository uses two licenses, split by directory. Nothing here is legal
advice; if your use depends on the details, read the license texts.

| Path | License | Kind | License file |
|---|---|---|---|
| `gateway/` | Functional Source License 1.1, Apache-2.0 future license (FSL-1.1-ALv2) | **Fair Source** (source-available, not OSI open source) | [`gateway/LICENSE.md`](gateway/LICENSE.md) |
| Everything else: `spec/`, `verifier/`, `sdk/python/`, `sdk/typescript/`, `integrations/`, `examples/`, and root files | Apache License 2.0 | Open source (OSI-approved) | [`LICENSE`](LICENSE) |

A file's license is the license of the directory it sits in.

## The gateway (`gateway/`): Fair Source

You may use, copy, modify, and redistribute the gateway for any purpose
**except a Competing Use**: making it available to others in a commercial
product or service that substitutes for the gateway or for another Agent
Middleware product or service that uses it, or that offers the same or
substantially similar functionality. The license text has the exact wording and
its carve-outs (internal use, non-commercial education and research, and
professional services to a licensee are all permitted).

In practice:

- **Yes:** run it in production in front of your own agents and tools; self-host
  it on your own infrastructure; modify it; fork it; read it in a security review.
- **Yes:** a consultancy deploying it for a client who uses it for themselves.
- **No (for two years per release):** selling a hosted or embedded "permit and
  receipt gateway" service built on this code.

**Each version becomes Apache-2.0 on the second anniversary of the date it is
made available.** From then on the Competing Use restriction no longer applies to
that version.

Why: the parts that should spread (the format and the verifiers) are fully
open; the gateway is protected from a hosted clone for a limited time, using a
standard license text with no custom parameters. FSL was written by Sentry and
is used by other Fair Source projects; see <https://fsl.software/> and
<https://fair.io/>.

We do not call the gateway "open source." It is Fair Source.

## Everything else: Apache-2.0

The spec, test vectors, verifiers, SDKs, framework integrations, and examples
are Apache-2.0 so that anyone (other gateways, auditors, framework maintainers)
can embed a verifier or implement the format without a license review. Apache-2.0
includes an express patent license from contributors.

## Contributions

Contributions are licensed under the license of the directory they change, and
are signed off under the Developer Certificate of Origin (see
[`CONTRIBUTING.md`](CONTRIBUTING.md)). There is no CLA.

## Names

The licenses cover code and text. They do not grant permission to use the names
"Agent Middleware" or "AMW", or any logo, in a way that suggests endorsement or
that a modified version is the original project.

## Commercial license

For a use the FSL does not allow before a release converts, open an issue titled
"Commercial license" or contact the maintainer through
<https://github.com/PetrefiedThunder>.
