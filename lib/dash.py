"""Live dashboard for a cr session: routing feed, tokens per provider, saving vs all-Opus.

    cr dash [--session ID] [--once] [--interval 2]

Run it in a split pane next to `cr`. Without --session it follows whichever cr session
most recently received a prompt.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import CACHE, Ledger, latest_session, short  # noqa: E402

B, DIM, CYAN, GREEN, YELLOW, RED, MAGENTA, R = ("\033[1m", "\033[2m", "\033[36m", "\033[32m",
                                                "\033[33m", "\033[31m", "\033[35m", "\033[0m")
PROVIDER_COLOR = {"Claude": YELLOW, "Antigravity": MAGENTA}


def k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e3 else str(int(n))


def bar(frac, width, color):
    n = max(0, min(width, round(width * frac)))
    return f"{color}{'█' * n}{R}{DIM}{'░' * (width - n)}{R}"


def render(sid, transcript, width):
    s = Ledger(sid, transcript).refresh().summary()
    try:
        status = json.loads((CACHE / f"status-{sid}.json").read_text())
    except (OSError, ValueError):
        status = {}
    w = max(40, min(width, 110))
    cwd = status.get("cwd") or next((p.get("cwd") for p in reversed(Ledger(sid, None).prompts())), "")
    out = [f"{B}{CYAN}⇄ switchyard{R}  {DIM}session {sid[:8]}  {cwd or ''}{R}", ""]

    # Saving vs all-Opus
    bw = w - 34
    if s["baseline"]:
        out.append(f"{B}Cost vs an all-Opus session{R} {DIM}(API prices as weights){R}")
        out.append(f"  all-Opus   {bar(1, bw, DIM)} ${s['baseline']:.2f}")
        out.append(f"  this run   {bar(s['actual'] / s['baseline'], bw, GREEN)} ${s['actual']:.2f}")
        out.append(f"  {GREEN}{B}saved {s['saved_pct']:.0f}%{R}  (${s['saved']:.2f}). Conservative: "
                   f"it ignores the context that hand-offs kept small.")
    else:
        out.append(f"{DIM}No tokens yet.{R}")
    out.append("")

    # Quota and context
    rl = status.get("rate_limits") or {}
    q = []
    for name, key in (("5-hour", "five_hour"), ("7-day", "seven_day")):
        v = (rl.get(key) or {}).get("used_percentage")
        if isinstance(v, (int, float)):
            color = GREEN if v < 60 else YELLOW if v < 85 else RED
            q.append(f"{name} {bar(v / 100, 12, color)} {v:.0f}%")
    cu = (status.get("context_window") or {}).get("current_usage") or {}
    ctx = sum(cu.get(f) or 0 for f in ("input_tokens", "cache_read_input_tokens",
                                       "cache_creation_input_tokens"))
    if q or ctx:
        out.append(f"{B}Claude plan{R}  " + "   ".join(q) + (f"   context {k(ctx)}" if ctx else ""))
        out.append("")

    # Tokens per provider and model
    out.append(f"{B}Tokens by provider and model{R}")
    rows = sorted(((key.split("|", 1), sum(c.values())) for key, c in s["by"].items()),
                  key=lambda r: -r[1])
    top = max((t for _, t in rows), default=0) or 1
    for (prov, model), t in rows:
        out.append(f"  {PROVIDER_COLOR.get(prov, '')}{prov:<11}{R} {model[:26]:<26} "
                   f"{bar(t / top, w - 52, PROVIDER_COLOR.get(prov, CYAN))} {k(t):>7}")
    if not rows:
        out.append(f"  {DIM}(none yet){R}")
    out.append("")

    # Feed
    out.append(f"{B}Routing feed{R}")
    feed = s["feed"][-14:]
    if not feed:
        out.append(f"  {DIM}Waiting for the first prompt in a cr session…{R}")
    for e in feed:
        t = time.strftime("%H:%M:%S", time.localtime(e["ts"])) if e["ts"] else "--:--:--"
        if e["kind"] == "prompt":
            out.append(f"  {DIM}{t}{R}  {CYAN}▸ request{R}  {e['desc'][:w - 44]!r:.{w - 40}}  "
                       f"{DIM}hint {e['route']}{R}")
        else:
            color = PROVIDER_COLOR.get(e["provider"], "")
            saved = f"saved ${e['opus'] - e['cost']:.2f}" if e["opus"] else ""
            out.append(f"  {DIM}{t}{R}  {color}→ {e['route'].split(':')[-1]:<8}{R} "
                       f"{short(e['model']) if '/' not in e['model'] else e['model']:<14} "
                       f"{color}{e['provider']:<11}{R} {k(e['tokens']):>7} tok  {DIM}{saved}{R}")
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(prog="cr dash", description=__doc__.split("\n")[0])
    p.add_argument("--session")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=2.0)
    a = p.parse_args(argv)
    try:
        while True:
            sid, transcript = (a.session, None) if a.session else latest_session()
            if a.session:
                for line in reversed((Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
                                      / "switchyard/routes.jsonl").read_text().splitlines()):
                    if a.session in line:
                        transcript = json.loads(line).get("transcript")
                        break
            cols = shutil.get_terminal_size((100, 40)).columns
            screen = render(sid, transcript, cols) if sid else \
                f"{CYAN}⇄ switchyard{R}\n\nNo cr session yet. Start one with `cr` in another pane."
            if a.once:
                print(screen)
                return 0
            sys.stdout.write("\033[H\033[2J" + screen + "\n")
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
