from __future__ import annotations

from processing.config import ProcessingConfig


def test_processing_config_allows_model_override_from_env(monkeypatch):
    monkeypatch.setenv("ENDPOINT_LLM_MODEL", "gpt-5-mini")

    config = ProcessingConfig()

    assert config.llm_model == "gpt-5-mini"


def test_processing_config_strips_llm_api_key_whitespace(monkeypatch):
    monkeypatch.setenv("ENDPOINT_LLM_API_KEY", "  sk-test-value\n")

    config = ProcessingConfig()

    assert config.llm_api_key == "sk-test-value"


def test_processing_config_reads_dedup_enabled(monkeypatch):
    monkeypatch.setenv("ENDPOINT_DEDUP_ENABLED", "true")

    config = ProcessingConfig()

    assert config.dedup_enabled is True
