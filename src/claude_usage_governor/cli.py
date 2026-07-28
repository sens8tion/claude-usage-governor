"""Read Claude plan headroom, and brake on it.

Two jobs in one tool:

  reader   `claude-usage-governor`  /  `--json`  /  `--quiet`
           GET /api/oauth/usage and report how much of each rate-limit window
           is LEFT. This is the server's authoritative `utilization` -- NOT a
           token estimate reconstructed from local transcripts, which is what
           ccusage and similar tools do, and which drifts from the number
           that actually cuts you off.

  brake    `claude-usage-governor --hook`
           A Claude Code PreToolUse hook. Reads the hook payload on stdin,
           and throttles (or blocks) tool use when headroom drops under a
           threshold, so a long unattended run paces itself instead of
           slamming into the 5h wall. Fails OPEN: any error at all allows
           the tool through.

The 5h window SLIDES. Headroom returns continuously as old usage ages out;
it does not all come back at once at `resets_at`. So the brake never sleeps
until the reset time -- it re-polls (subject to its own cache) and releases
the moment utilization drops, which is usually well before the stated reset.

Stdlib only, no third-party dependencies. See README.md for the endpoint's
undocumented status and the risk that carries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
CRED_PATH = CLAUDE_HOME / ".credentials.json"

STATE_DIR = Path(os.environ.get("CLAUDE_USAGE_GOVERNOR_HOME", CLAUDE_HOME / "usage-governor"))
CACHE_PATH = STATE_DIR / "cache.json"
CONFIG_PATH = STATE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    # Below this much headroom on a watched window, throttle every tool.
    "hard_floor_pct": 5.0,
    # Below this much headroom, throttle only the fan-out tools listed below.
    "fanout_floor_pct": 25.0,
    "fanout_tools": ["Agent", "Task", "Workflow"],
    # Which limit kinds the brake watches. "session" is the 5h window.
    "watch_kinds": ["session", "weekly_all", "weekly_scoped"],
    # The brake must be cheap: a live call per tool use would be absurd, and
    # the usage endpoint is itself rate-limitable.
    "cache_seconds": 60,
    # "ask" bounces the decision to you, via the normal permission prompt --
    # recoverable. "deny" is a hard block with NO in-session override: a
    # misconfigured floor strands every tool call, including the one that
    # would fix the config. Only use "deny" if you have another way to edit
    # this file (e.g. a second terminal) when locked out.
    "decision": "ask",
}

WINDOW_TITLES = {
    "session": "Current session (5h)",
    "weekly_all": "Current week (all models)",
}

# Legacy top-level response shape, used only if `limits[]` is absent.
LEGACY_KEYS = {
    "five_hour": ("session", "Current session (5h)"),
    "seven_day": ("weekly_all", "Current week (all models)"),
    "seven_day_sonnet": ("weekly_scoped", "Current week (Sonnet only)"),
    "seven_day_opus": ("weekly_scoped", "Current week (Opus only)"),
}


class UsageError(RuntimeError):
    pass


# --------------------------------------------------------------------------- auth


def get_token() -> tuple[str, str]:
    """Return (token, source). Never refreshes: rotating the refresh token from
    outside the CLI would invalidate the CLI's own copy and log it out."""
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env, "CLAUDE_CODE_OAUTH_TOKEN"

    if not CRED_PATH.exists():
        raise UsageError("no token; run: claude setup-token")

    oauth = json.loads(CRED_PATH.read_text(encoding="utf-8"))["claudeAiOauth"]
    expires = datetime.fromtimestamp(oauth["expiresAt"] / 1000, timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise UsageError(
            f"stored CLI token expired {expires.astimezone():%Y-%m-%d %H:%M}; "
            "run: claude setup-token"
        )
    return oauth["accessToken"], str(CRED_PATH)


# --------------------------------------------------------------------------- fetch


def fetch_usage() -> dict[str, Any]:
    token, source = get_token()
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    req = urllib.request.Request(
        f"{base}/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": f"claude-usage-governor/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise UsageError(
                "401 from /api/oauth/usage; token expired or lacks scope. "
                "Run: claude setup-token"
            ) from exc
        raise UsageError(f"usage request failed: HTTP {exc.code}") from exc
    except OSError as exc:
        raise UsageError(f"usage request failed: {exc}") from exc

    return {"data": data, "token_source": source, "fetched_at": time.time()}


def get_usage(cache_seconds: int) -> dict[str, Any]:
    if cache_seconds > 0 and CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if time.time() - cached["fetched_at"] < cache_seconds:
                cached["from_cache"] = True
                return cached
        except (OSError, ValueError, KeyError):
            pass  # a corrupt cache is not a reason to fail

    fresh = fetch_usage()
    fresh["from_cache"] = False
    if cache_seconds > 0:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(fresh), encoding="utf-8")
        except OSError:
            pass  # cache is an optimisation, not a requirement
    return fresh


