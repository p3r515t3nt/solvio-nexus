"""Deterministic HTML -> (title, visible text) extraction.

Uses only the standard-library html.parser. It NEVER executes JavaScript, NEVER
loads external resources (images/stylesheets/iframes/scripts), and drops the
content of <script>/<style>/<noscript>/<template>/<svg>/<iframe> and all comments.
Only the initial document is processed.

Website text is UNTRUSTED_WEB. This function returns it verbatim as data; it never
interprets it as an instruction.
"""
from __future__ import annotations

from html.parser import HTMLParser

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "canvas"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "ul", "ol", "table",
    "blockquote", "pre", "hr", "figure", "main", "aside", "nav",
}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._in_title = False
        self._title: list[str] = []
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip += 1
        elif t == "title":
            self._in_title = True
        elif t in _BLOCK_TAGS:
            self._text.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() in _BLOCK_TAGS:
            self._text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _SKIP_TAGS and self._skip > 0:
            self._skip -= 1
        elif t == "title":
            self._in_title = False
        elif t in _BLOCK_TAGS:
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip > 0:
            return
        if self._in_title:
            self._title.append(data)
        else:
            self._text.append(data)

    # handle_comment intentionally not overridden -> comments are dropped.


def extract_html(html_text: str) -> tuple[str | None, str]:
    """Return (title, text). Whitespace is collapsed; block tags become newlines."""
    parser = _Extractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 - tolerant parse of arbitrary/broken HTML
        pass
    title = " ".join("".join(parser._title).split()) or None
    lines = ("".join(parser._text)).split("\n")
    cleaned = [" ".join(line.split()) for line in lines]
    text = "\n".join(line for line in cleaned if line)
    return title, text


def strip_tags(xml_text: str) -> str:
    """Best-effort text for xml/xhtml: reuse the HTML extractor's text output."""
    return extract_html(xml_text)[1]
