"""Read text from user-provided chat attachments."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

MAX_EXTRACTED_CHARS = 16000


class AttachmentTextError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def read_attachment_text(filename: str, content_type: str, data: bytes) -> str:
    """Extract text from supported files without executing their contents."""
    suffix = Path(filename).suffix.casefold()
    if suffix in {".txt", ".md", ".csv"} or content_type.startswith("text/"):
        text = data.decode("utf-8-sig", errors="replace")
    elif suffix == ".docx":
        try:
            with ZipFile(BytesIO(data)) as document:
                xml = ElementTree.fromstring(document.read("word/document.xml"))
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            text = "\n".join(
                paragraph_text
                for paragraph in xml.iter(namespace + "p")
                if (
                    paragraph_text := "".join(
                        node.text or "" for node in paragraph.iter(namespace + "t")
                    )
                )
            )
        except (BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise AttachmentTextError("experience.file_read_failed") from exc
    elif suffix == ".pdf" or content_type == "application/pdf":
        try:
            from pypdf import PdfReader

            text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
        except Exception as exc:
            raise AttachmentTextError("experience.file_read_failed") from exc
    elif content_type.startswith("image/") or suffix in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
    }:
        from mailflow.ocr import extract_text, is_available

        if not is_available():
            raise AttachmentTextError("experience.file_ocr_unavailable")
        if not content_type.startswith("image/"):
            import mimetypes

            content_type = mimetypes.guess_type(filename)[0] or ""
        text = extract_text(data, content_type=content_type)
    else:
        raise AttachmentTextError("experience.file_read_unsupported")

    text = text.strip()
    if not text:
        raise AttachmentTextError("experience.file_text_empty")
    return text


__all__ = ["MAX_EXTRACTED_CHARS", "AttachmentTextError", "read_attachment_text"]
