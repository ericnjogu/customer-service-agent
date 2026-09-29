import asyncio
import logging
from typing import TypedDict

from langchain_core.documents import Document
from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import tracing_context

from app.adapters.llm import (
    langsmith_client,
    langsmith_runnable_config,
    langsmith_tracing_enabled,
    tenant_langsmith_project_name,
    tenant_trace_metadata,
    tenant_trace_tags,
)
from app.knowledge import chunk_text_with_metadata, stable_source_hash, tenant_knowledge_namespace
from app.models import (
    AnswerGenerationResult,
    ConversationPromptMetadata,
    ConversationRecord,
    IncomingMessage,
    QuestionPlan,
    ServiceReply,
    StoredMessage,
    TenantConfig,
)
from app.ports import (
    AnswerGenerator,
    ConversationRepository,
    OnboardingRepository,
    QuestionPlanner,
    RetrievalStore,
    RuntimeWebSearch,
    TenantConfigRepository,
)
from app.website_scope import allowed_website_source, website_host

logger = logging.getLogger(__name__)


class ServiceState(TypedDict, total=False):
    tenant_websites: list[str]
    message: IncomingMessage
    conversation: ConversationRecord
    tenant_config: TenantConfig
    question_plan: QuestionPlan
    conversation_history: list[StoredMessage]
    conversation_metadata: ConversationPromptMetadata
    documents: list[Document]
    answer: str
    confidence: float
    answer_found: bool
    grounded: bool
    citations: list[str]
    low_confidence: bool


