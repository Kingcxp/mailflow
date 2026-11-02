"""OCR normalization, image selection and failure tolerance with no OCR service.

Local engine recognition belongs to integration/test_ocr_engine.py.
"""

from __future__ import annotations

import pytest
from mailflow import ocr
from mailflow.domain import Attachment

OCR_RESULTS: list[tuple[object, str]] = [
    ([[[], "Research Seminar", 0.99]], "Research Seminar"),
    (([[[], "Research Seminar", 0.99]], [0.01]), "Research Seminar"),
    (None, ""),
    ([None, [], [0], [[], "  ", 0.99], [[], "Kept", 0.99]], "Kept"),
]


class TestNormalization:
    """OCR inserts spaces at glyph gaps; search must survive that."""

    def test_a_word_split_by_the_engine_is_findable_in_some_form(self) -> None:
        # this is a real observed result: "Seminar" came back as "Se minar"
        text = "PAIR Research Se minar\n15 October 2026"
        forms = ocr.searchable_forms(text)

        assert "seminar" not in text.casefold()  # the as-read form misses it
        assert any("seminar" in form for form in forms)

    def test_the_as_read_form_still_matches_ordinary_phrases(self) -> None:
        """Collapsing every letter gap glues real words, so both forms ship."""
        forms = ocr.searchable_forms("Room Y908 and Zoom")

        assert any("room y908 and zoom" in form for form in forms)

    def test_empty_input_yields_no_distinct_form(self) -> None:
        assert ocr.searchable_forms("") == [""]


class TestWhichImagesToRead:
    """Only poster-like images are worth a model pass."""

    @pytest.mark.parametrize(
        "filename",
        [
            "poster.jpg",
            "PAIR-Seminar-15Oct2026.png",
            "flyer.jpeg",
            "event-banner-2.png",
            # an observed real poster: "banner" must not be treated as chrome
            "20260908A_V6_ESG_leadership_talk_series_banner_2000x1050_20260904.jpg",
        ],
    )
    def test_poster_like_images_are_attempted(self, filename: str) -> None:
        assert ocr.should_attempt("image/jpeg", 200_000, filename=filename)

    @pytest.mark.parametrize(
        "filename",
        ["logo.png", "signature.jpg", "spacer.gif", "qr-code.png", "pixel.png", "avatar.png"],
    )
    def test_chrome_is_skipped(self, filename: str) -> None:
        assert not ocr.should_attempt("image/png", 5_000, filename=filename)

    def test_a_tiny_image_is_treated_as_a_pixel(self) -> None:
        assert not ocr.should_attempt("image/png", ocr.MIN_IMAGE_BYTES - 1, filename="poster.png")

    def test_a_nameless_image_is_skipped(self) -> None:
        """An unnamed inline image is a tracking pixel or a spacer."""
        assert not ocr.should_attempt("image/png", 8_000, filename="")

    def test_non_images_are_never_attempted(self) -> None:
        assert not ocr.should_attempt("application/pdf", 200_000, filename="poster.pdf")

    def test_an_oversized_image_is_skipped(self) -> None:
        assert not ocr.should_attempt(
            "image/png", ocr.MAX_IMAGE_BYTES + 1, filename="huge-poster.png"
        )


class TestFailureTolerance:
    """Nothing here may raise into the mail path."""

    def test_extract_text_without_bytes_returns_empty(self) -> None:
        assert ocr.extract_text(None) == ""
        assert ocr.extract_text(b"") == ""

    def test_extract_text_rejects_a_non_image_content_type(self) -> None:
        assert ocr.extract_text(b"\x00\x01binary", content_type="application/pdf") == ""

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

    def test_extract_text_survives_an_oversized_payload(self) -> None:
        oversized = b"\x89PNG" + b"\x00" * (ocr.MAX_IMAGE_BYTES + 10)

        assert ocr.extract_text(oversized, content_type="image/png") == ""

    def test_extract_mail_images_skips_chrome_and_returns_empty(self) -> None:
        attachments = [
            Attachment(filename="logo.png", content_type="image/png", size=1_000),
            Attachment(filename="signature.jpg", content_type="image/jpeg", size=2_000),
        ]

        assert ocr.extract_mail_images(attachments) == ""
