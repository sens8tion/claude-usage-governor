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
            "token_source": "test", "fetched_at": 0}


def _invoke_hook(monkeypatch, capsys, tool_name: str, percent: float, config: dict):
    monkeypatch.setattr(cli, "get_usage", lambda cache_seconds: _usage_payload(percent))
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
    def boom(cache_seconds):
        raise cli.UsageError("no token")

    monkeypatch.setattr(cli, "get_usage", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    rc = cli.run_hook(dict(cli.DEFAULT_CONFIG))
    assert rc == 0
    assert capsys.readouterr().out == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
