"""Shared offline plugin samples, checked against mailflow-core contracts."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from mailflow.bot_export import BotExportContext
from mailflow.config import (
    LLMConfig,
    MailAccountConfig,
    MailFlowConfig,
    NotifierConfig,
    ProcessorConfig,
    StorageConfig,
)
from mailflow.contracts import LLMCompletion, ProcessingContext
from mailflow.domain import (
    ActionItem,
    ComponentKind,
    MailAddress,
    MailAnalysis,
    MailMessage,
    MailRecord,
    ReplyDraft,
    Urgency,
)
from mailflow.llm import LLMRouterImpl
from mailflow.plugin_validation import validate_result
from mailflow.plugins import PluginManager

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "find_mail",
            "description": "Find a mail",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
TOOL_CALL = {
    "id": "call-1",
    "type": "function",
    "function": {"name": "find_mail", "arguments": "{}"},
}
PAYLOAD: dict[str, Any] = {
    "ok": True,
    "code": 0,
    "errcode": 0,
    "model": "validation",
    "output_text": "ok",
    "choices": [{"message": {"content": "ok", "tool_calls": [TOOL_CALL]}}],
    "content": [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "call-1", "name": "find_mail", "input": {}},
    ],
    "candidates": [
        {
            "content": {
                "parts": [
                    {"text": "ok"},
                    {"functionCall": {"name": "find_mail", "args": {}}},
                ]
            }
        }
    ],
    "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "find_mail",
            "arguments": "{}",
        },
    ],
}


class _URLReply:
    status = 200

    def __init__(self, data: bytes | None = None) -> None:
        self.data = data

    def __enter__(self) -> _URLReply:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def read(self) -> bytes:
        return self.data if self.data is not None else json.dumps(PAYLOAD).encode()


class _IMAP:
    def login(self, *args: Any, **kwargs: Any) -> tuple[str, list[bytes]]:
        return "OK", []

    select = login
    logout = login
    close = login

    def response(self, name: str) -> tuple[str, list[bytes]]:
        return name, [b"10"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "search":
            return "OK", [b"1 2"]
        uid = args[0]
        raw = (
            f"From: sender@example.test\r\nMessage-ID: <sample-{uid}>\r\n"
            f"Subject: sample {uid}\r\n\r\nbody"
        ).encode()
        return "OK", [(b'1 (INTERNALDATE "01-Jan-2026 12:00:00 +0000")', raw)]


class _SMTP:
    def __enter__(self) -> _SMTP:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def starttls(self, *args: Any, **kwargs: Any) -> None:
        pass

    login = starttls
    send_message = starttls
    quit = starttls
    close = starttls


def fake_io(stack: ExitStack) -> list[dict[str, Any]]:
    """Replace transports and reject any uncaught real connection/process."""
    requests: list[dict[str, Any]] = []
    try:
        import httpx
    except ImportError:
        pass  # A plugin requiring httpx must declare/install it itself.
    else:
        client = httpx.AsyncClient

        def respond(request: Any) -> Any:
            body: Any = json.loads(request.content) if request.content else {}
            requests.append({"body": body, "headers": request.headers, "url": request.url})
            return httpx.Response(200, json=PAYLOAD)

        def fake_client(**kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(respond)
            kwargs["trust_env"] = False
            return client(**kwargs)

        stack.enter_context(patch.object(httpx, "AsyncClient", side_effect=fake_client))

    feed_reads: dict[str, int] = {}

    def url_reply(url: Any, *args: Any, **kwargs: Any) -> _URLReply:
        address = str(url)
        if not address.endswith("feed.xml"):
            return _URLReply()
        count = feed_reads.get(address, 0)
        feed_reads[address] = count + 1
        if "/b/" in address:
            outer = (b'<feed xmlns="http://www.w3.org/2005/Atom">', b"</feed>")
            entries = [
                b"<entry><id>shared-id</id><title>first</title></entry>",
                b"<entry><title>anonymous</title><summary>stable body</summary></entry>",
            ]
        else:
            outer = (b"<rss><channel>", b"</channel></rss>")
            entries = [
                b"<item><guid>shared-id</guid><title>first</title></item>",
                b"<item><title>anonymous</title><description>stable body</description></item>",
            ]
        if count:
            entries.reverse()
        return _URLReply(outer[0] + b"".join(entries) + outer[1])

    stack.enter_context(patch("urllib.request.urlopen", side_effect=url_reply))

    def fake_imap(*_: Any, **__: Any) -> _IMAP:
        return _IMAP()

    def fake_smtp(*_: Any, **__: Any) -> _SMTP:
        return _SMTP()

    stack.enter_context(patch("imaplib.IMAP4_SSL", side_effect=fake_imap))
    stack.enter_context(patch("imaplib.IMAP4", side_effect=fake_imap))
    stack.enter_context(patch("smtplib.SMTP", side_effect=fake_smtp))
    stack.enter_context(patch("smtplib.SMTP_SSL", side_effect=fake_smtp))
    stack.enter_context(
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", "fake tool unavailable"),
        )
    )
    return requests


class NullRouter:
    async def chat(self, messages: Any, **kwargs: Any) -> LLMCompletion:
        return LLMCompletion(text=json.dumps({"urgency": "info", "summary": "validation"}))


async def check_source_delivery(instance: Any, config: MailAccountConfig) -> None:
    """A rejected second handoff must not consume it or replay the accepted prefix."""
    expected = 4 if config.provider == "rss" else 2
    stop = asyncio.Event()
    received: list[str] = []
    rejected = ""
    attempts = 0
    checkpoints: list[str] = []
    if hasattr(instance, "set_watermark_store"):
        instance.set_watermark_store(lambda: None, checkpoints.append)

    async def emit(mail: Any) -> None:
        nonlocal attempts, rejected
        validate_result(ComponentKind.MAIL_SOURCE, "fetch_history", [mail])
        attempts += 1
        if attempts == 2:
            rejected = mail.normalized_message_id()
            if checkpoints and json.loads(checkpoints[-1])["uid"] != 1:
                raise AssertionError("source advanced its checkpoint before handoff")
            raise RuntimeError("simulated rejected handoff")
        received.append(mail.normalized_message_id())
        if len(received) == expected:
            stop.set()

    await asyncio.wait_for(instance.run(emit, stop), timeout=2)
    if int(attempts) != expected + 1 or len(set(received)) != expected or rejected not in received:
        raise AssertionError("source lost a rejected mail or repeated an accepted mail")
    if checkpoints and json.loads(checkpoints[-1])["uid"] != 2:
        raise AssertionError("source did not save the accepted UID")


async def check_component(
    kind: ComponentKind,
    instance: Any,
    record: MailRecord,
    requests: list[dict[str, Any]],
    config: Any,
) -> None:
    async def call(method: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(instance, method)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        validate_result(kind, method, result)
        return result

    if kind is ComponentKind.LLM_BACKEND:
        # Use the real router so a backend must accept the host's full call.
        router = LLMRouterImpl({"validation": instance}, {"validation": config})
        result = await router.chat(
            [{"role": "user", "content": "Find mail"}],
            primary="validation",
            temperature=0.2,
            options={"headers": {"x-validation": "yes"}, "max_tokens": 32},
            tools=TOOLS,
        )
        validate_result(kind, "chat", result)
        if not result.tool_calls or result.tool_calls[0].name != "find_mail":
            raise TypeError("chat did not preserve the simulated tool call")
        if requests and not requests[-1]["body"].get("tools"):
            raise TypeError("chat did not send tools to its transport")
        if requests and requests[-1]["headers"].get("x-validation") != "yes":
            raise TypeError("chat ignored per-call options.headers")
    elif kind is ComponentKind.MAIL_PROCESSOR:
        await call("process", record.mail, ProcessingContext(account_id="validation"))
    elif kind is ComponentKind.NOTIFIER:
        await call("notify", record)
        if hasattr(instance, "notify_to_target"):
            target = (
                "contact:validation"
                if getattr(instance, "backend_id", "") == "openwechat"
                else "user:123"
            )
            await call("notify_to_target", target, record, text="validation")
        if hasattr(instance, "push_text"):
            await call("push_text", "validation")
    elif kind is ComponentKind.MAIL_SOURCE:
        stop = asyncio.Event()
        stop.set()

        async def emit(mail: Any) -> None:
            if not isinstance(mail, MailMessage):
                raise TypeError("run emitted something other than MailMessage")

        await call("run", emit, stop)
        if hasattr(instance, "check_reply_ready"):
            await call("check_reply_ready")
            await call(
                "send_reply",
                record.record_id,
                ReplyDraft(
                    draft_id="validation-reply",
                    mail_id=record.record_id,
                    account_id=config.account_id,
                    to=record.mail.sender,
                    subject="Validation",
                    body="Validation reply",
                ),
            )
        if config.provider in {"imap", "gmail", "outlook", "rss"}:
            await check_source_delivery(instance, config)
        if hasattr(instance, "fetch_history"):
            await call("fetch_history", limit=2, offset=0)
        await call("close")
    elif kind is ComponentKind.GATEWAY_PROVISIONER:
        # Installation/start/stop control real processes. Check those signatures
        # via core, then sample read-only results with the network mocked.
        await call("status", "validation")
        await call("qr", "validation")
    elif kind is ComponentKind.LLM_ENHANCER:
        for method, args in (
            ("system_prompt", ("base",)),
            (
                "extra_messages",
                (record.mail, ProcessingContext(account_id="validation")),
            ),
            (
                "post_process",
                (
                    record.analysis,
                    record.mail,
                    ProcessingContext(account_id="validation"),
                ),
            ),
        ):
            if hasattr(instance, method):
                await call(method, *args)
    elif kind is ComponentKind.STORAGE:
        await call("initialize")
        try:
            await call("save_mail", record)
            if await call("get_mail", record.record_id) != record:
                raise TypeError("save_mail/get_mail did not round-trip the sample")
            await call("list_mails", limit=2)
            await call("count_mails")
            await call("set_manual_urgency", record.record_id, Urgency.URGENT)
            await call(
                "update_mail_analysis",
                record.record_id,
                summary="updated",
                reason="validation",
            )
            await call("delete_mail", record.record_id, refresh_deleted_at=True)
            await call("list_trash")
            await call("restore_from_trash", record.record_id)
            await call("cleanup_mail", datetime.now(UTC) - timedelta(days=1))
            await call("purge_trash", datetime.now(UTC))
            draft = ReplyDraft(
                draft_id="draft",
                mail_id=record.record_id,
                account_id="validation",
                to=record.mail.sender,
                subject="reply",
                body="validation",
            )
            await call("save_draft", draft)
            await call("get_draft", "draft")
            if await call("list_drafts", record.record_id) != [draft]:
                raise TypeError("drafts did not round-trip")
            if await call("list_drafts", "other-mail"):
                raise TypeError("draft lookup crossed mail boundaries")
            await call("delete_draft", "draft")
            await call("set_preference", "validation", "yes")
            if await call("get_preference", "validation") != "yes":
                raise TypeError("preferences did not round-trip")
            action = ActionItem(
                item_id="action",
                mail_id=record.record_id,
                summary="validation",
                action_type="task",
                due_at=datetime.now(UTC),
            )
            await call("save_custom_action", action)
            await call("list_custom_actions")
            await call("delete_custom_action", "action")
        finally:
            await call("close")


async def validate(
    folder: Path, scratch: Path, requests: list[dict[str, Any]], source: bool
) -> None:
    project = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    manifest_path = folder / "plugin.json"
    manifest: dict[str, Any] = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {"id": project["name"], "version": project["version"], "categories": []}
    )
    plugin_id: str = manifest["id"]
    entry = project["entry-points"]["mailflow.plugins"][plugin_id]
    ep: metadata.EntryPoint | None
    if source:
        ep = metadata.EntryPoint(name=plugin_id, value=entry, group="mailflow.plugins")
        module = importlib.import_module(ep.module)
        if not Path(module.__file__ or "").resolve().is_relative_to(folder / "src"):  # noqa: ASYNC240
            raise TypeError(f"{plugin_id}: an installed copy shadowed the requested source")
    else:
        ep = next(
            (e for e in metadata.entry_points(group="mailflow.plugins") if e.name == plugin_id),
            None,
        )
        if ep is None:
            raise TypeError(f"{plugin_id}: installed entry point is missing")
    manager = PluginManager()
    manager.load(plugin_id, ep.load)
    registry = manager.build_registry()
    if manager.status(plugin_id) != "enabled":
        raise TypeError(manager.failures.get(plugin_id, "plugin was not registered"))
    info = next(i for i in manager.enabled_infos() if i.plugin_id == plugin_id)
    categories = manifest.get("categories", [])
    category_kind = (
        {
            "processor": "mail_processor",
            "gateway": "gateway_provisioner",
        }.get(categories[0], categories[0])
        if categories
        else None
    )
    if (
        info.version != project["version"]
        or info.version != manifest["version"]
        or (category_kind is not None and category_kind not in info.kinds)
    ):
        raise TypeError("runtime version/kinds differ from plugin.json")
    mail = MailMessage(
        message_id="validation",
        account_id="validation",
        subject="Validation",
        sender=MailAddress(address="sender@example.test"),
        date=datetime.now(UTC),
        received_at=datetime.now(UTC),
    )
    record = MailRecord(
        record_id="validation",
        mail=mail,
        analysis=MailAnalysis(summary="validation", urgency=Urgency.INFO),
    )
    options = {
        "host": "imap.example.test",
        "imap_host": "imap.example.test",
        "smtp_host": "smtp.example.test",
        "username": "sender@example.test",
        "password": "fake-password",
        "http_url": "https://example.test",
        "gateway_url": "https://example.test",
        "base_url": "https://example.test",
        "targets": ["user:123"],
        "project": "validation",
        "token": "validation",
        "auth_key": "validation",
        "bot_token": "validation",
        "chat_id": "validation",
        "topic": "validation",
        "send_key": "validation",
        "url": "https://example.test",
        "webhook_url": "https://example.test",
        "endpoint": "https://example.test",
        "feeds": ["https://example.test/a/feed.xml", "https://example.test/b/feed.xml"],
        "interval_seconds": 0.01,
        "initial_sync": "all",
        "stream": False,
        "from_addr": "sender@example.test",
        "to": ["receiver@example.test"],
    }
    config: Any
    for component in registry.snapshots():
        kind, component_id = component.kind, component.component_id
        if kind is ComponentKind.MAIL_SOURCE:
            config = MailAccountConfig(
                account_id="validation", provider=component_id, options=options
            )
        elif kind is ComponentKind.LLM_BACKEND:
            config = LLMConfig(
                llm_id="validation",
                provider=component_id,
                base_url="https://example.test",
                api_key="fake-token",
                model="validation",
                max_retries=0,
                options=options,
            )
        elif kind in (ComponentKind.MAIL_PROCESSOR, ComponentKind.LLM_ENHANCER):
            config = ProcessorConfig(processor_id="validation", provider=component_id)
        elif kind is ComponentKind.NOTIFIER:
            config = NotifierConfig(
                notifier_id="validation",
                provider=component_id,
                options={**options, "targets": ["contact:validation"]}
                if component_id == "openwechat"
                else options,
            )
        elif kind is ComponentKind.STORAGE:
            config = StorageConfig(provider=component_id, path=str(scratch / "mail.db"))
        elif kind is ComponentKind.BOT_EXPORTER:
            config = BotExportContext(
                config=MailFlowConfig(),
                plugin_ids=[],
                output_dir=scratch / component_id,
            )
        else:
            config = None
        args: tuple[Any, ...] = () if kind is ComponentKind.GATEWAY_PROVISIONER else (config,)
        if kind is ComponentKind.MAIL_PROCESSOR:
            args = (config, NullRouter())
        try:
            factory: Any = registry.factory(kind, component_id)
            instance: Any = factory(*args)
            if kind is ComponentKind.BOT_EXPORTER:
                validate_result(kind, "export", instance)
                if not instance.created or not all(
                    (cast(BotExportContext, config).output_dir / f).is_file()
                    for f in instance.created
                ):
                    raise TypeError("exporter result does not identify its generated files")
            else:
                await asyncio.wait_for(
                    check_component(kind, instance, record, requests, config), timeout=8
                )
        except Exception as exc:
            raise TypeError(f"{plugin_id}/{kind.value}/{component_id}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", type=Path)
    parser.add_argument("--root", type=Path, help="validate every plugin project under this folder")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source", action="store_true")
    mode.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    if args.root is not None:
        root = args.root.resolve()
        folders = sorted(p.parent for p in root.glob("*/pyproject.toml"))
        if not folders:
            parser.error(f"no plugin projects under {root}")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [*(str(p / "src") for p in folders), env.get("PYTHONPATH", "")]
        )
        failed = False
        for folder in folders:
            print(f"validating {folder.name}", flush=True)
            result = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-B",
                    "-m",
                    "mailflow_testkit.plugin_smoke",
                    str(folder),
                    "--installed" if args.installed else "--source",
                ],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
            if result.returncode:
                failed = True
                lines = (result.stdout + result.stderr).strip().splitlines()
                print(f"{folder.name}: {lines[-1] if lines else 'contract check failed'}")
        if not failed:
            print(f"ok: {len(folders)} plugin(s); core contracts and offline results checked")
        raise SystemExit(int(failed))
    if args.folder is None:
        parser.error("pass a plugin folder or --root")
    folder = args.folder.resolve()
    if args.source:
        sys.path.insert(0, str(folder / "src"))

    def audit(event: str, args: Any) -> None:
        if event in ("socket.connect", "socket.getaddrinfo", "subprocess.Popen"):
            raise RuntimeError(f"real I/O forbidden during contract validation: {event}")

    with (
        asyncio.Runner() as runner,
        tempfile.TemporaryDirectory(prefix="mailflow-plugin-sample-") as directory,
        ExitStack() as stack,
    ):
        # Windows initializes asyncio's internal socket pair here, before
        # plugins are imported and the real-I/O guard is installed.
        runner.get_loop()
        sys.addaudithook(audit)
        scratch = Path(directory)
        stack.enter_context(patch.object(Path, "home", return_value=scratch))
        requests = fake_io(stack)
        runner.run(validate(folder, scratch, requests, args.source))


if __name__ == "__main__":
    main()
