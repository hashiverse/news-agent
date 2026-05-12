"""Local OpenGraph fetcher — replaces the hashiverse-server round-trip.

Mirrors the fallback chain in hashiverse-lib's ``extract_url_preview``:

  title:       meta[property='og:title']        → meta[name='twitter:title']        → <title>
  description: meta[property='og:description']  → meta[name='twitter:description']  → meta[name='description']
  image_url:   meta[property='og:image']        → meta[name='twitter:image']        → meta[name='twitter:image:src']
  url:         meta[property='og:url']          → <link rel='canonical' href=…>     (href, not content)

Stdlib-only. The HTML parser is ``html.parser.HTMLParser``; the HTTP fetch
reuses ``urllib.request`` the same way ``rss_fetcher.py`` does. No new
runtime dependencies.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 2 * 1024 * 1024
# Many sites (incl. YouTube) whitelist Facebook's link-preview crawler from
# bot-detection because rich Facebook previews drive traffic, so they serve it
# the real OG tags. We don't lie about what we are anywhere else — RSS and
# control-file fetches in sibling modules keep the honest news-agent UA.
USER_AGENT = "facebookexternalhit/1.1"


@dataclass(frozen=True)
class UrlPreviewData:
    """OG/twitter/canonical fields used to render a ``plugin-urlpreview-card``.

    All fields default to "" so callers can construct a no-preview-available
    fallback. ``posting.format_post_html`` falls back to the article's title /
    raw URL when these are blank.
    """

    url: str = ""
    title: str = ""
    description: str = ""
    image_url: str = ""


class UrlPreviewError(RuntimeError):
    """Raised for URL-validation failures (e.g. non-HTTPS scheme)."""


class _PreviewExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_title = ""
        self.og_description = ""
        self.og_image = ""
        self.og_url = ""
        self.twitter_title = ""
        self.twitter_description = ""
        self.twitter_image = ""
        self.twitter_image_src = ""
        self.meta_description = ""
        self.title_tag = ""
        self.canonical_link = ""
        self._in_title = False
        self._title_buf: list[str] = []
        # Set once </head> is seen; further callbacks become no-ops. We can't
        # raise to short-circuit the parser — HTMLParser doesn't advance its
        # rawdata cursor when a handler raises, so the next goahead() (e.g.
        # from close()) re-fires the same tag and the exception leaks out.
        # A flag is cheaper than that footgun.
        self._stopped = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._stopped:
            return
        if tag == "meta":
            attrs_dict = {k.lower(): (v or "") for k, v in attrs}
            prop = attrs_dict.get("property", "").lower()
            name = attrs_dict.get("name", "").lower()
            content = attrs_dict.get("content", "")
            if prop == "og:title" and not self.og_title:
                self.og_title = content
            elif prop == "og:description" and not self.og_description:
                self.og_description = content
            elif prop == "og:image" and not self.og_image:
                self.og_image = content
            elif prop == "og:url" and not self.og_url:
                self.og_url = content
            elif name == "twitter:title" and not self.twitter_title:
                self.twitter_title = content
            elif name == "twitter:description" and not self.twitter_description:
                self.twitter_description = content
            elif name == "twitter:image" and not self.twitter_image:
                self.twitter_image = content
            elif name == "twitter:image:src" and not self.twitter_image_src:
                self.twitter_image_src = content
            elif name == "description" and not self.meta_description:
                self.meta_description = content
        elif tag == "link":
            attrs_dict = {k.lower(): (v or "") for k, v in attrs}
            if attrs_dict.get("rel", "").lower() == "canonical" and not self.canonical_link:
                self.canonical_link = attrs_dict.get("href", "")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if self._stopped:
            return
        if tag == "title":
            self._in_title = False
            if not self.title_tag:
                self.title_tag = "".join(self._title_buf).strip()
        elif tag == "head":
            self._stopped = True

    def handle_data(self, data: str) -> None:
        if self._stopped:
            return
        if self._in_title:
            self._title_buf.append(data)


def _extract_url_preview(html_text: str, fetched_from_url: str) -> UrlPreviewData:
    """Pure: parse HTML, apply the fallback chain, return UrlPreviewData."""
    extractor = _PreviewExtractor()
    extractor.feed(html_text)
    extractor.close()

    title = (
        _clean_text(extractor.og_title)
        or _clean_text(extractor.twitter_title)
        or _clean_text(extractor.title_tag)
    )
    description = (
        _clean_text(extractor.og_description)
        or _clean_text(extractor.twitter_description)
        or _clean_text(extractor.meta_description)
    )
    image_url = (
        _clean_url(extractor.og_image)
        or _clean_url(extractor.twitter_image)
        or _clean_url(extractor.twitter_image_src)
    )
    canonical_url = _clean_url(extractor.og_url) or _clean_url(extractor.canonical_link)

    return UrlPreviewData(
        url=canonical_url or fetched_from_url,
        title=title,
        description=description,
        image_url=image_url,
    )


# JS-side `undefined`/`null` can leak into server-rendered meta tags when a
# site's templating fails (observed on YouTube bot-detection pages served to
# datacenter IPs). Treat those literal strings as if the field were absent.
_BROKEN_TEMPLATE_SENTINELS = frozenset({"undefined", "null"})


def _clean_text(s: str) -> str:
    stripped = s.strip()
    if stripped.lower() in _BROKEN_TEMPLATE_SENTINELS:
        return ""
    return stripped


def _clean_url(s: str) -> str:
    """Return ``s`` only if it parses as an absolute http(s) URL.

    Drops empty, sentinel, relative, and protocol-relative values. We
    deliberately do NOT resolve relative URLs against the page; a field
    that wasn't worth a valid absolute URL just becomes empty and the
    upstream fallback chain (posting.format_post_html) kicks in.
    """
    cleaned = _clean_text(s)
    if not cleaned:
        return ""
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return ""
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return cleaned
    return ""


def _fetch_html(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_BODY_BYTES,
    opener: urllib.request.OpenerDirector | None = None,
) -> str:
    """HTTP GET ``url``, decode using the response charset (or UTF-8), return text.

    Truncates the response body at ``max_bytes`` to bound memory and to defend
    against pathological huge responses. urllib exceptions
    (``HTTPError`` / ``URLError`` / socket timeout) propagate; the caller
    decides how to degrade.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    with open_fn(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        body = body[:max_bytes]
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch_url_preview(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_BODY_BYTES,
) -> UrlPreviewData:
    """Fetch ``url``, extract OG/twitter/canonical metadata.

    Raises ``UrlPreviewError`` for non-HTTPS URLs (no network attempted).
    Lets urllib exceptions propagate — the caller (posting._fetch_preview_safely)
    catches ``Exception`` and degrades to a blank preview.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UrlPreviewError(f"refusing non-HTTPS URL: {url!r}")

    html_text = _fetch_html(url, timeout=timeout, max_bytes=max_bytes)
    return _extract_url_preview(html_text, fetched_from_url=url)
