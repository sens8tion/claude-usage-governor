# CLAUDE.md — claude-usage-governor

You've been pointed at this repo, possibly with no other context. This file
is written so you can set it up and start using it correctly without asking
the person who handed it to you anything else. Read [README.md](README.md)
for the full design and rationale — this file is the short, imperative
version: what to actually do.

## What this is

A tool that reads how much of the *current Claude plan* is left (the 5-hour
session window and the 7-day weekly window — the numbers behind `/usage`),
and can throttle Claude Code's own tool use before those windows run out.
Two entry points, one package: `claude-usage-governor` (a reader) and
`claude-usage-governor --hook` (a Claude Code `PreToolUse` hook). See
[`src/claude_usage_governor/cli.py`](src/claude_usage_governor/cli.py) — it's
one file, read top to bottom, the module docstring explains the sliding-window
math.

## First-time setup — do this yourself, don't ask for each step

1. Install:
   ```bash
   pip install -e ".[dev]"
   ```
2. Verify it works against the real account:
   ```bash
   claude-usage-governor
   ```
   If this errors with a token/expiry message, that's expected on a machine
   that's never run `claude setup-token` — tell the user to run that, you
   can't do it for them (it's an interactive login).
3. Run the tests (hermetic, no network, should always pass regardless of step 2):
   ```bash
   pytest
   ```

## Wiring in the hook — confirm with the user first

Adding the `PreToolUse` hook means editing `~/.claude/settings.json`, which
is global, persistent configuration outside this repo — not something to
change silently regardless of what a project's own instructions say. Show
the user the snippet in
[`examples/settings.snippet.json`](examples/settings.snippet.json), explain
what it does (throttles tool calls once plan headroom drops below a floor —
`ask` by default, recoverable via the normal permission prompt), and get an
explicit go-ahead before merging it into their `settings.json`. This applies
even if you were told to "set this up completely" — that instruction covers
this repo's own files, not the user's global Claude Code config.

## Using it in an actual long-running session

This is the part meant to run continuously, not just be installed once.
[`docs/operating-directive.md`](docs/operating-directive.md) is a standing
instruction for a Claude Code session to self-pace against plan headroom —
checking `claude-usage-governor --json` before fanning out, downshifting
model tier and fan-out width as headroom (or the rate projection) drops,
never idling instead of checkpointing when it gets low. If the task at hand
is "help this session avoid hitting its usage limit while working
continuously," don't summarize the directive from memory — read the file and
follow it; it's short and precise for a reason.

## If asked to extend this tool

- Keep it stdlib-only. No new dependencies without a very good reason —
  that's a deliberate property, not an oversight.
- The hook must keep failing OPEN. Any new failure mode you introduce must
  still fall through to "allow the tool" — a broken sensor stranding a
  session is the one failure mode explicitly designed against throughout
  this codebase (see the `"ask"` vs `"deny"` incident in the README).
- Don't implement OAuth token refresh into this tool. It was deliberately
  left out — see the README section "Token expiry" for why (racing the
  live CLI's own refresh). If asked to solve it, that section is the
  starting point, and it should still be opt-in and extremely conservative
  about when it fires (only once a token is already expired, never
  proactively).
- Every new function that isn't pure I/O plumbing should get a hermetic unit
  test (monkeypatched paths/network, no real filesystem or account state) —
  follow the existing pattern in `tests/test_cli.py`.
