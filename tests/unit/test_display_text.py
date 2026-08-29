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


def _png_with_filters(size: int = 256, channels: int = 4, split_idat: bool = True) -> bytes:
    """Build a checkerboard PNG using real scanline filters (0..4)
    and optional multi-chunk IDAT — like the gateway bridges emit."""
    import struct
    import zlib

    def row_pixels(y: int) -> bytes:
        out = bytearray()
        for x in range(size):
            mx, my = x // 8, y // 8
            dark = (mx % 2) == (my % 2)
            if channels == 4:
                out += bytes((0, 0, 0, 255)) if dark else bytes((255, 255, 255, 255))
            elif channels == 3:
                out += bytes((0, 0, 0)) if dark else bytes((255, 255, 255))
            else:
                out += b"\x00" if dark else b"\xff"
        return bytes(out)

    raw = bytearray()
    prev: bytes | None = None
    stride = size * channels
    for y in range(size):
        cur = row_pixels(y)
        ftype = y % 5
        if ftype == 0:
            raw.append(0)
            raw += cur
        elif ftype == 1:
            raw.append(1)
            raw += bytes(
                [
                    (cur[i] - (cur[i - channels] if i >= channels else 0)) & 0xFF
                    for i in range(stride)
                ]
            )
        elif ftype == 2:
            raw.append(2)
            raw += bytes([(cur[i] - (prev[i] if prev else 0)) & 0xFF for i in range(stride)])
        elif ftype == 3:
            raw.append(3)
            raw += bytes(
                [
                    (
                        cur[i]
                        - (
                            ((cur[i - channels] if i >= channels else 0) + (prev[i] if prev else 0))
                            >> 1
                        )
                    )
                    & 0xFF
                    for i in range(stride)
                ]
            )
        else:
            raw.append(4)
            out = bytearray()
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                b = prev[i] if prev else 0
                c = prev[i - channels] if prev and i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                out.append((cur[i] - pr) & 0xFF)
            raw += out
        prev = cur

    def chunk(tag: bytes, data: bytes) -> bytes:
        block = tag + data
        return struct.pack(">I", len(data)) + block + struct.pack(">I", zlib.crc32(block))

    color_type = 6 if channels == 4 else (2 if channels == 3 else 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, color_type, 0, 0, 0))
    compressed = zlib.compress(bytes(raw))
    if split_idat:
        half = len(compressed) // 2
        png += chunk(b"IDAT", compressed[:half])
        png += chunk(b"IDAT", compressed[half:])
    else:
        png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    return png


def test_ascii_qr_fits_dialog() -> None:
    """The QR render must fit the guide dialog (<=29 rows, <=58 cols),
    stay module-aligned, and decode real scanline filters (RGB+RGBA)."""
    import base64

    from mailflow_tui.gateway_guide import _ascii_qr

    for channels in (4, 3):
        rendered = _ascii_qr(base64.b64encode(_png_with_filters(channels=channels)).decode())
        lines = rendered.splitlines()
        assert len(lines) <= 30
        assert len(lines[0]) <= 60
        # finder pattern (top-left dark blocks) must survive the filters;
        # content starts after the quiet-zone padding rows
        content = [ln for ln in lines if ln.strip(" ")]
        assert content, f"channels={channels} all blank"
        assert "█" in content[0] or "▀" in content[0], f"channels={channels} finder lost"
    # single IDAT too
    rendered = _ascii_qr(base64.b64encode(_png_with_filters(channels=4, split_idat=False)).decode())
    assert len(rendered.splitlines()) >= 15


def test_ascii_qr_supports_4bit_gray_like_napcat() -> None:
    """NapCat's qrcode.png is a small 4-bit grayscale PNG (147x147);
    the renderer must decode sub-byte depths, not reject them."""
    import base64
    import struct
    import zlib

    from mailflow_tui.gateway_guide import _ascii_qr

    size = 147
    stride = (size * 4 + 7) // 8  # 1 channel, 4-bit

    def pack_bits(values: list[int], depth: int) -> bytes:
        out = bytearray()
        acc = 0
        nbits = 0
        for v in values:
            acc = (acc << depth) | v
            nbits += depth
            while nbits >= 8:
                out.append((acc >> (nbits - 8)) & 0xFF)
                nbits -= 8
                acc &= (1 << nbits) - 1
        if nbits:
            out.append((acc << (8 - nbits)) & 0xFF)
        return bytes(out)

    raw = bytearray()
    prev: bytes | None = None
    for y in range(size):
        vals = [0 if ((x // 8 + y // 8) % 2 == 0) else 15 for x in range(size)]
        cur = pack_bits(vals, 4)
        ftype = y % 5
        if ftype == 0:
            raw.append(0)
            raw += cur
        elif ftype == 1:
            raw.append(1)
            raw += bytes([(cur[i] - (cur[i - 1] if i >= 1 else 0)) & 0xFF for i in range(stride)])
        elif ftype == 2:
            raw.append(2)
            raw += bytes([(cur[i] - (prev[i] if prev else 0)) & 0xFF for i in range(stride)])
        elif ftype == 3:
            raw.append(3)
            raw += bytes(
                [
                    (cur[i] - (((cur[i - 1] if i >= 1 else 0) + (prev[i] if prev else 0)) >> 1))
                    & 0xFF
                    for i in range(stride)
                ]
            )
        else:
            raw.append(4)
            out = bytearray()
            for i in range(stride):
                a = cur[i - 1] if i >= 1 else 0
                b = prev[i] if prev else 0
                c = prev[i - 1] if prev and i >= 1 else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                out.append((cur[i] - pr) & 0xFF)
            raw += out
        prev = cur

    def chunk(tag: bytes, data: bytes) -> bytes:
        block = tag + data
        return struct.pack(">I", len(data)) + block + struct.pack(">I", zlib.crc32(block))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 4, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw)))
    png += chunk(b"IEND", b"")

    rendered = _ascii_qr(base64.b64encode(png).decode())
    lines = rendered.splitlines()
    assert len(lines) >= 10
    content = [ln for ln in lines if ln.strip(" ")]
    assert content and ("█" in content[0] or "▀" in content[0])
