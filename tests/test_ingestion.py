from app.adapters.memory import (
    MemoryKnowledgeIngestionJobRepository,
    MemoryRetrievalStore,
    MemoryTenantConfigRepository,
)
from app.adapters.object_store import MemoryKnowledgeObjectStore
from app.ingestion import (
    PDF_OCR_USER_PROMPT,
    ExtractedPdfPage,
    KnowledgeIngestionService,
    KnowledgeIngestionWorker,
    PdfOcrFallbackExtractor,
    generate_knowledge_ingestion_job_id,
    knowledge_filename,
    knowledge_object_key,
)


class StaticPdfExtractor:
    def extract_pages(self, content: bytes) -> list[ExtractedPdfPage]:
        return [
            ExtractedPdfPage(page_number=1, text="Page one explains booking policy."),
            ExtractedPdfPage(page_number=2, text="Page two lists contact channels."),
            ExtractedPdfPage(page_number=3, text=""),
        ]


class ScannedPdfExtractor:
    def extract_pages(self, content: bytes) -> list[ExtractedPdfPage]:
        return [
            ExtractedPdfPage(page_number=1, text="Embedded text page."),
            ExtractedPdfPage(page_number=2, text=""),
        ]


class RecordingOcrClient:
    def __init__(self) -> None:
        self.images: list[str] = []

    async def extract_text(self, image_data_url: str) -> str:
        self.images.append(image_data_url)
        return "OCR page lists support hours and escalation contacts."


async def test_ingest_pdf_chunks_and_upserts_text_into_tenant_namespace() -> None:
    retrieval = MemoryRetrievalStore()
    tenant_configs = MemoryTenantConfigRepository()
    service = KnowledgeIngestionService(
        retrieval,
        tenant_configs,
        chunk_size=40,
        chunk_overlap=5,
        pdf_extractor=StaticPdfExtractor(),
    )

    result = await service.ingest_pdf(
        "Acme Lounge",
        filename="../Menu July 2026.pdf",
        content=b"%PDF fake content handled by static extractor",
    )

    assert result.tenant_id == "Acme Lounge"
    assert result.namespace == "acme-lounge:seed-knowledge"
    assert result.filename == "Menu-July-2026.pdf"
    assert result.pages_read == 3
    assert result.pages_with_text == 2
    assert result.chunks_created == 2
    assert result.chunk_ids == [
        "kb/uploads/Menu-July-2026.pdf#p0001-c0000",
        "kb/uploads/Menu-July-2026.pdf#p0002-c0000",
    ]
    assert [
        document.metadata["chunk_id"]
        for document in retrieval.documents["acme-lounge:seed-knowledge"]
    ] == result.chunk_ids
    assert [
        document.metadata["extraction_method"]
        for document in retrieval.documents["acme-lounge:seed-knowledge"]
    ] == ["pdf-text", "pdf-text"]


async def test_worker_links_ingested_chunks_to_stored_object() -> None:
    retrieval = MemoryRetrievalStore()
    tenant_configs = MemoryTenantConfigRepository()
    jobs = MemoryKnowledgeIngestionJobRepository()
    object_store = MemoryKnowledgeObjectStore(bucket="knowledge")
    service = KnowledgeIngestionService(
        retrieval,
        tenant_configs,
        chunk_size=80,
        chunk_overlap=10,
        pdf_extractor=StaticPdfExtractor(),
    )
    worker = KnowledgeIngestionWorker(
        jobs=jobs,
        object_store=object_store,
        ingestion_service=service,
    )
    job_id = generate_knowledge_ingestion_job_id()
    object_key = knowledge_object_key(
        tenant_id="tenant-a",
        job_id=job_id,
        filename="policy.pdf",
    )
    stored_object = await object_store.put(
        key=object_key,
        content=b"%PDF stored object handled by static extractor",
        content_type="application/pdf",
        metadata={"tenant_id": "tenant-a", "job_id": job_id, "filename": "policy.pdf"},
    )
    await jobs.create(
        job_id=job_id,
        tenant_id="tenant-a",
        filename="policy.pdf",
        content_type="application/pdf",
        object_bucket=stored_object.bucket,
        object_key=stored_object.key,
        object_etag=stored_object.etag,
    )

    completed = await worker.run_job(job_id)

    assert completed.status == "SUCCEEDED"
    assert completed.chunks_created == 2
    first_document = retrieval.documents["tenant-a:seed-knowledge"][0]
    assert first_document.metadata["ingestion_job_id"] == job_id
    assert first_document.metadata["object_bucket"] == "knowledge"
    assert first_document.metadata["object_key"] == object_key
    assert first_document.metadata["object_etag"] == stored_object.etag


async def test_ingest_pdf_uses_ocr_fallback_for_scanned_pages(monkeypatch) -> None:
    ocr_client = RecordingOcrClient()

    def fake_render_pdf_pages_as_png_data_urls(content: bytes, *, dpi: int) -> dict[int, str]:
        assert content == b"%PDF scanned"
        assert dpi == 150
        return {2: "data:image/png;base64,page-two"}

    monkeypatch.setattr(
        "app.ingestion.render_pdf_pages_as_png_data_urls",
        fake_render_pdf_pages_as_png_data_urls,
    )
    retrieval = MemoryRetrievalStore()
    tenant_configs = MemoryTenantConfigRepository()
    service = KnowledgeIngestionService(
        retrieval,
        tenant_configs,
        chunk_size=80,
        chunk_overlap=10,
        pdf_extractor=PdfOcrFallbackExtractor(
            text_extractor=ScannedPdfExtractor(),
            ocr_client=ocr_client,
            render_dpi=150,
        ),
    )

    result = await service.ingest_pdf(
        "tenant-a",
        filename="scanned-policy.pdf",
        content=b"%PDF scanned",
    )

    assert result.pages_read == 2
    assert result.pages_with_text == 2
    assert result.chunks_created == 2
    assert ocr_client.images == ["data:image/png;base64,page-two"]
    assert [
        document.metadata["extraction_method"]
        for document in retrieval.documents["tenant-a:seed-knowledge"]
    ] == ["pdf-text", "vision-ocr"]
    assert retrieval.documents["tenant-a:seed-knowledge"][1].page_content == (
        "OCR page lists support hours and escalation contacts."
    )


def test_knowledge_filename_sanitizes_uploaded_pdf_names() -> None:
    assert knowledge_filename("../Maxys Lounge Menu v1.pdf") == "Maxys-Lounge-Menu-v1.pdf"
    assert knowledge_filename("!!!") == "uploaded.pdf"


def test_pdf_ocr_prompt_is_document_neutral() -> None:
    assert "knowledge-base text" in PDF_OCR_USER_PROMPT
    assert "policy wording" in PDF_OCR_USER_PROMPT
    assert "Return markdown only" in PDF_OCR_USER_PROMPT
    assert "markdown tables" in PDF_OCR_USER_PROMPT
    assert "menu section" not in PDF_OCR_USER_PROMPT.lower()
