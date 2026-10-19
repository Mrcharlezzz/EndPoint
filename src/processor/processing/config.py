from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProcessingConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENDPOINT_", extra="ignore")

    poll_interval_seconds: float = 5.0
    batch_size: int = 50
    max_concurrency: int = 10

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-5-nano"
    llm_prompt_version: str = "entity-inference-v3"
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 1
    university_timezone: str = "Europe/Amsterdam"

    batch_persist_retries: int = 2
    batch_persist_retry_delay_seconds: float = 1.0

    failure_mark_retries: int = 2
    failure_mark_retry_delay_seconds: float = 1.0

    course_title_match_threshold: float = 0.70
    dedup_enabled: bool = True

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def strip_llm_api_key(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value
