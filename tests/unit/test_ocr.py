"""Reading a poster's text out of an image attachment.

Opt-in and failure-tolerant by design: without the optional engine everything
here degrades to "no text" and the pipeline behaves exactly as before, because
most users will not have the OCR package installed. These tests therefore split
into two groups — the pure normalization/filtering logic (always runnable) and
recognition itself (skipped when the engine is absent).
"""

from __future__ import annotations

import pytest
from mailflow import ocr
from mailflow.domain import Attachment


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

    def test_collapsed_text_glues_word_boundaries_by_design(self) -> None:
        """Documents why the collapsed form may never be used alone."""
        assert ocr.collapsed_text("room y908 and zoom") == "roomy908 andzoom"

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

    def test_extract_text_survives_undecodable_bytes(self) -> None:
        assert ocr.extract_text(b"not an image at all", content_type="image/png") == ""

    def test_extract_text_survives_an_oversized_payload(self) -> None:
        oversized = b"\x89PNG" + b"\x00" * (ocr.MAX_IMAGE_BYTES + 10)

        assert ocr.extract_text(oversized, content_type="image/png") == ""

    def test_extract_mail_images_skips_chrome_and_returns_empty(self) -> None:
        attachments = [
            Attachment(filename="logo.png", content_type="image/png", size=1_000),
            Attachment(filename="signature.jpg", content_type="image/jpeg", size=2_000),
        ]

        assert ocr.extract_mail_images(attachments) == ""

    def test_is_available_never_raises(self) -> None:
        assert isinstance(ocr.is_available(), bool)


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


class TestPromptIntegration:
    """Recognised text must reach the analyser, labelled as derived content."""

    def test_image_text_is_appended_and_labelled(self) -> None:
        from mailflow.domain import MailAddress, MailMessage
        from mailflow.processors import _plain_body  # pyright: ignore[reportPrivateUsage]

        mail = MailMessage(
            message_id="m1",
            account_id="a",
            subject="Invitation",
            sender=MailAddress(address="s@example.com"),
            recipients=[],
            cc=[],
            date=__import__("datetime").datetime(2026, 10, 1, tzinfo=__import__("datetime").UTC),
            received_at=__import__("datetime").datetime(
                2026, 10, 1, tzinfo=__import__("datetime").UTC
            ),
            body_text="See the poster.",
            image_text="PAIR Research Seminar\n15 October 2026",
        )

        body = _plain_body(mail)  # pyright: ignore[reportPrivateUsage]

        assert "See the poster." in body
        assert "PAIR Research Seminar" in body
        assert "text read from images" in body

    def test_without_image_text_nothing_changes(self) -> None:
        from mailflow.domain import MailAddress, MailMessage
        from mailflow.processors import _plain_body  # pyright: ignore[reportPrivateUsage]

        mail = MailMessage(
            message_id="m1",
            account_id="a",
            subject="Invitation",
            sender=MailAddress(address="s@example.com"),
            recipients=[],
            cc=[],
            date=__import__("datetime").datetime(2026, 10, 1, tzinfo=__import__("datetime").UTC),
            received_at=__import__("datetime").datetime(
                2026, 10, 1, tzinfo=__import__("datetime").UTC
            ),
            body_text="Plain body.",
        )

        assert _plain_body(mail) == "Plain body."  # pyright: ignore[reportPrivateUsage]
