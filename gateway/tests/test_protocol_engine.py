"""Unit tests for app.services.protocol_engine (Pillar 11).

Exercises the code-to-discovery pipeline directly, without HTTP or the
database: parsing FastAPI-style source, llm.txt / OpenAPI / agent.json
generation, and the ProtocolEngine orchestration including Oracle
registration edge cases.
"""

from types import SimpleNamespace

import pytest

from app.services.protocol_engine import (
    AgentJsonGenerator,
    CodeParser,
    LlmTxtGenerator,
    OpenApiGenerator,
    ParsedEndpoint,
    ProtocolEngine,
)

SAMPLE_CODE = """
from fastapi import APIRouter
router = APIRouter()

@router.get("/api/v1/widgets", summary="List widgets", description="Get all widgets")
async def list_widgets():
    return []

@router.post("/api/v1/widgets", summary="Create widget", response_model="Widget")
async def create_widget():
    return {"id": "w1"}

@router.delete("/api/v1/widgets/{widget_id}")
async def delete_widget(widget_id: str):
    return {}
"""


def test_parse_extracts_methods_paths_and_metadata():
    endpoints = CodeParser().parse(SAMPLE_CODE, "widget-api")

    assert len(endpoints) == 3
    assert [(e.method, e.path) for e in endpoints] == [
        ("GET", "/api/v1/widgets"),
        ("POST", "/api/v1/widgets"),
        ("DELETE", "/api/v1/widgets/{widget_id}"),
    ]
    assert endpoints[0].summary == "List widgets"
    assert endpoints[0].description == "Get all widgets"
    assert endpoints[1].summary == "Create widget"
    assert all(e.tags == ["widget-api"] for e in endpoints)


def test_parse_extracts_bare_response_model():
    source = '@router.post("/items", response_model=Widget)\nasync def create():\n    return {}\n'
    endpoints = CodeParser().parse(source, "svc")

    assert len(endpoints) == 1
    assert endpoints[0].response_model == "Widget"


@pytest.mark.xfail(
    strict=True, reason="RESPONSE_RE misses quoted response_model values"
)
def test_parse_extracts_quoted_response_model():
    source = '@router.post("/items", response_model="Widget")\nasync def create():\n    return {}\n'
    endpoints = CodeParser().parse(source, "svc")

    assert len(endpoints) == 1
    assert endpoints[0].response_model == "Widget"


def test_parse_matches_methods_case_insensitively():
    source = '@router.GET("/upper")\nasync def upper():\n    return {}\n'
    endpoints = CodeParser().parse(source, "svc")

    assert len(endpoints) == 1
    assert endpoints[0].method == "GET"


def test_parse_returns_empty_for_code_without_endpoints():
    assert CodeParser().parse("# empty file\nprint('hello')\n", "svc") == []
    assert CodeParser().parse("", "svc") == []


def test_llm_txt_lists_endpoints_and_auth_footer():
    endpoints = CodeParser().parse(SAMPLE_CODE, "widget-api")
    text = LlmTxtGenerator().generate(
        service_name="widget-api",
        service_version="2.0.0",
        base_url="https://api.widgets.io",
        endpoints=endpoints,
    )

    assert "# widget-api API v2.0.0" in text
    assert "GET /api/v1/widgets" in text
    assert "DELETE /api/v1/widgets/{widget_id}" in text
    assert "List widgets" in text
    assert "X-API-Key" in text
    assert "120 requests per minute" in text


def test_llm_txt_renders_parameters():
    endpoints = [
        ParsedEndpoint(
            method="GET",
            path="/search",
            parameters=[{"name": "q", "type": "string"}],
        )
    ]
    text = LlmTxtGenerator().generate(
        service_name="svc",
        service_version="1.0.0",
        base_url="https://api.example.com",
        endpoints=endpoints,
    )

    assert "Parameters:" in text
    assert "- q: string" in text


def test_openapi_spec_groups_methods_under_shared_path():
    endpoints = CodeParser().parse(SAMPLE_CODE, "widget-api")
    spec = OpenApiGenerator().generate(
        service_name="widget-api",
        service_version="1.0.0",
        base_url="https://api.widgets.io",
        endpoints=endpoints,
    )

    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["title"] == "widget-api"
    assert spec["servers"] == [{"url": "https://api.widgets.io"}]
    assert set(spec["paths"]["/api/v1/widgets"]) == {"get", "post"}
    operation = spec["paths"]["/api/v1/widgets"]["get"]
    assert operation["summary"] == "List widgets"
    assert operation["description"] == "Get all widgets"
    assert operation["security"] == [{"ApiKeyAuth": []}]
    assert spec["components"]["securitySchemes"]["ApiKeyAuth"]["in"] == "header"


