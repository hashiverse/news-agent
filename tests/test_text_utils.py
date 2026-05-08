"""Tests for news_agent.text_utils — strip_html and first_sentence."""

from __future__ import annotations

from news_agent.text_utils import first_sentence, strip_html


# ---------------------------------------------------------------------------
# strip_html


def test_strip_html_returns_empty_for_empty_input():
    assert strip_html("") == ""


def test_strip_html_passes_plain_text_through():
    assert strip_html("hello world") == "hello world"


def test_strip_html_collapses_whitespace_in_plain_text():
    assert strip_html("hello   \n\t world") == "hello world"


def test_strip_html_removes_tags_and_decodes_entities():
    assert strip_html("<p>Cats &amp; Dogs</p>") == "Cats & Dogs"


def test_strip_html_collapses_whitespace_between_blocks():
    assert (
        strip_html("<p>First.</p>\n<p>Second &amp; sentence.</p>")
        == "First. Second & sentence."
    )


def test_strip_html_handles_self_closing_tags():
    assert strip_html("first<br/>second<br>third") == "first second third"


# ---------------------------------------------------------------------------
# first_sentence


def test_first_sentence_returns_empty_for_empty_input():
    assert first_sentence("") == ""
    assert first_sentence("   ") == ""


def test_first_sentence_returns_full_text_when_no_terminator():
    assert first_sentence("hello world") == "hello world"


def test_first_sentence_splits_on_period_followed_by_space():
    assert first_sentence("Hello world. Goodbye.") == "Hello world."


def test_first_sentence_includes_the_terminator():
    assert first_sentence("Yes! Done.") == "Yes!"
    assert first_sentence("What? OK.") == "What?"


def test_first_sentence_does_not_split_on_decimal_or_url():
    """The lookahead requires whitespace after the punctuation, so `$3.14`
    and `e.g.something.com` don't split early."""
    assert first_sentence("The price is $3.14 today.") == "The price is $3.14 today."
    assert first_sentence("Visit example.com today.") == "Visit example.com today."


def test_first_sentence_handles_terminator_at_end_of_string():
    assert first_sentence("Single sentence.") == "Single sentence."


def test_first_sentence_strips_leading_trailing_whitespace():
    assert first_sentence("   Hello world. Goodbye.   ") == "Hello world."


def test_first_sentence_splits_on_newline_after_punctuation():
    assert first_sentence("Hello.\nWorld.") == "Hello."


# ---------------------------------------------------------------------------
# strip_hashtags


def _import_strip_hashtags():
    from news_agent.text_utils import strip_hashtags
    return strip_hashtags


def test_strip_hashtags_passes_text_with_no_hashes_through():
    strip_hashtags = _import_strip_hashtags()
    assert strip_hashtags("plain text only") == "plain text only"


def test_strip_hashtags_removes_single_hashtag_token():
    strip_hashtags = _import_strip_hashtags()
    assert strip_hashtags("hello #rust world") == "hello world"


def test_strip_hashtags_removes_multiple_consecutive_hashtags():
    strip_hashtags = _import_strip_hashtags()
    assert (
        strip_hashtags("intro #tesla #robotaxi #FSD outro")
        == "intro outro"
    )


def test_strip_hashtags_at_start_and_end_of_string():
    strip_hashtags = _import_strip_hashtags()
    assert strip_hashtags("#open hello #close") == "hello"


def test_strip_hashtags_leaves_lone_hash_alone():
    strip_hashtags = _import_strip_hashtags()
    # `#` not followed by a word char isn't a hashtag token — leave it.
    assert strip_hashtags("price # 5") == "price # 5"


def test_strip_hashtags_supports_unicode_word_characters():
    strip_hashtags = _import_strip_hashtags()
    assert strip_hashtags("hi #日本語 bye") == "hi bye"


def test_strip_hashtags_removes_url_fragments():
    """Acceptable side-effect: the `#section` part of a URL gets stripped.
    Documented so it doesn't surprise anyone — URLs aren't typically
    substring-matched against keywords."""
    strip_hashtags = _import_strip_hashtags()
    assert (
        strip_hashtags("see https://example.com/page#section for details")
        == "see https://example.com/page for details"
    )


def test_strip_hashtags_collapses_whitespace_after_removal():
    strip_hashtags = _import_strip_hashtags()
    assert strip_hashtags("a   #tag1   #tag2   b") == "a b"
