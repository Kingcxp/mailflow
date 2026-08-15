"""Typed runtime configuration for MailFlow.

All secrets (API tokens) may be supplied either inline or as a whole-string
``${ENV_VARIABLE}`` placeholder that is expanded at load time. Only whole-string
placeholders are expanded; ``prefix-${VAR}-suffix`` is left literal.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator
from pydantic.fields import FieldInfo

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
    language: str = Field(
        default="en", description="Default display language code (en, zh-CN, or an external pack)"
    )
    timezone: str = Field(
        default="UTC", description="IANA timezone used for display, cleanup and reminders"
    )
    mail_retention_days: int = Field(
        default=30,
        ge=0,
        description="Mails older than this many days are moved to trash by the daily cleanup",
    )
    trash_retention_days: int = Field(
        default=7,
        ge=1,
        description="Trash records older than this many days (from deletion) are purged",
    )
    cleanup_hour: int = Field(
        default=4, ge=0, le=23, description="Local-time hour of the daily retention cleanup"
    )
    cleanup_minute: int = Field(
        default=0, ge=0, le=59, description="Local-time minute of the daily retention cleanup"
    )
    queue_size: int = Field(default=500, ge=1, description="Bounded inbound mail queue size")
    workers: int = Field(default=2, ge=1, description="Concurrent pipeline workers")
    reminder_days_before: int = Field(
        default=2,
        ge=0,
        le=30,
        description="Days before a timed action's due date that the early reminder fires",
    )
    reminder_hour: int = Field(
        default=8,
        ge=0,
        le=23,
        description="Local-time hour of the early reminder on the days-before date",
    )
    reminder_minute: int = Field(
        default=0,
        ge=0,
        le=59,
        description="Local-time minute of the early reminder on the days-before date",
    )
    reminder_interval_seconds: int = Field(
        default=60,
        ge=10,
        le=3600,
        description="How often the reminder scheduler checks due action items",
    )

    @model_validator(mode="after")
    def validate_timezone(self) -> GeneralConfig:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {self.timezone!r}") from exc
        return self


class LoggingConfig(BaseModel):
    """Console/file/jsonl sinks; levels and redirect targets are all configurable."""

    level: str = Field(default="INFO", description="Default level for the mailflow logger tree")
    console: bool = Field(default=True, description="Emit rich console output")
    console_level: str = Field(default="INFO", description="Console sink level")
    console_redirect: str | None = Field(
        default=None, description="Optional path: mirror console output to this file as well"
    )
    file: bool = Field(default=True, description="Write a rotating text log file")
    file_path: str = Field(default="logs/mailflow.log", description="Text log file path")
    file_level: str = Field(default="INFO", description="File sink level")
    file_max_bytes: int = Field(default=10 * 1024 * 1024, description="Rotating file max bytes")
    file_backup_count: int = Field(default=5, description="Rotating file backup count")
    jsonl: bool = Field(default=False, description="Write a JSON-lines log file")
    jsonl_path: str = Field(default="logs/mailflow.jsonl", description="JSONL log file path")
    jsonl_level: str = Field(default="INFO", description="JSONL sink level")
    logger_levels: dict[str, str] = Field(
        default_factory=lambda: {},
        description="Per-logger level overrides, e.g. mailflow.runtime = DEBUG",
    )

    @model_validator(mode="after")
    def validate_levels(self) -> LoggingConfig:
        for level in (
            self.level,
            self.console_level,
            self.file_level,
            self.jsonl_level,
            *self.logger_levels.values(),
        ):
            if level.upper() not in _LOG_LEVELS:
                raise ValueError(f"invalid log level {level!r}")
        return self


class PluginRepositoryConfig(BaseModel):
    name: str = Field(description="Repository display name")
    url: str = Field(description="URL of the repository's plugins.json index")


class PluginConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [],
        description="Plugin id allowlist; non-empty means only these load",
    )
    disabled: list[str] = Field(
        default_factory=lambda: [], description="Plugin ids that are never loaded"
    )
    repositories: list[PluginRepositoryConfig] = Field(
        default_factory=lambda: [],
        description="Plugin marketplaces: named URLs whose plugins.json indexes the market browses",
    )

    @model_validator(mode="after")
    def no_overlap(self) -> PluginConfig:
        overlap = set(self.enabled) & set(self.disabled)
        if overlap:
            raise ValueError(f"plugin ids in both enabled and disabled: {sorted(overlap)}")
        return self


class MailAccountConfig(BaseModel):
    account_id: str = Field(description="Unique account identifier")
    provider: str = Field(description="Mail source adapter component id (e.g. fake)")
    email: str = Field(default="", description="Account email address")
    enabled: bool = Field(default=True, description="Whether this account is polled")
    options: dict[str, Any] = Field(
        default_factory=lambda: {},
        description="Provider-specific settings (e.g. fake mail definitions)",
    )


class LLMConfig(BaseModel):
    """One named, OpenAI-compatible LLM. ``provider`` selects the backend plugin."""

    llm_id: str = Field(description="Unique name processors reference")
    name: str = Field(default="", description="Human-readable display name")
    provider: str = Field(
        default="openai-compatible", description="LLM backend adapter component id"
    )
    base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Remote OpenAI-compatible base URL (the chat path is appended)",
    )
    api_key: str = Field(
        default="", description="API token (secret; prefer api_key_env or ${ENV_VAR})"
    )
    api_key_env: str | None = Field(
        default=None, description="Environment variable holding the API token"
    )
    model: str = Field(default="gpt-4o-mini", description="Model identifier sent in the request")
    headers: dict[str, str] = Field(
        default_factory=lambda: {},
        description="Extra HTTP headers (secret values are redacted from output)",
    )
    query: dict[str, str] = Field(
        default_factory=lambda: {}, description="Extra query-string parameters"
    )
    extra_body: dict[str, Any] = Field(
        default_factory=lambda: {}, description="Extra JSON body fields merged into every request"
    )
    timeout_seconds: float = Field(
        default=60.0, ge=1.0, description="Per-request timeout in seconds"
    )
    max_retries: int = Field(default=2, ge=0, le=20, description="Bounded transport retries")
    default: bool = Field(
        default=False,
        description="Mark this LLM as the default for processors without an explicit one",
    )
    fallback: list[str] = Field(
        default_factory=lambda: [], description="Named LLMs tried in order when this one fails"
    )
    options: dict[str, Any] = Field(
        default_factory=lambda: {},
        description="Backend-specific options (e.g. path = chat/completions)",
    )

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
    processor_id: str = Field(description="Unique processor instance name")
    provider: str = Field(description="Processor plugin component id (e.g. rules, llm-importance)")
    enabled: bool = Field(default=True, description="Whether this processor runs in the chain")
    priority: int = Field(
        default=100, ge=0, description="Ascending execution order in the pipeline"
    )
    llm: str | None = Field(
        default=None, description="Named LLM id; None for rule-based processors"
    )
    fallback_llms: list[str] = Field(
        default_factory=lambda: [], description="Ordered fallback named LLMs"
    )
    failure_policy: str = Field(
        default="continue",
        description="continue runs the next processor after failure; stop halts the chain",
    )
    retries: int = Field(
        default=1, ge=0, le=5, description="Extra attempts after the initial processor run"
    )
    timeout_seconds: float = Field(
        default=30.0, ge=1.0, description="Per-processor timeout in seconds"
    )
    options: dict[str, Any] = Field(
        default_factory=lambda: {}, description="Processor-specific options"
    )

    @model_validator(mode="after")
    def validate_failure_policy(self) -> ProcessorConfig:
        if self.failure_policy not in _FAILURE_POLICIES:
            raise ValueError(
                f"processor {self.processor_id!r}: failure_policy must be one of {sorted(_FAILURE_POLICIES)}"
            )
        return self


class NotifierConfig(BaseModel):
    notifier_id: str = Field(description="Unique notifier instance name")
    provider: str = Field(description="Notifier plugin component id (e.g. console)")
    enabled: bool = Field(default=True, description="Whether this notifier is active")
    minimum_urgency: Urgency = Field(
        default=Urgency.IMPORTANT, description="Only mail at or above this urgency is delivered"
    )
    options: dict[str, Any] = Field(
        default_factory=lambda: {}, description="Notifier-specific options"
    )


class StorageConfig(BaseModel):
    provider: str = Field(default="sqlite", description="Storage backend component id")
    path: str = Field(default="data/mailflow.db", description="Database file path")
    options: dict[str, Any] = Field(
        default_factory=lambda: {}, description="Backend-specific options"
    )


class I18nConfig(BaseModel):
    language: str = Field(default="en", description="Display language code")
    extra_dirs: list[str] = Field(
        default_factory=lambda: [],
        description="Directories containing data-only JSON language packs",
    )


class MailFlowConfig(BaseModel):
    general: GeneralConfig = Field(
        default_factory=GeneralConfig,
        description="Runtime-wide behavior: language, timezone, retention, reminders",
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig, description="Console/file/jsonl log sinks and levels"
    )
    plugins: PluginConfig = Field(
        default_factory=PluginConfig, description="Plugin allowlist/denylist"
    )
    accounts: list[MailAccountConfig] = Field(
        default_factory=lambda: [], description="Mail accounts to poll"
    )
    llms: list[LLMConfig] = Field(
        default_factory=lambda: [], description="Named LLMs and their request configuration"
    )
    processors: list[ProcessorConfig] = Field(
        default_factory=lambda: [], description="The ordered processing chain"
    )
    notifiers: list[NotifierConfig] = Field(
        default_factory=lambda: [], description="Notification channels with urgency thresholds"
    )
    storage: StorageConfig = Field(
        default_factory=StorageConfig, description="Durable storage backend"
    )
    i18n: I18nConfig = Field(
        default_factory=I18nConfig, description="Language and external language-pack directories"
    )

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


# ---------------------------------------------------------------------------
# Config inspection and mutation (used by the `config` command and the TUI)
# ---------------------------------------------------------------------------

_SECRET_MARKERS = ("api_key", "token", "password", "secret", "authorization")


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def redact_value(key: str, value: Any) -> Any:
    """Mask secret values before they reach command/TUI output."""
    if value is None or value == "":
        return value
    if is_secret_key(key):
        return "***"
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        return {k: redact_value(k, v) for k, v in mapping.items()}
    return value


@dataclass(frozen=True)
class OptionInfo:
    """One configurable option, as shown by `config list`."""

    key: str  # dotted path, e.g. general.reminder_hour or accounts[].provider
    group: str
    type_name: str
    required: bool
    default: Any
    description: str
    value: Any  # current effective value (already redacted by the caller)

    def is_secret(self) -> bool:
        return is_secret_key(self.key)


def _field_info(field: FieldInfo) -> tuple[str, bool, Any]:
    annotation = field.annotation
    type_name = getattr(annotation, "__name__", str(annotation))
    return (type_name, field.is_required(), field.get_default(call_default_factory=False))


def _walk_group(prefix: str, model: BaseModel) -> list[OptionInfo]:
    options: list[OptionInfo] = []
    for name, field in type(model).__pydantic_fields__.items():
        key = f"{prefix}.{name}" if prefix else name
        type_name, required, default = _field_info(field)
        description = field.description or ""
        value = redact_value(name, getattr(model, name))
        options.append(
            OptionInfo(
                key=key,
                group=prefix,
                type_name=type_name,
                required=required,
                default=default,
                description=description,
                value=value,
            )
        )
    return options


def inspect_config(config: MailFlowConfig) -> list[OptionInfo]:
    """Flatten the config into per-option rows for commands and the TUI."""
    options: list[OptionInfo] = []
    for group_name, field in type(config).__pydantic_fields__.items():
        type_name, required, default = _field_info(field)
        options.append(
            OptionInfo(
                key=group_name,
                group="",
                type_name=type_name,
                required=required,
                default=default,
                description=field.description or "",
                value=redact_value(group_name, getattr(config, group_name)),
            )
        )
        child = getattr(config, group_name)
        if isinstance(child, BaseModel):
            options.extend(_walk_group(group_name, child))
        elif isinstance(child, list) and child:
            first = cast(Any, child)[0]
            if isinstance(first, BaseModel):
                options.extend(_walk_group(f"{group_name}[]", first))
    return options


def find_option(config: MailFlowConfig, key: str) -> OptionInfo | None:
    for option in inspect_config(config):
        if option.key == key:
            return option
    return None


def set_option_value(config: MailFlowConfig, key: str, raw_value: str) -> MailFlowConfig:
    """Return a new validated config with ``key`` set to the coerced value."""
    parts = key.split(".")
    if len(parts) < 2 or parts[0] not in type(config).__pydantic_fields__:
        raise KeyError(f"unknown config option {key!r}")
    group = parts[0]
    if group in ("accounts", "llms", "processors", "notifiers"):
        raise KeyError(f"{group!r} entries are lists; edit them in the TOML file")
    data = config.model_dump()
    target: Any = data[group]
    for part in parts[1:]:
        if not isinstance(target, dict) or part not in target:
            raise KeyError(f"unknown config option {key!r}")
        target = cast(dict[str, Any], target)[part]
    if isinstance(target, (dict, list)):
        raise KeyError(f"{key!r} is a structured option; edit the TOML file instead")
    if isinstance(target, bool):
        parsed: Any = raw_value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(target, int):
        parsed = int(raw_value)
    elif isinstance(target, float):
        parsed = float(raw_value)
    else:
        parsed = raw_value
    # walk the dump again to place the parsed value at the right node
    node: dict[str, Any] = data
    for part in parts[:-1]:
        node = cast(dict[str, Any], node[part])
    node[parts[-1]] = parsed
    return MailFlowConfig.model_validate(data)


def write_config(config: MailFlowConfig, path: str | Path) -> None:
    """Persist the current config to a TOML file (tomli-w)."""
    import tomli_w

    def strip_none(value: Any) -> Any:
        if isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            return {
                k: strip_none(v)
                for k, v in mapping.items()
                if v is not None  # pyright: ignore[reportUnknownVariableType]
            }
        if isinstance(value, list):
            return [strip_none(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return value

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        tomli_w.dump(strip_none(config.model_dump(mode="python")), handle)


def patch_config_value(path: str | Path, key: str, value: Any) -> bool:
    """Rewrite one ``key = value`` line in place, preserving comments.

    Returns True when the line existed and was patched; False when the key
    was absent (caller should fall back to a full rewrite).
    """
    path = Path(path)
    if not path.is_file():
        return False
    raw = path.read_text(encoding="utf-8")
    if isinstance(value, bool):
        value_repr = "true" if value else "false"
    elif isinstance(value, (int, float)):
        value_repr = str(value)
    else:
        value_repr = json.dumps(str(value))
    group, _, leaf = key.partition(".")
    patched = _patch_section_line(raw, group, leaf, value_repr)
    if patched is None:
        return False
    path.write_text(patched, encoding="utf-8")
    return True


def _patch_section_line(raw: str, group: str, leaf: str, value_repr: str) -> str | None:
    """Replace ``leaf = ...`` inside the ``[group]`` section; None if absent."""
    lines = raw.splitlines(keepends=True)
    start = -1
    end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            if section == group and start == -1:
                start = index
            elif start != -1:
                end = index
                break
    if start == -1:
        # no matching section header (e.g. group-less file): search the whole file
        start, end = 0, len(lines)
    for index in range(start, end):
        stripped = lines[index].strip()
        if re.match(rf"{re.escape(leaf)}\s*=", stripped):
            lines[index] = re.sub(
                rf"^(\s*{re.escape(leaf)}\s*=\s*).*$",
                rf"\g<1>{value_repr}",
                lines[index],
            )
            return "".join(lines)
    return None
