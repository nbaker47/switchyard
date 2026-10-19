"""UserPromptSubmit hook: tell the front desk the facts it needs to route, tick/expire warm
standby sessions after 3 idle turns, and show the user the live intake flow graph in the TUI.

It does not guess a route. The front-desk model reads the request and decides, using the
rules in prompts/router.md; it understands requests far better than keyword rules could, and
it is reading the request anyway, so deciding there costs nothing extra. This hook adds
what that model cannot see for itself: how full the Claude plan is (5h/7d usage + burn-rate
pace), how full the Antigravity pools are (Claude vs Gemini), which warm standby sessions
are currently open, how large the conversation has grown, and which Antigravity models exist today.
Each prompt is appended to routes.jsonl for `cr usage`, `cr dash` and `cr report`.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import agy  # noqa: E402
import compress  # noqa: E402
import flow  # noqa: E402
import quota  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "switchyard"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "switchyard"
CLEAR_AT = int(os.environ.get("CR_CLEAR_AT", "50000"))
WINDOW_NAMES = {"five_hour": "5-hour", "seven_day": "7-day"}


def context_tokens(transcript_path):
    """Tokens the last assistant turn read: the size of the conversation right now."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 512 * 1024))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except (OSError, TypeError):
        return 0
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            u = json.loads(line)["message"]["usage"]
        except (ValueError, KeyError, TypeError):
            continue
        return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
            + (u.get("cache_creation_input_tokens") or 0)
    return 0


def agy_tiers():
    """How each agy tier resolves today, from the cached model list (never calls agy here)."""
    try:
        models = (CACHE / "agy-models.txt").read_text().split()
    except OSError:
        return {}
    out = {}
    for tier in agy.TIERS:
        try:
            out[tier] = agy.pick(tier, models)
        except SystemExit:
            pass
    return out


BROWSER_RULE = (
    'Browser work never goes to agy. Anything that needs Claude in Chrome, Google Flow, AI Studio, Meta Business Suite, YouTube Studio, Buffer/TikTok, or runs podvid live / live-take / live-build stays on this Claude session or a Claude '
    'subagent, in saver mode too, because agy has no browser and none of our hooks. Below 95% of the plan limit, do the browser work on the cheapest Claude route that can do it (a Sonnet subagent for the browser steps, not Opus) and say the plan percentage in one line first; at 95% or more, stop and tell the user instead of starting it, and never hand it to agy. Reading, bulk edits, brainstorming and code that need no browser still go to agy in saver mode.'
)


AGY_FIRST = (
    "Sonnet/Opus-class work (scoped changes, design, hard bugs, brainstorming) goes to "
    "`cr agy --tier sonnet` / `--tier opus` (or `--tier strong` / `--tier auto`) by default, saver mode or not. "
    "Use the worker/deep subagents only for work that needs browser integration or Claude-only "
    "tooling (Chrome, Google Flow, AI Studio, Meta Business Suite, YouTube Studio, Buffer/TikTok, "
    "podvid live, computer-use, Claude-only MCP connectors, Agent Teams), or as the fallback if "
    "agy errors or its quota is exhausted."
)


def saver_state(q=None):
    q = quota.latest() if q is None else q
    active, pct = quota.saver(q)
    win = quota.window(q)
    resets = (q.get(win) or {}).get("resets_at") if win else None
    pace = quota.pace_status(q)
    if not active and pace.get("hot"):
        active = True
        pct = pace["used"]
        win = pace["window"]
        resets = (q.get(win) or {}).get("resets_at") if win else None
    return {
        "active": active,
        "pct": pct,
        "window": WINDOW_NAMES.get(win, win),
        "resets": resets,
        "pace": pace,
    }


