import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from langchain_core.documents import Document

from app.adapters.llm import SYSTEM_PROMPT, create_openai_answer_generator
from app.config import Settings
from app.models import ConversationPromptMetadata, StoredMessage, TenantConfig

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    os.getenv("AGENT_RUN_LIVE_OPENAI_TESTS", "").lower() not in {"1", "true", "yes"},
    reason="set AGENT_RUN_LIVE_OPENAI_TESTS=true to run live OpenAI integration tests",
)

FACEBOOK_LINK = "https://www.facebook.com/harborpinebakery"
INSTAGRAM_LINK = "https://www.instagram.com/harborpinebakery"
CONTACT_EMAIL = "hello@harborpine.example"
CONTACT_PHONE = "+254 700 123 456"
CONVERSATION_ID = "00000000-0000-0000-0000-000000000042"
GREETING_PATTERN = re.compile(
    r"^(?:hi|hello|hey|good morning|good afternoon|good evening|welcome(?: back)?)\b",
    re.IGNORECASE,
)
TRANSFER_CLAIMS = (
    "i'll connect",
    "i will connect",
    "let me connect",
    "connect you now",
    "i'll transfer",
    "i will transfer",
    "transferring you",
    "i'll escalate",
    "i will escalate",
    "escalating this",
    "handing you over",
)
UNAVAILABLE_PHRASES = (
    "not available",
    "unavailable",
    "cannot connect",
    "can't connect",
    "aren't available",
    "unable to connect",
    "do not offer",
    "don't offer",
)


@dataclass(frozen=True)
class LiveAnswerCase:
    name: str
    query: str
    documents: list[Document]
    conversation_history: list[StoredMessage]
    conversation_metadata: ConversationPromptMetadata
    tenant_config: TenantConfig
    should_greet: bool = False
    expected_name_in_greeting: str | None = None
    forbidden_literals: tuple[str, ...] = ()
    forbidden_phrases: tuple[str, ...] = ()
    require_unavailable_handover: bool = False


def stored_message(
    *, event_id: str, sender_type: str, body: str, minute: int
) -> StoredMessage:
    return StoredMessage(
        tenant_id="harbor-pine",
        conversation_id=CONVERSATION_ID,
        event_id=event_id,
        sender_type=sender_type,
        body=body,
        created_at=datetime(2026, 9, 10, 9, minute, tzinfo=timezone.utc),
    )


def document(text: str, source: str) -> Document:
    return Document(
        page_content=text,
        metadata={
            "source": source,
            "chunk_id": f"{source}#0000",
            "created_at": "2026-09-10T08:00:00+00:00",
        },
    )


def tenant_config(*, handover_available: bool = False) -> TenantConfig:
    handover = (
        "A human support team is available through the order help desk."
        if handover_available
        else "Live-agent handover and transfer to a support team are not available."
    )
    return TenantConfig.with_defaults(
        "harbor-pine",
        business_summary=(
            "Harbor & Pine is a neighborhood bakery in Nairobi. "
            f"Contact details are {CONTACT_EMAIL} and {CONTACT_PHONE}. {handover} "
            f"Facebook link: {FACEBOOK_LINK}. Instagram link: {INSTAGRAM_LINK}."
        ),
    )


