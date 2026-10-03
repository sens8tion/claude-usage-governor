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

Every fresh (non-cached) fetch is also appended to a small local history file,
which lets the reader project a RATE (%/hour of quota) and answer "at this
rate of token use, will I ever hit the limit" -- see `project()` below. That
question only has a sane answer because the windows are SLIDING, not
fixed-and-reset: if a constant rate is sustained forever, utilization
asymptotically approaches `rate * window_length_hours`, capped at 100%. Below
`100 / window_length_hours` (the sustainable rate), you converge under the
cap and never hit it, however long you keep going; above it, you trend
toward the cap regardless of how much headroom you have right now. That
steady-state framing is the correct one here -- "hours until reset" is not,
because headroom keeps returning throughout, continuously, as old usage ages
out from under the window.

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
HISTORY_PATH = STATE_DIR / "history.jsonl"
FAILURE_PATH = STATE_DIR / "fetch-failure.json"

# Fallbacks for get_usage() callers that don't pass the config values through.
MAX_STALE_SECONDS = 600
FAILURE_BACKOFF_SECONDS = 90

DEFAULT_CONFIG: dict[str, Any] = {
    # Below this much headroom on a watched window, throttle every tool.
    "hard_floor_pct": 5.0,
    # Below this much headroom, throttle only the fan-out tools listed below.
    "fanout_floor_pct": 25.0,
    "fanout_tools": ["Agent", "Task", "Workflow"],
    # Which limit kinds the brake watches. "session" is the 5h window.
    "watch_kinds": ["session", "weekly_all", "weekly_scoped"],
    # The brake must be cheap: a live call per tool use would be absurd, and
    # the usage endpoint is itself rate-limited -- observed to 429 fetches
    # spaced ~60s apart while tolerating ~120s. The cache is shared by every
    # caller (hook, reader, anything polling --json), so this is the minimum
    # spacing between live fetches across all of them.
    "cache_seconds": 150,
    # When a live fetch fails (429, network), serve the last good reading
    # instead, as long as it is younger than this. A reading a few minutes
    # old is a far better sensor than none at all.
    "max_stale_seconds": MAX_STALE_SECONDS,
    # After a failed fetch, no caller tries again for this long -- retrying
    # on every tool call only deepens a rate limit.
    "failure_backoff_seconds": FAILURE_BACKOFF_SECONDS,
    # "ask" bounces the decision to you, via the normal permission prompt --
    # recoverable. "deny" is a hard block with NO in-session override: a
    # misconfigured floor strands every tool call, including the one that
    # would fix the config. Only use "deny" if you have another way to edit
    # this file (e.g. a second terminal) when locked out.
    "decision": "ask",
    # The rate trend is measured over the trailing (window_length * this
    # fraction) hours -- e.g. 0.25 means the last 75 minutes for the 5h
    # session window, the last ~42 hours for the 7-day weekly window. Short
    # enough to reflect a recent change in pace, long enough to not be one
    # or two noisy samples.
    "lookback_fraction": 0.25,
    # How long raw samples are kept before being pruned from history.jsonl.
    # Must exceed the longest lookback actually used (168h * 0.25 = 42h), so
    # the default has headroom for a larger lookback_fraction too.
    "history_retention_hours": 48.0,
    # A cap on the reported fan-out multiplier. Without one, a very low or
    # negative measured rate (still-thin history, or a genuinely idle
    # stretch) would project an enormous or infinite multiplier -- true in
    # the math, misleading in practice, since a fan-out burst can spike well
    # above any trailing average before the next sample catches it.
    "fanout_multiplier_cap": 4.0,
}

WINDOW_TITLES = {
    "session": "Current session (5h)",
    "weekly_all": "Current week (all models)",
}

