"""Edge + production suite for ``_validate_secret_key``.

This file lives outside the default ``test_*.py`` collection pattern
(see pyproject.toml). It is run by passing the path explicitly to
pytest, which is exactly how the verification pipeline invokes it.

Each test gets a one-line comment naming the edge it pins.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings import _validate_secret_key

# Hypothesis is brought in via ``uv run --with hypothesis``; gate the import
# so the file still imports under bare environments (the property tests are
# then skipped instead of erroring at collection time).
hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings as hyp_settings, strategies as st  # noqa: E402

MARKERS = ("django-insecure", "change-me", "changeme", "default", "example")


def _has_marker(value: str) -> bool:
    lower = value.lower()
    return any(m in lower for m in MARKERS)


# ---------------------------------------------------------------------------
# Items 1-12 from the impl agent + production checklist
# ---------------------------------------------------------------------------


def test_unicode_50_codepoints_passes(monkeypatch):
    # EDGE 1: len() counts code points, not bytes — 50 CJK chars (~150 bytes UTF-8)
    # is treated as 50 chars by the validator. Pin this so we don't accidentally
    # tighten the length check to bytes later and break international operators.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    cjk_50 = "中" * 50
    assert len(cjk_50) == 50
    assert len(cjk_50.encode("utf-8")) == 150
    assert _validate_secret_key(cjk_50) is None


def test_unicode_49_codepoints_rejected(monkeypatch):
    # EDGE 1b: 49 unicode chars — even if 147 bytes — still rejected because we
    # measure code points not bytes. Documents the chosen semantics.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key("中" * 49)
    assert "(got 49)" in str(excinfo.value)


def test_validator_runs_after_load_dotenv():
    # EDGE 2: assert (via static-source guard) that load_dotenv() executes BEFORE
    # _validate_secret_key(SECRET_KEY) is called at module load. We do NOT
    # re-import settings — we just read the source file and check ordering.
    settings_path = Path(__file__).resolve().parents[2] / "config" / "settings.py"
    src = settings_path.read_text()
    load_idx = src.find("load_dotenv()")
    secret_assign_idx = src.find('SECRET_KEY = os.environ.get("SECRET_KEY"')
    validate_call_idx = src.find("_validate_secret_key(SECRET_KEY)")
    assert load_idx != -1, "load_dotenv() call missing"
    assert secret_assign_idx != -1, "SECRET_KEY assignment missing"
    assert validate_call_idx != -1, "validator invocation missing"
    assert load_idx < secret_assign_idx < validate_call_idx


def test_uppercase_default_substring_rejected(monkeypatch):
    # EDGE 3: "DEFAULT" baked into a 50-char value — caught by .lower() compare.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    value = "Padding" + "DEFAULT" + "padding" + "z" * 29  # length 50
    assert len(value) == 50
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(value)
    assert "default" in str(excinfo.value)


def test_bypass_emits_exactly_one_warning(monkeypatch, capsys):
    # EDGE 4: a single bypassed call produces a single stderr WARNING line.
    monkeypatch.setenv("ALLOW_INSECURE_SECRET_KEY", "1")
    _validate_secret_key("django-insecure-short")
    captured = capsys.readouterr()
    assert captured.err.count("WARNING") == 1


def test_validator_no_module_level_caching(monkeypatch):
    # EDGE 5: calling the function twice with the same insecure value raises both
    # times — there is no idempotent short-circuit / module-level memoization.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key("change-me-short")
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key("change-me-short")


def test_raises_django_improperly_configured(monkeypatch):
    # EDGE 6: the exception is the documented Django type, so middleware /
    # WSGI / ASGI loaders surface it as a config error rather than HTTP 500.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key("")
    assert isinstance(excinfo.value, ImproperlyConfigured)
    # And it inherits from Exception (not BaseException-only), so `except` works.
    assert isinstance(excinfo.value, Exception)


def test_validator_accepts_typeerror_on_non_string(monkeypatch):
    # EDGE 7: defensive guard — passing a non-string blows up loudly rather
    # than getting silently converted. Pin: validator does NOT catch unrelated
    # exceptions (e.g., AttributeError from .strip on an int).
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(AttributeError):
        _validate_secret_key(123)  # type: ignore[arg-type]


def test_validator_passes_through_shell_metachars(monkeypatch):
    # EDGE 8: shell meta-chars are treated as literal characters; the validator
    # only cares about length + markers. No shell interpretation, no escaping.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    value = "$VAR`whoami`;rm -rf / && echo pwned-padding-zzzzzzzz"  # length > 50
    assert len(value) >= 50
    assert not _has_marker(value)
    assert _validate_secret_key(value) is None


@hyp_settings(max_examples=50, deadline=200)
@given(st.text(min_size=50, max_size=200))
def test_property_clean_long_strings_pass(value):
    # EDGE 9a: any random string of length >= 50 with no marker substring
    # is accepted. Hypothesis generates a wide variety; we filter markers
    # via assume() so the test doesn't degenerate into trivial input.
    hypothesis.assume(not _has_marker(value))
    hypothesis.assume(value.strip())  # non-whitespace
    # We don't touch ALLOW_INSECURE_SECRET_KEY here because the property test
    # uses Hypothesis fixtures — relying on whatever the parent shell has set.
    # Either way, clean values must pass regardless of bypass state.
    assert _validate_secret_key(value) is None


@hyp_settings(max_examples=50, deadline=200)
@given(
    base=st.text(min_size=20, max_size=80),
    marker=st.sampled_from(MARKERS),
    tail=st.text(min_size=20, max_size=80),
)
def test_property_marker_strings_rejected(base, marker, tail):
    # EDGE 9b: any string containing a marker is rejected (regardless of what
    # else it contains). Length is comfortably above the floor by construction.
    import os

    # Ensure bypass is off for this property — Hypothesis tests run outside
    # function-scoped fixtures, so we touch os.environ directly and restore.
    prior = os.environ.pop("ALLOW_INSECURE_SECRET_KEY", None)
    try:
        value = base + marker + tail
        with pytest.raises(ImproperlyConfigured):
            _validate_secret_key(value)
    finally:
        if prior is not None:
            os.environ["ALLOW_INSECURE_SECRET_KEY"] = prior


def test_error_message_no_value_leak_double_check(monkeypatch):
    # EDGE 10: production accident — operator pastes secret into bug report;
    # validator's error message must not echo any portion of the value.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    secret = "PROD_LEAK_SENTINEL_django-insecure_TAILMARKER_PROD"
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(secret)
    msg = str(excinfo.value)
    # No prefix/suffix slices, no full value.
    assert secret not in msg
    assert "PROD_LEAK_SENTINEL" not in msg
    assert "TAILMARKER_PROD" not in msg


def test_migration_error_is_actionable(monkeypatch):
    # EDGE 11: an existing operator running an upgrade with their old insecure
    # value should get a copy-pasteable command + the env var name to set.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key("django-insecure-old-default-value")
    msg = str(excinfo.value)
    # Diagnosis.
    assert "Insecure SECRET_KEY rejected:" in msg
    # Generation.
    assert 'python -c "import secrets; print(secrets.token_urlsafe(50))"' in msg
    # Where to put it.
    assert "PRESENTATION_API_SECRET_KEY" in msg
    assert ".env" in msg
    # Bypass instructions for CI.
    assert "ALLOW_INSECURE_SECRET_KEY=1" in msg


def test_pytest_django_real_secret_key_loads(monkeypatch):
    # EDGE 12: simulate the pytest-django happy path — operator provides a real
    # SECRET_KEY via env, the validator is happy, settings can keep loading.
    real_key = "x" * 60
    monkeypatch.setenv("SECRET_KEY", real_key)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    # The function should accept it without raising.
    assert _validate_secret_key(real_key) is None


# ---------------------------------------------------------------------------
# Brainstorm — additional production / edge cases (>=6)
# ---------------------------------------------------------------------------


def test_settings_module_already_loaded_does_not_reinvoke_validator():
    # BRAINSTORM 1: settings module is imported once per process. Our test file
    # already triggered the module-level _validate_secret_key call when it
    # imported config.settings at the top. Pin that re-importing config.settings
    # does NOT re-run the validator (Python caches it in sys.modules).
    import sys

    assert "config.settings" in sys.modules
    import config.settings as s1  # noqa: PLC0415
    import config.settings as s2  # noqa: PLC0415

    assert s1 is s2  # same module object; no re-execution


def test_conftest_session_secret_key_no_collision(monkeypatch):
    # BRAINSTORM 2: the parent conftest does NOT auto-set SECRET_KEY for the
    # session, so our monkeypatch.delenv on ALLOW_INSECURE_SECRET_KEY won't
    # collide with any session fixture. Pin it explicitly so a future
    # conftest change that sets ALLOW_INSECURE_SECRET_KEY=1 globally would
    # break this guard rather than silently masking failures.
    import os

    # If a parent fixture sets the bypass, this assertion should fire so a
    # human investigates rather than letting tests silently pass via bypass.
    if os.environ.get("ALLOW_INSECURE_SECRET_KEY") == "1":
        pytest.fail(
            "ALLOW_INSECURE_SECRET_KEY=1 leaked from parent shell/fixture; "
            "tests in this file would silently pass via bypass."
        )
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key("default-pad")


def test_validator_is_threadsafe_concurrent_calls(monkeypatch):
    # BRAINSTORM 3: Django apps may load settings concurrently in some servers
    # (e.g., gunicorn workers). Validator has no shared mutable state — so
    # concurrent calls with the same input must be deterministic.
    import threading

    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    errors: list[BaseException] = []

    def call() -> None:
        try:
            _validate_secret_key("change-me-x")
        except ImproperlyConfigured:
            pass
        except BaseException as exc:  # pragma: no cover - safety net
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_validator_rejects_bytes_input(monkeypatch):
    # BRAINSTORM 4: bytes vs str — Django's SECRET_KEY is documented as str.
    # Bytes input goes through .strip() (which works on bytes), but len() works
    # too, then `value.lower()` returns bytes — and `marker in lowered` is
    # str-in-bytes which is a TypeError. We pin the actual behaviour so a
    # later change to coerce bytes is intentional.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises((TypeError, ImproperlyConfigured)):
        _validate_secret_key(b"x" * 60)  # type: ignore[arg-type]


def test_random_secure_key_containing_marker_substring_is_rejected(monkeypatch):
    # BRAINSTORM 5: known limitation — a legitimately-random URL-safe token can
    # by chance contain a 7-char marker like 'default'. We DOCUMENT this as a
    # known false-positive: rerun the generation command to get a fresh value.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    # Synthesise a "random-looking" key that happens to contain 'default'.
    random_with_default = "q7Xb9_wzKvL3default-rsT2pYn4vM6oUe1Hg5JfDc8aBz2QwR"
    assert len(random_with_default) >= 50
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key(random_with_default)
    # Document the false-positive in the message itself: operator rotates.
    assert "default" in str(excinfo.value)


def test_error_message_does_not_double_join_separator(monkeypatch):
    # BRAINSTORM 6: the impl uses "; ".join(reasons). With ONE reason there is
    # no separator; with TWO reasons exactly one "; " between them. Pin to
    # protect against a refactor that double-joins or trails a "; ".
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        _validate_secret_key("default-13ch")  # length 12 -> two reasons
    msg = str(excinfo.value)
    # Trim before the "Generate" tail so we only inspect the reason section.
    head = msg.split(". Generate", 1)[0]
    # head looks like "Insecure SECRET_KEY rejected: <r1>; <r2>"
    reason_section = head.split(": ", 1)[1]
    parts = reason_section.split("; ")
    assert len(parts) == 2
    assert all(parts), "no empty reason fragment"


def test_unset_bypass_via_delenv_truly_rejects(monkeypatch):
    # BRAINSTORM 7: belt-and-suspenders — if some upstream test left the env
    # var set, monkeypatch.delenv must truly remove it for our tests.
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    import os

    assert "ALLOW_INSECURE_SECRET_KEY" not in os.environ
    with pytest.raises(ImproperlyConfigured):
        _validate_secret_key("change-me")


def test_validator_accepts_high_entropy_token_urlsafe(monkeypatch):
    # BRAINSTORM 8: smoke-check the recommended generation command actually
    # produces values the validator accepts (very low chance of marker hit).
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    import secrets

    for _ in range(10):
        candidate = secrets.token_urlsafe(50)
        if _has_marker(candidate):
            # Statistically near-zero; if it ever fires, regenerate.
            continue
        assert _validate_secret_key(candidate) is None
        break
    else:  # pragma: no cover - astronomically unlikely
        pytest.fail("token_urlsafe(50) produced 10 marker-containing values")
