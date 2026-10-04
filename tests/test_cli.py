"""Unit tests for the pure logic: parsing, window extraction, and the brake's
decision rules. No network access -- get_usage is monkeypatched everywhere it
would otherwise hit the live endpoint."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from claude_usage_governor import cli


# --------------------------------------------------------------------------- parse_reset


def test_parse_reset_none():
    assert cli.parse_reset(None) is None


def test_parse_reset_iso_string():
    dt = cli.parse_reset("2026-07-28T05:59:59.628457+00:00")
    assert dt == datetime(2026, 7, 28, 5, 59, 59, 628457, tzinfo=timezone.utc)


def test_parse_reset_epoch_seconds():
    dt = cli.parse_reset(1700000000)
    assert dt.tzinfo is not None
    assert dt == datetime.fromtimestamp(1700000000, timezone.utc)


def test_parse_reset_epoch_millis():
    dt = cli.parse_reset(1700000000000)
    assert dt == datetime.fromtimestamp(1700000000, timezone.utc)


# --------------------------------------------------------------------------- countdown


def test_countdown_none():
    assert cli.countdown(None) is None


def test_countdown_due_now():
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert cli.countdown(past) == "due now"


def test_countdown_minutes():
    soon = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
    assert cli.countdown(soon) in ("44m", "45m")


def test_countdown_hours():
    later = (datetime.now(timezone.utc) + timedelta(hours=4, minutes=59)).isoformat()
    result = cli.countdown(later)
    assert result.endswith("m") and "h" in result


# --------------------------------------------------------------------------- window_title


def test_window_title_known_kinds():
    assert cli.window_title({"kind": "session"}) == "Current session (5h)"
    assert cli.window_title({"kind": "weekly_all"}) == "Current week (all models)"


def test_window_title_scoped_with_model():
    limit = {"kind": "weekly_scoped", "scope": {"model": {"display_name": "Opus"}}}
    assert cli.window_title(limit) == "Current week (Opus only)"


def test_window_title_scoped_without_model():
    assert cli.window_title({"kind": "weekly_scoped", "scope": None}) == "Current week (scoped)"


def test_window_title_unknown_kind():
    assert cli.window_title({"kind": "mystery"}) == "mystery"


# --------------------------------------------------------------------------- extract_windows


def test_extract_windows_prefers_limits_array():
    data = {
        "limits": [
            {"kind": "session", "group": "session", "percent": 16, "severity": "normal",
             "resets_at": "2026-07-28T05:59:59+00:00", "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": 84, "severity": "warning",
             "resets_at": "2026-07-28T02:59:59+00:00", "is_active": False},
        ],
        # legacy keys present too -- must be ignored once `limits` exists
        "five_hour": {"utilization": 999, "resets_at": None},
    }
    windows = cli.extract_windows(data)
    assert len(windows) == 2
    session = next(w for w in windows if w["kind"] == "session")
    assert session["percent"] == 16
    assert session["remaining"] == 84.0
    assert session["title"] == "Current session (5h)"


def test_extract_windows_skips_null_percent():
    data = {"limits": [{"kind": "session", "percent": None}]}
    assert cli.extract_windows(data) == []


def test_extract_windows_falls_back_to_legacy_shape():
    data = {
        "five_hour": {"utilization": 16.0, "resets_at": "2026-07-28T05:59:59+00:00"},
        "seven_day": {"utilization": 40.0, "resets_at": "2026-07-28T02:59:59+00:00"},
        "seven_day_opus": None,
    }
    windows = cli.extract_windows(data)
    kinds = {w["kind"] for w in windows}
    assert kinds == {"session", "weekly_all"}
    session = next(w for w in windows if w["kind"] == "session")
    assert session["remaining"] == 84.0


def test_extract_windows_excludes_credit_spend():
    """extra_usage / spend are money, not rate-limit headroom -- must never
    surface as a window the brake or reader could mistake for one."""
    data = {
        "limits": [{"kind": "session", "percent": 10}],
        "extra_usage": {"utilization": 0.0, "is_enabled": False},
        "spend": {"percent": 0},
    }
    windows = cli.extract_windows(data)
    assert all(w["kind"] != "extra_usage" and w["kind"] != "spend" for w in windows)


def test_extract_windows_empty_when_no_data():
    assert cli.extract_windows({}) == []


# --------------------------------------------------------------------------- extract_credits


def _credit(**over) -> dict:
    base = {"utilization": 0.0, "limit_dollars": 250, "used_dollars": 0.0,
            "remaining_dollars": 250.0, "locked_reason": None,
            "resets_at": (datetime.now(timezone.utc) + timedelta(days=25)).isoformat()}
    base.update(over)
    return base


def test_extract_credits_finds_dollar_buckets_only():
    data = {
        "some_codename": _credit(),
        # plan windows carry null dollars -- not credits
        "five_hour": {"utilization": 12.0, "limit_dollars": None, "resets_at": None},
        "limits": [{"kind": "session", "percent": 12}],
        "extra_usage": {"is_enabled": False, "monthly_limit": 2000},
    }
    credits = cli.extract_credits(data)
    assert [c["key"] for c in credits] == ["some_codename"]
    c = credits[0]
    assert c["remaining_dollars"] == 250.0 and c["percent"] == 0.0
    assert c["usable"] is True and c["advice"] == cli.CREDIT_ADVICE
    assert c["spend_per_day_to_use_up"] == pytest.approx(10.0, abs=0.1)  # 250 over 25 days


def test_extract_credits_uses_configured_title():
    data = {"some_codename": _credit()}
    assert cli.extract_credits(data)[0]["title"] == "Credit (some_codename)"
    titled = cli.extract_credits(data, {"some_codename": "Cloud credit"})
    assert titled[0]["title"] == "Cloud credit"


def test_extract_credits_spent_or_locked_is_not_usable():
    spent = cli.extract_credits({"k": _credit(used_dollars=250.0, remaining_dollars=0.0)})[0]
    assert spent["usable"] is False and spent["advice"] is None
    assert spent["spend_per_day_to_use_up"] is None
    locked = cli.extract_credits({"k": _credit(locked_reason="expired")})[0]
    assert locked["usable"] is False


def test_extract_credits_never_become_windows():
    """Credit is not plan headroom -- the brake must not see it as a window."""
    data = {"some_codename": _credit(), "limits": [{"kind": "session", "percent": 10}]}
    assert [w["kind"] for w in cli.extract_windows(data)] == ["session"]


def test_extract_credits_tolerates_malformed_values():
    data = {"bad": _credit(limit_dollars="lots"), "odd_reset": _credit(resets_at="not a date")}
    credits = cli.extract_credits(data)
    assert [c["key"] for c in credits] == ["odd_reset"]
    assert credits[0]["spend_per_day_to_use_up"] is None


def test_hook_mentions_unused_credit_when_throttling(monkeypatch, capsys):
    payload = _usage_payload(97.0)  # 3% left -> throttled
    payload["data"]["some_codename"] = _credit()
    monkeypatch.setattr(cli, "get_usage", lambda cache_seconds, **kw: payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    config = dict(cli.DEFAULT_CONFIG, credit_titles={"some_codename": "Cloud credit"})
    cli.run_hook(config)
    reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Cloud credit: $250 of $250 unused" in reason
    assert "without their approval" in reason


def test_hook_stays_silent_about_credit_when_not_throttling(monkeypatch, capsys):
    payload = _usage_payload(10.0)
    payload["data"]["some_codename"] = _credit()
    monkeypatch.setattr(cli, "get_usage", lambda cache_seconds, **kw: payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    assert cli.run_hook(dict(cli.DEFAULT_CONFIG)) == 0
    assert capsys.readouterr().out == ""  # credit never causes a throttle by itself


# --------------------------------------------------------------------------- load_config


def test_load_config_defaults_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "missing.json")
    config = cli.load_config()
    assert config == cli.DEFAULT_CONFIG


def test_load_config_merges_overrides(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hard_floor_pct": 1.0}), encoding="utf-8")
    monkeypatch.setattr(cli, "CONFIG_PATH", path)
    config = cli.load_config()
    assert config["hard_floor_pct"] == 1.0
    assert config["fanout_floor_pct"] == cli.DEFAULT_CONFIG["fanout_floor_pct"]


def test_load_config_corrupt_file_falls_back(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cli, "CONFIG_PATH", path)
    assert cli.load_config() == cli.DEFAULT_CONFIG


# --------------------------------------------------------------------------- run_hook (the brake)


def _usage_payload(percent: float) -> dict:
    return {"data": {"limits": [{"kind": "session", "percent": percent,
                                  "resets_at": None}]},
            "token_source": "test", "fetched_at": 0,
            # from_cache=True -- these tests exercise the throttle decision,
            # not history recording (see the dedicated rate-history tests),
            # so skip the real filesystem write run_hook does on a fresh fetch.
            "from_cache": True}


def _invoke_hook(monkeypatch, capsys, tool_name: str, percent: float, config: dict):
    monkeypatch.setattr(cli, "get_usage", lambda cache_seconds, **kw: _usage_payload(percent))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": tool_name})))
    rc = cli.run_hook(config)
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def test_hook_allows_when_headroom_healthy(monkeypatch, capsys):
    config = dict(cli.DEFAULT_CONFIG)
    rc, output = _invoke_hook(monkeypatch, capsys, "Bash", percent=10.0, config=config)
    assert rc == 0
    assert output is None  # no output at all = silent allow


def test_hook_throttles_fanout_tool_below_fanout_floor(monkeypatch, capsys):
    config = dict(cli.DEFAULT_CONFIG)
    rc, output = _invoke_hook(monkeypatch, capsys, "Agent", percent=80.0, config=config)  # 20% left
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "Agent" in output["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_allows_non_fanout_tool_in_fanout_band(monkeypatch, capsys):
    config = dict(cli.DEFAULT_CONFIG)
    rc, output = _invoke_hook(monkeypatch, capsys, "Bash", percent=80.0, config=config)  # 20% left
    assert output is None


def test_hook_throttles_everything_below_hard_floor(monkeypatch, capsys):
    config = dict(cli.DEFAULT_CONFIG)
    rc, output = _invoke_hook(monkeypatch, capsys, "Bash", percent=97.0, config=config)  # 3% left
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "Stop starting new work" in output["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_respects_configured_decision(monkeypatch, capsys):
    config = dict(cli.DEFAULT_CONFIG)
    config["decision"] = "deny"
    rc, output = _invoke_hook(monkeypatch, capsys, "Bash", percent=97.0, config=config)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_fails_open_on_bad_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    rc = cli.run_hook(dict(cli.DEFAULT_CONFIG))
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_hook_fails_open_when_usage_unavailable(monkeypatch, capsys):
    def boom(cache_seconds, **kw):
        raise cli.UsageError("no token")

    monkeypatch.setattr(cli, "get_usage", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    rc = cli.run_hook(dict(cli.DEFAULT_CONFIG))
    assert rc == 0
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- get_usage (cache, stale fallback, backoff)


def _fresh_reading(percent: float = 10.0) -> dict:
    return {"data": {"limits": [{"kind": "session", "percent": percent, "resets_at": None}]},
            "token_source": "test", "fetched_at": cli.time.time()}


@pytest.fixture
def state(monkeypatch, tmp_path):
    """Point the cache and failure marker at a temp dir, and count fetches."""
    monkeypatch.setattr(cli, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(cli, "FAILURE_PATH", tmp_path / "fetch-failure.json")
    calls = {"n": 0, "raises": None}

    def fake_fetch():
        calls["n"] += 1
        if calls["raises"] is not None:
            raise calls["raises"]
        return _fresh_reading()

    monkeypatch.setattr(cli, "fetch_usage", fake_fetch)
    return calls


def _seed_cache(age_seconds: float, percent: float = 42.0) -> None:
    reading = _fresh_reading(percent)
    reading["fetched_at"] -= age_seconds
    cli.CACHE_PATH.write_text(json.dumps(reading), encoding="utf-8")


def test_get_usage_fetches_and_caches(state):
    usage = cli.get_usage(60)
    assert usage["from_cache"] is False and usage["stale"] is False
    again = cli.get_usage(60)
    assert again["from_cache"] is True and again["stale"] is False
    assert state["n"] == 1


def test_get_usage_refetches_once_cache_expires(state):
    _seed_cache(age_seconds=200)
    usage = cli.get_usage(60)
    assert usage["from_cache"] is False
    assert state["n"] == 1


def test_get_usage_serves_stale_cache_when_fetch_fails(state):
    _seed_cache(age_seconds=200, percent=42.0)
    state["raises"] = cli.UsageError("usage request failed: HTTP 429")
    usage = cli.get_usage(60, max_stale_seconds=600)
    assert usage["stale"] is True and usage["from_cache"] is True
    assert usage["fetch_error"] == "usage request failed: HTTP 429"
    assert usage["data"]["limits"][0]["percent"] == 42.0


def test_get_usage_raises_when_cache_too_old_to_stand_in(state):
    _seed_cache(age_seconds=5000)
    state["raises"] = cli.UsageError("usage request failed: HTTP 429")
    with pytest.raises(cli.UsageError, match="429"):
        cli.get_usage(60, max_stale_seconds=600)


def test_get_usage_raises_when_fetch_fails_and_no_cache(state):
    state["raises"] = cli.UsageError("usage request failed: HTTP 429")
    with pytest.raises(cli.UsageError, match="429"):
        cli.get_usage(60)


def test_get_usage_backs_off_after_a_failure(state):
    _seed_cache(age_seconds=200)
    state["raises"] = cli.UsageError("usage request failed: HTTP 429")
    cli.get_usage(60, backoff_seconds=90)
    cli.get_usage(60, backoff_seconds=90)
    cli.get_usage(60, backoff_seconds=90)
    assert state["n"] == 1  # the failure was remembered; no retry storm


def test_get_usage_retries_once_backoff_elapses(state):
    _seed_cache(age_seconds=200)
    cli.FAILURE_PATH.write_text(
        json.dumps({"failed_at": cli.time.time() - 500, "error": "old"}), encoding="utf-8")
    usage = cli.get_usage(60, backoff_seconds=90)
    assert usage["from_cache"] is False
    assert state["n"] == 1
    assert not cli.FAILURE_PATH.exists()  # a success clears the marker


def test_get_usage_cache_zero_ignores_backoff_but_still_falls_back(state):
    _seed_cache(age_seconds=30)
    state["raises"] = cli.UsageError("usage request failed: HTTP 429")
    cli.get_usage(10, backoff_seconds=90)  # 30s-old cache is past 10s -> fails, marks
    usage = cli.get_usage(0, backoff_seconds=90)     # always-live: tries again anyway
    assert state["n"] == 2
    assert usage["stale"] is True


def test_get_usage_auth_error_is_never_masked_by_cache(state):
    _seed_cache(age_seconds=200)
    state["raises"] = cli.AuthError("stored CLI token expired")
    with pytest.raises(cli.AuthError):
        cli.get_usage(60)
    assert not cli.FAILURE_PATH.exists()  # and it does not start a backoff


def test_get_usage_corrupt_cache_is_ignored(state):
    cli.CACHE_PATH.write_text("{not json", encoding="utf-8")
    usage = cli.get_usage(60)
    assert usage["from_cache"] is False


def test_hook_still_brakes_on_a_stale_reading(state, monkeypatch, capsys):
    """A 429 must not blind the brake while a recent reading exists."""
    monkeypatch.setattr(cli, "HISTORY_PATH", cli.CACHE_PATH.parent / "history.jsonl")
    _seed_cache(age_seconds=200, percent=97.0)  # 3% left
    state["raises"] = cli.UsageError("usage request failed: HTTP 429")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    rc = cli.run_hook(dict(cli.DEFAULT_CONFIG))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert not (cli.CACHE_PATH.parent / "history.jsonl").exists()  # stale != a new sample


# --------------------------------------------------------------------------- rate history


def test_load_history_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "HISTORY_PATH", tmp_path / "missing.jsonl")
    assert cli._load_history() == []


def test_load_history_skips_corrupt_lines(monkeypatch, tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"ts": 1, "windows": {"session": 10}}\nnot json\n', encoding="utf-8")
    monkeypatch.setattr(cli, "HISTORY_PATH", path)
    history = cli._load_history()
    assert len(history) == 1
    assert history[0]["windows"]["session"] == 10


def test_record_history_appends_and_prunes(monkeypatch, tmp_path):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(cli, "HISTORY_PATH", path)

    old_sample = [{"kind": "session", "percent": 5.0}]
    cli.record_history(old_sample, retention_hours=1.0, ts=1000.0)

    # Well past the 1h retention window relative to the next sample.
    new_sample = [{"kind": "session", "percent": 8.0}]
    cli.record_history(new_sample, retention_hours=1.0, ts=1000.0 + 3 * 3600)

    history = cli._load_history()
    assert len(history) == 1  # the old sample was pruned
    assert history[0]["windows"]["session"] == 8.0


def test_record_history_ignores_windows_without_kind(monkeypatch, tmp_path):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(cli, "HISTORY_PATH", path)
    cli.record_history([{"kind": None, "percent": 5.0}], retention_hours=1.0, ts=1000.0)
    history = cli._load_history()
    assert history[0]["windows"] == {}


def test_record_history_write_failure_does_not_raise(monkeypatch, tmp_path):
    # HISTORY_PATH's parent is a file, not a directory -- mkdir must fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "HISTORY_PATH", blocker / "sub" / "history.jsonl")
    cli.record_history([{"kind": "session", "percent": 5.0}], retention_hours=1.0, ts=1000.0)  # no raise


# --------------------------------------------------------------------------- compute_rate


def test_compute_rate_needs_two_points():
    history = [{"ts": 1000.0, "windows": {"session": 10.0}}]
    assert cli.compute_rate(history, "session", now_ts=1000.0, lookback_hours=1.0) is None


def test_compute_rate_simple_linear_increase():
    # +10% over 1 hour -> 10%/hour.
    history = [
        {"ts": 0.0, "windows": {"session": 10.0}},
        {"ts": 3600.0, "windows": {"session": 20.0}},
    ]
    rate = cli.compute_rate(history, "session", now_ts=3600.0, lookback_hours=2.0)
    assert rate == pytest.approx(10.0, abs=0.01)


def test_compute_rate_declining():
    history = [
        {"ts": 0.0, "windows": {"session": 30.0}},
        {"ts": 3600.0, "windows": {"session": 20.0}},
    ]
    rate = cli.compute_rate(history, "session", now_ts=3600.0, lookback_hours=2.0)
    assert rate == pytest.approx(-10.0, abs=0.01)


def test_compute_rate_ignores_samples_outside_lookback():
    history = [
        {"ts": -100000.0, "windows": {"session": 999.0}},  # far outside lookback
        {"ts": 0.0, "windows": {"session": 10.0}},
        {"ts": 3600.0, "windows": {"session": 20.0}},
    ]
    rate = cli.compute_rate(history, "session", now_ts=3600.0, lookback_hours=2.0)
    assert rate == pytest.approx(10.0, abs=0.01)


def test_compute_rate_ignores_other_window_kinds():
    history = [
        {"ts": 0.0, "windows": {"weekly_all": 50.0}},
        {"ts": 3600.0, "windows": {"weekly_all": 60.0}},
    ]
    assert cli.compute_rate(history, "session", now_ts=3600.0, lookback_hours=2.0) is None


def test_compute_rate_all_same_timestamp_returns_none():
    history = [
        {"ts": 100.0, "windows": {"session": 10.0}},
        {"ts": 100.0, "windows": {"session": 20.0}},
    ]
    assert cli.compute_rate(history, "session", now_ts=100.0, lookback_hours=1.0) is None


# --------------------------------------------------------------------------- project


def test_project_unknown_kind_returns_none():
    assert cli.project("mystery", 10.0, rate_pct_per_hour=1.0, multiplier_cap=4.0) is None


def test_project_no_rate_returns_none():
    assert cli.project("session", 10.0, rate_pct_per_hour=None, multiplier_cap=4.0) is None


def test_project_flat_rate_never_hits_limit():
    result = cli.project("session", current_percent=42.0, rate_pct_per_hour=0.0, multiplier_cap=4.0)
    assert result["will_hit_limit_at_current_rate"] is False
    assert result["projected_steady_state_pct"] == 42.0
    assert result["fanout_multiplier"] == 4.0  # capped, since rate isn't > 0


def test_project_declining_rate_never_hits_limit():
    result = cli.project("session", current_percent=42.0, rate_pct_per_hour=-2.0, multiplier_cap=4.0)
    assert result["will_hit_limit_at_current_rate"] is False


def test_project_sustainable_rate_stays_under_limit():
    # Session window: sustainable rate is 100/5 = 20%/h. At exactly that
    # rate, steady state is exactly 100 -- the boundary counts as "will hit".
    result = cli.project("session", current_percent=16.0, rate_pct_per_hour=10.0, multiplier_cap=4.0)
    assert result["sustainable_rate_pct_per_hour"] == pytest.approx(20.0)
    assert result["projected_steady_state_pct"] == pytest.approx(50.0)  # 10 * 5h
    assert result["will_hit_limit_at_current_rate"] is False
    assert result["fanout_multiplier"] == pytest.approx(2.0)  # 20/10


def test_project_unsustainable_rate_will_hit_limit():
    result = cli.project("session", current_percent=16.0, rate_pct_per_hour=30.0, multiplier_cap=4.0)
    assert result["projected_steady_state_pct"] == 100.0  # capped, 30*5=150 -> 100
    assert result["will_hit_limit_at_current_rate"] is True
    assert result["fanout_multiplier"] < 1.0  # 20/30


def test_project_multiplier_is_capped():
    # A tiny positive rate would imply a huge multiplier without the cap.
    result = cli.project("session", current_percent=1.0, rate_pct_per_hour=0.01, multiplier_cap=4.0)
    assert result["fanout_multiplier"] == 4.0


# --------------------------------------------------------------------------- enrich_with_projections


def test_enrich_with_projections_adds_projection_key(monkeypatch, tmp_path):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(cli, "HISTORY_PATH", path)
    cli.record_history([{"kind": "session", "percent": 10.0}], retention_hours=1.0, ts=0.0)
    cli.record_history([{"kind": "session", "percent": 20.0}], retention_hours=1.0, ts=3600.0)

    windows = [{"kind": "session", "percent": 20.0}]
    config = dict(cli.DEFAULT_CONFIG)
    cli.enrich_with_projections(windows, config, now_ts=3600.0)

    assert windows[0]["projection"] is not None
    assert windows[0]["projection"]["rate_pct_per_hour"] == pytest.approx(10.0, abs=0.01)


def test_enrich_with_projections_none_when_kind_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "HISTORY_PATH", tmp_path / "history.jsonl")
    windows = [{"kind": "mystery", "percent": 20.0}]
    cli.enrich_with_projections(windows, dict(cli.DEFAULT_CONFIG), now_ts=0.0)
    assert windows[0]["projection"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
