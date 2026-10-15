"""Shared onboarding taxonomy and narrowly scoped template cleanup."""

import json
from pathlib import Path

DEFINITION = json.loads(Path(__file__).with_name("business-information.json").read_text())
BUSINESS_CATEGORIES = DEFINITION["categories"]
GUIDANCE = {f"[Guidance: {text}]" for _, text in DEFINITION["sections"]}
HEADINGS = {f"## {heading}" for heading, _ in DEFINITION["sections"]}


def clean_business_summary(value: str) -> str:
    return "\n".join(
        line
        for line in value.splitlines()
        if line.strip().replace("\\[", "[").replace("\\]", "]") not in GUIDANCE
    ).strip()


def has_business_content(value: str) -> bool:
    return any(
        line.strip() and line.strip() not in HEADINGS
        for line in clean_business_summary(value).splitlines()
    )
