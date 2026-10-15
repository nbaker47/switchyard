"""Status line for cr sessions: your own status line, then one switchyard line.

    ⇄ last: worker → sonnet · Claude │ ctx 45K │ vs all-Opus −62% ($1.20) │ 5h 10% · 7d 89%

cr sets this for its own sessions only, via --settings. The status line you configured
in ~/.claude/settings.json still runs first, with the same input, so nothing is lost.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import CACHE, Ledger  # noqa: E402

DIM, CYAN, GREEN, YELLOW, RED, RESET = "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def own_statusline(raw):
    """Run the status line from the user's settings, if any, with the same stdin."""
    if os.environ.get("CR_CHAIN_STATUSLINE", "1") != "1":
        return ""
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
    try:
        cmd = json.loads(cfg.read_text()).get("statusLine", {}).get("command")
    except (OSError, ValueError, AttributeError):
        return ""
    if not cmd or "switchyard" in cmd or "statusline.py" in cmd:
        return ""
    try:
        return subprocess.run(cmd, shell=True, input=raw, capture_output=True, text=True,
                              timeout=5).stdout.rstrip("\n")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K"


def pct_color(p):
    return GREEN if p < 60 else YELLOW if p < 85 else RED


def line(data):
    sid, transcript = data.get("session_id"), data.get("transcript_path")
    parts = [f"{CYAN}⇄{RESET}"]
    s = Ledger(sid, transcript).refresh().summary() if sid else None

    last = s and s["last"]
    if last:
        parts.append(f"last: {last['route'].split(':')[-1]} → {last['model']} · {last['provider']}")
    else:
        parts.append(f"{DIM}no hand-offs yet{RESET}")

    cw = data.get("context_window") or {}
    cu = cw.get("current_usage") or {}
    ctx = sum(cu.get(f) or 0 for f in ("input_tokens", "cache_read_input_tokens",
                                       "cache_creation_input_tokens"))
    if ctx:
        warn = YELLOW if ctx > int(os.environ.get("CR_CLEAR_AT", "120000")) else ""
        parts.append(f"{warn}ctx {k(ctx)}{RESET}")

    if s and s["baseline"]:
        parts.append(f"vs all-Opus {GREEN}−{s['saved_pct']:.0f}%{RESET} "
                     f"{DIM}(${s['saved']:.2f} at API prices){RESET}")

    rl = data.get("rate_limits") or {}
    quota = [f"{name} {pct_color(v)}{v:.0f}%{RESET}" for name, key in (("5h", "five_hour"), ("7d", "seven_day"))
             if isinstance((v := (rl.get(key) or {}).get("used_percentage")), (int, float))]
    if quota:
        parts.append(" · ".join(quota))
    return f" {DIM}│{RESET} ".join(parts)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    if data.get("session_id"):  # for cr dash: the latest quota and context, per session
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            (CACHE / f"status-{data['session_id']}.json").write_text(json.dumps(
                {k_: data.get(k_) for k_ in ("rate_limits", "context_window", "model", "cwd")}))
        except OSError:
            pass
    mine = own_statusline(raw)
    try:
        ours = line(data)
    except Exception as e:  # never break the status line
        ours = f"⇄ switchyard: {e.__class__.__name__}"
    print(f"{mine}\n{ours}" if mine else ours)


if __name__ == "__main__":
    main()
