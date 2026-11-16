import pytest
from sharepoint_rag import config


def test_database_url_is_required(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        config.require_database_url()


def test_database_url_returned_when_set(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@h/db")
    assert config.require_database_url() == "postgresql://u:p@h/db"