# Fixed window lengths the API's `kind` values correspond to. Used to derive
# the sustainable rate (100% / length) and the steady-state projection.
WINDOW_LENGTH_HOURS = {
    "session": 5.0,
    "weekly_all": 24.0 * 7,
    "weekly_scoped": 24.0 * 7,
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


class AuthError(UsageError):
    """The token itself is the problem. Waiting will not fix it, so this is
    never papered over with a stale reading or a backoff -- it surfaces at
    once."""


# --------------------------------------------------------------------------- auth


def get_token() -> tuple[str, str]:
    """Return (token, source). Never refreshes: rotating the refresh token from
    outside the CLI would invalidate the CLI's own copy and log it out."""
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env, "CLAUDE_CODE_OAUTH_TOKEN"

    if not CRED_PATH.exists():
        raise AuthError("no token; run: claude setup-token")

    oauth = json.loads(CRED_PATH.read_text(encoding="utf-8"))["claudeAiOauth"]
    expires = datetime.fromtimestamp(oauth["expiresAt"] / 1000, timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise AuthError(
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
        if exc.code in (401, 403):
            raise AuthError(
                f"{exc.code} from /api/oauth/usage; token expired or lacks scope. "
                "Run: claude setup-token"
            ) from exc
        raise UsageError(f"usage request failed: HTTP {exc.code}") from exc
    except OSError as exc:
        raise UsageError(f"usage request failed: {exc}") from exc

    return {"data": data, "token_source": source, "fetched_at": time.time()}


def _read_cache() -> dict[str, Any] | None:
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        float(cached["fetched_at"])
        cached["data"]
        return cached
    except (OSError, ValueError, KeyError, TypeError):
        return None  # a missing or corrupt cache is not a reason to fail


def _write_state(path: Path, obj: dict[str, Any]) -> None:
    """Atomic (temp + replace): several processes share these files, and a
    reader must never see a half-written one. Best-effort -- the cache is an
    optimisation, not a requirement."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(obj), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _recent_failure(backoff_seconds: float) -> str | None:
    """The error from the last failed fetch, if it is recent enough that no
    caller should be retrying yet."""
    try:
        failure = json.loads(FAILURE_PATH.read_text(encoding="utf-8"))
        if 0 <= time.time() - float(failure["failed_at"]) < backoff_seconds:
            return str(failure["error"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def get_usage(cache_seconds: int, max_stale_seconds: float = MAX_STALE_SECONDS,
              backoff_seconds: float = FAILURE_BACKOFF_SECONDS) -> dict[str, Any]:
    """Cached reading if younger than `cache_seconds`, else a live fetch.

    A failed fetch (429, network) is remembered for `backoff_seconds`, during
    which no caller retries, and is answered with the last good reading as
    long as that is younger than `max_stale_seconds` -- marked `stale`, with
    the reason in `fetch_error`. Only when there is nothing recent enough to
    stand in does the failure reach the caller. An AuthError always does.

    `cache_seconds` of 0 means "always try live": it skips the fresh-cache
    shortcut and the backoff, but still falls back to a stale reading."""
    cached = _read_cache()
    age = time.time() - cached["fetched_at"] if cached else None

    if cached and cache_seconds > 0 and age < cache_seconds:
        cached.update(from_cache=True, stale=False)
        return cached

    error = _recent_failure(backoff_seconds) if cache_seconds > 0 else None
    if error is None:
        try:
            fresh = fetch_usage()
        except AuthError:
            raise
        except UsageError as exc:
            error = str(exc)
            _write_state(FAILURE_PATH, {"failed_at": time.time(), "error": error})
        else:
            fresh.update(from_cache=False, stale=False)
            _write_state(CACHE_PATH, fresh)
            try:
                FAILURE_PATH.unlink()
            except OSError:
                pass
            return fresh

    if cached and age < max_stale_seconds:
        cached.update(from_cache=True, stale=True, fetch_error=error)
        return cached
    raise UsageError(error)


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


# --------------------------------------------------------------------------- rate history


def _load_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    out = []
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a corrupt line must not lose every other line
    return out


def record_history(windows: list[dict[str, Any]], retention_hours: float, ts: float) -> None:
    """Append one sample (percent per watched window kind, at `ts`) and prune
    anything older than `retention_hours`. History is advisory -- a write
    failure here must never break the primary read or hook path."""
    if not windows:
        return
    sample = {"ts": ts, "windows": {w["kind"]: w["percent"] for w in windows if w.get("kind")}}
    try:
        cutoff = ts - retention_hours * 3600
        history = [h for h in _load_history() if h.get("ts", 0) >= cutoff]
        history.append(sample)
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(
            "\n".join(json.dumps(h) for h in history) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def compute_rate(history: list[dict[str, Any]], kind: str, now_ts: float,
                  lookback_hours: float) -> float | None:
    """Least-squares slope of percent-vs-time for `kind`, in percent per
    hour, using samples within `lookback_hours` of `now_ts`. None if there
    are fewer than two samples, or they're all at the same instant -- not
    enough spread to measure a trend rather than a coincidence."""
    cutoff = now_ts - lookback_hours * 3600
    points = [
        (h["ts"], h["windows"][kind])
        for h in history
        if isinstance(h.get("windows"), dict)
        and h["windows"].get(kind) is not None
        and h.get("ts", 0) >= cutoff
    ]
    if len(points) < 2:
        return None

    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_p = sum(p for _, p in points) / n
    denom = sum((t - mean_t) ** 2 for t, _ in points)
    if denom == 0:
        return None
    numer = sum((t - mean_t) * (p - mean_p) for t, p in points)
    return (numer / denom) * 3600.0  # percent per second -> percent per hour


def project(kind: str, current_percent: float, rate_pct_per_hour: float | None,
            multiplier_cap: float) -> dict[str, Any] | None:
    """Steady-state projection for a SLIDING window: if `rate_pct_per_hour`
    were sustained indefinitely, utilization asymptotically approaches
    `rate * window_length_hours` (capped at 100). That -- not "hours until
    reset" -- is the question that actually has a stable answer for a window
    where headroom keeps returning continuously. None if the window's length
    isn't known, or there's not yet enough history to measure a rate."""
    length = WINDOW_LENGTH_HOURS.get(kind)
    if length is None or rate_pct_per_hour is None:
        return None

    sustainable = 100.0 / length

    if rate_pct_per_hour <= 0:
        # Flat or declining: utilization will not exceed where it is now.
        return {
            "rate_pct_per_hour": round(rate_pct_per_hour, 3),
            "sustainable_rate_pct_per_hour": round(sustainable, 3),
            "projected_steady_state_pct": round(current_percent, 1),
            "will_hit_limit_at_current_rate": False,
            "fanout_multiplier": multiplier_cap,
        }

    steady_state = min(100.0, rate_pct_per_hour * length)
    multiplier = min(multiplier_cap, sustainable / rate_pct_per_hour)
    return {
        "rate_pct_per_hour": round(rate_pct_per_hour, 3),
        "sustainable_rate_pct_per_hour": round(sustainable, 3),
        "projected_steady_state_pct": round(steady_state, 1),
        "will_hit_limit_at_current_rate": steady_state >= 100.0,
        "fanout_multiplier": round(multiplier, 2),
    }


def enrich_with_projections(windows: list[dict[str, Any]], config: dict[str, Any],
                             now_ts: float) -> None:
    """Mutates each window dict in place, adding a `projection` key (a dict,
    or None when there isn't yet enough history for that window's kind)."""
    history = _load_history()
    fraction = float(config["lookback_fraction"])
    cap = float(config["fanout_multiplier_cap"])
    for w in windows:
        kind = w.get("kind")
        length = WINDOW_LENGTH_HOURS.get(kind)
        rate = None
        if length is not None:
            lookback = max(0.05, length * fraction)
            rate = compute_rate(history, kind, now_ts, lookback)
        w["projection"] = project(kind, w["percent"], rate, cap)


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

        proj = w.get("projection")
        if proj is None:
            print(paint("  rate: not enough history yet", "90"))
        elif proj["rate_pct_per_hour"] <= 0:
            print(paint(
                f"  rate: {proj['rate_pct_per_hour']:+.2f}%/h (flat or declining) "
                f"- fan-out up to {proj['fanout_multiplier']:.1f}x", "90"))
        else:
            hit = "WILL trend toward the limit" if proj["will_hit_limit_at_current_rate"] \
                else "will stay under the limit"
            print(paint(
                f"  rate: {proj['rate_pct_per_hour']:.2f}%/h of {proj['sustainable_rate_pct_per_hour']:.2f}%/h "
                f"sustainable - at this rate you {hit} (~{proj['projected_steady_state_pct']:.0f}% "
                f"steady-state) - fan-out up to {proj['fanout_multiplier']:.1f}x", "90"))
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
        usage = get_usage(int(config["cache_seconds"]),
                          max_stale_seconds=float(config["max_stale_seconds"]),
                          backoff_seconds=float(config["failure_backoff_seconds"]))
        windows = extract_windows(usage["data"])
        if not usage["from_cache"]:
            # Keep collecting rate history even when only the hook runs, so
            # the reader's --json projection has data without needing to be
            # invoked separately. The hook's own throttle decision below
            # stays purely floor-based -- see README, "Decision: ask vs deny".
            record_history(windows, float(config["history_retention_hours"]), usage["fetched_at"])
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
        usage = get_usage(cache,
                          max_stale_seconds=float(config["max_stale_seconds"]),
                          backoff_seconds=float(config["failure_backoff_seconds"]))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    stale = bool(usage.get("stale"))
    if stale and not args.json:
        print(f"note: live fetch failed ({usage.get('fetch_error')}); showing the "
              f"reading from {int(time.time() - usage['fetched_at'])}s ago",
              file=sys.stderr)

    windows = extract_windows(usage["data"])
    if not windows:
        print("error: no rate-limit windows returned; usage data is only "
              "available on subscription plans.", file=sys.stderr)
        return 1

    if not usage["from_cache"]:
        record_history(windows, float(config["history_retention_hours"]), usage["fetched_at"])
    enrich_with_projections(windows, config, time.time())

    binding = min(windows, key=lambda w: w["remaining"])

    if args.json:
        for w in windows:
            w["resets_in"] = countdown(w["resets_at"])
        print(json.dumps({
            "token_source": usage["token_source"],
            "fetched_at": datetime.fromtimestamp(usage["fetched_at"], timezone.utc).isoformat(),
            "from_cache": usage["from_cache"],
            "stale": stale,
            "fetch_error": usage.get("fetch_error") if stale else None,
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
