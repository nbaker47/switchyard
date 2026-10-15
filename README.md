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
cr route "some prompt"   # preview the route and model a prompt would get (no model call)
cr dash                  # live dashboard for a side pane
```

## Seeing the routing

- **In the chat:** each prompt shows a notice such as
  `⇄ reading request → route: scout → Haiku · Claude   ctx 45K`, and every reply ends with
  the route that was actually taken (`route: worker (Sonnet)`).
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
2. **A hint for each prompt.** A `UserPromptSubmit` hook sorts the prompt with keyword rules
   (no model tokens spent). It also reads how large the conversation is and suggests
   `/clear` when that would help.
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
- The hook's keyword rules are a hint. The front desk can and does override them.
- An agy hand-off doesn't see your Claude Code skills or run your Claude hooks. MCP
  servers you add to agy (`agy mcp add`) are available to it.

## Settings

| Variable | Default | |
|---|---|---|
| `CR_FRONT_MODEL` | `sonnet` | model you chat with |
| `CR_SAFE` | `0` | `1` turns yolo mode off |
| `CR_AUTOCOMPACT` | `160k` | auto-compact threshold |
| `CR_CLEAR_AT` | `120000` | context size (tokens) at which the hook suggests `/clear` |

Logs live in `~/.local/state/switchyard/`: `routes.jsonl` and `agy.jsonl`.

## Tests

```sh
python3 -m unittest discover tests
```

## License

MIT
