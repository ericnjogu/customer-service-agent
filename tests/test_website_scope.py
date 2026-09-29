from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.adapters.llm import NoopRuntimeWebSearch, TavilyRuntimeWebSearch
from app.config import Settings
from app.container import create_container
from app.graph import build_service_graph, invoke_service_graph
from app.knowledge import tenant_knowledge_namespace
from app.models import AnswerGenerationResult, IncomingMessage, QuestionPlan
from app.website_scope import allowed_website_source, website_domains


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://bakery.example/menu", True),
        ("https://shop.bakery.example/menu", True),
        ("https://BAKERY.EXAMPLE/menu", True),
        ("https://bakery.example.evil.test/menu", False),
        ("https://notbakery.example", False),
        ("https://bakery.example@evil.test/menu", False),
        ("javascript:alert(1)", False),
        (None, False),
        ("", False),
    ],
)
def test_source_hostname_boundary(url, allowed):
    assert allowed_website_source(url, ["https://bakery.example"]) is allowed


def test_domains_are_validated_deduplicated_and_do_not_expand_subdomains():
    assert website_domains(
        ["invalid", "https://shop.bakery.example/a", "https://shop.bakery.example/b"]
    ) == ["shop.bakery.example"]
    assert not allowed_website_source("https://bakery.example", ["https://shop.bakery.example"])


@pytest.mark.parametrize("websites", [[], [""], ["not a url"], ["ftp://bakery.example"]])
async def test_tavily_never_called_without_valid_domains(monkeypatch, websites):
    def forbidden(*args, **kwargs):
        pytest.fail("Must not construct an HTTP client for unscoped search")

    monkeypatch.setattr("app.adapters.llm.httpx.AsyncClient", forbidden)
    search = TavilyRuntimeWebSearch(api_key="test", max_results=3, timeout_seconds=5)
    assert not (await search.search_answer("Where is my order?", None, websites)).sources


@pytest.mark.parametrize(
    "websites,unavailable,provider_available",
    [
        ([], False, True),
        ([None], False, True),
        ([""], False, True),
        (["not a url"], False, True),
        (["https://bakery.example"], True, True),
        (["https://bakery.example"], False, False),
    ],
)
async def test_graph_skips_search_without_configuration_or_provider(
    websites,
    unavailable,
    provider_available,
):
    container = await create_container(Settings())
    calls = []

    class Contacts:
        async def list_contact_points(self, tenant_id):
            if unavailable:
                raise RuntimeError("Unavailable")
            return [SimpleNamespace(kind="website", url=url, is_primary=False) for url in websites]

    class Planner:
        async def plan(self, *args):
            return QuestionPlan(in_scope=True, needs_conversation_history=False)

    class Generator:
        async def generate(self, query, documents, *args):
            # Old untrusted search documents must not reach the answering model.
            assert not any(d.metadata.get("source") == "runtime-web-search" for d in documents)
            return AnswerGenerationResult(
                answer="I cannot confirm the delivery status.",
                confidence=0,
                grounded=False,
                answer_found=False,
            )

    class Search:
        async def search_answer(self, *args):
            calls.append(args)
            pytest.fail("Graph must bypass the search node")

    await container.retrieval.upsert(
        [
            Document(
                page_content="chapati order delivery via Amazon",
                metadata={
                    "source": "runtime-web-search",
                    "source_url": "https://amazon.example/orders",
                },
            )
        ],
        tenant_knowledge_namespace("t1"),
    )
    graph = build_service_graph(
        container.conversations,
        container.tenant_configs,
        container.retrieval,
        Generator(),
        Planner(),
        0.6,
        10,
        60,
        runtime_web_search=Search() if provider_available else NoopRuntimeWebSearch(),
        onboarding=Contacts(),
    )
    try:
        result = await invoke_service_graph(
            graph,
            IncomingMessage(
                tenant_id="t1",
                event_id="turn",
                external_chat_id="chat",
                external_user_id="user",
                text="Why has my chapati order not been delivered?",
            ),
        )
        assert not calls
        assert result.answer.startswith("I cannot confirm")
        assert result.low_confidence
        assert not result.citations
    finally:
        await container.close()


