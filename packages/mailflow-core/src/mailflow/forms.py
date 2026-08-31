"""Plugin-declared form fields for custom login / option forms.

A plugin may declare, per component, the fields its login or option form
needs (endpoint URL, token, channel id, ...). The TUI renders them without
the core knowing the plugin: ``FormField`` is a pure data type here, and
only the host (TUI) turns it into widgets.

The contract is *capability-based*, not literal-type-based: a ``mail_source``
plugin is free to declare any fields its backend needs — it might connect to
a message platform that is only "like a mailbox" rather than an actual IMAP
server. Nothing here assumes what a component id means; plugins declare
exactly the fields their transport requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

FormFieldKind: TypeAlias = Literal[
    "string",
    "password",
    "number",
    "boolean",
    "list",
    "select",
    "textarea",
]

_VALID_KINDS = frozenset({"string", "password", "number", "boolean", "list", "select", "textarea"})


@dataclass(frozen=True)
class FormField:
    """One declarable form field.

    ``kind`` selects the widget the TUI renders: text/password inputs, a
    number editor, a boolean toggle, a list editor (one item per line),
    a dropdown (``choices``) or a multiline textarea.

    ``into_options`` decides where the collected value lands: the
    component's ``options`` dict (True, the common case for backend
    settings like endpoint/token) or a top-level config column
    (False, e.g. an account id).
    """

    field_id: str
    kind: FormFieldKind = "string"
    label_key: str = ""  # tui.extras_<id> fallback when empty
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    secret: bool = False
    into_options: bool = True
    description_key: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"FormField {self.field_id!r}: unknown kind {self.kind!r}; "
                f"use one of {sorted(_VALID_KINDS)}"
            )
        if self.kind == "select" and not self.choices:
            raise ValueError(f"FormField {self.field_id!r}: select needs choices")


@dataclass(frozen=True)
class FormSchema:
    """The ordered form a plugin declares for one component."""

    fields: tuple[FormField, ...] = ()

    @classmethod
    def of(cls, *fields: FormField) -> FormSchema:
        ids = [f.field_id for f in fields]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate FormField ids: {ids}")
        return cls(fields=tuple(fields))

    def field(self, field_id: str) -> FormField | None:
        for f in self.fields:
            if f.field_id == field_id:
                return f
        return None


__all__ = ["FormField", "FormFieldKind", "FormSchema"]