def test_openapi_spec_includes_parameters_and_request_body():
    endpoints = [
        ParsedEndpoint(
            method="POST",
            path="/items",
            summary="Create",
            parameters=[{"name": "verbose", "in": "query", "type": "boolean"}],
            request_body={"type": "object"},
        )
    ]
    spec = OpenApiGenerator().generate(
        service_name="svc",
        service_version="1.0.0",
        base_url="https://api.example.com",
        endpoints=endpoints,
    )

    operation = spec["paths"]["/items"]["post"]
    assert operation["parameters"] == [
        {
            "name": "verbose",
            "in": "query",
            "schema": {"type": "boolean"},
            "required": False,
        }
    ]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "type": "object"
    }


def test_agent_json_manifest_lists_capabilities():
    endpoints = CodeParser().parse(SAMPLE_CODE, "widget-api")
    manifest = AgentJsonGenerator().generate(
        service_name="widget-api",
        service_version="2.0.0",
        base_url="https://api.widgets.io",
        endpoints=endpoints,
    )

    assert manifest["schema_version"] == "1.0"
    assert manifest["name"] == "widget-api"
    assert manifest["auth"] == {"type": "api_key", "header": "X-API-Key"}
    assert len(manifest["capabilities"]) == 3
    assert manifest["capabilities"][0]["method"] == "GET"
    assert manifest["documentation"]["llm_txt"] == "https://api.widgets.io/llm.txt"


@pytest.mark.anyio
async def test_engine_generate_builds_full_package_and_stores_it():
    engine = ProtocolEngine()
    result = await engine.generate(
        source_code=SAMPLE_CODE,
        service_name="widget-api",
        service_version="2.0.0",
        base_url="https://api.widgets.io",
        owner_wallet_id="wallet-1",
    )

    assert result.generation_id.startswith("gen-")
    assert result.endpoints_parsed == 3
    assert "GET /api/v1/widgets" in result.llm_txt
    assert "/api/v1/widgets" in result.openapi_spec["paths"]
    assert len(result.agent_json["capabilities"]) == 3
    assert result.oracle_registration_id is None
    assert result.warnings == []
    assert result.owner_wallet_id == "wallet-1"
    assert await engine.get_generation(result.generation_id) is result
    assert await engine.list_generations() == [result]


@pytest.mark.anyio
async def test_engine_generate_warns_when_no_endpoints_found():
    engine = ProtocolEngine()
    result = await engine.generate(
        source_code="# empty file\nprint('hello')\n",
        service_name="empty-api",
    )

    assert result.endpoints_parsed == 0
    assert len(result.warnings) == 1
    assert "No endpoints detected" in result.warnings[0]
    assert result.openapi_spec["paths"] == {}


@pytest.mark.anyio
async def test_engine_generate_with_successful_oracle_registration():
    class FakeOracle:
        async def crawl(self, base_url):
            assert base_url == "https://api.widgets.io"
            return SimpleNamespace(api_id="oracle-123")

    engine = ProtocolEngine()
    result = await engine.generate(
        source_code=SAMPLE_CODE,
        service_name="widget-api",
        base_url="https://api.widgets.io",
        register_in_oracle=True,
        oracle_instance=FakeOracle(),
    )

    assert result.oracle_registration_id == "oracle-123"
    assert result.warnings == []


@pytest.mark.anyio
async def test_engine_generate_warns_when_oracle_indexes_nothing():
    class NullOracle:
        async def crawl(self, base_url):
            return None

    engine = ProtocolEngine()
    result = await engine.generate(
        source_code=SAMPLE_CODE,
        service_name="widget-api",
        register_in_oracle=True,
        oracle_instance=NullOracle(),
    )

    assert result.oracle_registration_id is None
    assert any("Oracle registration failed" in w for w in result.warnings)


@pytest.mark.anyio
async def test_engine_generate_warns_when_oracle_raises():
    class BrokenOracle:
        async def crawl(self, base_url):
            raise RuntimeError("oracle down")

    engine = ProtocolEngine()
    result = await engine.generate(
        source_code=SAMPLE_CODE,
        service_name="widget-api",
        register_in_oracle=True,
        oracle_instance=BrokenOracle(),
    )

    assert result.oracle_registration_id is None
    assert any("oracle down" in w for w in result.warnings)


@pytest.mark.anyio
async def test_engine_generate_skips_oracle_without_instance():
    engine = ProtocolEngine()
    result = await engine.generate(
        source_code=SAMPLE_CODE,
        service_name="widget-api",
        register_in_oracle=True,
        oracle_instance=None,
    )

    assert result.oracle_registration_id is None
    assert result.warnings == []


@pytest.mark.anyio
async def test_engine_get_generation_returns_none_for_unknown_id():
    engine = ProtocolEngine()

    assert await engine.get_generation("gen-does-not-exist") is None
    assert await engine.list_generations() == []
