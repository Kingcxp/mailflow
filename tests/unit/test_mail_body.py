"""Readable mail content, without changing the stored original."""

from mailflow.mail_body import (
    html_to_markdown,
    html_to_text,
    mail_body_markdown,
    mail_body_text,
    markdown_to_text,
    readable_markdown,
)
from mailflow.ocr import searchable_mail_text
from mailflow_testkit.fakes import make_mail
from rich.console import Console
from rich.markdown import Markdown


def test_markdown_becomes_readable_chat_text() -> None:
    source = (
        "## 查询结果\n\n找到 **3 封邮件**，其中 *1 封* 较重要。\n\n"
        "- 发件人：`Alice`\n- 主题：[查看发票](https://example.com/invoice)\n\n"
        "> 请先确认附件。\n\n"
        "| 日期 | 事项 |\n| --- | --- |\n| 今天 | 回复 |\n\n"
        "保留 snake_case 和 `__init__`。\n```python\nprint('__init__')\n```"
    )

    plain = markdown_to_text(source)

    assert plain == (
        "查询结果\n\n找到 3 封邮件，其中 1 封 较重要。\n\n"
        "• 发件人：Alice\n• 主题：查看发票 (https://example.com/invoice)\n\n"
        "请先确认附件。\n\n日期：今天 · 事项：回复\n\n"
        "保留 snake_case 和 __init__。\nprint('__init__')"
    )
    assert not any(marker in plain for marker in ("**", "##", "```", "| --- |", "[查看"))


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
    segments = list(Console(width=50).render(Markdown(html_to_markdown(source), justify="left")))
    rendered = "".join(s.text for s in segments)
    assert "你好 World" in rendered and "第二行 & <tag>" in rendered
    assert "First item" in rendered and "Nested" in rendered and "Read details" in rendered
    assert "secret-" not in rendered and "<p>" not in rendered and "tracker.invalid" not in rendered
    assert any("World" in s.text and s.style and s.style.bold for s in segments)
    assert any(
        s.style and s.style.link and "https://example.com/details" in s.style.link for s in segments
    )


def test_plain_text_keeps_math_addresses_indentation_and_html_examples() -> None:
    original = "1 < 2 and 3 > 2\n\n    Keep indentation\n<user@example.com> <topic> &amp;"
    mail = make_mail(body_text=original, body_html="")
    assert mail_body_text(mail) == original
    source = (
        "## Summary\n\n**Important**<br>Next &lt;tag&gt; [red]\n\n"
        "<strong>HTML emphasis </strong>and text\n\n"
        "```html\n<p>literal &amp; code</p>\n```\n\n"
        "`<b>literal inline</b>`\n\n<script>secret-script</script>"
    )
    mail.body_text = source
    markup = readable_markdown(source)
    assert mail_body_markdown(mail) == markup
    assert "<p>literal &amp; code</p>" in markup and "`<b>literal inline</b>`" in markup
    segments = list(Console(width=50).render(Markdown(markup, justify="left")))
    rendered = "".join(s.text for s in segments)
    assert "Next <tag> [red]" in rendered and "HTML emphasis and text" in rendered
    assert "<p>literal &amp; code</p>" in rendered and "<b>literal inline</b>" in rendered
    assert "secret-script" not in rendered and "<br>" not in rendered
    assert any("HTML emphasis" in s.text and s.style and s.style.bold for s in segments)
    lines = "First line\nSecond line\\\nThird line\n\n```text\nkeep\nthese lines\n```"
    markup = readable_markdown(lines, preserve_lines=True)
    rendered = "".join(s.text for s in Console(width=50).render(Markdown(markup)))
    assert "First line\nSecond line\nThird line\n" in "\n".join(
        line.rstrip() for line in rendered.splitlines()
    )
    assert "```text\nkeep\nthese lines\n```" in markup
