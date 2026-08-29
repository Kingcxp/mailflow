"""Canned processor phrases are stored as English data; display_text
localizes them at render time without rewriting records."""

from __future__ import annotations

from typing import Any, cast

from mailflow.config import MailFlowConfig
from mailflow.events import EventBus
from mailflow.i18n import I18n
from mailflow.pipeline import PipelineEngine
from mailflow.plugins import PluginManager
from mailflow.registry import ComponentRegistry
from mailflow.service import MailFlowService


def _service(language: str) -> MailFlowService:
    return MailFlowService(
        config=MailFlowConfig(),
        registry=ComponentRegistry(),
        plugin_manager=PluginManager(),
        storage=cast(Any, object()),
        sources={},
        router=cast(Any, None),
        pipeline=PipelineEngine([]),
        notifiers=[],
        notifier_configs=[],
        events=EventBus(),
        i18n=I18n(language),
    )


def test_canned_phrases_localize_per_language() -> None:
    zh = _service("zh-CN")
    assert zh.display_text("Advertisement detected by rules") == "规则判定为广告"
    assert zh.display_text("matches advertising keywords") == "命中广告关键词"
    assert zh.display_text("sender is on the important-senders list") == "发件人在重要发件人名单中"
    en = _service("en")
    assert en.display_text("Advertisement detected by rules") == "Advertisement detected by rules"


def test_free_text_passes_through_untouched() -> None:
    zh = _service("zh-CN")
    assert zh.display_text("领学生证需要本人到场") == "领学生证需要本人到场"
    assert zh.display_text(None) == ""
    assert zh.display_text("") == ""


def test_ascii_qr_fits_dialog() -> None:
    """The QR render must fit the guide dialog (<=32 rows, <=64 cols)
    while staying module-aligned so it remains scannable."""
    import base64
    import struct
    import zlib

    from mailflow_tui.gateway_guide import _ascii_qr

    width = height = 256
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            mx, my = x // 8, y // 8
            dark = (mx % 2) == (my % 2)
            raw += bytes((0, 0, 0)) if dark else bytes((255, 255, 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        block = tag + data
        return struct.pack(">I", len(data)) + block + struct.pack(">I", zlib.crc32(block))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw)))
    png += chunk(b"IEND", b"")

    rendered = _ascii_qr(base64.b64encode(png).decode())
    lines = rendered.splitlines()
    assert len(lines) <= 32
    assert len(lines[0]) <= 64
    # finder pattern (top-left 7x7 dark/light blocks) must survive
    assert "██" in lines[0]
