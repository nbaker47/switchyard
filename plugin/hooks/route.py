"""UserPromptSubmit hook: tell the front desk the facts it needs to route, and show the user
one line about it.

It does not guess a route. The front-desk model reads the request and decides, using the
rules in prompts/router.md; it understands requests far better than keyword rules could, and
it is reading the request anyway, so deciding there costs nothing extra. This hook only adds
what that model cannot see for itself: how full the Claude plan is (and so whether saver
mode is on), how large the conversation has grown, and which Antigravity models exist today.
Each prompt is appended to routes.jsonl for `cr usage`, `cr dash` and `cr report`.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import quota  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "switchyard"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "switchyard"
CLEAR_AT = int(os.environ.get("CR_CLEAR_AT", "120000"))
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
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
        import agy
        models = (CACHE / "agy-models.txt").read_text().split()
    except (OSError, ImportError):
        return {}
    out = {}
    for tier in agy.TIERS:
        try:
            out[tier] = agy.pick(tier, models)
        except SystemExit:
            pass
    return out


def saver_state(q=None):
    q = quota.latest() if q is None else q
    active, pct = quota.saver(q)
    win = quota.window(q)
    resets = (q.get(win) or {}).get("resets_at") if win else None
    return {"active": active, "pct": pct, "window": WINDOW_NAMES.get(win, win), "resets": resets}


def facts(transcript_path, saver, tiers):
    """The context added to the prompt for the front desk."""
    ctx = context_tokens(transcript_path) if transcript_path else 0
    lines = [f"[switchyard] Context so far: ~{ctx // 1000}K tokens."]
    if saver["active"]:
        when = time.strftime("%a %H:%M", time.localtime(saver["resets"])) if saver["resets"] else "its reset"
        lines.append(
            f"[switchyard] SAVER MODE ON: the Claude plan's {saver['window']} limit is at "
            f"{saver['pct']:.0f}% (threshold {quota.SAVER_AT:.0f}%) until {when}. Any request that "
            "needs work (reading files or data, running commands, searching, editing, reasoning "
            "at length) goes to the Antigravity plan via `cr agy`. Answer yourself only from what "
            "is already in this conversation. A tiny lookup may still use the scout subagent.")
    elif saver["pct"] is not None:
        lines.append(f"[switchyard] Claude plan at {saver['pct']:.0f}% of its {saver['window']} limit; "
                     "saver mode off.")
    if tiers:
        lines.append("[switchyard] agy tiers today: " + ", ".join(f"{t}={m}" for t, m in tiers.items()) + ".")
    if ctx > CLEAR_AT:
        lines.append(f"[switchyard] The context is over {CLEAR_AT // 1000}K tokens. If this request "
                     "is unrelated to the conversation so far, say in one line that /clear would "
                     "make every following turn cheaper.")
    return ctx, "\n".join(lines)


def notice(ctx, saver):
    """The one line the user sees in the TUI for each prompt."""
    parts = ["⇄ reading request"]
    if saver["active"]:
        parts.append(f"⛽ saver mode ({saver['window']} {saver['pct']:.0f}%)")
    parts.append(f"ctx {ctx // 1000}K" + ("  ⚠ large, /clear if unrelated" if ctx > CLEAR_AT else ""))
    return " · ".join(parts)


def show_state():
    """`cr route`: what the front desk is told right now."""
    saver = saver_state()
    _, text = facts(None, saver, agy_tiers())
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
    saver = saver_state()
    ctx, text = facts(data.get("transcript_path"), saver, agy_tiers())
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "routes.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.time(), "session": data.get("session_id"),
                                "route": None, "saver": saver["active"], "context": ctx,
                                "cwd": data.get("cwd"), "transcript": data.get("transcript_path"),
                                "prompt": " ".join(prompt.split())[:120]}) + "\n")
    except OSError:
        pass
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
    if os.environ.get("CR_NOTICE", "1") == "1":
        out["systemMessage"] = notice(ctx, saver)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
