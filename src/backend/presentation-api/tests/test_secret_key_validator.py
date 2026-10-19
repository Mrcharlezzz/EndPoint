"""Direct tests for ``config.settings._validate_secret_key``.

These tests import the validator function and call it with synthetic
inputs. They never re-import the settings module, so they don't fight
the module-level call that runs at process boot.

Coverage target: 100% line + branch on ``_validate_secret_key``.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings import _validate_secret_key

# A 50-character clean value with no insecure markers.
CLEAN_50 = "a" * 25 + "B" * 25  # 50 chars, no markers
CLEAN_51 = CLEAN_50 + "c"  # 51 chars
CLEAN_49 = "a" * 49  # 49 chars, too short

# Each marker the impl rejects.
MARKERS = ("django-insecure", "change-me", "changeme", "default", "example")


# ---------------------------------------------------------------------------
# Boundary: length
# ---------------------------------------------------------------------------


def test_50_char_clean_passes(monkeypatch):
    # Pins the documented length floor — exactly 50 is allowed.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    assert _validate_secret_key(CLEAN_50) is None


def test_51_char_clean_passes(monkeypatch):
    # Pins that values longer than the floor are not rejected.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    assert _validate_secret_key(CLEAN_51) is None


def test_49_char_clean_rejected_with_length_reason(monkeypatch):
    # Pins the < 50 rejection path and the length count in the reason.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(CLEAN_49)
    msg = str(excinfo.value)
    assert "at least 50 characters" in msg
    assert "(got 49)" in msg


@pytest.mark.parametrize("length", [0, 1, 10, 25, 48, 49])
def test_length_under_floor_rejected(monkeypatch, length):
    # Sweep below the floor to ensure no off-by-one in the length check.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    value = "x" * length
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key(value)


# ---------------------------------------------------------------------------
# Boundary: empty / whitespace
# ---------------------------------------------------------------------------


def test_empty_string_rejected(monkeypatch):
    # Pins the "empty" branch — the length check is skipped here.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key("")
    assert "empty or whitespace-only" in str(excinfo.value)


@pytest.mark.parametrize("value", ["    ", "\t", "\n", "  \t \n ", " " * 100])
def test_whitespace_only_rejected(monkeypatch, value):
    # Pins that .strip() drives the empty-or-whitespace branch.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(value)
    assert "empty or whitespace-only" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Markers — case-insensitive substring match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", MARKERS)
def test_marker_at_start_rejected(monkeypatch, marker):
    # Pins each declared marker is rejected when it appears at the start.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    pad = "z" * (50 - len(marker))
    value = marker + pad
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(value)
    msg = str(excinfo.value)
    assert marker in msg
    assert "case-insensitive" in msg


@pytest.mark.parametrize("marker", MARKERS)
def test_marker_in_middle_rejected(monkeypatch, marker):
    # Pins that markers anywhere inside the value still trigger.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    pad_each = "z" * 30
    value = pad_each + marker + pad_each  # length comfortably over 50
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(value)
    assert marker in str(excinfo.value)


@pytest.mark.parametrize("marker", MARKERS)
def test_marker_at_end_rejected(monkeypatch, marker):
    # Pins that markers at the tail are caught.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    pad = "z" * (50 - len(marker))
    value = pad + marker
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key(value)


@pytest.mark.parametrize(
    "value,expected_marker",
    [
        ("DJANGO-INSECURE-" + "a" * 40, "django-insecure"),
        ("Change-Me-" + "a" * 41, "change-me"),
        ("DefaultPad" + "a" * 40, "default"),
        ("EXAMPLEpad" + "a" * 40, "example"),
        ("ChangeMe" + "a" * 42, "changeme"),
    ],
)
def test_marker_case_insensitive(monkeypatch, value, expected_marker):
    # Pins the .lower() comparison — uppercase / TitleCase markers must reject.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    assert len(value) >= 50
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(value)
    assert expected_marker in str(excinfo.value)


def test_marker_inside_50_char_value_rejected(monkeypatch):
    # Length passes, marker fails — confirms marker check runs even when length is OK.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    # exactly 50 chars containing 'default'
    value = "abcdefghij" + "default" + "k" * 33
    assert len(value) == 50
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(value)
    msg = str(excinfo.value)
    assert "default" in msg
    # Length should NOT be cited (length check passed).
    assert "at least 50 characters" not in msg


def test_short_value_with_marker_reports_both_reasons(monkeypatch):
    # Pins the order-of-operations: len check appends FIRST, then marker check.
    # The impl appends one length reason and (after break) one marker reason.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    value = "default-short"  # 13 chars, under 50, contains 'default'
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(value)
    msg = str(excinfo.value)
    assert "at least 50 characters" in msg
    assert "(got 13)" in msg
    assert "default" in msg
    # Joined with "; " per the impl.
    assert "; " in msg


def test_multiple_markers_only_first_reported(monkeypatch):
    # Pins the `break` after first marker hit — only one marker reason emitted.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    # 'django-insecure' is the first marker in the tuple; include 'example' too.
    value = "django-insecure-and-also-example-padding-zzzzzzzzzz"
    assert len(value) >= 50
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(value)
    msg = str(excinfo.value)
    # First marker iterated must be present.
    assert "django-insecure" in msg
    # 'example' should NOT be reported because of the break.
    assert "'example'" not in msg


# ---------------------------------------------------------------------------
# Bypass via ALLOW_INSECURE_SECRET_KEY=1
# ---------------------------------------------------------------------------


def test_bypass_one_allows_insecure(monkeypatch, capsys):
    # Pins the documented bypass: literal "1" allows + emits stderr WARNING.
    monkeypatch.setenv("ALLOW_INSECURE_SECRET_KEY", "1")
    assert _validate_secret_key("django-insecure-short") is None
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "bypass active" in captured.err


def test_bypass_zero_does_not_bypass(monkeypatch):
    # Pins that "0" is NOT a truthy bypass token.
    monkeypatch.setenv("ALLOW_INSECURE_SECRET_KEY", "0")
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key("django-insecure-x")


@pytest.mark.parametrize("value", ["true", "yes", "True", "TRUE", "y", "on"])
def test_only_literal_one_bypasses(monkeypatch, value):
    # Pins that ONLY the literal string "1" enables bypass — no boolean coercion.
    monkeypatch.setenv("ALLOW_INSECURE_SECRET_KEY", value)
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key("django-insecure-short")


def test_bypass_unset_rejects_insecure(monkeypatch):
    # Pins that without the bypass env var, insecure values are rejected.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key("change-me-short")


def test_bypass_one_with_clean_value_no_warning(monkeypatch, capsys):
    # Pins that the bypass only fires the warning path when reasons exist.
    monkeypatch.setenv("ALLOW_INSECURE_SECRET_KEY", "1")
    assert _validate_secret_key(CLEAN_50) is None
    captured = capsys.readouterr()
    assert captured.err == ""


# ---------------------------------------------------------------------------
# Leak prevention — value MUST NOT appear in error messages
# ---------------------------------------------------------------------------


def test_error_message_never_contains_secret_value(monkeypatch):
    # The most important test: no substring of the user's value leaks into the
    # raised message. This guards against incident: secrets ending up in logs
    # or shipped to error trackers via the ImproperlyConfigured traceback.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    sentinel_value = "SENTINEL_django-insecure_TAILSENTINEL"
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(sentinel_value)
    msg = str(excinfo.value)
    # No prefix or suffix of the value should appear.
    assert "SENTINEL" not in msg
    assert "TAILSENTINEL" not in msg
    # Even longer slices.
    assert sentinel_value[:8] not in msg
    assert sentinel_value[-8:] not in msg


def test_error_message_does_not_leak_value_for_short_input(monkeypatch):
    # Same leak-prevention guarantee for the length-rejection branch.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    sentinel = "TOPSEKRET_VALUE_THAT_MUST_NOT_LEAK"  # length 34
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(sentinel)
    msg = str(excinfo.value)
    assert "TOPSEKRET" not in msg
    assert sentinel not in msg


def test_warning_does_not_leak_value(monkeypatch, capsys):
    # The bypass path also must not echo the value.
    monkeypatch.setenv("ALLOW_INSECURE_SECRET_KEY", "1")
    sentinel = "BYPASS_LEAK_django-insecure_PROBE"
    _validate_secret_key(sentinel)
    captured = capsys.readouterr()
    assert "BYPASS_LEAK" not in captured.err
    assert "PROBE" not in captured.err


# ---------------------------------------------------------------------------
# Helpful error message — generation command must be present
# ---------------------------------------------------------------------------


def test_error_message_includes_generation_command(monkeypatch):
    # Pins the actionable hint: operator gets a copy-pasteable command.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key("")
    msg = str(excinfo.value)
    assert "import secrets" in msg
    assert "secrets.token_urlsafe(50)" in msg
    assert "PRESENTATION_API_SECRET_KEY" in msg


def test_error_message_mentions_allow_insecure_bypass(monkeypatch):
    # Pins discoverability of the bypass for CI / throwaway envs.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key("")
    assert "ALLOW_INSECURE_SECRET_KEY=1" in str(excinfo.value)
