"""Status line for cr sessions: your own status line, then the live switchyard flow & quota line.

    ⇄ [haiku]──►[Antigravity:argon-high] │ ctx 18K │ 💰 vs Opus −84% ($4.12 saved) │ C: 5h 47%·7d 23% │ Agy: Cl 62%·Ge 3%

cr sets this for its own sessions only, via --settings. The status line you configured
in ~/.claude/settings.json still runs first, with the same input, so nothing is lost.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_quota  # noqa: E402
from ledger import CACHE, Ledger, short  # noqa: E402

DIM, CYAN, GREEN, YELLOW, RED, MAGENTA, RESET = (
    "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m", "\033[0m"
)


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
    desk = os.environ.get("CR_FRONT_MODEL", "haiku")
    parts = [f"{CYAN}⇄{RESET}"]
    s = Ledger(sid, transcript).refresh().summary() if sid else None

    last = s and s["last"]
    if last:
        prov_col = MAGENTA if last.get("provider") == "Antigravity" else GREEN
        m_short = short(last.get("model", "?"))
        r_short = last["route"].split(":")[-1]
        parts.append(f"[{desk}]──►[{prov_col}{r_short}:{m_short}{RESET}]")
    else:
        parts.append(f"[{desk} desk]──►{DIM}[ready]{RESET}")

    cw = data.get("context_window") or {}
    cu = cw.get("current_usage") or {}
    ctx = sum(cu.get(f) or 0 for f in ("input_tokens", "cache_read_input_tokens",
                                       "cache_creation_input_tokens"))
    if ctx:
        warn = YELLOW if ctx > int(os.environ.get("CR_CLEAR_AT", "50000")) else ""
        parts.append(f"{warn}ctx {k(ctx)}{RESET}")

    if s and s["baseline"]:
        parts.append(f"vs all-Opus {GREEN}−{s['saved_pct']:.0f}%{RESET} "
                     f"{DIM}(${s['saved']:.2f} saved){RESET}")

    rl = data.get("rate_limits") or {}
    c_quota = [f"{name} {pct_color(v)}{v:.0f}%{RESET}" for name, key in (("5h", "five_hour"), ("7d", "seven_day"))
               if isinstance((v := (rl.get(key) or {}).get("used_percentage")), (int, float))]
    if c_quota:
        parts.append("C:" + "·".join(c_quota))

    try:
        pools = agy_quota.latest()
    except Exception:
        pools = []
    if pools:
        a_quota = [f"{p['pool'][:2]} {pct_color(p['used'])}{p['used']:.0f}%{RESET}" for p in pools]
        parts.append("Agy:" + "·".join(a_quota))

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
