"""
Agent Middleware API — Framework Integrations
================================================

Integration packages for popular agent frameworks:
- LangGraph
- CrewAI
- AutoGen
- LlamaIndex

## Installation

No PyPI package is published; import this module from a checkout of the
repository after `python -m pip install -r gateway/requirements.txt -e ./sdk/python`
with `PYTHONPATH=integrations`.

## Quick Start

```python
from framework_integrations import B2AClient, get_langgraph_tools, get_llamaindex_tools

# Initialize client
client = B2AClient(
    api_url="http://localhost:8000",
    api_key="your-api-key",
    wallet_id="your-wallet-id"
)

# Get tools for your framework (async tools: drive them with ainvoke / acall)
langgraph_tools = get_langgraph_tools(client)
llamaindex_tools = get_llamaindex_tools(client)
```

Legacy status: ``get_langgraph_tools``, ``get_llamaindex_tools`` and
``get_autogen_tools`` are UNGOVERNED (no permit, no receipt) and emit a
``DeprecationWarning``. Prefer the governed wrappers first:
``CrewAIB2ATool``, the autogen / langchain / openai integrations, or
``LangGraphGovernedTools`` / ``PydanticAIGovernedTools``.

`get_crewai_tools` raises `NotImplementedError`: CrewAI runs each async tool
on a fresh event loop, which the async `B2AClient` cannot survive between
calls. Use the governed `CrewAIB2ATool` in `integrations/crewai-agent-middleware`.

## Framework-Specific Guides

See individual README files for each framework:
- README.langgraph.md
- README.crewai.md
- README.autogen.md
- README.llamaindex.md
"""

__version__ = "0.4.1"

from .client import B2AClient, B2AConfig
from .tools import (
    get_autogen_tools,
    get_crewai_tools,
    get_langgraph_tools,
    get_llamaindex_tools,
)

# Governed middleware surfaces (permit-verified in-process validation over the
# b2a_sdk governed loop). Exported lazily via PEP 562 so importing this
# package keeps working when the b2a_sdk sources are not on the path (the
# legacy client/tools above need only httpx) — and the middleware modules
# themselves import pydantic_ai / langgraph only inside their as_*_tool
# functions, so neither framework needs to be installed either.
_LAZY_ATTRS = {
    "LangGraphGovernedTools": "langgraph_middleware",
    "PydanticAIGovernedTools": "pydantic_ai_middleware",
}


def __getattr__(name):
    module_name = _LAZY_ATTRS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache so later access is a plain attribute lookup
    return value


__all__ = [
    "B2AClient",
    "B2AConfig",
    "LangGraphGovernedTools",
    "PydanticAIGovernedTools",
    "get_autogen_tools",
    "get_crewai_tools",
    "get_langgraph_tools",
    "get_llamaindex_tools",
]
