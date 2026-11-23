"""Bundled and companion Telegram implementations obey the same delivery contract."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest
from mailflow.config import NotifierConfig
from mailflow.contracts import Notifier
from mailflow.domain import ComponentKind, MailAnalysis, MailRecord, Urgency
from mailflow.notifications import DeliveryError
from mailflow.plugins import PluginManager
from mailflow_testkit.fakes import make_mail

ROOT = Path(__file__).resolve().parents[2]


async def test_marketplace_delivery_uses_the_current_platform_protocols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ROOT.parent / "mailflow-repo/notifier"
    if not root.is_dir():
        pytest.skip("companion plugin checkout is unavailable")
    sent: list[Request] = []
    body = b""

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return body

    def send(request: Request, timeout: float) -> Response:
        sent.append(request)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", send)
    record = MailRecord(
        record_id="test",
        mail=make_mail(subject="notice"),
        analysis=MailAnalysis(summary="x" * 2100, urgency=Urgency.INFO),
    )
    configurations: dict[str, dict[str, object]] = {
        "discord": {"webhook_url": "https://fake.test/hook?thread_id=42"},
        "slack": {"webhook_url": "https://fake.test/hook", "channel": "obsolete-override"},
        "ntfy": {"base_url": "https://fake.test", "topic": "inbox", "token": "private"},
        "serverchan": {"send_key": "sctp123tsecret"},
        "wecom": {"webhook_url": "https://fake.test/hook"},
        "webhook": {"url": "https://fake.test/hook"},
    }
    channels: dict[str, Notifier] = {}
    for provider, options in configurations.items():
        path = root / f"mailflow-notify-{provider}/src/mailflow_notify_{provider}/plugin.py"
        spec = importlib.util.spec_from_file_location(f"protocol_{provider}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manager = PluginManager()
        manager.register(module.plugin)
        registry = manager.build_registry()
        notifier = registry.notifier_factory(provider)(
            NotifierConfig(notifier_id=provider, provider=provider, options=options)
        )
        channels[provider] = notifier
        body = b"ok" if provider == "slack" else b'{"id":"message","code":0}'
        await notifier.notify(record)
        request = sent[-1]
        assert isinstance(request.data, bytes)
        payload = json.loads(request.data)
        if provider == "discord":
            assert parse_qs(urlparse(request.full_url).query) == {
                "thread_id": ["42"],
                "wait": ["true"],
            }
            assert len(payload["content"]) == 2000
        elif provider == "slack":
            assert "channel" not in payload
        elif provider == "ntfy":
            assert request.full_url == "https://fake.test/" and payload["topic"] == "inbox"
            assert request.get_header("Authorization") == "Bearer private"
        elif provider == "serverchan":
            assert request.full_url == "https://123.push.ft07.com/send/sctp123tsecret.send"
        body = b'{"code":123,"ok":false}'
        with pytest.raises(DeliveryError):
            await notifier.notify(record)

    from email.message import Message
    from io import BytesIO
    from urllib.error import HTTPError

    def limited(request: Request, timeout: float) -> Response:
        raise HTTPError(
            request.full_url,
            429,
            "limited",
            Message(),
            BytesIO(b'{"parameters":{"retry_after":3}}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", limited)
    with pytest.raises(DeliveryError) as failure:
        await channels["webhook"].notify(record)
    assert failure.value.retry_after == 3


async def test_marketplace_smtp_honors_encryption_choice_and_reports_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (
        ROOT.parent
        / "mailflow-repo/notifier/mailflow-notify-smtp/src/mailflow_notify_smtp/plugin.py"
    )
    if not path.is_file():
        pytest.skip("companion plugin checkout is unavailable")
    spec = importlib.util.spec_from_file_location("protocol_smtp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sent: list[object] = []
    refused: dict[str, object] = {}

    class Server:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> Server:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def send_message(self, message: object) -> dict[str, object]:
            sent.append(message)
            return refused

    monkeypatch.setattr(module.smtplib, "SMTP_SSL", Server)
    manager = PluginManager()
    manager.register(module.plugin)
    notifier = manager.build_registry().notifier_factory("smtp")(
        NotifierConfig(
            notifier_id="smtp",
            provider="smtp",
            options={
                "host": "smtp.test",
                "port": 465,
                "use_tls": "off",
                "from_addr": "from@test",
                "to": ["to@test"],
            },
        )
    )
    record = MailRecord(record_id="smtp", mail=make_mail(subject="notice"))
    await notifier.notify(record)
    assert len(sent) == 1
    refused["to@test"] = (550, "rejected")
    with pytest.raises(DeliveryError):
        await notifier.notify(record)


@pytest.fixture(params=["bundled", "marketplace"])
def telegram_modules(request: pytest.FixtureRequest) -> Iterator[tuple[ModuleType, ModuleType]]:
    root = ROOT
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


async def test_marketplace_signing_credentials_reach_delivery_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ROOT.parent / "mailflow-repo/notifier"
    if not root.is_dir():
        pytest.skip("companion plugin checkout is unavailable")
    requests: list[Request] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"code":0}'

    def send(request: Request, timeout: float) -> Response:
        requests.append(request)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", send)
    record = MailRecord(
        record_id="signed", mail=make_mail(subject="Signed notice"), auto_urgency=Urgency.INFO
    )
    for provider in ("feishu", "dingtalk"):
        path = root / f"mailflow-notify-{provider}/src/mailflow_notify_{provider}/plugin.py"
        spec = importlib.util.spec_from_file_location(f"signed_{provider}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manager = PluginManager()
        manager.register(module.plugin)
        registry = manager.build_registry()
        notifier = registry.notifier_factory(provider)(
            NotifierConfig(
                notifier_id=provider,
                provider=provider,
                options={"webhook_url": "http://fake-hook", "secret": "private-signing-key"},
            )
        )
        await notifier.notify(record)
        assert "secret" in {
            field.field_id for field in registry.form_fields(ComponentKind.NOTIFIER, provider)
        }
    first = requests[0]
    assert isinstance(first.data, bytes)
    feishu = json.loads(first.data)
    dingtalk = parse_qs(urlparse(requests[1].full_url).query)
    assert feishu["sign"] and feishu["timestamp"]
    assert dingtalk["sign"][0] and dingtalk["timestamp"][0]
    assert "private-signing-key" not in first.data.decode() + requests[1].full_url
