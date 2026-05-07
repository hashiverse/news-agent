"""Tests for rss_parser.parse_feed."""

from __future__ import annotations

from news_agent.rss_parser import Article, parse_feed


SOURCE = "https://example.com/rss"


def _rss_doc(entries_xml: str) -> bytes:
    """Wrap entry XML in a minimal RSS 2.0 envelope."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test feed</title>
    <link>https://example.com/</link>
    <description>x</description>
    {entries_xml}
  </channel>
</rss>""".encode("utf-8")


def test_parses_basic_rss_entry():
    body = _rss_doc(
        """
        <item>
          <title>Hello world</title>
          <link>https://example.com/articles/hello?utm_source=twitter</link>
          <guid>tag:example.com,2026-05-07:hello</guid>
          <description>An example post.</description>
          <pubDate>Wed, 07 May 2026 09:00:00 GMT</pubDate>
        </item>
        """
    )
    articles = parse_feed(body, SOURCE)
    assert len(articles) == 1
    a = articles[0]
    assert isinstance(a, Article)
    assert a.title == "Hello world"
    # Canonicalised: utm_source dropped.
    assert a.canonical_url == "https://example.com/articles/hello"
    # Raw URL preserved separately for the post body.
    assert a.raw_url == "https://example.com/articles/hello?utm_source=twitter"
    assert a.item_guid == "tag:example.com,2026-05-07:hello"
    assert a.summary == "An example post."
    assert a.source_url == SOURCE
    assert a.published_at_unix is not None
    assert isinstance(a.published_at_unix, int)


def test_parses_atom_entry():
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom feed</title>
  <link href="https://example.com/"/>
  <id>urn:example.com</id>
  <updated>2026-05-07T09:00:00Z</updated>
  <entry>
    <title>Atom hello</title>
    <link href="https://example.com/atom-hello"/>
    <id>urn:example.com:atom-hello</id>
    <updated>2026-05-07T09:30:00Z</updated>
    <summary>An atom post.</summary>
  </entry>
</feed>"""
    articles = parse_feed(body, SOURCE)
    assert len(articles) == 1
    assert articles[0].title == "Atom hello"
    assert articles[0].canonical_url == "https://example.com/atom-hello"
    assert articles[0].item_guid == "urn:example.com:atom-hello"


def test_skips_entry_without_link():
    body = _rss_doc(
        """
        <item>
          <title>Has no link</title>
          <description>Should be skipped.</description>
        </item>
        <item>
          <title>Has a link</title>
          <link>https://example.com/keepme</link>
        </item>
        """
    )
    articles = parse_feed(body, SOURCE)
    assert [a.title for a in articles] == ["Has a link"]


def test_falls_back_to_url_when_title_missing():
    body = _rss_doc(
        """
        <item>
          <link>https://example.com/notitle</link>
        </item>
        """
    )
    articles = parse_feed(body, SOURCE)
    assert len(articles) == 1
    assert articles[0].title == "https://example.com/notitle"


def test_no_pubdate_yields_none_published_at():
    body = _rss_doc(
        """
        <item>
          <title>No date</title>
          <link>https://example.com/no-date</link>
        </item>
        """
    )
    articles = parse_feed(body, SOURCE)
    assert articles[0].published_at_unix is None


def test_completely_malformed_input_returns_empty():
    body = b"<this is not valid xml>"
    articles = parse_feed(body, SOURCE)
    assert articles == []


def test_empty_feed_returns_empty_list():
    body = _rss_doc("")
    assert parse_feed(body, SOURCE) == []


def test_multiple_entries_preserved_in_order():
    body = _rss_doc(
        """
        <item>
          <title>First</title>
          <link>https://example.com/1</link>
        </item>
        <item>
          <title>Second</title>
          <link>https://example.com/2</link>
        </item>
        <item>
          <title>Third</title>
          <link>https://example.com/3</link>
        </item>
        """
    )
    articles = parse_feed(body, SOURCE)
    assert [a.title for a in articles] == ["First", "Second", "Third"]


def test_canonical_dedupes_two_entries_with_same_link_and_different_tracking():
    body = _rss_doc(
        """
        <item>
          <title>v1</title>
          <link>https://example.com/post?utm_source=newsletter</link>
        </item>
        <item>
          <title>v2</title>
          <link>https://example.com/post?utm_source=facebook</link>
        </item>
        """
    )
    articles = parse_feed(body, SOURCE)
    # rss_parser doesn't dedupe — that's the picker's job. But the
    # canonical_url field should be identical, which is the property the
    # downstream dedupe relies on.
    canonicals = [a.canonical_url for a in articles]
    assert len(canonicals) == 2
    assert canonicals[0] == canonicals[1] == "https://example.com/post"
