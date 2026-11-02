"""Bundled plugin composition and discovery through the real host registry."""

from __future__ import annotations


class TestBundledRegistration:
    """The composition package registers every official component."""

    def test_all_builtin_plugins_registered(self) -> None:
        from mailflow.config import MailFlowConfig
        from mailflow.domain import ComponentKind
        from mailflow_bundled import create_plugin_manager

        manager = create_plugin_manager(MailFlowConfig(), discover_external=False)
        registry = manager.build_registry()
        assert set(manager.plugin_ids) == {
            "mailflow-mail-fake",
            "mailflow-mail-imap",
            "mailflow-storage-sqlite",
            "mailflow-llm-openai-compatible",
            "mailflow-llm-anthropic",
            "mailflow-llm-google-generative-ai",
            "mailflow-llm-google-vertex",
            "mailflow-notify-console",
            "mailflow-notify-onebot",
            "mailflow-notify-openwechat",
            "mailflow-notify-telegram",
            "mailflow-notify-wechatpadpro",
            "mailflow-notify-whatsapp",
            "mailflow-export-nonebot",
            "mailflow-export-astrbot",
        }
        assert registry.has(ComponentKind.LLM_BACKEND, "openai-completions")
        assert registry.has(ComponentKind.LLM_BACKEND, "openai-responses")
        assert registry.has(ComponentKind.LLM_BACKEND, "openai-codex-responses")
        assert registry.has(ComponentKind.LLM_BACKEND, "azure-openai-responses")
        assert registry.has(ComponentKind.LLM_BACKEND, "anthropic-messages")
        assert registry.has(ComponentKind.LLM_BACKEND, "google-generative-ai")
        assert registry.has(ComponentKind.LLM_BACKEND, "google-vertex")
        assert registry.has(ComponentKind.MAIL_SOURCE, "fake")
        assert registry.has(ComponentKind.MAIL_SOURCE, "imap")
        assert registry.has(ComponentKind.STORAGE, "sqlite")
        assert registry.has(ComponentKind.LLM_BACKEND, "openai-compatible")
        assert registry.has(ComponentKind.LLM_BACKEND, "anthropic")
        assert registry.has(ComponentKind.NOTIFIER, "console")
        assert registry.has(ComponentKind.NOTIFIER, "onebot")
        assert registry.has(ComponentKind.NOTIFIER, "wechatpadpro")
        # the auto-deploy platforms ship a notifier AND its provisioner, so
        # the Notifications tab can offer guided setup for them
        for provider in ("telegram", "whatsapp"):
            assert registry.has(ComponentKind.NOTIFIER, provider)
            assert registry.has(ComponentKind.GATEWAY_PROVISIONER, provider)
        # rules/llm-importance are built into the core, not plugin-provided;
        # start_service registers them (covered by service pipeline integration tests)
        assert registry.has(ComponentKind.BOT_EXPORTER, "nonebot")
        assert registry.has(ComponentKind.BOT_EXPORTER, "astrbot")
        # ownership is stamped at registration
        assert registry.plugin_for("fake") == "mailflow-mail-fake"
        assert registry.plugin_for("sqlite") == "mailflow-storage-sqlite"
        assert registry.plugin_for("nonebot") == "mailflow-export-nonebot"
        assert registry.plugin_for("astrbot") == "mailflow-export-astrbot"

    def test_discovery_deduplicates_bundled_plugins(self) -> None:
        from mailflow.config import MailFlowConfig
        from mailflow_bundled import create_plugin_manager

        manager = create_plugin_manager(MailFlowConfig(), discover_external=True)
        # entry points resolve to the same singletons; no double registration
        assert manager.plugin_ids.count("mailflow-storage-sqlite") == 1
