"""Local, HTTP and remote displays share translated failures and safe parameters."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from mailflow.errors import error_info, error_text, translated_error
from mailflow.i18n import I18n
from mailflow_server.app import create_app
from mailflow_server.client import RemoteClient, RemoteError
from mailflow_tui.labels import error_detail

from mailflow_test_support.commands import commands_service
from mailflow_test_support.ui import mock_http_client


@pytest.mark.parametrize("language", ["en", "zh-CN"])
async def test_local_and_remote_failures_match_without_exposing_backend_text(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = commands_service()
    service.i18n.set_language(language)
    service.config.server.username, service.config.server.password = "admin", "secret"
    app = create_app(service)
    mock_http_client(monkeypatch, httpx.ASGITransport(app=app, raise_app_exceptions=False))
    client = RemoteClient("http://fake", "admin", "secret")

    with pytest.raises(RemoteError) as invalid:
        await client.set_setting("general.workers", "invalid secret backend text")
    assert invalid.value.code == "errors.setting"
    assert invalid.value.params == {"option": "general.workers"}
    expected = service.t("errors.setting", option="general.workers")
    assert error_detail(service, invalid.value) == expected
    assert "secret" not in str(invalid.value)

    failure = OSError("database is locked / secret backend text")
    monkeypatch.setattr(service, "list_mails", AsyncMock(side_effect=failure))
    with pytest.raises(RemoteError) as failed:
        await client.list_mails()
    assert failed.value.status == 500
    assert failed.value.code == "errors.storage"
    assert error_detail(service, failed.value) == error_detail(service, failure)
    assert "database is locked" not in str(failed.value)
    assert error_detail(service, PermissionError("access denied to secret file")) == service.t(
        "errors.access"
    )

    with pytest.raises(RemoteError) as missing:
        await client.set_setting("missing.option", 1)
    assert missing.value.code == "errors.setting"


@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_client_language_translates_server_codes_and_preserves_identifiers(language: str) -> None:
    english = I18n("en")
    client_language = I18n(language)
    local = translated_error(KeyError, english.t, "account.not_found", account_id="work")
    frame = error_info(local, english.t)
    remote = RemoteError(404, frame["code"], frame["message"], frame["params"])
    assert error_text(remote, client_language.t) == client_language.t(
        "account.not_found", account_id="work"
    )
    details: dict[str, Any] = {"error": "raw backend exception"}
    reload_failure = RemoteError(409, "tui.settings_reload_failed", "", details)
    message = error_text(reload_failure, client_language.t)
    assert message == client_language.t(
        "tui.settings_reload_failed", error=client_language.t("errors.operation")
    )
    assert "raw backend exception" not in message
