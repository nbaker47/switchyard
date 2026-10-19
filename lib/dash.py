"""Live dashboard for a cr session: requests feed, provider limits, saving from routing.

    cr dash [--session ID] [--once] [--interval 2] [--width N] [--height N] [--demo]

Run it in a split pane next to `cr`. Without --session it follows whichever cr session
most recently received a prompt. Narrow panes (under 100 columns) stack everything in one
column; wide terminals put the feed on the left and limits and savings on the right.
--demo renders made-up sample data instead of a real session (used for the README images).
Press q or Ctrl+C to quit.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_quota  # noqa: E402
import quota  # noqa: E402
from ledger import CACHE, Ledger, cost, latest_session, opus_cost, short  # noqa: E402

B, DIM, CYAN, GREEN, YELLOW, RED, MAGENTA, R = ("\033[1m", "\033[2m", "\033[36m", "\033[32m",
                                                 "\033[33m", "\033[31m", "\033[35m", "\033[0m")
PROVIDER_COLOR = {"Claude": YELLOW, "Antigravity": MAGENTA}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
WIDE = 100  # columns at which the layout switches from stacked to side by side


# ---- text helpers: every line is measured with ANSI stripped and clipped to the width ----

def vlen(s):
    return len(ANSI.sub("", s))


def clip(s, w):
    """Truncate s to w visible columns, keeping colour codes intact."""
    if w <= 0:
        return ""
    if vlen(s) <= w:
        return s
    out, n, i = [], 0, 0
    while i < len(s) and n < w:
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
        else:
            out.append(s[i])
            n += 1
            i += 1
    return "".join(out) + R


def pad(s, w):
    s = clip(s, w)
    return s + " " * (w - vlen(s))


def flat(s, w):
    """One line of plain text, cut with an ellipsis if longer than w."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= w else (s[:max(0, w - 1)] + "…" if w > 0 else "")


def k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e3 else str(int(n))


def bar(frac, width, color):
    width = max(0, width)
    n = max(0, min(width, round(width * frac)))
    return f"{color}{'█' * n}{R}{DIM}{'░' * (width - n)}{R}"


def heat(pct):
    return GREEN if pct < 60 else YELLOW if pct < 85 else RED


def until(ts, now):
    """Time until ts as 2h13m / 45m / 3d4h, or '' if unknown or past."""
    if not isinstance(ts, (int, float)) or ts <= now:
        return ""
    s = int(ts - now)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d{h}h" if d else f"{h}h{m:02d}m" if h else f"{m}m"


def header(w, text):
    return f"{B}{text}{R} {DIM}{'─' * max(0, w - len(text) - 1)}{R}"


# ---- sections ----

def route_color(route, provider=""):
    """Colour by route type: agy/Antigravity magenta, front desk (self) yellow, other subagents green."""
    r = (route or "").lower()
    if "agy" in r or provider == "Antigravity":
        return MAGENTA
    if r in ("self", "", "opus"):
        return YELLOW
    return GREEN


def feed_rows(requests, fw, rows, now):
    """Newest request last (a log tail). Columns are added as the feed gets wider."""
    cols_time = 8 if fw >= 56 else 5
    rw = 7 if fw < 56 else 10 if fw < 100 else 18
    mw = 0 if fw < 70 else 12 if fw < 120 else 22
    sw = 1 if fw < 70 else 6
    extra = fw >= 90
    fixed = cols_time + 1 + rw + 1 + (mw + 1 if mw else 0) + sw + 1 + (17 if extra else 0)
    dw = max(6, fw - fixed)
    out = []
    for r in requests[-rows:] if rows > 0 else []:
        t = time.strftime("%H:%M:%S" if cols_time == 8 else "%H:%M", time.localtime(r["ts"])) \
            if r.get("ts") else "-" * cols_time
        hand = r.get("handoffs") or []
        route = r.get("route") or "self"
        prov = hand[0].get("provider") if hand else "Claude"
        col = route_color(hand[0].get("route") if hand else "self", prov)
        models = [short(h["model"]) if "/" not in str(h.get("model")) else h["model"] for h in hand if h.get("model")]
        model = ", ".join(dict.fromkeys(models)) or ", ".join((r.get("desk") or {}).get("models") or []) or "-"
        live = now - (r.get("end") or r["ts"]) < 30
        done = r.get("tokens", 0) > 0
        st = ("●" if live else "✓" if done else "·")
        st_txt = {"●": "live", "✓": "done", "·": "wait"}[st]
        stc = CYAN if live else GREEN if done else DIM
        status = f"{stc}{st}{R}" if sw == 1 else f"{stc}{st} {st_txt}{R}"
        line = f"{DIM}{t}{R} {col}{pad(flat(route, rw), rw)}{R} "
        if mw:
            line += f"{pad(flat(model, mw), mw)} "
        line += f"{pad(status, sw)} "
        if extra:
            sv = f"{r['saved_pct']:.0f}%" if r.get("opus") else ""
            line += f"{k(r.get('tokens', 0)):>7} {DIM}{sv:>4}{R}{' ' * 4}"
        line += flat(r.get("prompt"), dw)
        out.append(line)
    return out