def facts(transcript_path, saver, tiers, balance=None, standby=None):
    """The context added to the prompt for the front desk."""
    ctx = context_tokens(transcript_path) if transcript_path else 0
    lines = [f"[switchyard] Context so far: ~{ctx // 1000}K tokens."]
    lines.append("[switchyard] Routing: " + AGY_FIRST)
    lines.append("[switchyard] " + compress.COD_WIRE_RULES)
    if saver["active"]:
        when = time.strftime("%a %H:%M", time.localtime(saver["resets"])) if saver.get("resets") else "its reset"
        pace_note = f" ({saver['pace']['reason']})" if saver.get("pace", {}).get("hot") else ""
        lines.append(
            f"[switchyard] SAVER MODE ON: the Claude plan's {saver['window']} limit is at "
            f"{saver['pct']:.0f}%{pace_note} (threshold {quota.SAVER_AT:.0f}%) until {when}. Any request that "
            "needs work (reading files or data, running commands, searching, editing, reasoning "
            "at length) goes to the Antigravity plan via `cr agy`, including what you would "
            "otherwise read or reason through yourself. Answer yourself only from what "
            "is already in this conversation. A tiny lookup may still use the scout subagent. "
            "HARD RULE: " + BROWSER_RULE)
    elif saver["pct"] is not None:
        lines.append(f"[switchyard] Claude plan at {saver['pct']:.0f}% of its {saver['window']} limit; "
                     "saver mode off.")
    if balance:
        ac, ag = balance.get("agy_claude"), balance.get("agy_gemini")
        if isinstance(ac, (int, float)) or isinstance(ag, (int, float)):
            c_txt = f"Claude pool {ac:.0f}%" if isinstance(ac, (int, float)) else ""
            g_txt = f"Gemini pool {ag:.0f}%" if isinstance(ag, (int, float)) else ""
            pool_line = f"[switchyard] Antigravity pools: {', '.join(x for x in (c_txt, g_txt) if x)}."
            if balance.get("shift_to_gemini"):
                pool_line += (
                    f" POOL SHIFT ACTIVE ({balance['shift_reason']}): `cr agy` automatically "
                    "remaps `--tier opus` and `--tier sonnet` to Gemini Argon (`strong`) / Flash (`fast`) "
                    "to balance your subscriptions. Prefer `--tier strong`, `--tier fast`, or `--tier auto`."
                )
            lines.append(pool_line)
    if standby:
        sb_desc = ", ".join(
            f"{s['model']} (conv {s['conversation_id'][:8]}, turn #{s.get('turns', 1)}, "
            f"idle {s.get('idle_turns', 0)}/{s.get('max_idle', 3)} turns)"
            for s in standby
        )
        lines.append(
            f"[switchyard] Warm standby agy sessions open: {sb_desc}. "
            "Follow-up `cr agy` calls to the same tier automatically resume the warm session "
            "with full prior context and 0 preamble overhead (send only the new delta/follow-up, "
            "or pass `--new` if switching to an unrelated topic). Standby sessions auto-close after "
            "3 idle turns."
        )
    if tiers:
        lines.append("[switchyard] agy tiers today: " + ", ".join(f"{t}={m}" for t, m in tiers.items()) + ".")
    if ctx > CLEAR_AT:
        lines.append(f"[switchyard] The context is over {CLEAR_AT // 1000}K tokens. If this request "
                     "is unrelated to the conversation so far, say in one line that /clear would "
                     "make every following turn cheaper.")
    return ctx, "\n".join(lines)


def notice(ctx, saver, balance=None, standby=None):
    """The live intake flow graph line the user sees in the TUI for each prompt."""
    return flow.intake_Banner(ctx, saver, balance=balance, clear_at=CLEAR_AT, standby=standby)


def _reset_turn_state(session_id, prompt):
    if not session_id:
        return
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"turn-{session_id}.json").write_text(json.dumps({
            "ts": time.time(),
            "prompt": " ".join((prompt or "").split())[:120],
            "reads": 0,
            "bash": 0,
        }))
    except OSError:
        pass


def show_state():
    """`cr route`: what the front desk is told right now."""
    saver = saver_state()
    bal = quota.pool_balance()
    _, text = facts(None, saver, agy_tiers(), balance=bal)
    print(text.replace("[switchyard] ", ""))
    print("The front desk reads each request and picks the route itself; see prompts/router.md.")
    return 0


def main():
    if sys.argv[1:2] == ["--dry-run"]:
        return show_state()
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    prompt = data.get("prompt") or ""
    if prompt.lstrip().startswith("<task-notification>"):
        return 0  # a background job reporting back, not a request from the user
    sid = data.get("session_id")
    cwd = data.get("cwd")
    _reset_turn_state(sid, prompt)
    standby_active, _ = agy.standby_tick(session_id=sid, cwd=cwd)
    saver = saver_state()
    bal = quota.pool_balance()
    ctx, text = facts(data.get("transcript_path"), saver, agy_tiers(), balance=bal, standby=standby_active)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "routes.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.time(), "session": sid,
                                "route": None, "saver": saver["active"], "context": ctx,
                                "cwd": cwd, "transcript": data.get("transcript_path"),
                                "prompt": " ".join(prompt.split())[:120]}) + "\n")
    except OSError:
        pass
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
    if os.environ.get("CR_NOTICE", "1") == "1":
        out["systemMessage"] = notice(ctx, saver, balance=bal, standby=standby_active)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
