"""Localized, credential-safe presentation helpers for the Textual client."""

from __future__ import annotations

from typing import Any

from mailflow.config import LLMConfig, MailFlowConfig, NotifierConfig, ServerConfig
from mailflow.i18n import I18n
from mailflow_tui.labels import (
    error_detail,
    error_message,
    gateway_error_detail,
    notifier_provider_label,
)


class _Service:
    def __init__(self, config: MailFlowConfig) -> None:
        self.config = config
        self.i18n = I18n(language="zh-CN")

    def t(self, key: str, **params: Any) -> str:
        return self.i18n.t(key, **params)


def test_notifier_labels_distinguish_onebot_transport_from_napcat_gateway() -> None:
    service = _Service(MailFlowConfig())

    for language in ("zh-CN", "en"):
        service.i18n.set_language(language)
        onebot = notifier_provider_label(
            service, "onebot", plugin_id="mailflow-notify-onebot"
        )
        napcat = notifier_provider_label(service, "onebot", {"gateway": "napcat"})

        assert "OneBot" in onebot
        assert "NapCat" in napcat
        assert "QQ" not in onebot and "QQ" not in napcat


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
    detail = error_detail(service, str(raw_error), extra_secrets=("draft-secret",))
    rendered = error_message(service, raw_error, extra_secrets=("draft-secret",))

    assert all(secret not in detail for secret in secrets)
    assert detail.count("***") == len(secrets)
    assert rendered.startswith(service.t("common.error", message=""))
    assert all(secret not in rendered for secret in secrets)
    assert error_detail(service, raw_error) == service.t("errors.invalid_value")

    from mailflow.gateway import GatewaySetupError

    diagnostic = (
        "E: Unable to locate package docker.io\n"
        "https://proxy-user:proxy-password@proxy.invalid token=notifier-token-secret\n"
        "access_token=unknown-token password=unknown-password Bearer unknown-bearer\n"
        "draft-secret [red]literal diagnostic[/red]"
    )
    for language in ("en", "zh-CN"):
        service.i18n.set_language(language)
        for failure in (
            GatewaySetupError("gateway.napcat_docker_installing_failed", detail=diagnostic),
            RuntimeError(diagnostic),
        ):
            visible = gateway_error_detail(service, failure, extra_secrets=("draft-secret",))
            assert "Unable to locate package docker.io" in visible
            assert "[red]literal diagnostic[/red]" in visible
            assert all(
                secret not in visible
                for secret in (
                    *secrets,
                    "proxy-user",
                    "proxy-password",
                    "unknown-token",
                    "unknown-password",
                    "unknown-bearer",
                )
            )
        assert service.t("gateway.napcat_docker_installing_failed") in gateway_error_detail(
            service, GatewaySetupError("gateway.napcat_docker_installing_failed", detail=diagnostic)
        )