def limits_lines(w, status, summary, now, q=None, agy=None):
    """One bar per window per plugged-in provider, plus saver mode."""
    q = q or quota.latest() or (status.get("rate_limits") or {})
    lines = []
    active, pct = quota.saver(q if q else None) if q else (False, None)
    on = f"{RED}{B}ON{R}" if active else f"{DIM}OFF{R}"
    lines.append(f"{B}Claude{R} {DIM}saver{R} {on}" + (f" {DIM}(at {quota.SAVER_AT:.0f}%){R}" if w >= 40 else ""))
    wins = [("5h", "five_hour"), ("7d", "seven_day")]
    shown = False
    for name, key in wins:
        win = q.get(key) or {}
        v = win.get("used_percentage")
        if not isinstance(v, (int, float)):
            continue
        shown = True
        rs = until(win.get("resets_at"), now)
        tail = f" {v:>3.0f}%" + (f" {rs:>6}" if rs else "")
        bw = max(4, w - 4 - len(tail) - 2)
        mark = f" {RED}!{R}" if v >= 85 else ""
        lines.append(f" {DIM}{name}{R} {bar(v / 100, bw, heat(v))}{heat(v)}{tail}{R}{mark}")
    if not shown:
        lines.append(f" {DIM}no limit data yet{R}")
    by = summary["by"]
    lines.append(f"{MAGENTA}{B}Antigravity{R}")
    pools = agy if agy is not None else agy_quota.latest()
    for p in pools:
        v = p["used"]
        rs = until(p.get("resets_at"), now)
        tail = f" {v:>3.0f}%" + (f" {rs:>6}" if rs else "")
        label = p["pool"].replace("Gemini ", "G ") if w < 50 else p["pool"]
        lw = 12 if w >= 50 else 7
        bw = max(4, w - 2 - lw - len(tail) - 1)
        mark = f" {RED}!{R}" if v >= 85 else ""
        lines.append(f" {DIM}{label:<{lw}}{R} {bar(v / 100, bw, heat(v))}{heat(v)}{tail}{R}{mark}")
    if not pools:
        lines.append(f" {DIM}no limit data yet{R}")
    for prov in dict.fromkeys(key.split("|", 1)[0] for key in by if not key.startswith("Claude|")):
        if prov == "Antigravity":
            tok = sum(sum(c.values()) for key, c in by.items() if key.split("|", 1)[0] == prov)
            lines.append(f" {DIM}{k(tok)} tok this session{R}")
        else:
            tok = sum(sum(c.values()) for key, c in by.items() if key.split("|", 1)[0] == prov)
            lines.append(f"{PROVIDER_COLOR.get(prov, CYAN)}{B}{prov}{R} {DIM}no limit data{R} {k(tok)} tok this session")
    return lines


def saved_lines(w, s):
    """Saving from routing vs running every token on the top model (Opus).

    Assumption: baseline is what the tokens actually processed (front desk, subagents, agy)
    would have cost on the top model at prices.json weights, from ledger.summary(); the
    plan-percentage view is that same ratio. Context kept out by hand-offs is not credited.
    """
    if not s["baseline"]:
        return [f"{DIM}No tokens yet.{R}"]
    bw = max(4, w - 11 - 8)
    top = sum(sum(c.values()) for key, c in s["by"].items() if "opus" in key.lower())
    total = sum(sum(c.values()) for c in s["by"].values()) or 1
    return [f"  {DIM}all-Opus{R} {bar(1, bw, DIM)} ${s['baseline']:.2f}",
            f"  {DIM}routed  {R} {bar(s['actual'] / s['baseline'], bw, GREEN)} ${s['actual']:.2f}",
            f"  {GREEN}{B}saved {s['saved_pct']:.0f}%{R} {GREEN}${s['saved']:.2f}{R}"
            f"  {DIM}{100 * (total - top) / total:.0f}% of tokens off Opus{R}"]


def tokens_lines(w, s, rows):
    by = sorted(((key.split("|", 1), sum(c.values())) for key, c in s["by"].items()), key=lambda r: -r[1])
    top = max((t for _, t in by), default=0) or 1
    out = []
    for (prov, model), t in by[:max(0, rows)]:
        m = flat(short(model) if "/" not in model else model, 14)
        bw = max(4, w - 12 - 14 - 8)
        out.append(f"{PROVIDER_COLOR.get(prov, CYAN)}{prov[:11]:<11}{R} {m:<14} "
                   f"{bar(t / top, bw, PROVIDER_COLOR.get(prov, CYAN))} {k(t):>6}")
    return out


