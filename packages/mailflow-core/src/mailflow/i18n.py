"""JSON-driven internationalization with persistent language selection.

Built-in packs ship with the core package; additional languages are loaded
from external directories as data-only JSON files (never code). Missing keys
fall back to English, then to the key itself. The selected language is
persisted by the storage backend through the service facade.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger("mailflow.i18n")

_DEFAULT_LANGUAGE = "en"
_BUILTIN_PACKS = {"en", "zh-CN"}


class LanguageInfo:
    def __init__(self, code: str, name: str) -> None:
        self.code = code
        self.name = name

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "name": self.name}


class I18n:
    """Loads packs, resolves messages and switches the active language."""

    def __init__(
        self, language: str = _DEFAULT_LANGUAGE, extra_dirs: list[str] | None = None
    ) -> None:
        self._packs: dict[str, dict[str, str]] = {}
        self._names: dict[str, str] = {}
        self._load_builtin_packs()
        for directory in extra_dirs or []:
            self._load_directory(directory)
        self.set_language(language if language in self._packs else _DEFAULT_LANGUAGE)

    # -- loading ---------------------------------------------------------------

    def _load_builtin_packs(self) -> None:
        package = resources.files("mailflow").joinpath("locale")
        for code in _BUILTIN_PACKS:
            try:
                raw = (package / f"{code}.json").read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.error("builtin locale %r missing from package data", code)
                continue
            self._load_payload(code, json.loads(raw))

    def _load_directory(self, directory: str) -> None:
        path = Path(directory)
        if not path.is_dir():
            logger.warning("i18n directory %r does not exist; skipping", directory)
            return
        for file in sorted(path.glob("*.json")):
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("invalid language pack %s: %s", file, exc)
                continue
            code = payload.get("locale")
            if not isinstance(code, str) or not code:
                logger.error("language pack %s missing string 'locale'", file)
                continue
            self._load_payload(code, payload)

    def _load_payload(self, code: str, payload: dict[str, Any]) -> None:
        messages = payload.get("messages")
        if not isinstance(messages, dict):
            logger.error("language pack %r missing 'messages' object", code)
            return
        flattened: dict[str, str] = {}

        def walk(prefix: str, node: dict[str, Any]) -> None:
            for key, value in node.items():
                full = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    walk(full, cast(dict[str, Any], value))
                elif isinstance(value, str):
                    flattened[full] = value

        walk("", cast(dict[str, Any], messages))
        self._packs[code] = flattened
        name = payload.get("name")
        self._names[code] = str(name) if isinstance(name, str) and name else code
        logger.debug("loaded language pack %r (%d keys)", code, len(flattened))

    # -- querying ---------------------------------------------------------------

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, code: str) -> None:
        if code not in self._packs:
            raise KeyError(f"language pack {code!r} is not loaded")
        self._language = code

    def t(self, key: str, **params: Any) -> str:
        """Translate ``key`` with ``str.format`` params; en fallback, then key."""
        message = self._packs.get(self._language, {}).get(key)
        if message is None:
            message = self._packs.get(_DEFAULT_LANGUAGE, {}).get(key)
        if message is None:
            message = key
        try:
            return message.format(**params)
        except (KeyError, IndexError, ValueError):
            logger.error("translation %r failed to format with %r", key, params)
            return message

    def available_languages(self) -> list[LanguageInfo]:
        return [LanguageInfo(code, self._names.get(code, code)) for code in sorted(self._packs)]

    def available_codes(self) -> list[str]:
        return sorted(self._packs)

    def keys(self, code: str) -> set[str]:
        return set(self._packs.get(code, {}))

    def key_count(self, code: str) -> int:
        return len(self._packs.get(code, {}))


__all__ = ["I18n", "LanguageInfo"]
