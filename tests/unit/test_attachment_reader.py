from __future__ import annotations

import sys
from io import BytesIO
from types import ModuleType, SimpleNamespace
from typing import Any
from zipfile import ZipFile

import pytest
from mailflow.attachment_reader import (
    AttachmentTextError,
    read_attachment_text,
)


def _docx(*paragraphs: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as document:
        body = "".join(f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>" for paragraph in paragraphs)
        document.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>",
        )
    return output.getvalue()


def test_reads_supported_text_formats_without_losing_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = ModuleType("pypdf")

    def reader(_: Any) -> SimpleNamespace:
        return SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "PDF content")])

    pdf.__dict__["PdfReader"] = reader
    monkeypatch.setitem(sys.modules, "pypdf", pdf)
    import mailflow.ocr as ocr

    monkeypatch.setattr(ocr, "is_available", lambda: True)

    def image_text(*_args: Any, **_kwargs: Any) -> str:
        return "image text"

    monkeypatch.setattr(ocr, "extract_text", image_text)

    assert read_attachment_text("note.md", "text/markdown", b"hello") == "hello"
    assert (
        read_attachment_text("note.docx", "application/octet-stream", _docx("first", "second"))
        == "first\nsecond"
    )
    assert read_attachment_text("note.pdf", "application/pdf", b"fake pdf") == "PDF content"
    assert read_attachment_text("poster.png", "image/png", b"fake image") == "image text"
    assert read_attachment_text("large.txt", "text/plain", b"x" * 20_000) == "x" * 20_000


def test_reports_unsupported_or_unreadable_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = ModuleType("pypdf")

    def reader(_: Any) -> SimpleNamespace:
        return SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "")])

    pdf.__dict__["PdfReader"] = reader
    monkeypatch.setitem(sys.modules, "pypdf", pdf)
    with pytest.raises(AttachmentTextError, match=r"experience\.file_read_unsupported"):
        read_attachment_text("archive.zip", "application/zip", b"zip")
    with pytest.raises(AttachmentTextError, match=r"experience\.file_text_empty"):
        read_attachment_text("scan.pdf", "application/pdf", b"fake pdf")

    import mailflow.ocr as ocr

    monkeypatch.setattr(ocr, "is_available", lambda: False)
    with pytest.raises(AttachmentTextError, match=r"experience\.file_ocr_unavailable"):
        read_attachment_text("poster.png", "image/png", b"fake image")