def demo_data(now):
    """Made-up (summary, status) with a populated feed, limits and savings, for screenshots."""
    # (seconds ago, prompt, [(route, provider, model, tokens)], desk tokens, desk model)
    script = [
        (2400, "list the open todos in src", [("haiku", "Claude", "claude-haiku-4-5", 8300)], 800, "sonnet"),
        (2100, "write a migration rollback script", [("sonnet", "Claude", "claude-sonnet-5-5", 41700)], 3300, "sonnet"),
        (1800, "compare two caching approaches", [("agy", "Antigravity", "gemini-3.8-pro-high", 36400)], 2200, "sonnet"),
        (1500, "explain the build pipeline", [("haiku", "Claude", "claude-haiku-4-5", 15200)], 1200, "sonnet"),
        (1260, "plan the database migration", [("opus", "Claude", "claude-opus-5", 58900)], 5900, "opus"),
        (960, "tidy the readme headings", [("haiku", "Claude", "claude-haiku-4-5", 9800)], 900, "sonnet"),
        (540, "brainstorm launch plan", [("agy", "Antigravity", "gemini-3.8-pro-high", 61200)], 3100, "sonnet"),
        (420, "rename config keys across the repo", [("haiku", "Claude", "claude-haiku-4-5", 28400)], 2400, "sonnet"),
        (330, "summarise the logs folder", [("agy", "Antigravity", "gemini-3.8-flash-medium", 47300)], 1800, "sonnet"),
        (240, "fix failing auth test", [("sonnet", "Claude", "claude-sonnet-5-5", 96800)], 5200, "opus"),
        (150, "draft release notes for v0.4", [("agy", "Antigravity", "gemini-3.8-flash-medium", 22100)], 1500, "sonnet"),
        (70, "review the retry logic for races", [("opus", "Claude", "claude-opus-5", 74500)], 6800, "opus"),
        (12, "add tests for the date parser", [("haiku", "Claude", "claude-haiku-4-5", 19600)], 2100, "sonnet"),
    ]
    by, reqs, actual, baseline = {}, [], 0.0, 0.0

    def add(prov, model, tok):
        c = by.setdefault(f"{prov}|{model}", dict.fromkeys(("input", "output", "cache_read", "cache_write"), 0))
        c["input"] += tok // 5
        c["output"] += tok // 20
        c["cache_read"] += tok - tok // 5 - tok // 20
        return {"input": tok // 5, "output": tok // 20, "cache_read": tok - tok // 5 - tok // 20, "cache_write": 0}

    for ago, prompt, hands, desk_tok, desk_model in script:
        ts = now - ago
        cost_ = opus = tokens = 0.0
        hs = []
        for route, prov, model, tok in hands:
            c = add(prov, model, tok)
            hc = 0.0 if prov == "Antigravity" else cost(model, c)
            cost_, opus, tokens = cost_ + hc, opus + opus_cost(c), tokens + tok
            hs.append({"route": route, "provider": prov, "model": model, "tokens": tok})
        dm = "claude-opus-5" if desk_model == "opus" else "claude-sonnet-5-5"
        c = add("Claude", dm, desk_tok)
        cost_, opus, tokens = cost_ + cost(dm, c), opus + opus_cost(c), tokens + desk_tok
        actual, baseline = actual + cost_, baseline + opus
        reqs.append({"ts": ts, "end": ts + 40 if ago > 30 else now, "prompt": prompt, "handoffs": hs,
                     "route": ", ".join(h["route"] for h in hs), "tokens": int(tokens), "opus": opus,
                     "saved_pct": 100 * (opus - cost_) / opus if opus else 0.0,
                     "desk": {"models": [desk_model]}})
    s = {"by": by, "actual": actual, "baseline": baseline, "saved": baseline - actual,
         "saved_pct": 100 * (baseline - actual) / baseline, "requests": reqs}
    status = {"cwd": "~/projects/demo-app", "rate_limits": {
        "five_hour": {"used_percentage": 42, "resets_at": now + 2 * 3600 + 13 * 60},
        "seven_day": {"used_percentage": 71, "resets_at": now + 3 * 86400 + 4 * 3600}},
        "agy": [{"pool": "Claude", "used": 23, "resets_at": now + 4 * 3600 + 41 * 60},
                {"pool": "Gemini Pro", "used": 8, "resets_at": now + 4 * 3600 + 59 * 60},
                {"pool": "Gemini Flash", "used": 64, "resets_at": now + 4 * 3600 + 59 * 60}]}
    return s, status


def render(sid, transcript, width, height=40, demo=False):
    width, height = max(1, width), max(1, height)
    now = time.time()
    if demo:
        sid = "demo0000"
        s, status = demo_data(now)
    else:
        s = Ledger(sid, transcript).refresh().summary()
        try:
            status = json.loads((CACHE / f"status-{sid}.json").read_text())
        except (OSError, ValueError):
            status = {}
    cwd = status.get("cwd") or ("" if demo else next((p.get("cwd") for p in reversed(Ledger(sid, None).prompts())), ""))
    head = f"{B}{CYAN}⇄ switchyard{R}  {DIM}{sid[:8]}  {cwd or ''}{R}"
    reqs = s["requests"]
    body = max(1, height - 2)  # header line, and keep the last row free so nothing scrolls

    if width >= WIDE:
        rw = 62 if width >= 160 else 50
        lw = width - rw - 3
        right = [header(rw, "Limits")] + limits_lines(rw, status, s, now, status.get("rate_limits") if demo else None, status.get("agy") if demo else None) + [""]
        right += [header(rw, "Saved by routing")] + saved_lines(rw, s) + [""]
        right += [header(rw, "Tokens")] + tokens_lines(rw, s, body - len(right) - 1)
        left = [header(lw, "Requests")]
        feed = feed_rows(reqs, lw, body - 1, now)
        left += [""] * (body - 1 - len(feed)) + feed
        right += [""] * (body - len(right))
        lines = [pad(head, width)] + [f"{pad(a, lw)} {DIM}│{R} {pad(b, rw)}"
                                      for a, b in zip(left, right)]
    else:
        panels = [header(width, "Limits")] + limits_lines(width, status, s, now, status.get("rate_limits") if demo else None, status.get("agy") if demo else None)
        panels += [header(width, "Saved by routing")] + saved_lines(width, s)
        feed_h = body - 1 - len(panels)
        if feed_h < 3:  # too short for everything: keep the feed, drop panels from the bottom
            panels = panels[:max(0, body - 1 - 3)]
            feed_h = body - 1 - len(panels)
        feed = feed_rows(reqs, width, feed_h, now)
        if not reqs:
            feed = [f"{DIM}Waiting for the first prompt…{R}"]
        lines = [head, header(width, "Requests")] + [""] * (feed_h - len(feed)) + feed + panels
    return "\n".join(clip(ln, width) for ln in lines[:height])


# ---- terminal loop ----

def _screen(a, cols, rows):
    if a.demo:
        return render("demo0000", None, cols, rows, demo=True)
    sid, transcript = (a.session, None) if a.session else latest_session()
    if a.session:
        try:
            for line in reversed((Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
                                  / "switchyard/routes.jsonl").read_text().splitlines()):
                if a.session in line:
                    transcript = json.loads(line).get("transcript")
                    break
        except (OSError, ValueError):
            pass
    if not sid:
        return "\n".join(clip(x, cols) for x in
                         (f"{CYAN}⇄ switchyard{R}", "", "No cr session yet. Start one with `cr` in another pane."))
    try:
        return render(sid, transcript, cols, rows)
    except Exception as e:  # never kill the dashboard over one bad refresh
        return clip(f"{RED}dash error: {type(e).__name__}: {e}{R}", cols)


def _key_pressed(timeout):
    """True if q was typed within timeout seconds; plain sleep when stdin is not a terminal."""
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return False
    import select
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return False
        if select.select([sys.stdin], [], [], left)[0]:
            ch = os.read(sys.stdin.fileno(), 32)
            if b"q" in ch.lower() or ch == b"":
                return True


def main(argv=None):
    p = argparse.ArgumentParser(prog="cr dash", description=__doc__.split("\n")[0])
    p.add_argument("--session")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--width", type=int, help="override terminal width (for testing)")
    p.add_argument("--height", type=int, help="override terminal height (for testing)")
    p.add_argument("--demo", action="store_true", help="show made-up sample data")
    a = p.parse_args(argv)

    def size():
        t = shutil.get_terminal_size((100, 30))
        return a.width or t.columns, a.height or t.lines

    if a.once:
        print(_screen(a, *size()))
        return 0
    old = None
    try:
        if sys.stdin.isatty():
            import termios
            import tty
            old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write("\033[?25l\033[2J")  # hide cursor, clear once
        while True:
            cols, rows = size()  # re-read each refresh so resizing works
            screen = _screen(a, cols, rows)
            # cursor home, clear each line's tail, then everything below: no full-clear flicker
            sys.stdout.write("\033[H" + "\033[K\n".join(screen.split("\n")) + "\033[K\033[J")
            sys.stdout.flush()
            if _key_pressed(a.interval):
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        sys.stdout.write("\033[?25h\033[0m\n")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
