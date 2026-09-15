"""Localized, credential-safe presentation helpers for the Textual client."""

from __future__ import annotations

from typing import Any

from mailflow.config import LLMConfig, MailFlowConfig, NotifierConfig, ServerConfig
from mailflow.i18n import I18n
from mailflow_tui.labels import error_detail, error_message


class _Service:
    def __init__(self, config: MailFlowConfig) -> None:
        self.config = config
        self._i18n = I18n(language="zh-CN")

    def t(self, key: str, **params: Any) -> str:
        return self._i18n.t(key, **params)


def test_error_presentation_localizes_and_redacts_configured_secrets() -> None:
    """Failures can guide a Chinese UI user without exposing credentials."""
    secrets = {
        "sk-ui-test-secret",
        "Bearer header-secret",
        "notifier-token-secret",
        "server-password-secret",
        "draft-secret",
    }
    service = _Service(
        MailFlowConfig(
            llms=[
                LLMConfig(
                    llm_id="llm",
                    api_key="sk-ui-test-secret",
                    headers={"Authorization": "Bearer header-secret"},
                )
            ],
            notifiers=[
                NotifierConfig(
                    notifier_id="notifier",
                    provider="console",
                    options={"access_token": "notifier-token-secret"},
                )
            ],
            server=ServerConfig(password="server-password-secret"),
        )
    )

    raw_error = ValueError(" / ".join(sorted(secrets)))
    detail = error_detail(service, raw_error, extra_secrets=("draft-secret",))
    rendered = error_message(service, raw_error, extra_secrets=("draft-secret",))

    assert all(secret not in detail for secret in secrets)
    assert detail.count("***") == len(secrets)
    assert rendered.startswith(service.t("common.error", message=""))
    assert all(secret not in rendered for secret in secrets)
