from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from classroom_interpreter import __version__


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Classroom Interpreter"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Classroom Interpreter"
    app_version: str = __version__
    app_phase: str = "PHASE_1_SKELETON"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    classroom_interpreter_data_dir: Path = Field(default_factory=default_data_dir)
    log_level: str = "INFO"

    mimo_api_key: str | None = None
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_asr_model: str = "mimo-v2.5-asr"
    mimo_translation_model: str = "mimo-v2.5"
    mimo_summary_model: str = "mimo-v2.5-pro"
    mimo_asr_language: str = "en"

    debug_classroom_content: bool = False

    segment_min_seconds: float = Field(default=2, gt=0)
    segment_preferred_seconds: float = Field(default=12, gt=0)
    segment_max_seconds: float = Field(default=30, gt=0)
    segment_silence_milliseconds: int = Field(default=700, gt=0)
    segment_overlap_milliseconds: int = Field(default=250, ge=0)
    vad_threshold_rms: float = Field(default=300, gt=0)
    context_max_segments: int = Field(default=8, gt=0)
    context_max_seconds: int = Field(default=90, gt=0)
    context_max_tokens: int = Field(default=1200, gt=0)

    asr_worker_count: int = Field(default=2, ge=1, le=8)
    asr_lease_seconds: float = Field(default=120, gt=0)
    asr_max_attempts: int = Field(default=5, ge=1)
    asr_backoff_base_seconds: float = Field(default=1, gt=0)
    asr_backoff_cap_seconds: float = Field(default=60, gt=0)
    asr_request_timeout_seconds: float = Field(default=30, gt=0)
    asr_backoff_jitter_fraction: float = Field(default=0.1, ge=0, le=1)

    translation_worker_count: int = Field(default=2, ge=1, le=8)
    translation_lease_seconds: float = Field(default=120, gt=0)
    translation_max_attempts: int = Field(default=5, ge=1)
    translation_backoff_base_seconds: float = Field(default=1, gt=0)
    translation_backoff_cap_seconds: float = Field(default=60, gt=0)
    translation_request_timeout_seconds: float = Field(default=30, gt=0)

    summary_chunk_token_budget: int = Field(default=3000, gt=0)
    summary_max_attempts: int = Field(default=3, ge=1)

    @field_validator("app_host")
    @classmethod
    def host_must_be_loopback(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("V1 APP_HOST must be exactly 127.0.0.1")
        return value

    @field_validator("mimo_asr_language")
    @classmethod
    def language_must_be_english(cls, value: str) -> str:
        if value != "en":
            raise ValueError("V1 MIMO_ASR_LANGUAGE must be en")
        return value

    @field_validator(
        "mimo_base_url",
        "mimo_asr_model",
        "mimo_translation_model",
        "mimo_summary_model",
    )
    @classmethod
    def provider_values_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider configuration values must not be blank")
        return value.strip()

    @field_validator("classroom_interpreter_data_dir")
    @classmethod
    def data_dir_must_not_be_broad(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if resolved in {Path("/"), Path.home().resolve()}:
            raise ValueError("runtime data directory cannot be / or the home directory")
        return resolved

    @model_validator(mode="after")
    def segment_bounds_must_be_ordered(self) -> Self:
        if not (
            self.segment_min_seconds
            <= self.segment_preferred_seconds
            <= self.segment_max_seconds
        ):
            raise ValueError("segment durations must satisfy min <= preferred <= max")
        if self.segment_overlap_milliseconds >= self.segment_max_seconds * 1000:
            raise ValueError("segment overlap must be shorter than maximum segment duration")
        return self

    @property
    def database_path(self) -> Path:
        return self.classroom_interpreter_data_dir.expanduser() / "runtime.sqlite3"


def get_settings() -> Settings:
    return Settings()
