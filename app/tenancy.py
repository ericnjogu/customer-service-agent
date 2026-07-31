import re
from uuid import uuid4

DEFAULT_TENANT_ID = "default"
DEFAULT_TENANT_PLAN = "sme"
DEFAULT_VECTOR_PROVIDER = "pgvector"
DEFAULT_VECTOR_ISOLATION_MODE = "shared_collection"
DEFAULT_VECTOR_COLLECTION = "customer-service"
SEED_KNOWLEDGE_NAMESPACE = "seed-knowledge"


def normalize_tenant_id(tenant_id: str | None) -> str:
    return (tenant_id or DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID


def tenant_slug(tenant_id: str) -> str:
    normalized = normalize_tenant_id(tenant_id).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug or DEFAULT_TENANT_ID


def generate_tenant_id() -> str:
    return f"tnt_{uuid4().hex}"


def default_llm_project_name(tenant_id: str) -> str:
    return f"customer-service-{tenant_slug(tenant_id)}"


def default_langsmith_project(tenant_id: str) -> str:
    return f"customer-service-{tenant_slug(tenant_id)}"


def default_vector_namespace(tenant_id: str) -> str:
    normalized_tenant_id = normalize_tenant_id(tenant_id)
    if normalized_tenant_id == DEFAULT_TENANT_ID:
        return SEED_KNOWLEDGE_NAMESPACE
    return f"{tenant_slug(normalized_tenant_id)}:{SEED_KNOWLEDGE_NAMESPACE}"


def tenant_knowledge_namespace(tenant_id: str) -> str:
    return default_vector_namespace(tenant_id)