# --------------------------------------------------------------------------- parse


def parse_reset(value: Any) -> datetime | None:
    """resets_at is ISO-8601 in the current API; older shapes used epoch s/ms."""
    if value is None:
        return None
    if isinstance(value, str) and not value.isdigit():
        return datetime.fromisoformat(value)
    n = int(value)
    return datetime.fromtimestamp(n / 1000 if n > 1e12 else n, timezone.utc)


def window_title(limit: dict[str, Any]) -> str:
    kind = limit.get("kind")
    if kind in WINDOW_TITLES:
        return WINDOW_TITLES[kind]
    if kind == "weekly_scoped":
        scope = limit.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name")
        return f"Current week ({model} only)" if model else "Current week (scoped)"
    return str(kind)


def extract_windows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """`extra_usage` and `spend` are credit spend, NOT rate-limit headroom, and
    are deliberately excluded here -- callers must not treat them as fungible
    with plan headroom."""
    windows = []

    limits = data.get("limits")
    if limits:
        for lim in limits:
            if lim.get("percent") is None:
                continue
            pct = float(lim["percent"])
            windows.append(
                {
                    "kind": lim.get("kind"),
                    "group": lim.get("group"),
                    "title": window_title(lim),
                    "percent": pct,
                    "remaining": round(max(0.0, 100.0 - pct), 1),
                    "severity": lim.get("severity"),
                    "is_active": lim.get("is_active"),
                    "resets_at": lim.get("resets_at"),
                }
            )
        return windows

    for key, (kind, title) in LEGACY_KEYS.items():
        lim = data.get(key)
        if not isinstance(lim, dict) or lim.get("utilization") is None:
            continue
        pct = float(lim["utilization"])
        windows.append(
            {
                "kind": kind,
                "group": "session" if kind == "session" else "weekly",
                "title": title,
                "percent": pct,
                "remaining": round(max(0.0, 100.0 - pct), 1),
                "severity": None,
                "is_active": None,
                "resets_at": lim.get("resets_at"),
            }
        )
    return windows