LIVE_CASES = (
    LiveAnswerCase(
        name="greets_first_customer_message",
        query="What time do you open on Saturday?",
        documents=[document("The bakery opens at 7:00 AM on Saturdays.", "hours.md")],
        conversation_history=[],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=True,
            customer_name="Amina",
            minutes_since_last_customer_message=None,
            should_greet_customer=True,
            greeting_reason="first customer message in this conversation",
        ),
        tenant_config=tenant_config(),
        should_greet=True,
        expected_name_in_greeting="Amina",
    ),
    LiveAnswerCase(
        name="does_not_greet_during_active_conversation",
        query="And what time do you close?",
        documents=[document("The bakery closes at 6:00 PM on Saturdays.", "hours.md")],
        conversation_history=[
            stored_message(
                event_id="active-1",
                sender_type="CUSTOMER",
                body="What time do you open on Saturday?",
                minute=10,
            ),
            stored_message(
                event_id="active-2",
                sender_type="BOT",
                body="Hi Amina, we open at 7:00 AM on Saturdays.",
                minute=11,
            ),
        ],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name="Amina",
            minutes_since_last_customer_message=1,
            should_greet_customer=False,
            greeting_reason="active conversation; avoid repeated greeting",
        ),
        tenant_config=tenant_config(),
        forbidden_phrases=("welcome back",),
    ),
    LiveAnswerCase(
        name="does_not_volunteer_contact_information",
        query="Do you sell vegan pastries?",
        documents=[
            document(
                "The bakery sells vegan cinnamon rolls every Friday and Saturday.",
                "menu.md",
            )
        ],
        conversation_history=[
            stored_message(
                event_id="vegan-1",
                sender_type="CUSTOMER",
                body="I am planning breakfast for Saturday.",
                minute=20,
            )
        ],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name="Amina",
            minutes_since_last_customer_message=2,
            should_greet_customer=False,
            greeting_reason="active conversation; avoid repeated greeting",
        ),
        tenant_config=tenant_config(),
        forbidden_literals=(CONTACT_EMAIL, CONTACT_PHONE, FACEBOOK_LINK, INSTAGRAM_LINK),
        forbidden_phrases=("contact us", "get in touch", "reach us"),
    ),
    LiveAnswerCase(
        name="does_not_repeat_contact_information_from_history",
        query="What is the nature of your business?",
        documents=[document("On weekdays, the bakery closes at 6:00 PM.", "hours.md")],
        conversation_history=[
            stored_message(
                event_id="contact-1",
                sender_type="CUSTOMER",
                body="How can I contact the bakery?",
                minute=30,
            ),
            stored_message(
                event_id="contact-2",
                sender_type="BOT",
                body=f"Email {CONTACT_EMAIL} or call {CONTACT_PHONE}.",
                minute=31,
            ),
        ],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name="Amina",
            minutes_since_last_customer_message=1,
            should_greet_customer=False,
            greeting_reason="active conversation; avoid repeated greeting",
        ),
        tenant_config=tenant_config(),
        forbidden_literals=(CONTACT_EMAIL, CONTACT_PHONE),
    ),
    LiveAnswerCase(
        name="does_not_proactively_offer_unavailable_handover",
        query="Can I change tomorrow's pickup time?",
        documents=[
            document(
                "Customers can change a pickup time from the Orders page until two hours "
                "before collection. Live-agent handover is not available.",
                "orders.md",
            )
        ],
        conversation_history=[
            stored_message(
                event_id="order-1",
                sender_type="CUSTOMER",
                body="I placed order HP-1042 for tomorrow.",
                minute=40,
            )
        ],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name="Amina",
            minutes_since_last_customer_message=3,
            should_greet_customer=False,
            greeting_reason="active conversation; avoid repeated greeting",
        ),
        tenant_config=tenant_config(),
        forbidden_literals=(CONTACT_EMAIL, CONTACT_PHONE),
        forbidden_phrases=(
            "support team",
            "human agent",
            "representative",
            "handover",
            "transfer",
            "escalat",
        ),
    ),
    LiveAnswerCase(
        name="states_unavailable_handover_without_claiming_transfer",
        query="Please connect me to a person about my order.",
        documents=[
            document(
                "Live-agent handover and transfer to a support team are not available. "
                "Order changes can be made from the Orders page.",
                "support.md",
            )
        ],
        conversation_history=[
            stored_message(
                event_id="handover-1",
                sender_type="CUSTOMER",
                body="I need help changing order HP-1042.",
                minute=50,
            )
        ],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name="Amina",
            minutes_since_last_customer_message=1,
            should_greet_customer=False,
            greeting_reason="active conversation; avoid repeated greeting",
        ),
        tenant_config=tenant_config(),
        forbidden_literals=(CONTACT_EMAIL, CONTACT_PHONE),
        forbidden_phrases=TRANSFER_CLAIMS,
        require_unavailable_handover=True,
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", LIVE_CASES, ids=lambda case: case.name)
async def test_live_llm_answer_prompt_behavior(case: LiveAnswerCase) -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY is required for live OpenAI integration tests")

    settings = Settings()
    generator = create_openai_answer_generator(
        api_key=api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
    )

    result = await generator.generate(
        case.query,
        case.documents,
        conversation_history=case.conversation_history,
        conversation_metadata=case.conversation_metadata,
        tenant_config=case.tenant_config,
    )
    answer = result.answer.strip()
    normalized_answer = answer.casefold().replace("’", "'").replace("‘", "'")
    logger.info(
        "Live answer prompt result case=%s model=%s answer_found=%s grounded=%s "
        "confidence=%.3f answer=%s",
        case.name,
        settings.llm_model,
        result.answer_found,
        result.grounded,
        result.confidence,
        answer,
    )

    assert SYSTEM_PROMPT
    assert answer, f"Expected a non-empty answer for {case.name}"
    assert result.answer_found is True, f"Expected a grounded answer for {case.name}: {answer}"
    assert result.grounded is True, f"Expected grounded=true for {case.name}: {answer}"

    greeting_match = GREETING_PATTERN.match(answer)
    name_salutation = normalized_answer.startswith("amina")
    if case.should_greet:
        assert greeting_match, f"Expected a greeting for {case.name}: {answer}"
        if case.expected_name_in_greeting:
            assert case.expected_name_in_greeting.casefold() in normalized_answer[:100], (
                f"Expected customer name in greeting for {case.name}: {answer}"
            )
    else:
        assert not greeting_match, f"Unexpected greeting for {case.name}: {answer}"
        assert not name_salutation, f"Unexpected name salutation for {case.name}: {answer}"

    for literal in case.forbidden_literals:
        assert literal.casefold() not in normalized_answer, (
            f"Unexpected contact detail {literal!r} for {case.name}: {answer}"
        )
    for phrase in case.forbidden_phrases:
        assert phrase.casefold() not in normalized_answer, (
            f"Unexpected phrase {phrase!r} for {case.name}: {answer}"
        )
    if case.require_unavailable_handover:
        assert any(phrase in normalized_answer for phrase in UNAVAILABLE_PHRASES), (
            f"Expected an explicit unavailable-handover statement for {case.name}: {answer}"
        )
