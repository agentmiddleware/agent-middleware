# AutoGen Integration

Use Agent Middleware API tools with Microsoft AutoGen agents.

## Installation

No PyPI package is published. Work from a checkout of this repository:

```bash
git clone https://github.com/PetrefiedThunder/agent-middleware.git
cd agent-middleware
python -m pip install -r gateway/requirements.txt -e ./sdk/python
export PYTHONPATH="$PWD/integrations"
python -m pip install autogen
```

Import from `framework_integrations` (the module in this repository); there is
no `agent_middleware` package.

## Quick Start

```python
import autogen
from framework_integrations import B2AClient, get_autogen_tools

# Initialize client
client = B2AClient(
    api_url="http://localhost:8000",
    api_key="your-api-key",
    wallet_id="your-wallet-id"
)

# Get AutoGen-compatible function map
function_map = get_autogen_tools(client)

# Create assistant agent
assistant = autogen.AssistantAgent(
    name="assistant",
    llm_config=llm_config,
    function_map=function_map
)

# Create user proxy
user_proxy = autogen.UserProxyAgent(
    name="user_proxy",
    human_input_mode="NEVER",
    max_consecutive_auto_reply=10
)

# Start conversation
user_proxy.initiate_chat(
    assistant,
    message="Check my balance and emit a telemetry event"
)
```

## Available Functions

| Function | Description | Credits |
|----------|-------------|---------|
| `emit_telemetry` | Track agent events | 1 |
| `get_balance` | Check wallet balance | 0 |
| `send_message` | Message another agent | 1 |
| `ai_decide` | Make AI decision | 10 |
| `self_heal` | Diagnose and fix issues | 15 |
| `create_awi_session` | Start web automation | 5 |

## Example: Group Chat

```python
import autogen

# Create agents with B2A tools
researcher = autogen.AssistantAgent(
    name="researcher",
    llm_config=llm_config,
    function_map=get_autogen_tools(client)
)

writer = autogen.AssistantAgent(
    name="writer",
    llm_config=llm_config,
    function_map=get_autogen_tools(client)
)

# Group chat
group_chat = autogen.GroupChat(
    agents=[researcher, writer],
    messages=[],
    max_round=10
)

manager = autogen.GroupChatManager(groupchat=group_chat)

user_proxy.initiate_chat(
    manager,
    message="Research AI developments and write a summary"
)
```
