"""Optional local OCR engine integration; skipped explicitly when the engine is unavailable."""

from __future__ import annotations

import pytest
from mailflow import ocr
from mailflow.domain import Attachment


class TestRecognition:
    """The real thing, when the optional engine is installed."""

    @staticmethod
    def _poster_bytes() -> bytes:
        """Render a poster with Pillow.

        Imported dynamically: Pillow arrives with the optional OCR package, so
        a static import would make the type checker (and the test suite) depend
        on a dependency most installs do not have.
        """
        pytest.importorskip("PIL.Image")
        import importlib
        import io
        from typing import Any

        image_module: Any = importlib.import_module("PIL.Image")
        draw_module: Any = importlib.import_module("PIL.ImageDraw")

        image: Any = image_module.new("RGB", (900, 200), "white")
        draw: Any = draw_module.Draw(image)
        draw.text((20, 40), "PAIR Research Seminar", fill="black")
        draw.text((20, 120), "15 October 2026, 14:00", fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return bytes(buffer.getvalue())

    def test_poster_text_is_read_and_searchable(self) -> None:
        if not ocr.is_available():
            pytest.skip("optional OCR engine not installed")
        data = self._poster_bytes()

        text = ocr.extract_text(data, content_type="image/png")
        forms = ocr.searchable_forms(text)

        assert any("seminar" in form for form in forms)
        assert any("15 october 2026" in form for form in forms)

    def test_the_mail_level_helper_reads_a_poster(self) -> None:
        if not ocr.is_available():
            pytest.skip("optional OCR engine not installed")
        attachment = Attachment(
            filename="PAIR-Seminar-poster.png",
            content_type="image/png",
            size=len(self._poster_bytes()),
            data=self._poster_bytes(),
        )

        text = ocr.extract_mail_images([attachment])

        assert "seminar" in ocr.searchable_text(text)
