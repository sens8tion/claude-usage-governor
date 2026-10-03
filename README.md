# claude-usage-governor

Read how much of your Claude plan is left, and throttle Claude Code before it
hits the wall.

`claude` (the Claude Code CLI) has no `usage` subcommand — `/usage` is an
interactive panel only, with no scriptable equivalent. This tool does what
that panel does programmatically: it reads the server's authoritative
headroom for the current 5-hour session window and the 7-day weekly window,
and (optionally) enforces a floor on it via a Claude Code hook.

This is **not** a token-count estimator reconstructed from local transcript
files, which is what tools like `ccusage` do. It calls the same endpoint the
CLI itself calls, and reports the same `utilization` number `/usage` shows
you — the one that actually determines when you get cut off.

## Give this to a Claude Code session

Paste this verbatim into any session — new or already mid-task, doesn't
matter — to have it install this tool and self-pace against your plan limit
for the rest of that session:

> Clone https://github.com/sens8tion/claude-usage-governor, set it up per
> `CLAUDE.md`, and follow `docs/operating-directive.md` from here on.

Say just "clone this: `<url>`" and a session may treat it as reference
material rather than something to act on — the phrasing above forces both
the install step and the standing self-pacing behavior; the first alone
(`CLAUDE.md`) sets the tool up but doesn't change how the session behaves
afterward.

## The two jobs

**Reader** — `claude-usage-governor`, `--json`, or `--quiet`. Prints
remaining % per rate-limit window, which window is currently binding (has the
least headroom), and when each resets.

