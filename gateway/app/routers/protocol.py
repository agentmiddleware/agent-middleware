"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Protocol Generation Engine Router (Pillar 11)
-----------------------------------------------
Code-to-discovery pipeline. Feed raw API code,
get back llm.txt + OpenAPI spec + agent.json + Oracle registration.

The go-to-market engine for agent-built tools.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from datetime import datetime

from ..core.auth import AuthContext, get_auth_context
from ..core.dependencies import get_protocol_engine, get_agent_oracle
from ..services.protocol_engine import GenerationResult, ProtocolEngine

router = APIRouter(
    prefix="/v1/protocol",
    tags=["Protocol Generation Engine"],
)


def _can_read_generation(auth: AuthContext, generation: GenerationResult) -> bool:
    """Generations are owned by the wallet that submitted the source.

    Bootstrap admins see every generation. A wallet-scoped caller sees only
    generations it created; a generation made with a bootstrap key has no
    owning wallet and stays admin-only.
    """
    if auth.is_bootstrap_admin:
        return True
    return auth.wallet_id is not None and generation.owner_wallet_id == auth.wallet_id


# --- Schemas ---


class GenerateRequest(BaseModel):
    """Submit source code for protocol generation."""

    source_code: str = Field(
        ...,
        min_length=10,
        description="Raw API source code (Python/FastAPI style).",
    )
    service_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Name of the service being documented.",
        examples=["widget-api"],
    )
    service_version: str = Field(
        default="1.0.0",
        description="Semantic version.",
    )
    base_url: str = Field(
        default="https://api.example.com",
        description="Production base URL for the service.",
    )
    register_in_oracle: bool = Field(
        default=False,
        description=(
            "Auto-register the generated service in the Agent Oracle directories."
        ),
    )


class GenerateResponse(BaseModel):
    """Full protocol generation output."""

    generation_id: str
    service_name: str
    service_version: str
    endpoints_parsed: int
    llm_txt: str = Field(..., description="LLM-optimized plaintext documentation.")
    openapi_spec: dict = Field(..., description="OpenAPI 3.1 JSON specification.")
    agent_json: dict = Field(..., description="/.well-known/agent.json manifest.")
    oracle_registration_id: str | None = None
    generated_at: datetime
    warnings: list[str] = Field(default_factory=list)


class GenerationListResponse(BaseModel):
    """List of all generations."""

    generations: list[dict]
    total: int


# --- Endpoints ---


@router.post(
    "/generate",
    response_model=GenerateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate protocol package from source code",
    description=(
        "Submit raw API source code and receive a complete "
        "agent-discoverable package:\n\n"
        "1. **llm.txt** — LLM-optimized documentation\n"
        "2. **OpenAPI 3.1 spec** — Machine-readable API schema\n"
        "3. **agent.json** — Agent discovery manifest\n"
        "4. **Oracle registration** — (optional) Push to agent directories\n\n"
        "The instant go-to-market engine for any tool an agent builds."
    ),
)
async def generate_protocol(
    request: GenerateRequest,
    auth: AuthContext = Depends(get_auth_context),
    engine: ProtocolEngine = Depends(get_protocol_engine),
    oracle=Depends(get_agent_oracle),
):
    if request.register_in_oracle:
        # Oracle registration writes the shared, global Oracle index, which
        # no single tenant owns.
        auth.require_bootstrap_admin()
    result = await engine.generate(
        source_code=request.source_code,
        service_name=request.service_name,
        service_version=request.service_version,
        base_url=request.base_url,
        register_in_oracle=request.register_in_oracle,
        oracle_instance=oracle if request.register_in_oracle else None,
        owner_wallet_id=auth.wallet_id,
    )
    return GenerateResponse(
        generation_id=result.generation_id,
        service_name=result.service_name,
        service_version=result.service_version,
        endpoints_parsed=result.endpoints_parsed,
        llm_txt=result.llm_txt,
        openapi_spec=result.openapi_spec,
        agent_json=result.agent_json,
        oracle_registration_id=result.oracle_registration_id,
        generated_at=result.generated_at,
        warnings=result.warnings,
    )


@router.get(
    "/generations",
    response_model=GenerationListResponse,
    summary="List all protocol generations",
    description="Retrieve historical generation runs.",
)
async def list_generations(
    auth: AuthContext = Depends(get_auth_context),
    engine: ProtocolEngine = Depends(get_protocol_engine),
):
    gens = [g for g in await engine.list_generations() if _can_read_generation(auth, g)]
    return GenerationListResponse(
        generations=[
            {
                "generation_id": g.generation_id,
                "service_name": g.service_name,
                "service_version": g.service_version,
                "endpoints_parsed": g.endpoints_parsed,
                "generated_at": g.generated_at,
            }
            for g in gens
        ],
        total=len(gens),
    )


@router.get(
    "/generations/{generation_id}",
    response_model=GenerateResponse,
    summary="Get a specific generation",
    description="Retrieve the full output of a protocol generation run.",
)
async def get_generation(
    generation_id: str,
    auth: AuthContext = Depends(get_auth_context),
    engine: ProtocolEngine = Depends(get_protocol_engine),
):
    result = await engine.get_generation(generation_id)
    # A foreign generation gets the same 404 as a missing one, so the
    # endpoint is not an existence oracle for other tenants' generation ids.
    if not result or not _can_read_generation(auth, result):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "generation_not_found"},
        )
    return GenerateResponse(
        generation_id=result.generation_id,
        service_name=result.service_name,
        service_version=result.service_version,
        endpoints_parsed=result.endpoints_parsed,
        llm_txt=result.llm_txt,
        openapi_spec=result.openapi_spec,
        agent_json=result.agent_json,
        oracle_registration_id=result.oracle_registration_id,
        generated_at=result.generated_at,
        warnings=result.warnings,
    )