async def test_adapter_discards_foreign_sources_and_synthesized_answer(monkeypatch):
    import httpx

    def respond(request):
        import json

        payload = json.loads(request.content)
        assert payload["include_domains"] == ["bakery.example", "bakery.other"]
        assert payload["include_answer"] is False
        return httpx.Response(
            200,
            json={
                "answer": "Amazon says use Your Orders.",
                "results": [
                    {"url": url, "content": "delivery information"}
                    for url in [
                        "https://bakery.example/delivery",
                        "https://shop.bakery.other/help",
                        "https://bakery.example.evil.test/orders",
                        "https://amazon.example/orders",
                    ]
                ],
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    search = TavilyRuntimeWebSearch(api_key="test", max_results=4, timeout_seconds=5)
    result = await search.search_answer(
        "Delivery?", None, ["https://bakery.example", "https://bakery.other"]
    )
    assert result.answer == ""
    assert [source.url for source in result.sources] == [
        "https://bakery.example/delivery",
        "https://shop.bakery.other/help",
    ]


@pytest.mark.parametrize("search_mode", ["mixed", "foreign", "empty", "failure"])
async def test_graph_regenerates_from_allowed_sources_only(search_mode):
    import asyncio

    from app.models import RuntimeWebSearchResult, RuntimeWebSearchSource

    container = await create_container(Settings())
    generated_documents, searches, cached = [], [], []

    class Contacts:
        async def list_contact_points(self, tenant_id):
            return [
                SimpleNamespace(kind="website", url=url, is_primary=False)
                for url in [
                    "https://bakery.example",
                    "https://bakery.other",
                ]
            ] + [
                SimpleNamespace(
                    kind="instagram", url="https://instagram.com/bakery", is_primary=False
                )
            ]

    class Retrieval:
        async def search(self, *args):
            return []

        async def delete_by_source_url(self, *args):
            pass

        async def upsert(self, documents, tenant):
            cached.extend(documents)

    class Planner:
        async def plan(self, *args):
            return QuestionPlan(in_scope=True, needs_conversation_history=False)

    class Generator:
        async def generate(self, query, documents, *args):
            generated_documents.append(documents)
            if documents:
                assert all(
                    d.metadata["source_url"] == "https://shop.bakery.other/delivery"
                    for d in documents
                )
                return AnswerGenerationResult(
                    answer="Our delivery partner is PostNL.",
                    confidence=0.9,
                    grounded=True,
                    answer_found=True,
                )
            return AnswerGenerationResult(
                answer="I cannot confirm that.", confidence=0, grounded=False, answer_found=False
            )

    class Search:
        async def search_answer(self, question, tenant, websites):
            searches.append(websites)
            if search_mode == "failure":
                raise RuntimeError("Provider unavailable")
            sources = (
                []
                if search_mode == "empty"
                else [
                    RuntimeWebSearchSource(
                        url="https://amazon.example/orders",
                        text="Use Your Orders",
                        provider="tavily",
                    )
                ]
            )
            if search_mode == "mixed":
                sources.append(
                    RuntimeWebSearchSource(
                        url="https://shop.bakery.other/delivery",
                        text="Our delivery partner is PostNL.",
                        provider="tavily",
                    )
                )
            return RuntimeWebSearchResult(
                answer="Amazon says the order is backordered.", sources=sources
            )

    graph = build_service_graph(
        container.conversations,
        container.tenant_configs,
        Retrieval(),
        Generator(),
        Planner(),
        0.6,
        10,
        60,
        runtime_web_search=Search(),
        onboarding=Contacts(),
    )
    try:
        result = await invoke_service_graph(
            graph,
            IncomingMessage(
                tenant_id="bakery",
                event_id="delivery",
                external_chat_id="chat",
                external_user_id="user",
                text="Who delivers my chapati?",
            ),
        )
        await asyncio.sleep(0)
        assert searches == [["https://bakery.example", "https://bakery.other"]]
        assert "Amazon" not in result.answer and "backordered" not in result.answer
        if search_mode == "mixed":
            assert len(generated_documents) == 2
            assert result.answer == "Our delivery partner is PostNL."
            assert not result.low_confidence
            assert result.citations == ["https://shop.bakery.other/delivery"]
            assert cached and all(d.metadata["source_url"] == result.citations[0] for d in cached)
        else:
            assert len(generated_documents) == 1
            assert result.low_confidence
            assert not cached
    finally:
        await container.close()