def countdown(resets_at: Any) -> str | None:
    reset = parse_reset(resets_at)
    if reset is None:
        return None
    secs = (reset - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "due now"
    if secs >= 86400:
        return f"{int(secs // 86400)}d {int(secs % 86400 // 3600)}h"
    if secs >= 3600:
        return f"{int(secs // 3600)}h {int(secs % 3600 // 60)}m"
    return f"{int(secs // 60)}m"


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass  # a broken config must not brick the session
    return config


# --------------------------------------------------------------------------- report


def report(windows: list[dict[str, Any]], data: dict[str, Any], colour: bool) -> None:
    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if colour else text

    width = 28
    for w in windows:
        filled = round(w["percent"] / 100 * width)
        bar = "#" * filled + "." * (width - filled)
        code = "31" if w["remaining"] <= 10 else "33" if w["remaining"] <= 25 else "32"
        remaining_str = paint(f"{w['remaining']:5.1f}% left", code)
        print(paint(w["title"], "36"))
        print(f"  [{bar}] {remaining_str}  ({w['percent']:.1f}% used)")
        left = countdown(w["resets_at"])
        if left:
            reset = parse_reset(w["resets_at"]).astimezone()
            print(paint(f"  resets {reset:%a %H:%M} - in {left}", "90"))
        print()

    extra = data.get("extra_usage")
    if extra:
        state = "enabled" if extra.get("is_enabled") else f"disabled ({extra.get('disabled_reason')})"
        print(paint(
            f"Extra usage credits: {state} - {float(extra.get('utilization') or 0):.1f}% "
            f"of {extra.get('monthly_limit')} {extra.get('currency')} used", "90"))

    binding = min(windows, key=lambda w: w["remaining"])
    binding_left = countdown(binding["resets_at"])
    suffix = f", resets in {binding_left}" if binding_left else ""
    print(f"Binding limit: {binding['title']} - {binding['remaining']}% left{suffix}")


# --------------------------------------------------------------------------- brake


def run_hook(config: dict[str, Any]) -> int:
    """PreToolUse. Fails OPEN on absolutely everything -- a broken sensor or a
    bad config must never be the thing that strands a session."""
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0  # no payload we understand -> allow

    tool = payload.get("tool_name", "")

    try:
        usage = get_usage(int(config["cache_seconds"]))
        windows = extract_windows(usage["data"])
    except (UsageError, KeyError, ValueError, OSError):
        return 0  # cannot measure -> allow

    watched = [w for w in windows if w["kind"] in config["watch_kinds"]]
    if not watched:
        return 0

    binding = min(watched, key=lambda w: w["remaining"])
    remaining = binding["remaining"]
    hard = float(config["hard_floor_pct"])
    fanout = float(config["fanout_floor_pct"])
    is_fanout = tool in config["fanout_tools"]

    if remaining > hard and not (is_fanout and remaining <= fanout):
        return 0

    if remaining <= hard:
        reason = (
            f"Plan headroom is {remaining}% on {binding['title']} (floor {hard}%). "
            f"Stop starting new work. This window SLIDES -- headroom returns "
            f"continuously as older usage ages out, so re-check in a few minutes "
            f"rather than waiting for the reset. Report to the user and stop."
        )
    else:
        reason = (
            f"Plan headroom is {remaining}% on {binding['title']} (fan-out floor "
            f"{fanout}%). Do this work inline in a single agent instead of fanning "
            f"out to {tool}; parallel agents burn the 5h window fastest."
        )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": config["decision"],
            "permissionDecisionReason": reason,
        }
    }))
    return 0


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-usage-governor",
                                      description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="machine-readable report")
    mode.add_argument("--quiet", action="store_true", help="binding window only")
    mode.add_argument("--hook", action="store_true", help="run as a PreToolUse hook")
    parser.add_argument("--cache-seconds", type=int, default=None,
                        help="serve from cache if younger than this (0 = always live)")
    args = parser.parse_args(argv)

    config = load_config()
    if args.cache_seconds is not None:
        config["cache_seconds"] = args.cache_seconds

    if args.hook:
        return run_hook(config)

    cache = int(config["cache_seconds"]) if (args.json or args.quiet) else 0
    try:
        usage = get_usage(cache)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    windows = extract_windows(usage["data"])
    if not windows:
        print("error: no rate-limit windows returned; usage data is only "
              "available on subscription plans.", file=sys.stderr)
        return 1

    binding = min(windows, key=lambda w: w["remaining"])

    if args.json:
        for w in windows:
            w["resets_in"] = countdown(w["resets_at"])
        print(json.dumps({
            "token_source": usage["token_source"],
            "fetched_at": datetime.fromtimestamp(usage["fetched_at"], timezone.utc).isoformat(),
            "from_cache": usage["from_cache"],
            "windows": windows,
            "lowest_remaining": binding["remaining"],
            "binding_window": binding["title"],
            "binding_kind": binding["kind"],
        }, indent=2))
        return 0

    if args.quiet:
        left = countdown(binding["resets_at"])
        print(f"{binding['remaining']}% left ({binding['title']}"
              f"{f', resets in {left}' if left else ''})")
        return 0

    report(windows, usage["data"], colour=sys.stdout.isatty())
    return 0


if __name__ == "__main__":
    sys.exit(main())
