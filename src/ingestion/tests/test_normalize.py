"""Unit tests for the strip_html() function in ingestion.normalize."""

from __future__ import annotations

import pytest
from ingestion.normalize import strip_html

# --- Style and script block removal ---


def test_strips_style_blocks() -> None:
    assert strip_html("<style>.c0{max-width:100%}</style>Hello") == "Hello"


def test_strips_script_blocks() -> None:
    assert strip_html("<script>alert('x')</script>Content") == "Content"


def test_strips_style_with_attributes() -> None:
    assert strip_html('<style type="text/css">body{color:red}</style>Text') == "Text"


def test_strips_style_mixed_case() -> None:
    assert strip_html("<STYLE>h1{font-size:2em}</STYLE>Title") == "Title"


def test_strips_script_mixed_case() -> None:
    assert strip_html("<Script>var x=1;</Script>Done") == "Done"


def test_strips_nested_style_blocks() -> None:
    # Non-greedy .*? matches up to the first </style>, leaving "outer</style>".
    # _HTML_TAG_RE then strips the dangling </style> tag.
    result = strip_html("<style><style>inner</style>outer</style>After")
    assert result == "outerAfter"


def test_strips_style_block_leaving_surrounding_text() -> None:
    result = strip_html("Before<style>.cls{}</style>After")
    assert result == "BeforeAfter"


# --- HTML entity decoding ---


def test_decodes_amp_entity() -> None:
    assert strip_html("Digital Systems &amp; Computer Architecture") == (
        "Digital Systems & Computer Architecture"
    )


def test_decodes_nbsp_entity() -> None:
    assert strip_html("Module&nbsp;Overview") == "Module Overview"


def test_decodes_apos_numeric_entity() -> None:
    assert strip_html("it&#39;s here") == "it's here"


def test_decodes_numeric_hex_entity() -> None:
    assert strip_html("&#x26;") == "&"


def test_decodes_lt_gt_entities() -> None:
    assert strip_html("&lt;tag&gt;") == "<tag>"


# --- Plain HTML tag removal ---


def test_strips_basic_html_tags() -> None:
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strips_div_em_tags() -> None:
    assert strip_html("<div>Hello <em>world</em></div>") == "Hello world"


def test_strips_anchor_tag() -> None:
    result = strip_html('<a href="http://example.com">Click here</a>')
    assert result == "Click here"


# --- Whitespace normalisation ---


def test_crlf_normalised_to_lf() -> None:
    result = strip_html("line1\r\nline2")
    assert result == "line1\nline2"


def test_cr_normalised_to_lf() -> None:
    result = strip_html("line1\rline2")
    assert result == "line1\nline2"


def test_leading_trailing_whitespace_stripped() -> None:
    assert strip_html("  spaces   everywhere  \r\n  here  ") == "spaces everywhere\nhere"


def test_repeated_spaces_collapsed() -> None:
    assert strip_html("one   two\tthree") == "one two three"


def test_non_breaking_space_replaced() -> None:
    # U+00A0 non-breaking space should become a regular space
    assert strip_html("word\xa0word") == "word word"


# --- Edge cases ---


def test_empty_string() -> None:
    assert strip_html("") == ""


def test_plain_text_unchanged() -> None:
    assert strip_html("plain text no html") == "plain text no html"


def test_whitespace_only_returns_empty() -> None:
    assert strip_html("   \t\n  ") == ""


def test_none_equivalent_empty_style() -> None:
    assert strip_html("<style></style>") == ""


# --- Parametrised acceptance criteria ---


@pytest.mark.parametrize(
    ("html_input", "expected"),
    [
        ("<style>.c0{max-width:100%}</style>Hello", "Hello"),
        ("<script>alert('x')</script>Content", "Content"),
        ("Digital Systems &amp; Computer Architecture", "Digital Systems & Computer Architecture"),
        ("Module&nbsp;Overview", "Module Overview"),
        ("<p>Hello <b>world</b></p>", "Hello world"),
        ("", ""),
        ("plain text no html", "plain text no html"),
    ],
)
def test_acceptance_criteria(html_input: str, expected: str) -> None:
    assert strip_html(html_input) == expected


# --- decode_entities tests ---


def test_decode_entities_amp() -> None:
    from ingestion.normalize import decode_entities

    assert decode_entities("Digital Systems &amp; Architecture") == "Digital Systems & Architecture"


def test_decode_entities_plain_text() -> None:
    from ingestion.normalize import decode_entities

    assert decode_entities("No entities here") == "No entities here"


def test_decode_entities_empty() -> None:
    from ingestion.normalize import decode_entities

    assert decode_entities("") == ""


def test_decode_entities_nbsp() -> None:
    from ingestion.normalize import decode_entities

    # html.unescape converts &nbsp; to \xa0 (non-breaking space), not a regular space
    assert decode_entities("Module&nbsp;Overview") == "Module\xa0Overview"


def test_decode_entities_apos() -> None:
    from ingestion.normalize import decode_entities

    assert decode_entities("it&#39;s here") == "it's here"


def test_decode_entities_numeric_hex() -> None:
    from ingestion.normalize import decode_entities

    assert decode_entities("&#x26;") == "&"


def test_decode_entities_lt_gt() -> None:
    from ingestion.normalize import decode_entities

    assert decode_entities("&lt;tag&gt;") == "<tag>"


def test_decode_entities_whitespace_stripped() -> None:
    from ingestion.normalize import decode_entities

    assert decode_entities("  padded  ") == "padded"


def test_decode_entities_preserves_internal_whitespace() -> None:
    from ingestion.normalize import decode_entities

    # Unlike strip_html, decode_entities does NOT normalise internal whitespace
    assert decode_entities("multiple   spaces") == "multiple   spaces"


def test_decode_entities_example_from_issue() -> None:
    from ingestion.normalize import decode_entities

    assert (
        decode_entities("S26_CH-234-B_Digital Systems &amp; Computer Architecture Tutorial")
        == "S26_CH-234-B_Digital Systems & Computer Architecture Tutorial"
    )
