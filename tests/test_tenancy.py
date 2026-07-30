from app.tenancy import (
    DEFAULT_TENANT_ID,
    SEED_KNOWLEDGE_NAMESPACE,
    default_langsmith_project,
    default_llm_project_name,
    default_vector_namespace,
    generate_tenant_id,
    normalize_tenant_id,
    tenant_slug,
)


def test_normalize_tenant_id_uses_default_for_missing_or_blank_values() -> None:
    assert normalize_tenant_id(None) == DEFAULT_TENANT_ID
    assert normalize_tenant_id("") == DEFAULT_TENANT_ID
    assert normalize_tenant_id("   ") == DEFAULT_TENANT_ID


def test_normalize_tenant_id_trims_non_blank_values() -> None:
    assert normalize_tenant_id("  Acme Lounge  ") == "Acme Lounge"


def test_tenant_slug_normalizes_provider_safe_names() -> None:
    assert tenant_slug("Acme Lounge") == "acme-lounge"
    assert tenant_slug("  Tenant_123!! ") == "tenant-123"
    assert tenant_slug("North/East.Co") == "north-east-co"


def test_tenant_slug_uses_default_when_no_alphanumeric_characters_remain() -> None:
    assert tenant_slug("") == DEFAULT_TENANT_ID
    assert tenant_slug("!!!") == DEFAULT_TENANT_ID


def test_default_project_names_use_tenant_slug() -> None:
    assert default_llm_project_name("Acme Lounge") == "customer-support-acme-lounge"
    assert default_langsmith_project("Acme Lounge") == "customer-support-acme-lounge"


def test_default_vector_namespace_preserves_default_tenant_namespace() -> None:
    assert default_vector_namespace(DEFAULT_TENANT_ID) == SEED_KNOWLEDGE_NAMESPACE


def test_default_vector_namespace_uses_tenant_slug_for_non_default_tenants() -> None:
    assert default_vector_namespace("Acme Lounge") == "acme-lounge:seed-knowledge"


def test_generate_tenant_id_uses_internal_tenant_prefix() -> None:
    tenant_id = generate_tenant_id()

    assert tenant_id.startswith("tnt_")
    assert len(tenant_id) == 36
