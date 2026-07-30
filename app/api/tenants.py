import logging
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Path, Request, UploadFile, status

from app.ingestion import (
    generate_knowledge_ingestion_job_id,
    knowledge_filename,
    knowledge_object_key,
)
from app.models import (
    KnowledgeIngestionJob,
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
    )


@router.post(
    "/{tenant_id}/knowledge/pdf",
    response_model=KnowledgeIngestionJob,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_tenant_pdf_knowledge(
    request: Request,
    tenant_id: Annotated[str, Path(min_length=1, pattern=r".*\S.*")],
    file: Annotated[UploadFile, File()],
) -> KnowledgeIngestionJob:
    content_type = file.content_type or "application/octet-stream"
    filename = file.filename or "uploaded.pdf"
    if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Only PDF knowledge uploads are supported")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded PDF is empty")

    logger.info(
        "Received tenant PDF knowledge upload tenant_id=%s filename=%s content_type=%s bytes=%d",
        tenant_id,
        filename,
        content_type,
        len(content),
    )
    job_id = generate_knowledge_ingestion_job_id()
    safe_filename = knowledge_filename(filename)
    object_key = knowledge_object_key(
        tenant_id=tenant_id,
        job_id=job_id,
        filename=safe_filename,
    )
    stored_object = await request.app.state.container.knowledge_object_store.put(
        key=object_key,
        content=content,
        content_type=content_type,
        metadata={
            "tenant_id": tenant_id,
            "job_id": job_id,
            "filename": safe_filename,
        },
    )
    logger.info(
        "Stored tenant PDF knowledge upload job_id=%s tenant_id=%s bucket=%s key=%s etag=%s",
        job_id,
        tenant_id,
        stored_object.bucket,
        stored_object.key,
        stored_object.etag or "<none>",
    )
    job = await request.app.state.container.knowledge_ingestion_jobs.create(
        job_id=job_id,
        tenant_id=tenant_id,
        filename=safe_filename,
        content_type=content_type,
        object_bucket=stored_object.bucket,
        object_key=stored_object.key,
        object_etag=stored_object.etag,
    )
    await request.app.state.container.knowledge_ingestion_queue.enqueue(job.job_id)
    logger.info(
        "Queued tenant PDF knowledge ingestion job_id=%s tenant_id=%s status=%s",
        job.job_id,
        job.tenant_id,
        job.status,
    )
    return job


@router.get(
    "/{tenant_id}/knowledge/ingestions/{job_id}",
    response_model=KnowledgeIngestionJob,
)
async def get_tenant_knowledge_ingestion(
    request: Request,
    tenant_id: Annotated[str, Path(min_length=1, pattern=r".*\S.*")],
    job_id: Annotated[str, Path(min_length=1, pattern=r".*\S.*")],
) -> KnowledgeIngestionJob:
    job = await request.app.state.container.knowledge_ingestion_jobs.get(tenant_id, job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Knowledge ingestion job not found",
        )
    return job
