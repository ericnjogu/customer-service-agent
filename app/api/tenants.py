import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status

from app.models import (
    TenantConfig,
    TenantConfigUpdate,
    TenantCreateRequest,
    TenantRecord,
)
from app.tenancy import tenant_slug

router = APIRouter(prefix="/tenants", tags=["tenants"])
logger = logging.getLogger(__name__)


@router.post("", response_model=TenantRecord, status_code=201)
async def create_tenant(
    request: Request,
    tenant_request: TenantCreateRequest,
) -> TenantRecord:
    slug = tenant_slug(tenant_request.slug or tenant_request.display_name)
    existing_tenant = await request.app.state.container.tenants.get_by_slug(slug)
    if existing_tenant is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant with matching name or slug already exists",
        )

    tenant = await request.app.state.container.tenants.create(
        display_name=tenant_request.display_name,
        slug=slug,
        selected_plan=tenant_request.selected_plan,
    )
    return tenant


@router.get("/by-slug/{slug}", response_model=TenantRecord)
async def get_tenant_by_slug(
    request: Request,
    slug: Annotated[str, Path(min_length=1, pattern=r".*\S.*")],
) -> TenantRecord:
    tenant = await request.app.state.container.tenants.get_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.get("/{tenant_id}", response_model=TenantRecord)
async def get_tenant(
    request: Request,
    tenant_id: Annotated[str, Path(min_length=1, pattern=r".*\S.*")],
) -> TenantRecord:
    tenant = await request.app.state.container.tenants.get(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.get("/{tenant_id}/config", response_model=TenantConfig)
async def get_tenant_config(
    request: Request,
    tenant_id: Annotated[str, Path(min_length=1, pattern=r".*\S.*")],
) -> TenantConfig:
    tenant_config = await request.app.state.container.tenant_configs.get_existing(
        tenant_id
    )
    if tenant_config is None:
        raise HTTPException(status_code=404, detail="Tenant config not found")
    return tenant_config


@router.put("/{tenant_id}/config", response_model=TenantConfig)
async def update_tenant_config(
    request: Request,
    update: TenantConfigUpdate,
    tenant_id: Annotated[str, Path(min_length=1, pattern=r".*\S.*")],
) -> TenantConfig:
    return await request.app.state.container.tenant_configs.upsert(
        tenant_id,
        selected_plan=update.selected_plan,
        enabled_features=update.enabled_features,
        answer_prompt_instructions=update.answer_prompt_instructions,
        planner_prompt_instructions=update.planner_prompt_instructions,
        llm_project_id=update.llm_project_id,
        llm_project_name=update.llm_project_name,
        langsmith_project=update.langsmith_project,
        llm_provider=update.llm_provider,
        llm_model=update.llm_model,
        llm_base_url=update.llm_base_url,
        vector_provider=update.vector_provider,
        vector_isolation_mode=update.vector_isolation_mode,
        vector_collection=update.vector_collection,
        vector_namespace=update.vector_namespace,
        telegram_secret_name=update.telegram_secret_name,
        whatsapp_secret_name=update.whatsapp_secret_name,
        web_search_provider=update.web_search_provider,
        web_search_project_name=update.web_search_project_name,
    )
