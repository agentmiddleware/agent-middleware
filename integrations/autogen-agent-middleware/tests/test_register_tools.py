"""The declared framework dependency must provide the API register_b2a_tools uses.

This guards the install contract: the wrapper imports the ``autogen`` namespace
and calls ``ConversableAgent.register_function(function_map=...)``. If the
dependency in pyproject.toml ever resolves to a package that does not ship that
API (for example ``autogen-agentchat`` 0.4+ or ``ag2`` 1.0+), this test fails
at import time rather than in a user's agent run.
"""

from autogen import ConversableAgent

from autogen_b2a import B2AFunctionTool, register_b2a_tools


def test_register_b2a_tools_binds_every_schema_to_the_agent():
    agent = ConversableAgent(
        name="executor",
        llm_config=False,
        human_input_mode="NEVER",
        code_execution_config=False,
    )
    tool = B2AFunctionTool(api_key="test-key", wallet_id="wallet-1")

    register_b2a_tools(agent, tool)

    expected = {schema["function"]["name"] for schema in tool.get_function_schemas()}
    assert set(agent.function_map) == expected
    assert agent.function_map["call_mcp_tool"] == tool.call_mcp_tool
    assert agent.function_map["discover_tools"] == tool.discover_tools
    assert agent.function_map["get_wallet_balance"] == tool.get_wallet_balance
