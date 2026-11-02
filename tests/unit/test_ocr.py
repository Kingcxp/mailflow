"""OCR normalization, image selection and failure tolerance with no OCR service.

Local engine recognition belongs to integration/test_ocr_engine.py.
"""

from __future__ import annotations

import pytest
from mailflow import ocr
from mailflow.domain import Attachment

OCR_RESULTS: list[tuple[object, str]] = [
    ([[[], "Research Meeting", 0.99]], "Research Meeting"),
    (([[[], "Research Meeting", 0.99]], [0.01]), "Research Meeting"),
    (None, ""),
    ([None, [], [0], [[], "  ", 0.99], [[], "Kept", 0.99]], "Kept"),
]


def test_only_readable_posters_reach_ocr_and_limit_is_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bytes] = []

    def recognize(data: bytes, *, content_type: str) -> str:
        assert content_type == "image/png"
        seen.append(data)
        return "Poster " + str(len(seen))

    monkeypatch.setattr(ocr, "extract_text", recognize)

    def image(name: str, data: bytes, content_type: str = "image/png") -> Attachment:
        return Attachment(filename=name, content_type=content_type, size=len(data), data=data)

    first, second = b"a" * 2048, b"b" * 2048
    attachments = [
        image("logo.png", first),
        image("signature.jpg", first),
        image("poster.pdf", first, "application/pdf"),
        image("", first),
        image("tiny.png", b"x"),
        image("oversized.png", b"x" * (ocr.MAX_IMAGE_BYTES + 1)),
        Attachment(filename="missing.png", content_type="image/png", size=2048),
        image("event-banner-20260908.png", first),
        image("poster.png", second),
        image("beyond-limit.png", b"c" * 2048),
    ]
    assert ocr.extract_mail_images(attachments, limit=2) == "Poster 1\nPoster 2"
    assert seen == [first, second]


class TestFailureTolerance:
    """Nothing here may raise into the mail path."""

    def test_extract_text_survives_undecodable_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import Mock

        engine = Mock(side_effect=AssertionError("undecodable data must not reach recognition"))
        monkeypatch.setattr(ocr, "_load_engine", lambda: engine)
        monkeypatch.setattr(ocr, "_decode_image", Mock(return_value=None))
        assert ocr.extract_text(b"not an image at all", content_type="image/png") == ""

    @pytest.mark.parametrize(
        ("result", "expected"),
        OCR_RESULTS,
    )
    def test_engine_results_reach_extracted_text(
        self, result: object, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import Mock

        engine = Mock(return_value=result)
        monkeypatch.setattr(ocr, "_load_engine", lambda: engine)
        monkeypatch.setattr(ocr, "_decode_image", Mock(return_value=object()))
        assert ocr.extract_text(b"fake image", content_type="image/png") == expected

    def test_engine_failure_does_not_escape_into_mail_processing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import Mock

        engine = Mock(side_effect=RuntimeError("OCR model unavailable"))
        monkeypatch.setattr(ocr, "_load_engine", lambda: engine)
        monkeypatch.setattr(ocr, "_decode_image", Mock(return_value=object()))
        assert ocr.extract_text(b"fake image", content_type="image/png") == ""
