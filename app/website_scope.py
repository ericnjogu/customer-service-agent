"""Validation and hostname scoping shared by runtime retrieval and website search."""

from pydantic import HttpUrl, TypeAdapter, ValidationError

_url = TypeAdapter(HttpUrl)


def website_host(value: str | None) -> str | None:
    try:
        parsed = _url.validate_python(value)
    except ValidationError:
        return None
    if parsed.username or parsed.password:
        return None
    return parsed.host.lower().rstrip(".") if parsed.host else None


def website_domains(urls: list[str]) -> list[str]:
    return list(dict.fromkeys(host for url in urls if (host := website_host(url))))


def allowed_website_source(url: str | None, websites: list[str]) -> bool:
    host = website_host(url)
    return bool(host) and any(
        host == domain or host.endswith("." + domain) for domain in website_domains(websites)
    )
