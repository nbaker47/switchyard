"""Dynamic ASCII Routing Flow Graph & Savings Renderer for the Claude Code TUI.

Renders live progression nodes across:
1. UserPromptSubmit (`intake_Banner`): shows prompt -> front desk -> live 4-pool quota radar + warm standby.
2. PreToolUse (`dispatch_banner`): shows front desk -> chosen route/model + pool-shift + warm standby status.
3. cr agy / PostToolUse (`completion_box`): shows the completed pipeline + exact tokens +
   $ saved vs running the same work on pure Opus.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import cost, opus_cost, short  # noqa: E402


def _fmt_tok(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def intake_Banner(ctx, saver, balance=None, clear_at=50000, standby=None):
    """Single/multi-line TUI notice for UserPromptSubmit.
    Preserves exact substrings tested by test_switchyard.py (`⇄ reading request`,
    `⛽ saver mode (...)`, `ctx NK`, `⚠ large, /clear if unrelated`).
    """
    desk = os.environ.get("CR_FRONT_MODEL", "haiku")
    parts = [f"⇄ reading request ──► [{desk} desk]"]
    if saver.get("active"):
        parts.append(f"⛽ saver mode ({saver['window']} {saver['pct']:.0f}%)")
    elif balance and balance.get("pace", {}).get("hot"):
        p = balance["pace"]
        parts.append(f"⛽ pace saver ({p['reason']})")
    parts.append(f"ctx {ctx // 1000}K" + ("  ⚠ large, /clear if unrelated" if ctx > clear_at else ""))

    if standby:
        sb_items = [f"♻ {short(s['model'])} (idle {s.get('idle_turns', 0)}/{s.get('max_idle', 3)})"
                    for s in standby]
        parts.append("standby: " + ", ".join(sb_items))

    if balance:
        pools = []
        if isinstance(balance.get("claude_used"), (int, float)):
            pools.append(f"Claude {balance['claude_used']:.0f}%")
        ac, ag = balance.get("agy_claude"), balance.get("agy_gemini")
        if isinstance(ac, (int, float)) or isinstance(ag, (int, float)):
            c_str = f"Cl {ac:.0f}%" if isinstance(ac, (int, float)) else ""
            g_str = f"Ge {ag:.0f}%" if isinstance(ag, (int, float)) else ""
            pools.append("Agy " + "·".join(x for x in (c_str, g_str) if x))
        if balance.get("shift_to_gemini"):
            pools.append("⚡ shift→Gemini")
        if pools:
            parts.append(" │ ".join(pools))
    return " · ".join(parts)


def dispatch_banner(target_label, provider, reason="", remapped_from=None, standby_info=None):
    """Live TUI node printed when the front desk dispatches a task to cr agy or a subagent."""
    desk = os.environ.get("CR_FRONT_MODEL", "haiku")
    remap = f" (remapped {remapped_from}→{target_label})" if remapped_from else ""
    sb = ""
    if standby_info:
        sb = f" ♻ warm standby #{standby_info['turns']} ({standby_info['conversation_id'][:8]})"
    why = f" │ why: {reason}" if reason else ""
    return f"⇄ [prompt] ──► [{desk} desk] ──► [{provider}: {target_label}]{remap}{sb}{why}"


def completion_box(model, provider, status, seconds, usage, reason="", remapped_from=None,
                   comp_stats=None, standby_info=None):
    """Multi-line dynamic flow graph rendered by `cr agy` on completion, showing the route,
    why it was chosen, warm standby state, compression stats, and $ saved vs pure Opus.
    """
    desk = os.environ.get("CR_FRONT_MODEL", "haiku")
    inp = usage.get("input_tokens", 0) or usage.get("input", 0) or 0
    cached = usage.get("cache_read_tokens", 0) or usage.get("cached", 0) or 0
    out = (usage.get("output_tokens", 0) or usage.get("output", 0) or 0) + (
        usage.get("thinking_tokens", 0) or usage.get("thinking", 0) or 0
    )
    tot_tok = inp + cached + out
    c_dict = {"input": inp, "cache_read": cached, "output": out, "cache_write": 0}
    pure_opus = opus_cost(c_dict)
    actual = 0.0 if provider == "Antigravity" else cost(model, c_dict)
    saved_usd = max(0.0, pure_opus - actual)
    saved_pct = (100.0 * saved_usd / pure_opus) if pure_opus > 0 else 100.0

    ok_mark = "✓" if status == "SUCCESS" else "✗"
    short_m = short(model)
    remap_str = f"{remapped_from}→{short_m}" if remapped_from else short_m
    if standby_info and standby_info.get("resumed"):
        remap_str += f" ♻#{standby_info['turns']}"
    why_line = reason or ("Antigravity subscription (0 Claude quota)" if provider == "Antigravity" else "Claude subagent")

    comp_note = ""
    if standby_info and standby_info.get("resumed"):
        comp_note = f" · ♻ warm standby reused (0 preamble tok, closes after {standby_info.get('max_idle', 3)} idle turns)"
    elif comp_stats and comp_stats.get("saved_chars", 0) > 0:
        raw_c = comp_stats.get("raw_chars", 1) or 1
        pct_c = round(100.0 * comp_stats["saved_chars"] / raw_c)
        comp_note = f" · preamble −{pct_c}% ({comp_stats.get('omitted_skills', 0)} skills pruned)"

    lines = [
        f"╭─ ⇄ switchyard flow ─────────────────────────────────────────────────────────",
        f"│  [prompt] ──► [{desk} desk] ──► [{provider}: {remap_str}] ──► [{ok_mark} {seconds:.1f}s]",
        f"│  model:  {model} ({why_line})",
        f"│  tokens: {_fmt_tok(tot_tok)} ({_fmt_tok(inp + cached)} in, {_fmt_tok(out)} out){comp_note}",
        f"│  saved:  💰 ${saved_usd:.3f} saved vs pure Opus (${actual:.3f} actual vs ${pure_opus:.3f} Opus · −{saved_pct:.0f}%)",
        f"╰─────────────────────────────────────────────────────────────────────────────",
    ]
    return "\n".join(lines)
