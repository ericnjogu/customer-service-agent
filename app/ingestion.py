import base64
import inspect
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol
from uuid import uuid4

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pypdf import PdfReader

from app.knowledge import chunk_text, tenant_knowledge_namespace
from app.models import KnowledgeIngestionJob, KnowledgeIngestionResult
from app.ports import (
    KnowledgeIngestionJobRepository,
    KnowledgeObjectStore,
    RetrievalStore,
    TenantConfigRepository,
)
from app.tenancy import normalize_tenant_id

logger = logging.getLogger(__name__)

PDF_OCR_SYSTEM_PROMPT = "You are an OCR assistant for customer support KB ingestion."
PDF_OCR_USER_PROMPT = (
    "Extract all visible customer-facing knowledge-base text from this PDF page image. "
    "Preserve headings, labels, tables, lists, field names, item names, descriptions, "
    "prices, dates, opening hours, addresses, phone numbers, URLs, email addresses, social "
    "handles, policy wording, and other operational details. Keep the original language "
    "and wording as much as possible. Return markdown only, using headings, bullet lists, "
    "and markdown tables when they reflect the visible page structure. Do not add facts, "
    "summaries, commentary, or explanations that are not visible on the page. If no "
    "readable text is visible, return an empty string."
)


@dataclass(frozen=True)
class ExtractedPdfPage:
    page_number: int
    text: str
    extraction_method: str = "pdf-text"


class VisionOcrClient(Protocol):
    async def extract_text(self, image_data_url: str) -> str: ...


class PdfTextExtractor:
    def extract_pages(self, content: bytes) -> list[ExtractedPdfPage]:
        logger.info("Extracting embedded text from PDF bytes=%d", len(content))
        reader = PdfReader(BytesIO(content))
        pages: list[ExtractedPdfPage] = []
        for page_index, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            logger.info(
                "Extracted embedded PDF page text page_number=%d chars=%d",
                page_index + 1,
                len(text),
            )
            pages.append(
                ExtractedPdfPage(
                    page_number=page_index + 1,
                    text=text,
                    extraction_method="pdf-text",
                )
            )
        return pages


class OpenAIVisionOcrClient:
    def __init__(self, *, api_key: str, model: str) -> None:
        self.chat_model = ChatOpenAI(api_key=api_key, model=model, temperature=0)

    async def extract_text(self, image_data_url: str) -> str:
        logger.info("Calling vision OCR model for rendered PDF page image")
        response = await self.chat_model.ainvoke(
            [
                SystemMessage(content=PDF_OCR_SYSTEM_PROMPT),
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": PDF_OCR_USER_PROMPT,
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url},
                        },
                    ]
                ),
            ]
        )
        text = str(response.content or "").strip()
        logger.info("Vision OCR model returned chars=%d", len(text))
        return text


class PdfOcrFallbackExtractor:
    def __init__(
        self,
        text_extractor: PdfTextExtractor | None = None,
        *,
        ocr_client: VisionOcrClient,
        render_dpi: int = 120,
    ) -> None:
        self.text_extractor = text_extractor or PdfTextExtractor()
        self.ocr_client = ocr_client
        self.render_dpi = render_dpi

    async def extract_pages(self, content: bytes) -> list[ExtractedPdfPage]:
        pages = self.text_extractor.extract_pages(content)
        blank_pages = [page.page_number for page in pages if not page.text]
        if not blank_pages:
            logger.info("PDF OCR fallback found embedded text on all pages")
            return pages
        if len(blank_pages) == len(pages):
            logger.info(
                "PDF contains no embedded text; running OCR for all pages page_count=%d",
                len(pages),
            )
        else:
            logger.info(
                "PDF contains %d page(s) without embedded text; running OCR blank_pages=%s",
                len(blank_pages),
                blank_pages,
            )
        rendered_pages = None
        extracted_pages: list[ExtractedPdfPage] = []
        for page in pages:
            if page.text:
                extracted_pages.append(page)
                continue

            if rendered_pages is None:
                logger.info(
                    "Rendering PDF pages to PNG for OCR dpi=%d page_count=%d",
                    self.render_dpi,
                    len(pages),
                )
                rendered_pages = render_pdf_pages_as_png_data_urls(
                    content,
                    dpi=self.render_dpi,
                )
            image_data_url = rendered_pages.get(page.page_number)
            if not image_data_url:
                logger.warning(
                    "No rendered image available for OCR page_number=%d",
                    page.page_number,
                )
                extracted_pages.append(page)
                continue

            logger.info("Running OCR for PDF page page_number=%d", page.page_number)
            ocr_text = await self.ocr_client.extract_text(image_data_url)
            extracted_pages.append(
                ExtractedPdfPage(
                    page_number=page.page_number,
                    text=ocr_text,
                    extraction_method="vision-ocr",
                )
            )
        return extracted_pages


