"""Unit tests for plugin-declared form fields and connection probes.

The core capability: a plugin may declare, per component, the ordered form
fields its login/option form needs (endpoint, token, channel id, ...) and an
optional connection probe. The registry stores them as pure data; the TUI
renders them generically. The contract is capability-based — a ``mail_source``
plugin may declare any fields its transport needs, not just IMAP ones.
"""

from __future__ import annotations

from typing import cast

import pytest
from mailflow.config import MailFlowConfig
from mailflow.domain import ComponentKind
from mailflow.forms import FormField, FormFieldKind, FormSchema
from mailflow.registry import ComponentRegistry, PluginRegistrar


def _registrar() -> tuple[ComponentRegistry, PluginRegistrar]:
    registry = ComponentRegistry()
    registrar = PluginRegistrar(registry, MailFlowConfig(), "mailflow-test-plugin")
    return registry, registrar


def test_form_field_validates_kind() -> None:
    FormField("endpoint", kind="string")
    FormField("token", kind="password", secret=True)
    FormField("retries", kind="number", default=3)
    FormField("channel", kind="select", choices=("group", "contact"))
    FormField("tags", kind="list")
    with pytest.raises(ValueError):
        FormField("bad", kind=cast(FormFieldKind, "not-a-kind"))


def test_form_field_select_needs_choices() -> None:
    with pytest.raises(ValueError):
        FormField("channel", kind="select")


def test_form_schema_deduplicates_ids() -> None:
    FormSchema.of(FormField("endpoint"), FormField("token"))
    with pytest.raises(ValueError):
        FormSchema.of(FormField("endpoint"), FormField("endpoint"))


def test_registry_stores_and_returns_form_fields() -> None:
    registry, registrar = _registrar()
    fields = (
        FormField("endpoint", required=True),
        FormField("token", kind="password", secret=True),
    )
    registrar.add_form_fields(ComponentKind.NOTIFIER, "custom-chan", fields)
    assert registry.form_fields(ComponentKind.NOTIFIER, "custom-chan") == fields
    # unrelated components have no fields
    assert registry.form_fields(ComponentKind.NOTIFIER, "other-chan") == ()
    assert registry.form_fields(ComponentKind.MAIL_SOURCE, "custom-chan") == ()


def test_registry_ignores_empty_form_fields() -> None:
    registry, registrar = _registrar()
    registrar.add_form_fields(ComponentKind.NOTIFIER, "nope", ())
    assert registry.form_fields(ComponentKind.NOTIFIER, "nope") == ()


def test_registry_stores_and_returns_probe() -> None:
    registry, registrar = _registrar()

    async def probe(options: dict[str, object], t: object) -> str:
        return "online"

    registrar.add_probe(ComponentKind.NOTIFIER, "custom-chan", probe)
    assert registry.probe(ComponentKind.NOTIFIER, "custom-chan") is probe
    assert registry.probe(ComponentKind.NOTIFIER, "other") is None


def test_forms_are_capability_based_not_type_based() -> None:
    """A mail_source plugin may declare arbitrary fields — it could connect
    to a message platform that is only 'like a mailbox'."""
    registry, registrar = _registrar()
    fields = (
        FormField("gateway_url", required=True),
        FormField("channel", kind="select", choices=("inbox", "updates")),
        FormField("tags", kind="list"),
    )
    registrar.add_form_fields(ComponentKind.MAIL_SOURCE, "mailflow-messaging", fields)
    declared = registry.form_fields(ComponentKind.MAIL_SOURCE, "mailflow-messaging")
    assert [f.field_id for f in declared] == ["gateway_url", "channel", "tags"]
    assert declared[1].choices == ("inbox", "updates")
    # the registry never assumes imap-style fields
    assert not any(f.field_id == "imap_host" for f in declared)
