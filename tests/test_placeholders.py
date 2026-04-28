"""Tests for the placeholder substitution helper."""

from audit_pipeline.utils import render_placeholders


def test_substitutes_known_placeholders():
    out = render_placeholders(
        "Repo: {ENGINE_REPO_URL} @ {ENGINE_SHA}",
        ENGINE_REPO_URL="https://github.com/x/y",
        ENGINE_SHA="abc123",
    )
    assert out == "Repo: https://github.com/x/y @ abc123"


def test_leaves_unknown_placeholders_alone():
    out = render_placeholders(
        "Known: {ENGINE_SHA}, Unknown: {NOT_PROVIDED}",
        ENGINE_SHA="abc",
    )
    assert out == "Known: abc, Unknown: {NOT_PROVIDED}"


def test_ignores_lowercase_braces():
    """Anything that isn't ALL_CAPS shouldn't be touched (e.g. code, JSON)."""
    out = render_placeholders(
        "Code: {let x = 1;} JSON: {'key': 'val'} Math: {n}",
        X="should-not-substitute",
    )
    assert out == "Code: {let x = 1;} JSON: {'key': 'val'} Math: {n}"


def test_handles_digits_and_underscores():
    out = render_placeholders(
        "Versions: {API_V2_URL} and {LIST_RELEVANT_CONSTANTS_2}",
        API_V2_URL="https://api/v2",
        LIST_RELEVANT_CONSTANTS_2="MAX = 1e16",
    )
    assert out == "Versions: https://api/v2 and MAX = 1e16"


def test_no_substitutions_returns_template_unchanged():
    template = "Hello {WORLD}, this is {FOO}."
    out = render_placeholders(template)
    assert out == template


def test_empty_template():
    assert render_placeholders("") == ""


def test_repeated_placeholder():
    out = render_placeholders(
        "{KEY} and {KEY} and {KEY}",
        KEY="x",
    )
    assert out == "x and x and x"


def test_substitution_with_special_chars_in_value():
    """Values containing braces should be inserted literally, not re-substituted."""
    out = render_placeholders(
        "Path: {LOCAL_ENGINE_PATH}",
        LOCAL_ENGINE_PATH="/tmp/foo/{bar}/baz",
    )
    assert out == "Path: /tmp/foo/{bar}/baz"
