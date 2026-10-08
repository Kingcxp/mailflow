"""Optional text extraction from images attached to mail.

A large share of event notices state everything that matters inside a poster:
the title, the date, the room. The mail body then carries only the greeting, so
a search for the event's own name finds nothing and the analyser sees no date.
This module reads that text back.

Design constraints, in order:

- **Never required.** The OCR engine is an optional dependency resolved at call
  time. Without it every function here degrades to "no text" and the rest of
  MailFlow behaves exactly as before; nothing in the pipeline may fail because
  a poster could not be read.
- **Never blocking on the mail path.** Recognition is CPU-bound and takes
  hundreds of milliseconds per image, so the pipeline decides *which* images are
  worth reading and the caller runs :func:`extract_text` off the event loop.
- **Untrusted output.** Recognised text is model/OCR output: it is data to
  search and to reason about, never an instruction to follow. It is normalized
  before use because engines split words at glyph gaps ("Se minar").
"""

from __future__ import annotations

import importlib
import logging
import re
from typing import Any, cast

logger = logging.getLogger("mailflow.ocr")

# Poster images are usually the inline ones; a scan of a 40-page brochure would
# cost more than it returns, so only images under this size are attempted.
MAX_IMAGE_BYTES = 4_000_000
"""Largest attachment worth OCR-ing, in bytes."""

_ENGINE_PACKAGE = "rapidocr_onnxruntime"
"""The engine this module uses.

Chosen because it ships its own ONNX models: `pytesseract` needs a separately
installed Tesseract binary, which on Windows means hunting down an installer
before the feature works at all. The package is heavy, but the feature is
optional and strictly opt-in."""

_engine_cache: dict[str, Any] = {}
_engine_unavailable = False


def is_available() -> bool:
    """Whether an OCR engine can be imported (cheap, cached after first call)."""
    if _engine_cache:
        return True
    if _engine_unavailable:
        return False
    return _load_engine() is not None


def _load_engine() -> Any | None:
    """Resolve the OCR engine once; a missing dependency is not an error."""
    global _engine_unavailable
    if "engine" in _engine_cache:
        return _engine_cache["engine"]
    if _engine_unavailable:
        return None
    try:
        module: Any = importlib.import_module(_ENGINE_PACKAGE)
    except ImportError:
        logger.debug(
            "OCR unavailable: install the optional extra "
            "(uv pip install %s) to read text from posters",
            _ENGINE_PACKAGE,
        )
        _engine_unavailable = True
        return None
    try:
        engine = module.RapidOCR()
    except Exception as exc:  # model files missing/corrupt
        logger.warning("OCR engine could not start (%s); posters stay unread", type(exc).__name__)
        _engine_unavailable = True
        return None
    _engine_cache["engine"] = engine
    return engine


def collapsed_text(text: str) -> str:
    """The same text with every letter-to-letter gap closed.

    Recognition inserts spaces where a glyph has a gap — "PAIR Research Se
    minar" is a real observed result for "Seminar" — so a plain substring test
    for the user's word fails on text that plainly contains it.

    This form is a *matching aid only*: collapsing every letter gap also glues
    legitimate word boundaries ("Room Y908 and Zoom" becomes "roomy908
    andzoom"), so it must be searched *alongside* the original text, never
    instead of it.
    """
    return re.sub(r"(?<=[^\W\d_])\s+(?=[^\W\d_])", "", text)


def searchable_forms(text: str) -> list[str]:
    """The forms a literal needle is tested against: as-read, and gap-closed.

    A match in either counts. Keeping the original means ordinary phrases
    ("room y908") still match; adding the closed form means a word the engine
    split ("se minar") does too.
    """
    lowered = text.casefold()
    collapsed = collapsed_text(lowered)
    return [lowered] if collapsed == lowered else [lowered, collapsed]


def searchable_text(text: str) -> str:
    """Every searchable form joined, for callers that do a substring test."""
    return "\n".join(searchable_forms(text))


def _decode_image(data: bytes) -> Any | None:
    """Decode bytes to an image array the engine accepts, or None."""
    try:
        numpy: Any = importlib.import_module("numpy")
        image_module: Any = importlib.import_module("PIL.Image")
        io_module: Any = importlib.import_module("io")
    except ImportError:
        return None
    try:
        with image_module.open(io_module.BytesIO(data)) as handle:
            rgb = handle.convert("RGB")
            return numpy.array(rgb)
    except Exception as exc:
        # a corrupt or unsupported image is not an error worth surfacing
        logger.debug("image could not be decoded for OCR: %s", type(exc).__name__)
        return None