def build_service_graph(
    conversations: ConversationRepository,
    tenant_configs: TenantConfigRepository,
    retrieval: RetrievalStore,
    generator: AnswerGenerator,
    question_planner: QuestionPlanner,
    confidence_threshold: float,
    conversation_history_max_messages: int,
    greeting_lapse_minutes: int,
    runtime_web_search: RuntimeWebSearch | None = None,
    kb_chunk_size: int = 1_000,
    kb_chunk_overlap: int = 180,
    onboarding: OnboardingRepository | None = None,
    issues=None,
):
    async def get_or_create_conversation(state: ServiceState) -> dict:
        conversation = await conversations.get_or_create(state["message"])
        return {"conversation": conversation}

    async def persist_customer_message(state: ServiceState) -> dict:
        conversation = state["conversation"]
        save = issues.repository.save_message if issues else conversations.save_message
        await save(
            StoredMessage(
                tenant_id=state["message"].tenant_id,
                conversation_id=conversation.id,
                event_id=state["message"].event_id,
                sender_type="CUSTOMER",
                body=state["message"].text,
                in_scope=state["question_plan"].in_scope,
            )
        )
        return {"conversation": conversation}

    async def load_tenant_config(state: ServiceState) -> dict:
        if state.get("tenant_config") is not None:
            return {"tenant_config": state["tenant_config"]}
        tenant_config = await tenant_configs.get(state["conversation"].tenant_id)
        return {"tenant_config": tenant_config}

    async def plan_question(state: ServiceState) -> dict:
        history = (
            await issues.repository.history(
                state["conversation"], conversation_history_max_messages
            )
            if issues
            else []
        )
        minutes_since_last_customer_message = (
            await conversations.minutes_since_previous_customer_message(
                state["conversation"].id,
                state["message"].event_id,
                state["message"].received_at,
            )
        )
        metadata = build_prompt_metadata_from_last_customer_delta(
            state["message"],
            minutes_since_last_customer_message,
            greeting_lapse_minutes,
        )
        plan = await question_planner.plan(
            state["message"],
            metadata,
            state.get("tenant_config"),
            **({"conversation_history": history} if issues else {}),
        )
        return {"question_plan": plan, "conversation_metadata": metadata}

    async def apply_human_request_state(state: ServiceState) -> dict:
        conversation = state["conversation"]
        if state["question_plan"].explicit_human_request and conversation.state == "BOT_ACTIVE":
            conversation = await conversations.update_state(
                conversation.id,
                state="HUMAN_REQUESTED",
                reason="Customer explicitly requested human support",
            )
        return {"conversation": conversation}

    async def load_conversation_history(state: ServiceState) -> dict:
        conversation = state["conversation"]
        history = (
            await issues.repository.history(conversation, conversation_history_max_messages)
            if issues
            else await conversations.list_messages_since(
                conversation.id,
                conversation.created_at,
                conversation_history_max_messages,
            )
        )
        return {"conversation_history": history}

    async def skip_conversation_history(state: ServiceState) -> dict:
        return {"conversation_history": []}

    async def build_conversation_metadata(state: ServiceState) -> dict:
        if "conversation_metadata" in state:
            return {"conversation_metadata": state["conversation_metadata"]}

        history = state.get("conversation_history", [])
        if history:
            metadata = build_prompt_metadata(
                state["message"],
                history,
                greeting_lapse_minutes,
            )
        else:
            metadata = build_prompt_metadata_without_history(
                state["message"],
                should_greet_customer=False,
                greeting_reason="conversation history was not needed for this standalone question",
            )
        return {"conversation_metadata": metadata}

    async def retrieve(state: ServiceState) -> dict:
        websites = await tenant_website_urls(state["message"].tenant_id)
        tenant_config = state.get("tenant_config")
        knowledge_tenant_id = (
            tenant_config.vector_namespace
            if tenant_config and tenant_config.vector_namespace
            else tenant_knowledge_namespace(state["message"].tenant_id)
        )
        documents = await retrieval.search(
            state["message"].text,
            knowledge_tenant_id,
        )
        documents = [
            document
            for document in documents
            if document.metadata.get("source") != "runtime-web-search"
            or allowed_website_source(document.metadata.get("source_url"), websites)
        ]
        return {"documents": documents, "tenant_websites": websites}

    async def answer_out_of_scope(state: ServiceState) -> dict:
        metadata = state.get("conversation_metadata") or build_prompt_metadata_without_history(
            state["message"],
            should_greet_customer=False,
        )
        text = format_out_of_scope_answer(state["question_plan"].explanation)
        return {
            "answer": text,
            "confidence": 0.0,
            "citations": [],
            "low_confidence": True,
            "documents": [],
            "conversation_history": [],
            "conversation_metadata": metadata,
        }

    async def answer(state: ServiceState) -> dict:
        if state["question_plan"].explicit_human_request:
            return {
                "answer": "I've recorded your request for human support. "
                "This does not mean a team member has been notified or assigned yet.",
                "confidence": 1.0,
                "answer_found": True,
                "grounded": True,
                "citations": [],
                "low_confidence": False,
            }
        if state["conversation"].state == "HUMAN_ACTIVE":
            return {
                "answer": "This conversation is currently being handled by human support.",
                "confidence": 0.0,
                "answer_found": False,
                "grounded": False,
                "citations": [],
                "low_confidence": True,
            }
        answer_result = normalize_answer_result(
            await generator.generate(
                state["message"].text,
                state["documents"],
                state.get("conversation_history", []),
                state.get("conversation_metadata"),
                state.get("tenant_config"),
            )
        )
        citations = document_citations(state["documents"])
        return {
            "answer": answer_result.answer,
            "confidence": answer_result.confidence,
            "answer_found": answer_result.answer_found,
            "grounded": answer_result.grounded,
            "citations": citations,
            "low_confidence": should_search_tenant_website(answer_result),
        }

    async def search_tenant_website(state: ServiceState) -> dict:
        websites = state.get("tenant_websites", [])
        if (
            not getattr(runtime_web_search, "available", runtime_web_search is not None)
            or not websites
        ):
            return {"low_confidence": True}
        try:
            search_result = await runtime_web_search.search_answer(
                state["message"].text,
                state.get("tenant_config"),
                websites,
            )
        except Exception:
            logger.warning("Tenant website search unavailable; preserving KB answer")
            return {"low_confidence": True}
        sources = [
            source
            for source in search_result.sources
            if source.text.strip() and allowed_website_source(source.url, websites)
        ]
        if not sources:
            return {"low_confidence": True}
        documents = [
            document
            for source in sources
            for document in runtime_web_search_documents(
                source,
                query=state["message"].text,
                chunk_size=kb_chunk_size,
                chunk_overlap=kb_chunk_overlap,
            )
        ]
        try:
            result = normalize_answer_result(
                await generator.generate(
                    state["message"].text,
                    documents,
                    state.get("conversation_history", []),
                    state.get("conversation_metadata"),
                    state.get("tenant_config"),
                )
            )
        except Exception:
            logger.warning("Website answer generation unavailable; preserving KB answer")
            return {"low_confidence": True}
        asyncio.create_task(
            refresh_runtime_web_search_knowledge(
                retrieval,
                tenant_config=state.get("tenant_config"),
                tenant_id=state["message"].tenant_id,
                query=state["message"].text,
                sources=sources,
                chunk_size=kb_chunk_size,
                chunk_overlap=kb_chunk_overlap,
            )
        )
        return {
            "answer": result.answer,
            "confidence": result.confidence,
            "answer_found": result.answer_found,
            "grounded": result.grounded,
            "citations": [source.url for source in sources] if result.grounded else [],
            "low_confidence": should_search_tenant_website(result),
        }

    def document_citations(documents: list[Document]) -> list[str]:
        return [
            str(item.metadata.get("chunk_id") or item.metadata.get("source", "unknown"))
            for item in documents
        ]

    async def tenant_website_urls(tenant_id: str) -> list[str]:
        if onboarding is None:
            return []
        try:
            contact_points = await onboarding.list_contact_points(tenant_id)
        except Exception:
            logger.warning("Tenant websites unavailable; disabling website search for this turn")
            return []
        return list(
            dict.fromkeys(
                point.url
                for point in sorted(contact_points, key=lambda point: not point.is_primary)
                if point.kind == "website" and website_host(point.url)
            )
        )

    async def refresh_runtime_web_search_knowledge(
        retrieval: RetrievalStore,
        *,
        tenant_config: TenantConfig | None,
        tenant_id: str,
        query: str,
        sources,
        chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        knowledge_tenant_id = (
            tenant_config.vector_namespace
            if tenant_config and tenant_config.vector_namespace
            else tenant_knowledge_namespace(tenant_id)
        )
        for source in sources:
            if not source.url or not source.text.strip():
                continue
            try:
                await retrieval.delete_by_source_url(knowledge_tenant_id, source.url)
                documents = runtime_web_search_documents(
                    source,
                    query=query,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                )
                if documents:
                    await retrieval.upsert(documents, knowledge_tenant_id)
            except Exception:
                logger.exception(
                    "Failed to refresh runtime web search KB source tenant_id=%s "
                    "knowledge_tenant_id=%s source_url=%s",
                    tenant_id,
                    knowledge_tenant_id,
                    source.url,
                )

    def runtime_web_search_documents(
        source,
        *,
        query: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> list[Document]:
        chunks = chunk_text_with_metadata(
            source.text,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        source_hash = stable_source_hash(source.url)
        return [
            Document(
                page_content=chunk.content,
                metadata={
                    "source": "runtime-web-search",
                    "source_url": source.url,
                    "source_type": "website",
                    "source_title": source.title or source.url,
                    "provider": source.provider,
                    "retrieved_at": source.retrieved_at.isoformat(),
                    "query": query,
                    "section_title": chunk.section_title,
                    "chunk_index": chunk.chunk_index,
                    "chunk_count": chunk.chunk_count,
                    "content_hash": chunk.content_hash,
                    "chunk_id": f"url:{source_hash}#{chunk.chunk_index:04d}",
                },
            )
            for chunk in chunks
        ]

    def normalize_answer_result(result) -> AnswerGenerationResult:
        if isinstance(result, AnswerGenerationResult):
            return result
        text, confidence = result
        return AnswerGenerationResult(
            answer=text,
            confidence=confidence,
            answer_found=confidence >= confidence_threshold,
            grounded=confidence >= confidence_threshold,
        )

    def should_search_tenant_website(answer_result: AnswerGenerationResult) -> bool:
        return (
            not answer_result.answer_found
            or not answer_result.grounded
            or answer_result.confidence < confidence_threshold
        )

    def route_after_plan(state: ServiceState) -> str:
        return "in_scope" if state["question_plan"].in_scope else "out_of_scope"

    def route_history(state: ServiceState) -> str:
        return (
            "load_history"
            if issues or state["question_plan"].needs_conversation_history
            else "skip_history"
        )

    def route_after_kb_answer(state: ServiceState) -> str:
        return (
            "search"
            if (
                state["low_confidence"]
                and state["question_plan"].in_scope
                and state.get("tenant_websites")
                and getattr(runtime_web_search, "available", runtime_web_search is not None)
                and state["conversation"].state != "HUMAN_ACTIVE"
            )
            else "reply"
        )

    async def persist_reply(state: ServiceState) -> dict:
        conversation = state["conversation"]
        answer_text = state["answer"]
        reply = StoredMessage(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            event_id=f"reply:{state['message'].event_id}",
            sender_type="BOT",
            body=answer_text,
            in_scope=state["question_plan"].in_scope,
        )
        if issues:
            await issues.record(reply, state["message"])
        else:
            await conversations.save_message(reply)
        return {"conversation": conversation, "answer": answer_text}

    workflow = StateGraph(ServiceState)
    workflow.add_node("get_or_create_conversation", get_or_create_conversation)
    workflow.add_node("load_tenant_config", load_tenant_config)
    workflow.add_node("plan_question", plan_question)
    workflow.add_node("persist_customer_message", persist_customer_message)
    workflow.add_node("apply_human_request_state", apply_human_request_state)
    workflow.add_node("load_conversation_history", load_conversation_history)
    workflow.add_node("skip_conversation_history", skip_conversation_history)
    workflow.add_node("build_conversation_metadata", build_conversation_metadata)
    workflow.add_node("retrieve", retrieve)
    workflow.add_node("answer_out_of_scope", answer_out_of_scope)
    workflow.add_node("answer", answer)
    workflow.add_node("search_tenant_website", search_tenant_website)
    workflow.add_node("persist_reply", persist_reply)
    workflow.add_edge(START, "get_or_create_conversation")
    workflow.add_edge("get_or_create_conversation", "load_tenant_config")
    workflow.add_edge("load_tenant_config", "plan_question")
    workflow.add_edge("plan_question", "persist_customer_message")
    workflow.add_conditional_edges(
        "persist_customer_message",
        route_after_plan,
        {"in_scope": "apply_human_request_state", "out_of_scope": "answer_out_of_scope"},
    )
    workflow.add_conditional_edges(
        "apply_human_request_state",
        route_history,
        {"load_history": "load_conversation_history", "skip_history": "skip_conversation_history"},
    )
    workflow.add_edge("load_conversation_history", "build_conversation_metadata")
    workflow.add_edge("skip_conversation_history", "build_conversation_metadata")
    workflow.add_edge("build_conversation_metadata", "retrieve")
    workflow.add_edge("retrieve", "answer")
    workflow.add_edge("answer_out_of_scope", "persist_reply")
    workflow.add_conditional_edges(
        "answer",
        route_after_kb_answer,
        {"search": "search_tenant_website", "reply": "persist_reply"},
    )
    workflow.add_edge("search_tenant_website", "persist_reply")
    workflow.add_edge("persist_reply", END)
    return workflow.compile()


def build_prompt_metadata(
    current_message: IncomingMessage,
    conversation_history: list[StoredMessage],
    greeting_lapse_minutes: int,
) -> ConversationPromptMetadata:
    previous_customer_messages = [
        message
        for message in conversation_history
        if message.sender_type == "CUSTOMER" and message.event_id != current_message.event_id
    ]
    previous_customer_message = (
        previous_customer_messages[-1] if previous_customer_messages else None
    )

    if previous_customer_message is None:
        return ConversationPromptMetadata(
            is_first_customer_message=True,
            customer_name=customer_first_name(current_message.sender_name),
            should_greet_customer=True,
            greeting_reason="first customer message in this conversation",
        )

    minutes_since_last_customer_message = max(
        0,
        int(
            (current_message.received_at - previous_customer_message.created_at).total_seconds()
            // 60
        ),
    )
    should_greet_customer = minutes_since_last_customer_message >= greeting_lapse_minutes
    greeting_reason = (
        f"last customer message was {minutes_since_last_customer_message} minutes ago"
        if should_greet_customer
        else "active conversation; avoid repeated greeting"
    )
    return ConversationPromptMetadata(
        is_first_customer_message=False,
        customer_name=customer_first_name(current_message.sender_name),
        minutes_since_last_customer_message=minutes_since_last_customer_message,
        should_greet_customer=should_greet_customer,
        greeting_reason=greeting_reason,
    )


def build_prompt_metadata_from_last_customer_delta(
    current_message: IncomingMessage,
    minutes_since_last_customer_message: int | None,
    greeting_lapse_minutes: int,
) -> ConversationPromptMetadata:
    if minutes_since_last_customer_message is None:
        return ConversationPromptMetadata(
            is_first_customer_message=True,
            customer_name=customer_first_name(current_message.sender_name),
            should_greet_customer=True,
            greeting_reason="first customer message in this conversation",
        )

    should_greet_customer = minutes_since_last_customer_message >= greeting_lapse_minutes
    greeting_reason = (
        f"last customer message was {minutes_since_last_customer_message} minutes ago"
        if should_greet_customer
        else "active conversation; avoid repeated greeting"
    )
    return ConversationPromptMetadata(
        is_first_customer_message=False,
        customer_name=customer_first_name(current_message.sender_name),
        minutes_since_last_customer_message=minutes_since_last_customer_message,
        should_greet_customer=should_greet_customer,
        greeting_reason=greeting_reason,
    )


def format_out_of_scope_answer(explanation: str | None) -> str:
    return explanation or "I could not route that request safely."


def build_prompt_metadata_without_history(
    current_message: IncomingMessage,
    *,
    should_greet_customer: bool,
    greeting_reason: str | None = None,
) -> ConversationPromptMetadata:
    return ConversationPromptMetadata(
        is_first_customer_message=should_greet_customer,
        customer_name=customer_first_name(current_message.sender_name),
        should_greet_customer=should_greet_customer,
        greeting_reason=greeting_reason,
    )


def customer_first_name(sender_name: str | None) -> str | None:
    if not sender_name:
        return None

    parts = sender_name.strip().split()
    return parts[0] if parts else None


async def invoke_service_graph(
    graph,
    message: IncomingMessage,
    tenant_configs: TenantConfigRepository | None = None,
) -> ServiceReply:
    tenant_config = (
        await tenant_configs.get(message.tenant_id) if tenant_configs is not None else None
    )
    initial_state: ServiceState = {"message": message}
    if tenant_config is not None:
        initial_state["tenant_config"] = tenant_config

    config = langsmith_runnable_config("service_graph", tenant_config)
    with tracing_context(
        client=langsmith_client(),
        project_name=tenant_langsmith_project_name(tenant_config),
        tags=tenant_trace_tags(tenant_config),
        metadata=tenant_trace_metadata(tenant_config),
        enabled=langsmith_tracing_enabled(),
    ):
        state = await graph.ainvoke(initial_state, config=config)
    return ServiceReply(
        tenant_id=state["conversation"].tenant_id,
        conversation_id=state["conversation"].id,
        answer=state["answer"],
        confidence=state["confidence"],
        citations=state["citations"],
        low_confidence=state["low_confidence"],
        state=state["conversation"].state,
    )