class KnowledgeIngestionService:
    def __init__(
        self,
        retrieval: RetrievalStore,
        tenant_configs: TenantConfigRepository,
        *,
        chunk_size: int,
        chunk_overlap: int,
        pdf_extractor: PdfTextExtractor | None = None,
    ) -> None:
        self.retrieval = retrieval
        self.tenant_configs = tenant_configs
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.pdf_extractor = pdf_extractor or PdfTextExtractor()

    async def ingest_pdf(
        self,
        tenant_id: str,
        *,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
        job_id: str | None = None,
        object_bucket: str | None = None,
        object_key: str | None = None,
        object_etag: str | None = None,
    ) -> KnowledgeIngestionResult:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        safe_filename = knowledge_filename(filename)
        logger.info(
            "Starting PDF knowledge ingestion job_id=%s tenant_id=%s filename=%s "
            "content_type=%s bytes=%d object_bucket=%s object_key=%s",
            job_id or "<none>",
            normalized_tenant_id,
            safe_filename,
            content_type,
            len(content),
            object_bucket or "<none>",
            object_key or "<none>",
        )
        pages = await extract_pdf_pages(self.pdf_extractor, content)
        tenant_config = await self.tenant_configs.get(normalized_tenant_id)
        namespace = tenant_config.vector_namespace or tenant_knowledge_namespace(
            normalized_tenant_id
        )
        logger.info(
            "Resolved PDF knowledge namespace job_id=%s tenant_id=%s namespace=%s "
            "vector_provider=%s vector_collection=%s",
            job_id or "<none>",
            normalized_tenant_id,
            namespace,
            tenant_config.vector_provider,
            tenant_config.vector_collection,
        )

        documents: list[Document] = []
        pages_with_text = 0
        for page in pages:
            if not page.text:
                logger.warning(
                    "Skipping PDF page with no extractable text job_id=%s page_number=%d "
                    "extraction_method=%s",
                    job_id or "<none>",
                    page.page_number,
                    page.extraction_method,
                )
                continue
            pages_with_text += 1
            chunks = chunk_text(
                page.text,
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
            )
            logger.info(
                "Chunked PDF page job_id=%s page_number=%d extraction_method=%s "
                "chars=%d chunks=%d",
                job_id or "<none>",
                page.page_number,
                page.extraction_method,
                len(page.text),
                len(chunks),
            )
            for chunk_index, chunk in enumerate(chunks):
                source = f"kb/uploads/{safe_filename}"
                chunk_id = f"{source}#p{page.page_number:04d}-c{chunk_index:04d}"
                documents.append(
                    Document(
                        page_content=chunk,
                        metadata={
                            "source": source,
                            "chunk_id": chunk_id,
                            "chunk_index": chunk_index,
                            "chunk_count": len(chunks),
                            "filename": safe_filename,
                            "page_number": page.page_number,
                            "content_type": content_type,
                            "ingestion_source": "tenant-pdf-upload",
                            "extraction_method": page.extraction_method,
                            "ingestion_job_id": job_id,
                            "object_bucket": object_bucket,
                            "object_key": object_key,
                            "object_etag": object_etag,
                        },
                    )
                )

        if documents:
            logger.info(
                "Ingesting tenant PDF knowledge tenant_id=%s namespace=%s filename=%s "
                "job_id=%s pages=%d pages_with_text=%d chunks=%d",
                normalized_tenant_id,
                namespace,
                safe_filename,
                job_id or "<none>",
                len(pages),
                pages_with_text,
                len(documents),
            )
            await self.retrieval.upsert(documents, namespace)
        else:
            logger.warning(
                "PDF knowledge upload contained no extractable text tenant_id=%s filename=%s "
                "pages=%d",
                normalized_tenant_id,
                safe_filename,
                len(pages),
            )

        return KnowledgeIngestionResult(
            tenant_id=normalized_tenant_id,
            namespace=namespace,
            filename=safe_filename,
            content_type=content_type,
            pages_read=len(pages),
            pages_with_text=pages_with_text,
            chunks_created=len(documents),
            chunk_ids=[str(document.metadata["chunk_id"]) for document in documents],
        )