def extract_text(image_bytes: bytes | None, *, content_type: str = "") -> str:
    """Read the text out of one image; '' when unreadable or OCR is absent.

    Truncates oversized images rather than refusing them: a poster that big is
    usually a scan, and the headline is what matters.
    """
    if not image_bytes:
        return ""
    if content_type and not content_type.startswith("image/"):
        return ""
    if len(image_bytes) > MAX_IMAGE_BYTES:
        logger.debug("image of %d bytes exceeds the OCR budget; skipping", len(image_bytes))
        return ""
    engine = _load_engine()
    if engine is None:
        return ""
    array = _decode_image(image_bytes)
    if array is None:
        return ""
    try:
        result: Any = engine(array)
    except Exception as exc:
        logger.warning("OCR failed on one image (%s); continuing", type(exc).__name__)
        return ""
    # the engine returns (rows, elapsed) where each row is [box, text, score];
    # some builds return the rows directly. Both shapes are accepted.
    return _rows_to_text(result)


def _rows_to_text(result: Any) -> str:
    """Join the recognised lines out of an engine result.

    The engine returns ``(rows, elapsed)`` where each row is
    ``[box, text, score]``; some builds return the rows list directly. Both
    shapes are accepted, and anything unexpected yields '' rather than raising.
    """
    boxed: Any = result
    if isinstance(boxed, tuple):
        # the engine's own tuple: pyright narrows Any to tuple[Unknown, ...]
        # here, which cannot be annotated away at the boundary
        pair: Any = boxed  # pyright: ignore[reportUnknownVariableType]
        unboxed: Any = pair[0] if len(pair) else None
    else:
        unboxed = boxed
    if not isinstance(unboxed, list):
        return ""
    lines: list[str] = []
    for raw in cast("list[Any]", unboxed):
        entry: Any = raw
        if not isinstance(entry, (list, tuple)):
            continue
        parts: list[Any] = list(cast("list[Any]", entry))
        if len(parts) < 2:
            continue
        text = str(parts[1]).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def should_attempt(content_type: str, size: int, *, filename: str = "") -> bool:
    """Whether an attachment looks worth OCR-ing.

    Kept deliberately narrow so a mailbox full of screenshots does not pay for
    recognition on every sync: images only, within the size budget, and not the
    chrome every mail carries (logos, QR codes, tracking pixels, signatures).
    """
    if not content_type.startswith("image/"):
        return False
    if size < MIN_IMAGE_BYTES or size > MAX_IMAGE_BYTES:
        return False
    lowered = filename.casefold()
    if any(token in lowered for token in _IGNORED_NAME_TOKENS):
        return False
    # an image with no name at all is usually a tracking pixel or a spacer
    return bool(filename.strip())


_IGNORED_NAME_TOKENS = (
    # tokens that are unambiguously mail chrome. Deliberately narrow: "banner"
    # and "header" are *not* here, because real event posters use them —
    # an observed poster was named "20260908A ... leadership talk series
    # banner_2000x1050_20260904.jpg", so skipping those names would drop the
    # very mails this feature exists for. Reading a header banner costs one
    # bounded model pass; missing a poster loses the event entirely.
    "logo",
    "signature",
    "avatar",
    "icon",
    "spacer",
    "pixel",
    "qr",
    "barcode",
)

MIN_IMAGE_BYTES = 1024
"""Below this an image is a tracking pixel or a spacer, not a poster."""


def extract_mail_images(attachments: list[Any], *, limit: int = 3) -> str:
    """Read the poster text out of a mail's image attachments.

    ``attachments`` are the parsed ones (``data`` may still be populated; the
    storage backend strips payloads later). At most ``limit`` images are read
    and the result is capped, because a mail carrying a photo album would
    otherwise dominate the analysis prompt.

    Runs synchronously and is CPU-bound: call it off the event loop.
    """
    from mailflow.domain import Attachment

    chunks: list[str] = []
    for item in attachments:
        if len(chunks) >= limit:
            break
        attachment: Attachment = item
        if not should_attempt(
            str(attachment.content_type), int(attachment.size), filename=str(attachment.filename)
        ):
            continue
        text = extract_text(attachment.data, content_type=str(attachment.content_type))
        if text.strip():
            chunks.append(text.strip())
    return "\n".join(chunks)[:_MAX_TEXT_CHARS]


_MAX_TEXT_CHARS = 4000
"""Ceiling on the recognised text carried into the prompt."""


__all__ = [
    "MAX_IMAGE_BYTES",
    "MIN_IMAGE_BYTES",
    "collapsed_text",
    "extract_mail_images",
    "extract_text",
    "is_available",
    "searchable_forms",
    "searchable_text",
    "should_attempt",
]
