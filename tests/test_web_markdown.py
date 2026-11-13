"""Tests for the dependency-free Markdown renderer used by the web UI."""

from __future__ import annotations

from aqm.web.markdown import render_markdown


def test_empty_input_returns_empty_string():
    assert render_markdown("") == ""
    assert render_markdown("   \n\t\n") == ""


def test_plain_text_keeps_line_breaks():
    html = render_markdown("第一行\n第二行\n\n新段落")
    assert html == "<p>第一行<br>第二行</p><p>新段落</p>"


def test_headings_and_inline_formatting():
    html = render_markdown("# 标题\n\n正文 **加粗** *斜体* `code`")
    assert "<h1>标题</h1>" in html
    assert "<strong>加粗</strong>" in html
    assert "<em>斜体</em>" in html
    assert "<code>code</code>" in html


def test_nested_lists():
    html = render_markdown("- a\n- b\n  - b1\n  - b2\n- c")
    assert html == (
        "<ul><li>a</li><li>b<ul><li>b1</li><li>b2</li></ul></li><li>c</li></ul>"
    )


def test_ordered_list():
    html = render_markdown("1. 甲\n2. 乙")
    assert html == "<ol><li>甲</li><li>乙</li></ol>"


def test_table():
    src = "| 项 | 说明 |\n| --- | --- |\n| A | **重要** |"
    html = render_markdown(src)
    assert "<table>" in html
    assert "<th>项</th>" in html
    assert "<td><strong>重要</strong></td>" in html
    assert "---" not in html


def test_fenced_code_and_blockquote():
    html = render_markdown("```python\nprint('<x>')\n```\n\n> 引用")
    assert 'class="language-python"' in html
    assert "&lt;x&gt;" in html
    assert "<blockquote>引用</blockquote>" in html


def test_html_is_escaped():
    html = render_markdown("<script>alert(1)</script>\n\n<img src=x onerror=1>")
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html


def test_horizontal_rule():
    assert "<hr>" in render_markdown("上面\n\n---\n\n下面")