class KnowledgeIngestionWorker:
    def __init__(
        self,
        *,
        jobs: KnowledgeIngestionJobRepository,
        object_store: KnowledgeObjectStore,
        ingestion_service: KnowledgeIngestionService,
    ) -> None:
        self.jobs = jobs
        self.object_store = object_store
        self.ingestion_service = ingestion_service

    async def run_job(self, job_id: str) -> KnowledgeIngestionJob:
        logger.info("Starting knowledge ingestion worker job_id=%s", job_id)
        job = await self.jobs.get_by_id(job_id)
        if job is None:
            logger.error("Knowledge ingestion job not found job_id=%s", job_id)
            raise KeyError(f"Knowledge ingestion job not found: {job_id}")

        await self.jobs.mark_running(job_id)
        try:
            logger.info(
                "Loading source PDF for knowledge ingestion job_id=%s tenant_id=%s "
                "bucket=%s key=%s etag=%s",
                job.job_id,
                job.tenant_id,
                job.object_bucket,
                job.object_key,
                job.object_etag or "<none>",
            )
            content = await self.object_store.get(key=job.object_key)
            result = await self.ingestion_service.ingest_pdf(
                job.tenant_id,
                filename=job.filename,
                content=content,
                content_type=job.content_type,
                job_id=job.job_id,
                object_bucket=job.object_bucket,
                object_key=job.object_key,
                object_etag=job.object_etag,
            )
            completed = await self.jobs.mark_succeeded(job_id, result=result)
            logger.info(
                "Completed knowledge ingestion job_id=%s tenant_id=%s status=%s "
                "pages_read=%d pages_with_text=%d chunks_created=%d",
                completed.job_id,
                completed.tenant_id,
                completed.status,
                completed.pages_read,
                completed.pages_with_text,
                completed.chunks_created,
            )
            return completed
        except Exception as exc:
            logger.exception("Knowledge ingestion job failed job_id=%s", job_id)
            failed = await self.jobs.mark_failed(job_id, error_message=str(exc))
            logger.info(
                "Marked knowledge ingestion job failed job_id=%s tenant_id=%s status=%s",
                failed.job_id,
                failed.tenant_id,
                failed.status,
            )
            return failed


def knowledge_filename(filename: str) -> str:
    normalized = filename.strip().replace("\\", "/").split("/")[-1].strip()
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip(".-")
    return normalized or "uploaded.pdf"


def generate_knowledge_ingestion_job_id() -> str:
    return f"kbi_{uuid4().hex}"


def knowledge_object_key(*, tenant_id: str, job_id: str, filename: str) -> str:
    return (
        f"tenants/{normalize_tenant_id(tenant_id)}/knowledge/"
        f"{job_id}/{knowledge_filename(filename)}"
    )


async def extract_pdf_pages(pdf_extractor, content: bytes) -> list[ExtractedPdfPage]:
    pages = pdf_extractor.extract_pages(content)
    if inspect.isawaitable(pages):
        return await pages
    return pages


def render_pdf_pages_as_png_data_urls(content: bytes, *, dpi: int) -> dict[int, str]:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - defensive dependency guard
        raise RuntimeError("PyMuPDF is required for scanned PDF OCR") from exc

    rendered: dict[int, str] = {}
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(stream=content, filetype="pdf") as pdf_document:
        for page_index, page in enumerate(pdf_document):
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            png_bytes = pixmap.tobytes("png")
            encoded = base64.b64encode(png_bytes).decode("utf-8")
            rendered[page_index + 1] = f"data:image/png;base64,{encoded}"
    return rendered
