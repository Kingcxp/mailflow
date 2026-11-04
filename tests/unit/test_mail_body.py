"""Readable mail content, without changing the stored original."""

from mailflow.mail_body import html_to_text, mail_body_text
from mailflow.ocr import searchable_mail_text
from mailflow_testkit.fakes import make_mail


def test_html_body_keeps_reading_structure_links_and_literal_text() -> None:
    source = """<html><head><style>secret-style</style></head><body>
      <p>你好&nbsp;<b>World</b><BR class="break" />第二行 &amp; &lt;tag&gt;</p>
      <div>First block</div><div>Second block</div>
      <ol start="3"><li>First item</li><li>Second item<ul><li>Nested</li></ul></li></ol>
      <table><tr><th>Place</th><th>Time</th></tr><tr><td>Room</td><td>09:00</td></tr></table>
      <pre>if 1 &lt; 2:\n    proceed()</pre>
      <p><a href="https://example.com/details?a=1&amp;b=2">Read details</a></p>
      <div hidden>secret-hidden</div><span style="display: none">secret-preview</span>
      <script>secret-script</script><img src="https://tracker.invalid/" alt="Illustration">
    </body></html>"""
    readable = html_to_text(source)
    assert "你好 World\n第二行 & <tag>\n\nFirst block\nSecond block" in readable
    assert "3. First item\n4. Second item\n  • Nested" in readable
    assert "Place\tTime\nRoom\t09:00" in readable
    assert "if 1 < 2:\n    proceed()" in readable
    assert "Read details (https://example.com/details?a=1&b=2)" in readable
    assert "Illustration" in readable and "tracker.invalid" not in readable
    assert "secret-" not in readable
    mail = make_mail(body_text="", body_html=source)
    assert mail_body_text(mail) == readable
    mail.body_text = source  # Some sources put HTML in text/plain; old records retain it.
    assert mail_body_text(mail) == readable
    assert mail.body_text == source and mail.body_html == source
    assert "https://example.com/details" in searchable_mail_text(mail)
    assert "secret-" not in searchable_mail_text(mail)


def test_plain_text_keeps_math_addresses_indentation_and_html_examples() -> None:
    original = "1 < 2 and 3 > 2\n\n    Keep indentation\n<user@example.com> <topic> &amp;"
    mail = make_mail(body_text=original, body_html="")
    assert mail_body_text(mail) == original
