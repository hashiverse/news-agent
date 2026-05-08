"""Small text-processing helpers shared by the article picker and the post formatter.

Both modules need to convert RSS-flavoured strings (potentially HTML-wrapped
``<description>``) into clean plain text, and the picker additionally needs
to extract just the first sentence of a summary so a keyword filter doesn't
match against trailing SEO/hashtag dumps. Stdlib only — ``html.parser`` for
the strip and a small regex for the sentence terminator.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []

    def handle_data(self, data: str) -> None:
        self._buf.append(data)

    # Insert a separator at every tag boundary so adjacent inline runs like
    # `<p>One.</p><p>Two.</p>` become `One. Two.` (not `One.Two.`) and self-
    # closing `<br/>` separates surrounding text. The whitespace-collapse pass
    # below squashes multiple inserted spaces into one.
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        self._buf.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._buf.append(" ")

    def get_text(self) -> str:
        return "".join(self._buf)


_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(value: str) -> str:
    """Return ``value`` with HTML tags removed and whitespace collapsed.

    Plain-text strings (no `<` and no `&`) pass through with only whitespace
    collapse. RSS summaries often arrive wrapped in ``<p>`` / ``<br>`` markup;
    this returns just the visible text.
    """
    if not value:
        return ""
    if "<" not in value and "&" not in value:
        return _WHITESPACE_RE.sub(" ", value).strip()
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return _WHITESPACE_RE.sub(" ", unescape(parser.get_text())).strip()


# Match a sentence-ending `.`, `!`, or `?` that is *followed by* whitespace
# or end-of-string. The look-ahead lets us correctly skip mid-token punctuation
# like "$3.14" (next char is a digit, not whitespace) and "U.S.A." (next char
# is a letter), but we DO split on "Mr. Smith" — accepted tradeoff vs. NLTK.
_SENTENCE_TERMINATOR = re.compile(r"[.!?](?=\s|$)")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text``.

    Definition: everything up to and including the first ``.``, ``!``, or
    ``?`` that is followed by whitespace or end-of-string. Returns the whole
    input when no terminator is found. Leading/trailing whitespace is
    stripped before scanning.
    """
    text = text.strip()
    if not text:
        return ""
    m = _SENTENCE_TERMINATOR.search(text)
    if m is None:
        return text
    return text[: m.end()]


# Match a `#` followed by one or more word characters (letters, digits,
# underscore — `\w` is Unicode-aware in Python 3, so `#日本語` also matches).
_HASHTAG_RE = re.compile(r"#\w+")


def strip_hashtags(text: str) -> str:
    """Remove `#tag` tokens and collapse the resulting whitespace.

    Used by the keyword filter to defend against SEO/hashtag dumps that
    creators commonly stuff into RSS / YouTube descriptions (e.g.
    ``... #tesla #robotaxi #FSD #autonomousdriving``). Without this, a
    keyword like ``robotaxi`` would match every video that has ``#robotaxi``
    in its descriptive tag block, even when the video isn't actually about
    robotaxis.

    A bare ``#`` (no word chars after) is left in place. URL fragments like
    ``example.com/page#section`` lose the ``#section`` suffix — acceptable,
    URLs aren't typically substring-matched against keywords.
    """
    if "#" not in text:
        return text
    return _WHITESPACE_RE.sub(" ", _HASHTAG_RE.sub("", text)).strip()
