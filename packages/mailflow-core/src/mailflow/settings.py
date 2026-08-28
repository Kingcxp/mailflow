"""Editable settings model shared by every host.

``mailflow.config`` owns the typed schema; this module turns it into an
editor-shaped surface: sections (``general``, ``logging``, one per plugin
that owns configurable components, ...), per-option editors derived from the
pydantic field type, validation that reports *which* option is invalid, and
mutation helpers for scalars, lists and structured list entries.

Everything here is host-agnostic: the TUI settings screen, the ``config``
command and any embedding host use the same functions, so an option is
never editable in one surface and invisible in another.
"""

from __future__ import annotations

import json
from collections.abc import Sized
from dataclasses import dataclass, field
from enum import StrEnum
from types import UnionType
from typing import Any, Union, cast, get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from mailflow.config import MailFlowConfig, is_secret_key

# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

CORE_SECTION = "general"
"""Section id holding the options that belong to MailFlow itself."""

_CORE_GROUPS = ("general", "logging", "plugins", "storage", "i18n")
"""Top-level config groups that are always MailFlow's own, never a plugin's."""

_LIST_GROUPS = ("accounts", "llms", "processors", "notifiers")
"""Groups whose value is a list of entries, each editable as a form."""

_PROVIDER_FIELD = {
    "accounts": "provider",
    "llms": "provider",
    "processors": "provider",
    "notifiers": "provider",
}
"""Which field of a list entry names the component that owns it."""


class EditorKind(StrEnum):
    """How a host should render one option."""

    TEXT = "text"
    SECRET = "secret"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    CHOICE = "choice"  # closed set of values (enum / literal)
    STRING_LIST = "string_list"  # list[str]: editable rows
    MAPPING = "mapping"  # dict[str, str]: editable key/value rows
    STRUCT_LIST = "struct_list"  # list[BaseModel]: rows edited in a form
    STRUCT = "struct"  # nested model: navigated, not edited inline


@dataclass(frozen=True)
class OptionSpec:
    """One editable option: identity, editor, default and current value."""

    key: str  # dotted path, e.g. general.timezone or llms[0].model
    section: str  # section id this option is shown under
    label: str  # leaf name, e.g. timezone
    editor: EditorKind
    description: str
    default: Any
    value: Any  # current value, never redacted (hosts redact for display)
    required: bool = False
    choices: tuple[str, ...] = ()
    item_fields: tuple[OptionSpec, ...] = ()  # STRUCT_LIST entry schema

    @property
    def secret(self) -> bool:
        return self.editor is EditorKind.SECRET

    def display_value(self) -> str:
        """Short, non-secret rendering for list rows."""
        value: Any = self.value
        if self.secret and value:
            return "***"
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None or value == "":
            return "-"
        if isinstance(value, (list, dict)):
            return f"{len(cast(Sized, value))} item(s)"
        return str(value)

    def is_default(self) -> bool:
        current: Any = self.value
        return bool(current == self.default)


@dataclass
class SettingsSection:
    """A sidebar entry: MailFlow itself or one plugin that owns options."""

    section_id: str
    title: str
    plugin_id: str = ""  # "" for MailFlow's own sections
    options: list[OptionSpec] = field(default_factory=lambda: [])


# ---------------------------------------------------------------------------
# Field introspection
# ---------------------------------------------------------------------------


