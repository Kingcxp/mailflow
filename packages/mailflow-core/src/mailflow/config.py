"""Typed runtime configuration for MailFlow.

All secrets (API tokens) may be supplied either inline or as a whole-string
``${ENV_VARIABLE}`` placeholder that is expanded at load time. Only whole-string
placeholders are expanded; ``prefix-${VAR}-suffix`` is left literal.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator

from mailflow.domain import Urgency

_ENV_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_FAILURE_POLICIES = {"continue", "stop"}


def _interpolate(value: JsonValue, origin: str) -> JsonValue:
    """Expand whole-string ``${VAR}`` placeholders recursively."""
    if isinstance(value, str):
        return _expand_string(value, origin)
    if isinstance(value, dict):
        return _interpolate_mapping(value, origin)
    if isinstance(value, list):
        return _interpolate_sequence(value, origin)
    return value


def _expand_string(value: str, origin: str) -> str:
    match = _ENV_RE.match(value)
    if match:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"environment variable {name!r} (referenced by {origin}) is not set")
        return os.environ[name]
    return value


def _interpolate_mapping(value: dict[str, JsonValue], origin: str) -> dict[str, JsonValue]:
    return {k: _interpolate(v, f"{origin}.{k}") for k, v in value.items()}


def _interpolate_sequence(value: list[JsonValue], origin: str) -> list[JsonValue]:
    return [_interpolate(v, f"{origin}[{i}]") for i, v in enumerate(value)]


class GeneralConfig(BaseModel):
    language: str = "en"
    timezone: str = "UTC"
    mail_retention_days: int = Field(default=30, ge=0)
    trash_retention_days: int = Field(default=7, ge=1)
    cleanup_hour: int = Field(default=4, ge=0, le=23)
    cleanup_minute: int = Field(default=0, ge=0, le=59)
    queue_size: int = Field(default=500, ge=1)
    workers: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def validate_timezone(self) -> GeneralConfig:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {self.timezone!r}") from exc
        return self


class LoggingConfig(BaseModel):
    """Console/file/jsonl sinks; levels and redirect targets are all configurable."""

    level: str = "INFO"
    console: bool = True
    console_level: str = "INFO"
    # Optional path: when set, console output is also mirrored to this file.
    console_redirect: str | None = None
    file: bool = True
    file_path: str = "logs/mailflow.log"
    file_level: str = "INFO"
    file_max_bytes: int = 10 * 1024 * 1024
    file_backup_count: int = 5
    jsonl: bool = False
    jsonl_path: str = "logs/mailflow.jsonl"
    jsonl_level: str = "INFO"

    @model_validator(mode="after")
    def validate_levels(self) -> LoggingConfig:
        for level in (
            self.level,
            self.console_level,
            self.file_level,
            self.jsonl_level,
        ):
            if level.upper() not in _LOG_LEVELS:
                raise ValueError(f"invalid log level {level!r}")
        return self


class PluginConfig(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: [])  # non-empty = allowlist
    disabled: list[str] = Field(default_factory=lambda: [])

    @model_validator(mode="after")
    def no_overlap(self) -> PluginConfig:
        overlap = set(self.enabled) & set(self.disabled)
        if overlap:
            raise ValueError(f"plugin ids in both enabled and disabled: {sorted(overlap)}")
        return self


class MailAccountConfig(BaseModel):
    account_id: str
    provider: str  # mail source plugin id
    email: str = ""
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=lambda: {})


class LLMConfig(BaseModel):
    """One named, OpenAI-compatible LLM. ``provider`` selects the backend plugin."""

    llm_id: str
    name: str = ""
    provider: str = "mailflow-llm-openai-compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str | None = None
    model: str = "gpt-4o-mini"
    headers: dict[str, str] = Field(default_factory=lambda: {})
    query: dict[str, str] = Field(default_factory=lambda: {})
    extra_body: dict[str, Any] = Field(default_factory=lambda: {})
    timeout_seconds: float = Field(default=60.0, ge=1.0)
    max_retries: int = Field(default=2, ge=0, le=20)
    default: bool = False
    fallback: list[str] = Field(default_factory=lambda: [])
    options: dict[str, Any] = Field(default_factory=lambda: {})

    @model_validator(mode="after")
    def resolve_key(self) -> LLMConfig:
        if self.api_key_env and not self.api_key:
            if self.api_key_env not in os.environ:
                raise ValueError(
                    f"llm {self.llm_id!r}: api_key_env {self.api_key_env!r} is not set"
                )
            self.api_key = os.environ[self.api_key_env]
        return self


class ProcessorConfig(BaseModel):
    processor_id: str
    provider: str  # processor plugin id
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    llm: str | None = None  # named LLM id; None = rule-based processor
    fallback_llms: list[str] = Field(default_factory=lambda: [])
    failure_policy: str = "continue"
    retries: int = Field(default=1, ge=0, le=5)
    timeout_seconds: float = Field(default=30.0, ge=1.0)
    options: dict[str, Any] = Field(default_factory=lambda: {})

    @model_validator(mode="after")
    def validate_failure_policy(self) -> ProcessorConfig:
        if self.failure_policy not in _FAILURE_POLICIES:
            raise ValueError(
                f"processor {self.processor_id!r}: failure_policy must be one of {sorted(_FAILURE_POLICIES)}"
            )
        return self


class NotifierConfig(BaseModel):
    notifier_id: str
    provider: str
    enabled: bool = True
    minimum_urgency: Urgency = Urgency.IMPORTANT
    options: dict[str, Any] = Field(default_factory=lambda: {})


class StorageConfig(BaseModel):
    provider: str = "mailflow-storage-sqlite"
    path: str = "data/mailflow.db"
    options: dict[str, Any] = Field(default_factory=lambda: {})


class I18nConfig(BaseModel):
    language: str = "en"
    extra_dirs: list[str] = Field(default_factory=lambda: [])


class MailFlowConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    accounts: list[MailAccountConfig] = Field(default_factory=lambda: [])
    llms: list[LLMConfig] = Field(default_factory=lambda: [])
    processors: list[ProcessorConfig] = Field(default_factory=lambda: [])
    notifiers: list[NotifierConfig] = Field(default_factory=lambda: [])
    storage: StorageConfig = Field(default_factory=StorageConfig)
    i18n: I18nConfig = Field(default_factory=I18nConfig)

    # -- cross-reference validation ------------------------------------------

    @model_validator(mode="after")
    def validate_references(self) -> MailFlowConfig:
        llm_ids = {llm.llm_id for llm in self.llms}

        for llm in self.llms:
            for fallback in llm.fallback:
                if fallback not in llm_ids:
                    raise ValueError(
                        f"llm {llm.llm_id!r} fallback {fallback!r} does not match any configured llm"
                    )
        defaults = [llm.llm_id for llm in self.llms if llm.default]
        if len(defaults) > 1:
            raise ValueError(f"multiple default llms configured: {defaults}")

        for processor in self.processors:
            if processor.llm is not None and processor.llm not in llm_ids:
                raise ValueError(
                    f"processor {processor.processor_id!r} references unknown llm {processor.llm!r}"
                )
            for fallback in processor.fallback_llms:
                if fallback not in llm_ids:
                    raise ValueError(
                        f"processor {processor.processor_id!r} references unknown fallback llm {fallback!r}"
                    )
            if processor.llm is None and processor.fallback_llms:
                raise ValueError(
                    f"processor {processor.processor_id!r} has fallback_llms but no primary llm"
                )

        return self

    # -- convenience accessors -------------------------------------------------

    def llm(self, llm_id: str) -> LLMConfig | None:
        return next((llm for llm in self.llms if llm.llm_id == llm_id), None)

    def default_llm(self) -> LLMConfig | None:
        return next((llm for llm in self.llms if llm.default), None) or (
            self.llms[0] if self.llms else None
        )

    def account(self, account_id: str) -> MailAccountConfig | None:
        return next((a for a in self.accounts if a.account_id == account_id), None)

    def processor(self, processor_id: str) -> ProcessorConfig | None:
        return next((p for p in self.processors if p.processor_id == processor_id), None)


def load_config(path: str | Path | None = None) -> MailFlowConfig:
    """Load and validate TOML config, expanding ${ENV_VAR} placeholders."""
    import tomllib

    if path is None:
        raw: dict[str, Any] = {}
    else:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    interpolated = _interpolate(raw, str(path) if path else "<memory>")
    return MailFlowConfig.model_validate(interpolated)
