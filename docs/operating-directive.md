# Operating directive: pace a session against Claude plan usage

Paste this as the first message of a long-running Claude Code session, pass it
via `claude --append-system-prompt <file>`, or use it as the recurring prompt
to `/loop`. It's the proactive counterpart to the `--hook` brake this repo
also ships (see [README.md](../README.md)) — this is what keeps the session
well clear of ever tripping that brake, rather than relying on it as the only
line of defense.

---

Before starting any new unit of work — and always before spinning up
subagents (Agent/Workflow) — check current plan headroom:

    claude-usage-governor --json

Read `lowest_remaining` (the binding window's headroom) and `binding_window`
(which limit — the 5h session window or the 7-day weekly window — is closer
to empty). The 5h window SLIDES: headroom returns continuously as old usage
ages out. Never wait for `resets_at` — re-check every few minutes instead,
since headroom can recover well before the stated reset time.

A `PreToolUse` hook already enforces a hard floor automatically (asks at 5%
headroom on any tool, asks at 25% headroom on Agent/Task/Workflow calls). Your
job is to stay well clear of ever tripping it — the bands below are your own
throttle, tighter than the hook's, so the hook is a backstop you should rarely
see fire.

## Bands (by lowest_remaining)

**≥ 50% — normal.** Any model tier the task complexity actually calls for.
Fan out freely (parallel Agent/Workflow calls) when the work is genuinely
independent.

**25–50% — throttled.** Default subagents to Sonnet or Haiku; reserve Opus for
the one step that specifically needs its judgment (architecture decisions,
ambiguous requirements, adversarial verification). Halve your usual fan-out
width. Prefer `pipeline()` over `parallel()` — a barrier makes every branch
pay the slowest branch's cost at once, right when you can least afford it.

**10–25% — conservative.** Haiku-first for all delegated work; escalate to
Sonnet/Opus only when Haiku's output fails verification or the step is
judgment-critical, not mechanical. Serialize fan-out — one Agent/Workflow call
in flight at a time, not concurrent.

**< 10% — minimal.** No subagents at all; do the smallest safely-committable
unit of work inline, checkpoint state (commit, or leave a clear note of
exactly where you stopped and why), and pause starting anything new. Re-check
headroom every few minutes — resume the moment it clears your conservative
band, don't wait for a fixed reset time.

## Model selection: least viable, not cheapest by default

For every subagent or Workflow `agent()` call, ask: what is the cheapest tier
that can produce a result I can actually verify as correct for this specific
step? Start there. Escalate one tier only when that tier's output fails
verification, or the step requires judgment a cheaper model demonstrably
can't do (architecture tradeoffs, ambiguous intent, synthesizing conflicting
signals) — not because the step merely "seems important." Pair model tier
with reasoning effort (`low`/`medium`/`high`/`xhigh`) as an independent lever:
a Sonnet call at `low` effort is often enough for mechanical steps that don't
need Opus at all.

## Keep going, continuously

This directive holds for the life of the session, not just the first task.
After finishing each checkable unit of work: re-check headroom, re-derive
your current band, and continue — do not stop just because a lull, an
ambiguous next step, or task completion tempts you to. If running under
`/loop`, end each cycle with a scheduled wakeup whose delay scales with your
current band (shorter near a floor, so you catch recovery quickly; longer
when headroom is healthy, so you're not burning cycles polling). Only stop
outright — and say so explicitly — when the < 10% band's checkpoint-and-pause
condition is reached, or a request only the user can resolve blocks you.
