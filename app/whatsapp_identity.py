"""WhatsApp phone and business-scoped identifiers (kept opaque and tenant-scoped)."""

import re


def is_bsuid(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"[A-Z]{2}\.(?:ENT\.)?[A-Za-z0-9]{1,128}", value
    ) is not None


def whatsapp_identity(*values: object) -> str | None:
    """Prefer the existing phone identity when available; never stringify missing values."""
    for value in values:
        if isinstance(value, str) and (
            re.fullmatch(r"[0-9]{1,32}", value) or is_bsuid(value)
        ):
            return value
    return None


def message_identities(message: dict, direction: str) -> set[str]:
    return {
        identity
        for key in (direction, f"{direction}_user_id", f"{direction}_parent_user_id")
        if (identity := whatsapp_identity(message.get(key))) is not None
    }


def message_identity(message: dict, direction: str) -> str | None:
    return whatsapp_identity(
        message.get(direction),
        message.get(f"{direction}_user_id"),
        message.get(f"{direction}_parent_user_id"),
    )
