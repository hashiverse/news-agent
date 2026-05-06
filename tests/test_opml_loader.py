"""Tests for opml_loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from news_agent.opml_loader import FeedSpec, OpmlParseError, load_opml

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_minimal_opml_returns_only_outlines_with_xml_url():
    feeds = load_opml(FIXTURES / "feeds_minimal.opml")
    assert len(feeds) == 2
    titles = [feed.title for feed in feeds]
    assert "BBC News — Africa" in titles
    assert "Computerphile" in titles
    # Outlines without xmlUrl or without type=rss are skipped.
    assert "Outline without xmlUrl" not in titles
    assert "Folder, not a feed" not in titles


def test_feed_spec_fields_are_populated():
    feeds = {feed.title: feed for feed in load_opml(FIXTURES / "feeds_minimal.opml")}
    bbc = feeds["BBC News — Africa"]
    assert bbc.feed_url == "https://feeds.bbci.co.uk/news/world/africa/rss.xml"
    assert bbc.site_url == "https://www.bbc.com/news/world/africa"
    assert bbc.raw_categories == ("/country/gb", "/region/africa", "/topic/news")
    assert bbc.tags == ("gb", "africa", "news")


def test_leaf_tags_are_lowercased():
    feeds = {feed.title: feed for feed in load_opml(FIXTURES / "feeds_minimal.opml")}
    cphile = feeds["Computerphile"]
    assert "cryptography" in cphile.tags
    assert "computer-science" in cphile.tags
    assert "video" in cphile.tags


def test_malformed_xml_raises(tmp_path):
    bad = tmp_path / "bad.opml"
    bad.write_text("<not-xml>oops")
    with pytest.raises(OpmlParseError):
        load_opml(bad)


def test_wrong_root_element_raises(tmp_path):
    not_opml = tmp_path / "not_opml.xml"
    not_opml.write_text("<?xml version=\"1.0\"?><other/>")
    with pytest.raises(OpmlParseError):
        load_opml(not_opml)


def test_empty_body_returns_empty_list(tmp_path):
    empty = tmp_path / "empty.opml"
    empty.write_text(
        "<?xml version=\"1.0\"?>"
        "<opml version=\"2.0\"><head><title>empty</title></head><body></body></opml>"
    )
    feeds = load_opml(empty)
    assert feeds == []


def test_duplicate_category_entries_are_deduplicated(tmp_path):
    dupe = tmp_path / "dupe.opml"
    dupe.write_text(
        "<?xml version=\"1.0\"?>"
        "<opml version=\"2.0\"><head/><body>"
        "<outline text=\"x\" type=\"rss\" xmlUrl=\"http://a/feed\" "
        "category=\"/topic/news,/topic/news,/region/eu\" />"
        "</body></opml>"
    )
    feed: FeedSpec = load_opml(dupe)[0]
    assert feed.raw_categories == ("/topic/news", "/region/eu")
    assert feed.tags == ("news", "eu")
