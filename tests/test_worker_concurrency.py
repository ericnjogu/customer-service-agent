import pytest
from pydantic import ValidationError

from app.config import Settings


def test_worker_concurrency_defaults(monkeypatch):
    monkeypatch.delenv("AGENT_ISSUE_PROCESSING_CONCURRENCY", raising=False)
    monkeypatch.delenv("AGENT_MEDIA_ARCHIVE_CONCURRENCY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.issue_processing_concurrency == 4
    assert settings.media_archive_concurrency == 1


def test_worker_concurrency_environment(monkeypatch):
    monkeypatch.setenv("AGENT_ISSUE_PROCESSING_CONCURRENCY", "6")
    monkeypatch.setenv("AGENT_MEDIA_ARCHIVE_CONCURRENCY", "2")
    settings = Settings(_env_file=None)
    assert settings.issue_processing_concurrency == 6
    assert settings.media_archive_concurrency == 2


@pytest.mark.parametrize("field,value", [
    ("issue_processing_concurrency", 0), ("issue_processing_concurrency", 33),
    ("media_archive_concurrency", 0), ("media_archive_concurrency", 5),
])
def test_invalid_worker_concurrency(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
