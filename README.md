# switchyard

Model routing for people who pay for **subscriptions**, not per-token API access. You chat in the
normal Claude Code TUI. Each request goes to the cheapest model that can do it: a Haiku, Sonnet
or Opus subagent, or your Antigravity CLI (Gemini Flash, Gemini Argon, or the Claude models on
that plan). You never pick the model yourself.

> switchyard is a community launcher that runs Claude Code and the Antigravity CLI. It is not made by,
> affiliated with, or endorsed by Anthropic or Google.

## ⚠️ It runs in yolo mode by default

`cr` starts Claude Code with `--dangerously-skip-permissions`, and agy hand-offs run with
agy's `--dangerously-skip-permissions`. Commands and edits run without asking you first.
Your hooks still run. Use `cr --safe` (or `CR_SAFE=1`) to get permission prompts back, which
also makes agy hand-offs run in `accept-edits` mode.

## Install

```sh
git clone https://github.com/nbaker47/switchyard && cd switchyard && ./install.sh
```

It needs [Claude Code](https://code.claude.com), signed in with your own plan. The
[Antigravity CLI](https://antigravity.google) (`agy`) is optional and also needs your own
login. Python 3.9+ and nothing else.

## Use

```sh
cr                       # chat, with routing (any claude flag works: cr -c, cr -p "...", ...)
cr --safe                # same, with permission prompts
cr models                # what your agy plan offers, and what each tier resolves to
cr agy --tier fast "..." # hand a task to agy yourself
cr usage [--days 7]      # tokens per subscription and model, and the routes taken
cr route                 # what the front desk is told right now: saver mode, quota, agy tiers
cr dash                  # live dashboard for a side pane
cr report --json         # everything above as JSON, for your own dashboards (versioned schema)
```

## Project context for agy

`cr agy` prepends the project context Claude Code would have loaded to the task, under a
`## Task` header: every `CLAUDE.md` (and `CLAUDE.local.md`) from `~/.claude` and from the
filesystem root down to the current directory, inlined, with simple `@path` import lines
followed; a list of all skills (name, description, path, plus reference `.md` files) from
`~/.claude/skills` and each `.claude/skills` on that path, which agy reads itself; and the
project's `~/.claude/projects/<cwd with / as ->/memory/MEMORY.md` index. Nothing is added
when there are no `CLAUDE.md` files and no skills. A line such as
`context: 3 CLAUDE.md, 41 skills, ~9.2k tokens (chars/4)` shows what it costs.

- `--bare` or `CR_AGY_BARE=1` skips the preamble.
- `--show-context` prints the preamble and exits without calling agy.
- `CR_AGY_CONTEXT_MAX_CHARS` (default `120000`): above it, the largest `CLAUDE.md` files are
  replaced by a path pointer, and the preamble says so.

## Seeing the routing

- **In the chat:** each prompt shows a notice such as
  `⇄ reading request · ⛽ saver mode (7-day 91%) · ctx 45K`. Before a hand-off the front
  desk says in one line where the work is going and why. `cr agy` prints a line per step
  while agy works, and anything long runs in the background so you can keep talking. Every
  reply ends with the route taken and why
  (`route: agy fast (gemini-3.8-flash-medium): needed reading the code, saver mode`).
- **Status line:** your own status line still runs first, and switchyard adds one line below it:
  `⇄ │ last: scout → haiku · Claude │ ctx 45K │ vs all-Opus −62% ($1.20 at API prices) │ 5h 10% · 7d 89%`
- **`cr dash`:** a live view to run next to the chat. It shows the cost against an all-Opus
  session, your plan's 5-hour and 7-day quota, tokens by provider and model, and a feed of
  requests and the routes they took.

**"Saved vs all-Opus"** prices every token that was actually processed (front desk,
subagents, agy) as if Opus had processed it, and compares that with what each model really
costs. agy work costs nothing on the Claude plan. The prices in `lib/prices.json` are API
list prices, used only as weights. The figure is conservative because it doesn't credit
the context that hand-offs kept out of the main conversation, which is usually the bigger
saving.

## How it works

Claude Code can't switch the model you chat with on each prompt: no hook or setting allows
it. Switching would also be slow and expensive, because every switch caches the whole
conversation again for the new model. So switchyard routes **tasks**, not messages:

1. **A front desk.** `cr` starts `claude` on Sonnet (`CR_FRONT_MODEL` changes that). It
   answers simple things itself and hands everything else off.
2. **The front desk decides; nothing guesses for it.** Sonnet reads every request anyway,
   and it understands requests far better than keyword rules, so it picks the route by what
   the request *needs*: answerable from the conversation, a small lookup, a lot of reading,
   a scoped change, a large mechanical change, or deep judgement. A `UserPromptSubmit` hook
   only adds facts it can't see itself: how full your Claude plan is (and so saver mode), the
   conversation's size, and which agy models exist today. It costs no extra model call.
3. **Routes:**

   | Route | Model | For |
   |---|---|---|
   | self | front desk | conversation, short answers |
   | `switchyard:scout` | Haiku | read-only lookups |
   | `switchyard:worker` | Sonnet | scoped changes |
   | `switchyard:deep` | Opus | design, hard bugs, security |
   | `cr agy --tier fast` | Gemini Flash | bulk and mechanical work |
   | `cr agy --tier strong` | Gemini Argon | whole-repo context, second opinions |

4. **Small context.** File reads, searches and test output stay inside the subagent or the
   agy session. The front desk only sees a short report back, so every following turn
   costs less. `cr` also sets `--autocompact 160k` (`CR_AUTOCOMPACT`).

5. **Saver mode.** When your Claude plan reaches 80% of its 5-hour or 7-day limit
   (`CR_SAVER_AT`), any request that needs work goes to the Antigravity plan: Flash for
   reading and mechanical work, Sonnet via agy for scoped changes, Argon or Opus via agy for
   deep judgement. The front desk only answers from what is already in the conversation, and
   a tiny lookup may still use Haiku. It switches off when the window
   resets. The usage figures come from Claude Code's status line, so they are as fresh as
   your last `cr` session. Browser work (Claude in Chrome, Google Flow, AI Studio, Meta
   Business Suite, YouTube Studio, Buffer/TikTok, `podvid live`) never goes to agy, even in saver mode; agy has no browser.

**No hard-coded model list.** agy tiers are matched by family name (flash, argon, pro, opus,
sonnet) and compute level against what `agy models` returns for your account. Claude tiers
use Claude Code's aliases (`haiku`, `sonnet`, `opus`), which always point at the current
models. New models get picked up without code changes.

## Your config is left alone

Everything is passed as flags for that one session: `--plugin-dir`, `--append-system-prompt-file`,
`--model`, `--autocompact`. Your global `~/.claude` settings, and any scripts or scheduled jobs
that run plain `claude`, behave exactly as before. Your own plugins, hooks, skills, MCP servers
and CLAUDE.md files all still load in a `cr` session.

## Terms of service

switchyard never reads, stores or forwards a Claude or Google credential. It starts the
unmodified `claude` and `agy` binaries, each signed in by you through its vendor's own
login, and each request is billed to your own plan. That is the use Anthropic's
[legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance) allows ("an
end user signing in to the unmodified Claude Code binary with their own Claude subscription").
Proxies that reuse a subscription login outside the official client are what is not allowed.
This is our reading of the terms, not legal advice.

## Limits

- `cr usage` shows token counts, not how much of your plan is left. Neither CLI reports that.
- Routing is the front desk's judgement, so it can misjudge; it escalates a tier when a
  cheap route's result is wrong, and the route line tells you what it chose.
- An agy hand-off can't run your Claude Code skills as tools or run your Claude hooks. MCP
  servers you add to agy (`agy mcp add`) are available to it. See "Project context for agy".

## Settings

| Variable | Default | |
|---|---|---|
| `CR_FRONT_MODEL` | `sonnet` | model you chat with |
| `CR_SAFE` | `0` | `1` turns yolo mode off |
| `CR_AUTOCOMPACT` | `160k` | auto-compact threshold |
| `CR_CLEAR_AT` | `120000` | context size (tokens) at which the hook suggests `/clear` |
| `CR_SAVER_AT` | `80` | Claude plan usage (%) at which saver mode starts |
| `CR_NOTICE` | `1` | `0` hides the per-prompt routing notice |

Logs live in `~/.local/state/switchyard/`: `routes.jsonl` and `agy.jsonl`.

## Tests

```sh
python3 -m unittest discover tests
```

## License

MIT
