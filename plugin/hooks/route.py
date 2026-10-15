"""UserPromptSubmit hook: suggest a route for the prompt, and flag a large context.

It costs no model tokens: the classification is a few keyword rules. The suggestion is a
hint for the front-desk model, which makes the final call with the routing rules in
prompts/router.md. Every decision is appended to routes.jsonl for `cr usage`.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import quota  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "switchyard"
CLEAR_AT = int(os.environ.get("CR_CLEAR_AT", "120000"))

ROUTES = {
    "self": "answer directly, no hand-off",
    "scout": "scout subagent (Haiku) - read-only lookup",
    "worker": "worker subagent (Sonnet) - scoped change",
    "deep": "deep subagent (Opus) - hard reasoning",
    "agy-fast": "`cr agy --tier fast` (Gemini Flash) - bulk or mechanical work",
    "agy-strong": "`cr agy --tier strong` (Gemini Argon) - large-context or second opinion",
    "agy-sonnet": "`cr agy --tier sonnet` (Claude Sonnet on the Antigravity plan) - scoped change",
}

# When the Claude plan is nearly used up, Claude-heavy routes move to the Antigravity plan.
SAVER = {"deep": "agy-strong", "worker": "agy-sonnet"}


def apply_saver(route, active):
    return SAVER.get(route, route) if active else route

# First match wins, so the order matters: bulk before deep before lookup before change.
RULES = [
    ("agy-strong", r"\b(second opinion|whole (repo|codebase)|entire (repo|codebase)|across the (repo|codebase))\b"),
    ("agy-fast", r"\b(scaffold|boilerplate|generate (unit )?tests|write tests for (all|every)|"
                 r"(all|every|each) (the )?(files?|tests?|components?|modules?|endpoints?)|"
                 r"migrate|bulk|summari[sz]e|translate|transcribe|rename \S+ (across|everywhere))\b"),
    ("deep", r"\b(architect\w*|design|root cause|why (does|is|did)|race condition|deadlock|"
             r"security|threat model|trade-?offs?|strategy|prove|plan (for|out)|how should we)\b"),
    ("scout", r"(^|\b)(where (is|are|do|does)|which files?|find|search|grep|look up|list|"
              r"what does \S+ do|show me)\b"),
    ("worker", r"\b(fix|add|implement|change|update|refactor|build|write|remove|delete|rename|"
               r"make|wire|hook up|bump)\b"),
]


def classify(prompt):
    text = prompt.lower().strip()
    if not text:
        return "self", "empty prompt"
    if len(text) > 6000:
        return "agy-fast", "long pasted input"
    for route, pattern in RULES:
        m = re.search(pattern, text)
        if m:
            return route, f'matched "{m.group(0).strip()}"'
    return "self", "conversational or short"


def context_tokens(transcript_path):
    """Tokens the last assistant turn read: the size of the conversation right now."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 512 * 1024))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
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


def advise(prompt, transcript_path, saver=None):
    active, pct = quota.saver() if saver is None else saver
    route, why = classify(prompt)
    if apply_saver(route, active) != route:
        why += f", saver mode moved it from {route}"
        route = apply_saver(route, active)
    ctx = context_tokens(transcript_path) if transcript_path else 0
    lines = [f"[switchyard] suggested route: {route} = {ROUTES[route]} ({why}). "
             f"Context so far: ~{ctx // 1000}K tokens."]
    if active:
        lines.append(f"[switchyard] Saver mode: the Claude plan is at {pct:.0f}% of a limit "
                     f"(threshold {quota.SAVER_AT:.0f}%). Use the Antigravity plan for anything "
                     "substantial: `cr agy --tier strong` instead of the deep subagent, "
                     "`cr agy --tier sonnet` or `--tier fast` instead of the worker subagent. "
                     "The scout subagent (Haiku) is cheap enough to keep.")
    if ctx > CLEAR_AT:
        lines.append(f"[switchyard] The context is over {CLEAR_AT // 1000}K tokens. Hand off "
                     "anything that reads a lot. If this request is unrelated to the conversation "
                     "so far, tell the user in one line that /clear would make every following "
                     "turn cheaper.")
    return route, ctx, "\n".join(lines)


def dry_run(prompts):
    """`cr route "..."`: show the hint each prompt would get, and the model behind it."""
    models = {"self": "front desk (CR_FRONT_MODEL, default sonnet)", "scout": "haiku",
              "worker": "sonnet", "deep": "opus"}
    active, pct = quota.saver()
    if active:
        print(f"saver mode on: Claude plan at {pct:.0f}% (threshold {quota.SAVER_AT:.0f}%)")
    for prompt in prompts:
        route, why = classify(prompt)
        route = apply_saver(route, active)
        model = models.get(route)
        if model is None:  # an agy tier: resolve it against the live model list
            import agy
            try:
                model = "agy " + agy.pick(route.split("-")[1], agy.list_models())
            except (SystemExit, OSError) as e:
                model = f"agy (unavailable: {e})"
        print(f"{route:<10} -> {model:<34} {why}   ← {prompt[:60]}")
    return 0


def main():
    if sys.argv[1:2] == ["--dry-run"]:
        prompts = sys.argv[2:] or [line.strip() for line in sys.stdin if line.strip()]
        return dry_run(prompts)
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    prompt = data.get("prompt") or ""
    if prompt.lstrip().startswith("<task-notification>"):
        return 0  # a background job reporting back, not a request from the user
    saver = quota.saver()
    route, ctx, text = advise(prompt, data.get("transcript_path"), saver)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "routes.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.time(), "session": data.get("session_id"),
                                "route": route, "context": ctx, "cwd": data.get("cwd"),
                                "transcript": data.get("transcript_path"),
                                "prompt": " ".join(prompt.split())[:120]}) + "\n")
    except OSError:
        pass
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
    if os.environ.get("CR_NOTICE", "1") == "1":
        out["systemMessage"] = notice(route, ctx, saver)
    print(json.dumps(out))
    return 0


def notice(route, ctx, saver=(False, None)):
    """The one line the user sees in the TUI for each prompt."""
    target = {"self": "front desk", "scout": "Haiku · Claude", "worker": "Sonnet · Claude",
              "deep": "Opus · Claude", "agy-fast": "Gemini Flash · Antigravity",
              "agy-strong": "Gemini Argon · Antigravity",
              "agy-sonnet": "Sonnet · Antigravity"}[route]
    big = "  ⚠ large, /clear if unrelated" if ctx > CLEAR_AT else ""
    save = f"  ⛽ saver mode (Claude plan {saver[1]:.0f}%)" if saver[0] else ""
    return f"⇄ reading request → route: {route} → {target}   ctx {ctx // 1000}K{big}{save}"


if __name__ == "__main__":
    sys.exit(main())
