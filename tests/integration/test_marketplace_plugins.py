"""Bundled and companion Telegram implementations obey the same delivery contract."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs
from urllib.request import Request

import pytest
from mailflow.config import NotifierConfig
from mailflow.domain import ComponentKind, MailRecord, Urgency
from mailflow.plugins import PluginManager
from mailflow_testkit.fakes import make_mail


@pytest.fixture(params=["bundled", "marketplace"])
def telegram_modules(request: pytest.FixtureRequest) -> Iterator[tuple[ModuleType, ModuleType]]:
    root = Path(__file__).resolve().parents[2]
    if request.param == "bundled":
        package_dir = root / "plugins/mailflow-notify-telegram/src/mailflow_notify_telegram"
    else:
        package_dir = (
            root.parent
            / "mailflow-repo/notifier/mailflow-notify-telegram/src/mailflow_notify_telegram"
        )
    if request.param == "marketplace" and not package_dir.is_dir():
        pytest.skip("companion plugin checkout is unavailable")
    namespace = f"telegram_contract_{request.param}"
    package = ModuleType(namespace)
    package.__path__ = [str(package_dir)]
    sys.modules[namespace] = package
    try:
        yield (
            importlib.import_module(f"{namespace}.plugin"),
            importlib.import_module(f"{namespace}.gateway"),
        )
    finally:
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                sys.modules.pop(name, None)


async def test_registered_notifier_delivers_once_per_target_without_exposing_credentials(
    telegram_modules: tuple[ModuleType, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_module, gateway_module = telegram_modules
    sent: list[Request] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"ok":true}'

    def urlopen(request: Request, timeout: float) -> Response:
        sent.append(request)
        return Response()

    monkeypatch.setattr(gateway_module, "urlopen", urlopen)
    manager = PluginManager()
    manager.register(plugin_module.plugin)
    registry = manager.build_registry()
    assert registry.has(ComponentKind.GATEWAY_PROVISIONER, "telegram")
    assert not manager.failures
    notifier = registry.notifier_factory("telegram")(
        NotifierConfig(
            notifier_id="telegram-contract",
            provider="telegram",
            options={
                "bot_token": "sensitive-token",
                "chat_id": "42",
                "targets": ["user:42", "group:73"],
            },
        )
    )
    record = MailRecord(
        record_id="mail", mail=make_mail(subject="Exam notice 合同"), auto_urgency=Urgency.URGENT
    )
    await notifier.notify(record)

    payloads: list[dict[str, list[str]]] = []
    for request in sent:
        assert isinstance(request.data, bytes)
        payloads.append(parse_qs(request.data.decode("utf-8")))
    assert [payload["chat_id"][0] for payload in payloads] == ["42", "73"]
    assert all(request.full_url.endswith("botsensitive-token/sendMessage") for request in sent)
    assert all(record.mail.subject in payload["text"][0] for payload in payloads)
    assert all("sensitive-token" not in payload["text"][0] for payload in payloads)
