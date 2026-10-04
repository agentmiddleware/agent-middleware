# Agent Interaction Examples & Recipes

> **Status: legacy proof-surface material.** These examples include APIs outside
> the supported one-tool MCP pilot and may refer to unmounted or simulated
> routes. Do not use this page for product evaluation or integration; start with
> the [documentation guide](README.md).
>
> Agent comms, AI decide/heal, telemetry, and AWI sessions are frozen proof
> surfaces, not the product wedge (see [WEDGE.md](../WEDGE.md)). Their routers
> mount only with `ENABLE_PROOF_SURFACES=true`, which production-like deploys
> refuse at boot, so these routes do not exist on a production instance. See
> [PROOF_SURFACES.md](PROOF_SURFACES.md). The snippets are also illustrative
> pseudocode: most `client.*` calls shown (for example `decide`, `heal`,
> `emit_telemetry`, `send_message`, `register_service`, `set_memory`) are not
> methods of `b2a_sdk.AgentMiddlewareClient`.

Practical examples for building autonomous agents with the Agent Middleware API.

## Table of Contents

- [Quick Agent Setup](#quick-agent-setup)
- [Billing & Payments](#billing--payments)
- [Agent-to-Agent Communication](#agent-to-agent-communication)
- [AI-Powered Decision Making](#ai-powered-decision-making)
- [Self-Healing Agents](#self-healing-agents)
- [Telemetry & Monitoring](#telemetry--monitoring)
- [MCP Tool Integration](#mcp-tool-integration)
- [AWI Session Management](#awi-session-management)
- [Advanced Patterns](#advanced-patterns)

---

## Quick Agent Setup

### Basic Agent Initialization

```python
from b2a_sdk import AgentMiddlewareClient

client = AgentMiddlewareClient(
    base_url="http://localhost:8000",
    api_key="your-api-key",
)

# Check wallet balance
balance = await client.get_balance()
print(f"Current balance: {balance}")
```

### Agent with Memory Persistence

```python
from b2a_sdk import AgentMiddlewareClient

client = AgentMiddlewareClient(base_url="http://localhost:8000", api_key="key")

async def agent_loop():
    while True:
        # Load previous state
        state = await client.get_memory("agent_state")

        # Make decision based on state
        decision = await client.decide(
            agent_id="agent-001",
            context=state or {"task": "start"},
            options=["process", "wait", "report"]
        )

        # Store updated state
        await client.set_memory("agent_state", {"last_decision": decision})

        # Process task
        await process_task(decision)
```

---

## Billing & Payments

### Create Wallet with Initial Credits

```bash
curl -X POST http://localhost:8000/v1/billing/wallets \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{"wallet_id": "agent-001", "balance": 10000}'
```

### Track Usage Automatically

```python
from b2a_sdk.decorators import billable

b2a = AgentMiddlewareClient(api_key="key")

@billable(b2a, wallet_id="agent-001", service_category="content_factory", units=1.0)
async def generate_report(data: dict) -> dict:
    """Generate report - costs 50 credits per call."""
    return {"report_url": f"https://storage.example.com/{data['id']}.pdf"}
```

### Dry-Run Budget Planning

```python
async with b2a.simulate_session(wallet_id="agent-001") as sim:
    # Test operations without real charges
    await generate_report({"id": "test-1"})
    await generate_report({"id": "test-2"})
    await generate_report({"id": "test-3"})

    print(f"Total cost: {sim.total_cost}")  # 150 credits
    print(f"Would succeed: {sim.would_succeed}")  # True if wallet has funds

    if sim.would_succeed:
        # Commit to real billing
        await sim.commit()
```

### Service Marketplace Integration

```python
# Register a service
await client.register_service(
    service_id="data-indexer",
    name="Data Indexing Service",
    price_per_call=50,
    provider_wallet_id="provider-001"
)

# Invoke a service
result = await client.invoke_service(
    service_id="data-indexer",
    caller_wallet_id="agent-001",
    input_data={"documents": ["doc1.pdf", "doc2.pdf"]}
)
```

---

## Agent-to-Agent Communication

### Register Agent and Send Messages

```python
# Register as available agent
await client.register_agent(
    agent_id="agent-001",
    capabilities=["data-processing", "report-generation"]
)

# Send message to another agent
await client.send_message(
    from_agent_id="agent-001",
    to_agent_id="agent-002",
    message={"task": "process_data", "data": {...}}
)
```

### Receive and Process Messages

```python
# Check inbox
inbox = await client.get_inbox("agent-001")

for message in inbox.messages:
    if message.priority == "high":
        # Process high priority immediately
        await process_task(message.content)
        await client.mark_read(message.message_id)
    else:
        # Queue for later
        pending_tasks.append(message)
```

### Broadcast to Multiple Agents

```python
# Broadcast to all agents with specific capability
await client.broadcast(
    from_agent_id="agent-001",
    capability_filter="data-processing",
    message={"task": "batch_process", "dataset": "large"}
)
```

---

## AI-Powered Decision Making

### Autonomous Task Selection

```python
decision = await client.decide(
    agent_id="agent-001",
    context={
        "pending_tasks": ["email_response", "data_sync", "report_gen"],
        "current_load": 0.7,
        "battery_level": 0.4,
        "time_of_day": "afternoon"
    },
    options=[
        "process_high_priority",
        "defer_non_urgent",
        "wait_for_resources"
    ]
)

print(f"Decision: {decision}")  # "process_high_priority"
```

### Multi-Criteria Decision Making

```python
decision = await client.decide(
    agent_id="agent-001",
    context={
        "tasks": [
            {"id": 1, "cost": 100, "reward": 500, "time_required": 60},
            {"id": 2, "cost": 50, "reward": 100, "time_required": 15},
            {"id": 3, "cost": 200, "reward": 1000, "time_required": 120}
        ],
        "budget": 300,
        "deadline": "2024-12-31T23:59:59Z"
    },
    options=["select_task_1", "select_task_2", "select_task_3", "select_multiple"]
)
```

### Self-Healing Diagnostics

```python
async def monitor_and_heal():
    while True:
        # Check system health
        health = await client.get_telemetry_summary("agent-001")

        if health.get("error_rate", 0) > 0.1:
            # Trigger self-healing
            fix = await client.heal(
                issue="High error rate detected",
                context={
                    "error_log": health.get("recent_errors", []),
                    "system_state": health
                }
            )

            if fix.get("action"):
                await execute_fix(fix["action"])

        await asyncio.sleep(60)
```

---

## Telemetry & Monitoring

### Emit Custom Events

```python
# Track custom metrics
await client.emit_telemetry(
    event="task_completed",
    agent_id="agent-001",
    properties={
        "task_type": "data_processing",
        "duration_ms": 1500,
        "records_processed": 10000,
        "success": True
    }
)

# Track agent behavior
await client.emit_telemetry(
    event="behavior_decision",
    agent_id="agent-001",
    properties={
        "decision": "process_high_priority",
        "confidence": 0.95,
        "reasoning": "High priority task with deadline approaching"
    }
)
```

### Anomaly Detection

```python
# Check for anomalies
anomalies = await client.get_anomalies(
    agent_id="agent-001",
    time_window_minutes=60
)

for anomaly in anomalies:
    print(f"Anomaly: {anomaly.type}")
    print(f"Severity: {anomaly.severity}")
    print(f"Details: {anomaly.details}")

    # Trigger alert response
    if anomaly.severity == "critical":
        await client.alert("critical_anomaly", anomaly.details)
```

### Performance Metrics Dashboard

```python
metrics = await client.get_metrics(
    agent_id="agent-001",
    metrics=["tasks_completed", "errors", "credits_spent", "uptime"]
)

print(f"Tasks completed: {metrics['tasks_completed']}")
print(f"Success rate: {metrics['success_rate']}%")
print(f"Credits spent: {metrics['credits_spent']}")
```

---

## MCP Tool Integration

### Create and Register MCP Tools

```python
from b2a_sdk.decorators import mcp_tool

@mcp_tool(
    service_id="image-processor",
    name="Image Processor",
    description="Process and transform images",
    category="media",
    credits_per_unit=10.0,
    unit_name="image"
)
async def process_image(image_url: str, operations: list[str]) -> dict:
    """Your tool implementation."""
    result = await transform_image(image_url, operations)
    return {"processed_url": result, "operations_applied": operations}
```

### Discover and Call Tools

```python
# List available MCP tools
tools = await client.list_mcp_tools()

for tool in tools:
    print(f"{tool.name}: {tool.description} ({tool.credits_per_call} credits)")

# Call a tool
result = await client.call_mcp_tool(
    name="image-processor",
    arguments={"image_url": "https://example.com/photo.jpg", "operations": ["resize", "crop"]}
)
```

### Retired standalone MCP generator

`python -m b2a_sdk.mcp standalone` and
`McpGenerator.generate_standalone_server` now refuse without fetching tools or
writing files. Their generated billing proxies never implemented governed
permit authorization or signed receipts. Use the supported
[governed quickstart](quickstart.md) and `POST /mcp/messages` with a wallet,
permit, and caller-owned idempotency key.

---

## AWI Session Management

### Create Browser Automation Session

```python
# Start AWI session for web interaction
session = await client.create_awi_session(
    target_url="https://ecommerce.example.com",
    max_steps=100
)

print(f"Session ID: {session.session_id}")

# Execute standardized actions
result = await client.execute_awi_action(
    session_id=session.session_id,
    action="search_and_filter",
    parameters={
        "query": "laptops",
        "filters": {"price_range": [500, 1500], "brand": "Dell"}
    }
)
```

### Get Progressive Representations

```python
# Get summary representation
summary = await client.get_awi_representation(
    session_id=session.session_id,
    representation_type="summary"
)

# Get detailed representation
detailed = await client.get_awi_representation(
    session_id=session.session_id,
    representation_type="full"
)

# Get embedding for similarity search
embedding = await client.get_awi_representation(
    session_id=session.session_id,
    representation_type="embedding"
)
```

### Human-in-the-Loop Intervention

```python
# Pause for human review
await client.pause_awi_session(
    session_id=session.session_id,
    reason="High-value transaction requires approval"
)

# Wait for human response
while True:
    status = await client.get_session_status(session.session_id)
    if status.state == "paused_awaiting_approval":
        approval = await status.human_decision  # Wait for human
        if approval == "approved":
            await client.resume_awi_session(session.session_id)
        else:
            await client.cancel_awi_session(session.session_id)
            break
    await asyncio.sleep(1)
```

---

## Advanced Patterns

### Agent Swarm Coordination

```python
class SwarmCoordinator:
    def __init__(self, client: AgentMiddlewareClient):
        self.client = client
        self.agents = {}

    async def register_agents(self, agent_configs: list[dict]):
        for config in agent_configs:
            await self.client.register_agent(
                agent_id=config["id"],
                capabilities=config["capabilities"]
            )
            self.agents[config["id"]] = config

    async def delegate_task(self, task: dict) -> str:
        # Find best agent for task
        for capability in task["required_capabilities"]:
            suitable_agents = [
                aid for aid, cfg in self.agents.items()
                if capability in cfg["capabilities"]
            ]
            if suitable_agents:
                # Delegate to first available
                target_agent = suitable_agents[0]
                await self.client.send_message(
                    from_agent_id="coordinator",
                    to_agent_id=target_agent,
                    message={"task": task}
                )
                return target_agent

        raise ValueError("No suitable agent found")

    async def collect_results(self, task_ids: list[str]) -> list[dict]:
        results = []
        for task_id in task_ids:
            # Poll for results
            while True:
                inbox = await self.client.get_inbox("coordinator")
                for msg in inbox.messages:
                    if msg.content.get("task_id") == task_id:
                        results.append(msg.content["result"])
                        break
                await asyncio.sleep(1)
        return results
```

### Hierarchical Task Decomposition

```python
async def decompose_and_execute(task: str, budget: int):
    # Use AI to break down task
    decomposition = await client.decide(
        agent_id="planner",
        context={"task": task, "budget": budget},
        options=[
            "decompose_simple",
            "decompose_medium",
            "decompose_complex"
        ]
    )

    subtasks = await generate_subtasks(task, decomposition)

    # Create child wallets for each subtask
    total_credits = budget
    per_task_credits = total_credits // len(subtasks)

    task_wallets = []
    for subtask in subtasks:
        wallet = await client.create_child_wallet(
            parent_wallet_id="planner",
            max_spend=per_task_credits,
            task_description=subtask,
            ttl_seconds=3600
        )
        task_wallets.append({"subtask": subtask, "wallet": wallet})

    # Execute subtasks in parallel
    results = await asyncio.gather(*[
        execute_subtask(st["subtask"], st["wallet"])
        for st in task_wallets
    ])

    # Aggregate results
    return aggregate_results(results)
```

### Rate-Limited Batch Processing

```python
async def batch_process_with_rate_limit(items: list, rate_limit: int = 60):
    """Process items with per-minute rate limiting."""
    client = AgentMiddlewareClient(api_key="key")
    processed = 0
    window_start = time.time()

    for item in items:
        # Wait if rate limit exceeded
        elapsed = time.time() - window_start
        if elapsed < 60 and processed >= rate_limit:
            wait_time = 60 - elapsed
            print(f"Rate limit reached. Waiting {wait_time:.1f}s...")
            await asyncio.sleep(wait_time)
            window_start = time.time()
            processed = 0

        # Process item
        await process_item(item)
        processed += 1

        # Emit telemetry
        await client.emit_telemetry(
            event="batch_progress",
            properties={"processed": processed, "total": len(items)}
        )
```

---

## Best Practices

### Error Handling

```python
try:
    result = await client.call_mcp_tool("my-tool", args)
except RateLimitError:
    await asyncio.sleep(60)  # Wait and retry
except InsufficientCreditsError:
    await client.freeze_and_alert("Low credits", {...})
except ServiceUnavailableError:
    await client.heal("Service down", {...})  # Self-heal
```

### Idempotency

```python
async def process_with_idempotency(task_id: str, operation: callable):
    # Check if already processed
    existing = await client.get_memory(f"processed:{task_id}")
    if existing:
        return existing["result"]

    # Process task
    result = await operation()

    # Store result with idempotency key
    await client.set_memory(f"processed:{task_id}", {
        "result": result,
        "processed_at": datetime.utcnow().isoformat()
    })

    return result
```

### Graceful Shutdown

```python
async def agent_shutdown(agent_id: str):
    # Save state
    state = get_current_state()
    await client.set_memory(f"state:{agent_id}", state)

    # Mark offline
    await client.unregister_agent(agent_id)

    # Flush pending messages
    await client.flush_outbox(agent_id)

    print(f"Agent {agent_id} shut down gracefully")
```

---

## See Also

- [AWI Adoption Guide](awi-adoption-guide.md) - Make your site agent-native
- [API Reference](http://localhost:8000/docs) - Full API documentation
- [B2A SDK](../../sdk/python/README.md) - Python SDK documentation
