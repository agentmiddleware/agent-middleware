# MCP tool metadata — honesty fields (Phase 0)

Autonomous clients use `GET /mcp/tools.json` as the canonical tool list. Every
tool in that manifest includes **annotations** beyond pricing:

| Field | Type | Meaning |
|-------|------|--------|
| `simulation` | boolean | `true` if this tool’s billing category maps to a runtime pillar that is **currently simulated** (`is_simulation(category)`). |
| `integrationStatus` | string | `simulated` — pillar is synthetic; `integrated` — pillar flag is off (non-Postgres integration); `postgres` — Oracle, Agent Comms, or Content Factory durable path (and real LLM for text generation) when the matching `SIMULATION_MODE_*` is false; `platform` — not a gated pillar; the wallet/platform-level categories `platform_fee` and `swarm_delegation`, which are never invoked as tools. |
| `runtimeService` | string or omitted | When status is `simulated`, `integrated`, or `postgres`, the **runtime registry** pillar id (`oracle`, `agent_comms`, …). Omitted / `null` for `platform`. |
| `creditsPerCallExact` | decimal string or omitted | Authoritative price for tools that expose an exact runtime price. Prefer this over the legacy floating-point `creditsPerCall` annotation for authorization and budget math. |

Existing fields (`creditsPerCall`, `unitName`, `category`, …) are unchanged.
`creditsPerCallExact` is an additive compatibility companion; the numeric field
remains available for older clients.

## Implementation

- Logic: `app/services/mcp_integration_truth.py` → `truth_for_category(category)`.
- Applied in: `app/services/mcp_generator.py` → `_service_to_mcp_tool`.

## Agent integration snippet

After `tools/list` or fetching `tools.json`, filter or label:

```python
for tool in manifest["tools"]:
    ann = tool.get("annotations") or {}
    if ann.get("simulation") is True:
        # Synthetic outcomes for this deployment unless SIMULATION_MODE_* is off
        ...
```

When `ENABLE_PROOF_SURFACES=true`, cross-check deployment truth with
`GET /health/dependencies` → `simulation_modes`. With proof surfaces disabled,
autonomous clients rely on each tool's public annotations. Operators separately
verify those annotations against the startup `runtime_posture` log and deployed
`SIMULATION_MODE_*` configuration.