**Brake** — `claude-usage-governor --hook`. A Claude Code `PreToolUse` hook.
Throttles tool calls once headroom drops below a configurable floor, so a
long unattended session paces itself instead of running straight into the
wall. See [Wiring in the hook](#wiring-in-the-hook) below.

Both read the same `~/.claude/.credentials.json` the `claude` CLI itself
uses, so no separate login is required.

## Install

```bash
git clone https://github.com/sens8tion/claude-usage-governor
cd claude-usage-governor
pip install -e .
```

Or run it without installing:

```bash
python -m claude_usage_governor.cli --json
```

## Usage

```bash
claude-usage-governor                # human-readable report, colour bars
claude-usage-governor --json         # machine-readable, for scripts/hooks
claude-usage-governor --quiet        # one line: the binding window only
claude-usage-governor --cache-seconds 60   # serve from cache if fresh enough
```

Example output:

```
Current session (5h)
  [####........................]  84.0% left  (16.0% used)
  resets Tue 06:59 - in 4h 59m
  rate: 3.20%/h of 20.00%/h sustainable - at this rate you will stay under the limit (~16% steady-state) - fan-out up to 4.0x

Current week (all models)
  [####........................]  84.0% left  (16.0% used)
  resets Tue 03:59 - in 60m
  rate: not enough history yet

Binding limit: Current week (all models) - 84% left
```

If you've never run `/usage` won't show anything either — usage data is only
available on subscription plans, not API-key/console billing.

## Rate projection: "at this rate, will I ever hit the limit?"

Every fresh fetch is appended to a small local history file
(`~/.claude/usage-governor/history.jsonl`: just `{timestamp, percent}` per
window, nothing else). Once there are a couple of samples spread over time,
each window in `--json` output gets a `projection`:

```json
{
  "rate_pct_per_hour": 3.2,
  "sustainable_rate_pct_per_hour": 20.0,
  "projected_steady_state_pct": 16.0,
  "will_hit_limit_at_current_rate": false,
  "fanout_multiplier": 4.0
}
```

The windows are **sliding**, not fixed-and-reset (see above), which is what
makes this projection well-defined: if a rate is sustained forever, headroom
converges toward a steady state — `rate * window_length_hours`, capped at
100% — instead of ever cleanly "resetting." Below `100 / window_length_hours`
(the sustainable rate: 20%/h for the 5h session window, ~0.6%/h for the 7-day
weekly window), you converge under the cap and genuinely never hit it, no
matter how long you keep going. Above it, you trend toward the cap regardless
of how much headroom you currently have. `fanout_multiplier` is
`sustainable_rate / current_rate`, capped at `fanout_multiplier_cap` (default
4x) — roughly, "you could sustain up to this much more throughput and still
never trend toward the wall."

Caveats, stated plainly: this is a trailing average, not a crystal ball. A
sudden fan-out burst can spike well above it before the next sample catches
up. `projection` is `null` until there are at least two samples in the
lookback window (`lookback_fraction * window_length`, default the trailing
25%) — nothing is fabricated from a single reading. Treat it as "safe to
widen your default," never as a substitute for the hard-floor brake, which
stays purely reactive and unaffected by any of this (see
[Wiring in the hook](#wiring-in-the-hook)).

## Wiring in the hook

Add to `~/.claude/settings.json` (see
[`examples/settings.snippet.json`](examples/settings.snippet.json)):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [{ "type": "command", "command": "claude-usage-governor --hook" }]
      }
    ]
  }
}
```

The hook is cheap by design — it caches the usage fetch (150s by default) so
it doesn't hit the network on every single tool call, and it **fails open**:
any error at all (network failure, expired token, malformed config) allows
the tool through. A broken sensor must never be the thing that strands a
session.

### The usage endpoint is itself rate-limited

`/api/oauth/usage` answers `429` when polled too often — observed: fetches
~60s apart are mostly refused, ~120s apart are fine. The cache file is shared
by every caller (the hook in every session, the reader, anything polling
`--json`), so `cache_seconds` is effectively the minimum spacing between live
fetches across all of them. Two further rules keep a refusal from mattering:

- **Stale fallback** — when a live fetch fails (429, network), the last good
  reading is served instead as long as it is younger than
  `max_stale_seconds`. `--json` marks it `"stale": true` with the reason in
  `"fetch_error"`. The brake keeps braking on it rather than going blind.
- **Backoff** — a failed fetch is remembered for `failure_backoff_seconds`,
  and no caller retries during that time.

An auth failure (no token, expired token, 401/403) is never masked by either:
it surfaces immediately, since waiting will not fix it.

### Decision: `ask` vs `deny`

The default is `"decision": "ask"` — headroom below the floor routes the tool
call through Claude Code's normal permission prompt, which you can approve
manually if you want to proceed anyway. This is deliberate: `"deny"` is a
**hard block with no in-session override**. During development of this tool,
testing with `"deny"` and a misconfigured floor stranded every single tool
call — including the one that would have fixed the config — until the config
file was deleted from outside the session. `"ask"` doesn't have that failure
mode. Only switch to `"deny"` if you've confirmed you have another way to
edit the config when locked out (a second terminal, another machine).

### Config

`~/.claude/usage-governor/config.json` (all keys optional, shown with
defaults):

```json
{
  "hard_floor_pct": 5.0,
  "fanout_floor_pct": 25.0,
  "fanout_tools": ["Agent", "Task", "Workflow"],
  "watch_kinds": ["session", "weekly_all", "weekly_scoped"],
  "cache_seconds": 150,
  "max_stale_seconds": 600,
  "failure_backoff_seconds": 90,
  "decision": "ask",
  "lookback_fraction": 0.25,
  "history_retention_hours": 48.0,
  "fanout_multiplier_cap": 4.0
}
```

- **`hard_floor_pct`** — below this much headroom on any watched window, the
  hook throttles *every* tool call.
- **`fanout_floor_pct`** — below this much headroom, the hook throttles only
  the fan-out tools in `fanout_tools` (the ones that multiply how fast you
  burn the window). Everything else still runs normally down to
  `hard_floor_pct`.
- **`watch_kinds`** — which of the API's rate-limit windows the hook
  considers. `session` is the 5h window; `weekly_all` is the 7-day cap;
  `weekly_scoped` covers model-specific weekly caps when active.
- **`lookback_fraction`** — the rate projection (below) measures trend over
  the trailing `window_length * lookback_fraction` hours: 75 minutes for the
  5h session window, ~42 hours for the 7-day weekly window, at the default.
- **`history_retention_hours`** — how long raw samples are kept before being
  pruned. Must stay larger than the longest lookback in use.
- **`fanout_multiplier_cap`** — ceiling on the reported fan-out multiplier,
  so a very low or still-thin measured rate doesn't project an enormous or
  unbounded number.

The hook's own throttle decision (above) stays purely floor-based and does
**not** consume the rate projection — it only records history in the
background so `--json` has data to work with. Keeping the brake's decision
rule simple and reactive is deliberate; the projection is for a session's own
judgment (see the operating directive below), not an input to the automatic
block.

## Pacing a long session: the operating directive

The hook is a backstop, not a pacing strategy — it only fires once you're
already close to a floor. [`docs/operating-directive.md`](docs/operating-directive.md)
is a standing instruction you can paste into a session (or pass via
`claude --append-system-prompt`, or drive with `/loop`) that has the session
police its *own* pace continuously: checking headroom (and the rate
projection above) before fanning out, downshifting model tier and fan-out
width as headroom drops or the projection turns unfavorable, and
checkpointing work rather than idling when it gets low — so the hook rarely
needs to fire at all.

### For a Claude Code session reading this repo cold

If you're a Claude Code session that's been pointed at this repo without
further explanation: [`CLAUDE.md`](CLAUDE.md) is written for exactly that —
read it first, it tells you what to set up and how to use this tool without
needing anything else from whoever handed you the repo.

## How this works, and its risks

`/api/oauth/usage` is **not a documented, public Anthropic API**. It was
found by reading the `@anthropic-ai/claude-code` npm package's own bundled
source (`cli.js`) — the same request the `/usage` panel makes, with the same
OAuth bearer token and `anthropic-beta: oauth-2025-04-20` header the CLI
itself sends. That means:

- It can change or disappear on any Claude Code update, without notice, and
  this tool would start failing.
- It only works with a `claude.ai` OAuth login (Pro/Max subscription), not an
  Anthropic Console API key — API-key billing doesn't have "plan headroom" in
  this sense.
- The response shape has both a current `limits[]` array and an older
  top-level shape (`five_hour`/`seven_day`/…). This tool reads both, but
  either could change independently.

**Token expiry.** The access token in `.credentials.json` is short-lived
(hours, not days) and is refreshed automatically by the `claude` CLI itself
during normal use. This tool deliberately **does not implement token
refresh** — it only ever reads the token the CLI already has. If it's expired
and no `claude` process is actively running to refresh it (e.g. this tool is
being polled standalone, with no active session behind it, for many hours),
you'll get a clear error telling you to run:

```bash
claude setup-token
```

This is a known, real limitation for fully unattended use spanning many
hours with no active session — there is currently no way around it without
implementing OAuth refresh in this tool, which was deliberately not done here
(see the commit history for why: it risks racing the CLI's own refresh and
invalidating its stored session). If you need this solved, that's the
starting point.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests are hermetic — no network access, no dependency on a real
`.credentials.json`. `get_usage` and `sys.stdin` are monkeypatched with
canned data throughout.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