def _unwrap_optional(annotation: Any) -> Any:
    """Inner type of an ``X | None`` annotation (``X`` itself otherwise)."""
    if get_origin(annotation) in (Union, UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _editor_for(annotation: Any) -> tuple[EditorKind, tuple[str, ...], Any]:
    """Map a field annotation to ``(editor, choices, item_model)``."""
    inner = _unwrap_optional(annotation)
    origin = get_origin(inner)
    if isinstance(inner, type) and issubclass(inner, StrEnum):
        return EditorKind.CHOICE, tuple(str(member.value) for member in inner), None
    if inner is bool:
        return EditorKind.BOOLEAN, (), None
    if inner is int:
        return EditorKind.INTEGER, (), None
    if inner is float:
        return EditorKind.NUMBER, (), None
    if origin in (list, tuple):
        args = get_args(inner)
        item = args[0] if args else str
        if isinstance(item, type) and issubclass(item, BaseModel):
            return EditorKind.STRUCT_LIST, (), item
        return EditorKind.STRING_LIST, (), None
    if origin is dict:
        return EditorKind.MAPPING, (), None
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return EditorKind.STRUCT, (), inner
    return EditorKind.TEXT, (), None


def _field_default(info: FieldInfo) -> Any:
    """Schema default, or ``None`` for a required field.

    ``FieldInfo.get_default`` yields the ``PydanticUndefined`` sentinel for
    required fields; leaking it would render "PydanticUndefined" as a form
    value and let an empty required field validate.
    """
    default = info.get_default(call_default_factory=True)
    return None if default is PydanticUndefined else default


def _spec_for_field(
    *,
    key: str,
    section: str,
    name: str,
    info: FieldInfo,
    value: Any,
    language_choices: tuple[str, ...] = (),
) -> OptionSpec:
    editor, choices, item_model = _editor_for(info.annotation)
    if editor is EditorKind.TEXT and is_secret_key(name):
        editor = EditorKind.SECRET
    if key == "general.language" and language_choices:
        editor = EditorKind.CHOICE
        choices = tuple(language_choices)
    item_fields: tuple[OptionSpec, ...] = ()
    if editor is EditorKind.STRUCT_LIST and item_model is not None:
        item_fields = tuple(entry_field_specs(item_model, section=section))
    return OptionSpec(
        key=key,
        section=section,
        label=name,
        editor=editor,
        description=info.description or "",
        default=_field_default(info),
        value=value,
        required=info.is_required(),
        choices=choices,
        item_fields=item_fields,
    )


def _fields_of(model: type[BaseModel]) -> dict[str, FieldInfo]:
    """Typed view of a model's pydantic fields."""
    return model.__pydantic_fields__


def _as_list(value: Any) -> list[Any]:
    """Typed view of a value already known to be a list."""
    return list(value)


def _as_mapping(value: Any) -> dict[Any, Any]:
    """Typed view of a value already known to be a dict."""
    return dict(value)


def entry_field_specs(model: type[BaseModel], *, section: str = "") -> list[OptionSpec]:
    """Field specs describing one entry of a structured list (no values)."""
    return [
        _spec_for_field(
            key=name,
            section=section,
            name=name,
            info=info,
            value=_field_default(info),
        )
        for name, info in _fields_of(model).items()
    ]


# ---------------------------------------------------------------------------
# Section assembly
# ---------------------------------------------------------------------------


def _group_specs(
    config: MailFlowConfig, group: str, *, language_choices: tuple[str, ...] = ()
) -> list[OptionSpec]:
    model: Any = getattr(config, group)
    if not isinstance(model, BaseModel):
        return []
    return [
        _spec_for_field(
            key=f"{group}.{name}",
            section=group,
            name=name,
            info=info,
            value=getattr(model, name),
            language_choices=language_choices,
        )
        for name, info in _fields_of(type(model)).items()
    ]


def _entry_specs(config: MailFlowConfig, group: str, index: int, section: str) -> list[OptionSpec]:
    entries: list[Any] = getattr(config, group)
    entry: BaseModel = entries[index]
    return [
        _spec_for_field(
            key=f"{group}[{index}].{name}",
            section=section,
            name=name,
            info=info,
            value=getattr(entry, name),
        )
        for name, info in _fields_of(type(entry)).items()
    ]


def plugin_of_component(registry: Any, component_id: str) -> str:
    """Owning plugin id for a component id ("" when not registered)."""
    if registry is None:
        return ""
    plugin_id = registry.plugin_for(component_id)
    return str(plugin_id) if plugin_id else ""


def build_sections(
    config: MailFlowConfig,
    *,
    registry: Any = None,
    language_choices: tuple[str, ...] = (),
) -> list[SettingsSection]:
    """Sidebar model: MailFlow's own sections first, then one per plugin.

    ``processors`` and ``notifiers`` entries are grouped under the plugin
    that registered their ``provider`` component, so installing a plugin
    makes its options appear in its own section. Plugin sections are
    titled with their plugin id — the same lowercase-dash convention as
    the core sections. Accounts and LLMs are edited in their dedicated
    tabs and are deliberately excluded here.

    ``language_choices`` turns ``general.language`` into a dropdown of the
    host's available languages instead of a free-text field.
    """
    sections: list[SettingsSection] = [
        SettingsSection(
            section_id=group,
            title=group,
            options=_group_specs(config, group, language_choices=language_choices),
        )
        for group in _CORE_GROUPS
    ]
    by_plugin: dict[str, SettingsSection] = {}
    for group in ("processors", "notifiers"):
        for index, entry in enumerate(getattr(config, group)):
            provider = str(getattr(entry, _PROVIDER_FIELD[group], "") or "")
            plugin_id = plugin_of_component(registry, provider) or provider or group
            section = by_plugin.get(plugin_id)
            if section is None:
                # title stays empty: hosts render the id-style section_id
                section = SettingsSection(
                    section_id=plugin_id,
                    title="",
                    plugin_id=plugin_id,
                )
                by_plugin[plugin_id] = section
            section.options.extend(_entry_specs(config, group, index, section.section_id))
    sections.extend(by_plugin[key] for key in sorted(by_plugin))
    return [section for section in sections if section.options]


def find_spec(config: MailFlowConfig, key: str, **kwargs: Any) -> OptionSpec | None:
    for section in build_sections(config, **kwargs):
        for spec in section.options:
            if spec.key == key:
                return spec
    return None


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------


class SettingsError(ValueError):
    """An edit was rejected; ``option`` names the offending config path."""

    def __init__(self, option: str, message: str) -> None:
        super().__init__(message)
        self.option = option
        self.message = message


def _split_key(key: str) -> list[str | int]:
    """``llms[1].headers`` -> ``['llms', 1, 'headers']``."""
    parts: list[str | int] = []
    for chunk in key.split("."):
        name, bracket, rest = chunk.partition("[")
        if name:
            parts.append(name)
        if bracket:
            index, _, _ = rest.partition("]")
            try:
                parts.append(int(index))
            except ValueError as exc:
                raise SettingsError(key, f"unknown config option {key!r}") from exc
    return parts


def _carried_placeholders(
    config: MailFlowConfig,
    *,
    drop: tuple[str | int, ...] | None = None,
    group_removed: tuple[str, int] | None = None,
    group_swapped: tuple[str, int, int] | None = None,
) -> dict[tuple[str | int, ...], str]:
    """Placeholder map for a mutated config: ``drop`` removes one leaf,
    ``group_removed`` drops the entry's own placeholders and closes the gap,
    ``group_swapped`` follows entries across a list move (all affected
    indices shift, not just the two endpoints)."""
    carried: dict[tuple[str | int, ...], str] = {}
    for path, placeholder in config.env_placeholders.items():
        if drop is not None and path == drop:
            continue
        group = path[0] if path else None
        entry_index = path[1] if len(path) >= 2 else None
        if isinstance(group, str):
            if group_removed is not None:
                removed_group, removed_index = group_removed
                if group == removed_group and isinstance(entry_index, int):
                    if entry_index == removed_index:
                        continue  # placeholder belongs to the removed entry
                    if entry_index > removed_index:
                        carried[(removed_group, entry_index - 1, *path[2:])] = placeholder
                        continue
            if group_swapped is not None:
                swapped_group, source, destination = group_swapped
                if group == swapped_group and isinstance(entry_index, int):
                    # entries.insert(destination, entries.pop(source)) —
                    # map every old index to its new position so
                    # placeholders follow their entry across multi-step
                    # moves, not just the two endpoints.
                    if source < destination:
                        if entry_index == source:
                            new_index = destination
                        elif source < entry_index <= destination:
                            new_index = entry_index - 1
                        else:
                            new_index = entry_index
                    else:
                        if entry_index == source:
                            new_index = destination
                        elif destination <= entry_index < source:
                            new_index = entry_index + 1
                        else:
                            new_index = entry_index
                    if new_index != entry_index:
                        carried[(swapped_group, new_index, *path[2:])] = placeholder
                        continue
        carried[path] = placeholder
    return carried


def _navigate(data: Any, path: list[str | int], key: str) -> Any:
    node = data
    for part in path:
        try:
            node = node[part]
        except (KeyError, IndexError, TypeError) as exc:
            raise SettingsError(key, f"unknown config option {key!r}") from exc
    return node


def coerce_value(spec: OptionSpec, raw: Any) -> Any:
    """Parse a host-provided value into the option's Python type."""
    if spec.editor is EditorKind.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if spec.editor is EditorKind.INTEGER:
        if isinstance(raw, int):
            return raw
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise SettingsError(spec.key, f"{spec.label}: expected a whole number") from exc
    if spec.editor is EditorKind.NUMBER:
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            return float(str(raw).strip())
        except ValueError as exc:
            raise SettingsError(spec.key, f"{spec.label}: expected a number") from exc
    if spec.editor is EditorKind.STRING_LIST:
        if isinstance(raw, list):
            return [str(item) for item in _as_list(raw)]
        return [line.strip() for line in str(raw).splitlines() if line.strip()]
    if spec.editor is EditorKind.MAPPING:
        if isinstance(raw, dict):
            return {str(k): v for k, v in _as_mapping(raw).items()}
        return _parse_mapping(spec, str(raw))
    if spec.editor is EditorKind.CHOICE:
        value = str(raw).strip()
        if spec.choices and value not in spec.choices:
            allowed = ", ".join(spec.choices)
            raise SettingsError(spec.key, f"{spec.label}: must be one of {allowed}")
        return value
    if spec.editor in (EditorKind.TEXT, EditorKind.SECRET):
        text = "" if raw is None else str(raw)
        if text == "":
            if spec.required:
                raise SettingsError(spec.key, f"{spec.label} is required")
            if spec.default is None:
                return None  # optional string: empty means "unset"
        return text
    return raw


def _parse_mapping(spec: OptionSpec, raw: str) -> dict[str, Any]:
    """Accept JSON or ``key = value`` lines for a mapping option."""
    stripped = raw.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        try:
            parsed: Any = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SettingsError(spec.key, f"{spec.label}: invalid JSON ({exc.msg})") from exc
        if not isinstance(parsed, dict):
            raise SettingsError(spec.key, f"{spec.label}: expected a JSON object")
        return {str(k): v for k, v in _as_mapping(parsed).items()}
    mapping: dict[str, Any] = {}
    for line in stripped.splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition("=")
        if not separator:
            raise SettingsError(spec.key, f"{spec.label}: expected 'key = value' lines or JSON")
        mapping[name.strip()] = value.strip()
    return mapping


def _revalidate(data: dict[str, Any], key: str) -> MailFlowConfig:
    try:
        return MailFlowConfig.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(key, _format_validation_error(exc)) from exc
    except ValueError as exc:
        raise SettingsError(key, str(exc)) from exc


def _format_validation_error(exc: ValidationError) -> str:
    """One readable line naming the field and what is wrong with it."""
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid value"))
        messages.append(f"{location}: {message}" if location else message)
    return "; ".join(messages[:3])


def apply_value(config: MailFlowConfig, key: str, raw: Any, **kwargs: Any) -> MailFlowConfig:
    """Validated copy of ``config`` with ``key`` set to ``raw``.

    Raises :class:`SettingsError` (carrying the offending option key) when
    the value cannot be coerced or the resulting config fails validation.
    """
    spec = find_spec(config, key, **kwargs)
    if spec is None:
        raise SettingsError(key, f"unknown config option {key!r}")
    if spec.editor is EditorKind.STRUCT:
        raise SettingsError(key, f"{key!r} is a group; edit the options inside it")
    value = coerce_value(spec, raw)
    data = config.model_dump()
    path = _split_key(key)
    parent = _navigate(data, path[:-1], key)
    leaf = path[-1]
    try:
        parent[leaf] = value
    except (KeyError, IndexError, TypeError) as exc:
        raise SettingsError(key, f"unknown config option {key!r}") from exc
    updated = _revalidate(data, key)
    updated.env_placeholders = _carried_placeholders(config, drop=tuple(path))
    return updated


def reset_value(config: MailFlowConfig, key: str, **kwargs: Any) -> MailFlowConfig:
    """Validated copy of ``config`` with ``key`` back at its schema default."""
    spec = find_spec(config, key, **kwargs)
    if spec is None:
        raise SettingsError(key, f"unknown config option {key!r}")
    if spec.required and spec.default is None:
        raise SettingsError(key, f"{spec.label} is required and has no default")
    return apply_value(config, key, spec.default, **kwargs)


# ---------------------------------------------------------------------------
# List entries (accounts, llms, processors, notifiers)
# ---------------------------------------------------------------------------


def entry_model(group: str) -> type[BaseModel]:
    """The pydantic model describing one entry of a list group."""
    if group not in _LIST_GROUPS:
        raise SettingsError(group, f"{group!r} is not a list of entries")
    annotation = MailFlowConfig.__pydantic_fields__[group].annotation
    args = get_args(annotation)
    model = args[0] if args else None
    if not (isinstance(model, type) and issubclass(model, BaseModel)):  # pragma: no cover
        raise SettingsError(group, f"{group!r} has no entry model")
    return model


def add_entry(config: MailFlowConfig, group: str, values: dict[str, Any]) -> MailFlowConfig:
    """Append one validated entry to a list group."""
    model = entry_model(group)
    data = config.model_dump()
    try:
        entry = model.model_validate(values)
    except ValidationError as exc:
        raise SettingsError(group, _format_validation_error(exc)) from exc
    data[group] = [*data[group], entry.model_dump()]
    updated = _revalidate(data, group)
    updated.env_placeholders = dict(config.env_placeholders)
    return updated


def update_entry(
    config: MailFlowConfig, group: str, index: int, values: dict[str, Any]
) -> MailFlowConfig:
    """Replace one entry of a list group with validated ``values``."""
    model = entry_model(group)
    data = config.model_dump()
    entries = data[group]
    if not 0 <= index < len(entries):
        raise SettingsError(group, f"{group}[{index}] does not exist")
    merged = {**entries[index], **values}
    try:
        entry = model.model_validate(merged)
    except ValidationError as exc:
        raise SettingsError(group, _format_validation_error(exc)) from exc
    entries[index] = entry.model_dump()
    updated = _revalidate(data, group)
    updated.env_placeholders = _carried_placeholders(config)
    for name in values:
        updated.env_placeholders.pop((group, index, str(name)), None)
    return updated


def remove_entry(config: MailFlowConfig, group: str, index: int) -> MailFlowConfig:
    """Drop one entry of a list group."""
    entry_model(group)  # validates the group name
    data = config.model_dump()
    entries = data[group]
    if not 0 <= index < len(entries):
        raise SettingsError(group, f"{group}[{index}] does not exist")
    removed = entries.pop(index)
    if group == "llms":
        _drop_llm_references(data, str(removed.get("llm_id", "")))
    updated = _revalidate(data, group)
    updated.env_placeholders = _carried_placeholders(config, group_removed=(group, index))
    return updated


def _drop_llm_references(data: dict[str, Any], llm_id: str) -> None:
    """Keep cross-references valid after an LLM is deleted."""
    if not llm_id:
        return
    for llm in data.get("llms", []):
        llm["fallback"] = [name for name in llm.get("fallback", []) if name != llm_id]
    for processor in data.get("processors", []):
        if processor.get("llm") == llm_id:
            processor["llm"] = None
        processor["fallback_llms"] = [
            name for name in processor.get("fallback_llms", []) if name != llm_id
        ]
        if processor.get("llm") is None:
            processor["fallback_llms"] = []


def move_entry(config: MailFlowConfig, group: str, index: int, offset: int) -> MailFlowConfig:
    """Move one entry within its list group by ``offset`` positions."""
    entry_model(group)
    data = config.model_dump()
    entries = data[group]
    if not 0 <= index < len(entries):
        raise SettingsError(group, f"{group}[{index}] does not exist")
    target = index + offset
    if not 0 <= target < len(entries):
        return config  # already at the edge: a no-op, not an error
    entries.insert(target, entries.pop(index))
    if group == "llms":
        _rebuild_llm_chain(entries)
    updated = _revalidate(data, group)
    updated.env_placeholders = _carried_placeholders(config, group_swapped=(group, index, target))
    return updated


def _rebuild_llm_chain(entries: list[dict[str, Any]]) -> None:
    """List order *is* the fallback chain: first entry is the default and
    every entry falls back to the ones after it."""
    ids = [str(entry.get("llm_id", "")) for entry in entries]
    for position, entry in enumerate(entries):
        entry["default"] = position == 0
        entry["fallback"] = [name for name in ids[position + 1 :] if name]


def normalize_llm_chain(config: MailFlowConfig) -> MailFlowConfig:
    """Re-derive default/fallback from the current LLM order."""
    data = config.model_dump()
    if not data.get("llms"):
        return config
    _rebuild_llm_chain(data["llms"])
    updated = _revalidate(data, "llms")
    updated.env_placeholders = dict(config.env_placeholders)
    return updated


__all__ = [
    "CORE_SECTION",
    "EditorKind",
    "OptionSpec",
    "SettingsError",
    "SettingsSection",
    "add_entry",
    "apply_value",
    "build_sections",
    "coerce_value",
    "entry_field_specs",
    "entry_model",
    "find_spec",
    "move_entry",
    "normalize_llm_chain",
    "remove_entry",
    "reset_value",
    "update_entry",
]
