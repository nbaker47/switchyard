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


def quota_rationale(balance=None, saver=None):
    """Build a concise 1-line summary of why the current subscription/pool state favours this route."""
    parts = []
    pace = ((saver or {}).get("pace") if isinstance(saver, dict) else None) or (balance or {}).get("pace") or {}
    c_used = (saver or {}).get("pct") if isinstance(saver, dict) and saver.get("pct") is not None else (balance or {}).get("claude_used")
    win_label = (saver or {}).get("window") if isinstance(saver, dict) and saver.get("window") else ("7-day" if pace.get("window") == "seven_day" else "5-hour")

    if isinstance(saver, dict) and saver.get("active"):
        pace_detail = (
            f" ({pace['elapsed_pct']:.0f}% elapsed → proj {pace['projected']:.0f}%)"
            if pace.get("hot") and isinstance(pace.get("projected"), (int, float))
            else ""
        )
        parts.append(f"⛽ saver mode ({win_label} {saver['pct']:.0f}%){pace_detail}")
    elif pace.get("hot"):
        win = "7d" if pace.get("window") == "seven_day" else "5h"
        parts.append(
            f"Claude {win} {pace['used']:.0f}% ({pace['elapsed_pct']:.0f}% elapsed → proj {pace['projected']:.0f}% saver)"
        )
    elif balance and balance.get("saver_limit") and isinstance(c_used, (int, float)):
        parts.append(f"Claude {c_used:.0f}% (saver ≥80%)")
    elif isinstance(c_used, (int, float)):
        proj = f", proj {pace['projected']:.0f}%" if isinstance(pace.get("projected"), (int, float)) else ""
        parts.append(f"Claude {win_label} {c_used:.0f}%{proj}")

    if balance:
        ac, ag = balance.get("agy_claude"), balance.get("agy_gemini")
        if isinstance(ac, (int, float)) or isinstance(ag, (int, float)):
            c_str = f"Cl {ac:.0f}%" if isinstance(ac, (int, float)) else ""
            g_str = f"Ge {ag:.0f}%" if isinstance(ag, (int, float)) else ""
            pool_txt = "Agy " + " · ".join(x for x in (c_str, g_str) if x)
            if balance.get("shift_to_gemini"):
                pool_txt += " (⚡ shift→Gemini)"
            parts.append(pool_txt)
    return " │ ".join(parts)


def tier_comparison(tier, remapped_from=None, balance=None):
    """Explain why `tier` was chosen over alternative tiers (e.g. why Argon vs Opus)."""
    ac = (balance or {}).get("agy_claude")
    ag = (balance or {}).get("agy_gemini")
    pool_cmp = ""
    if isinstance(ac, (int, float)) and isinstance(ag, (int, float)):
        pool_cmp = f" (Agy Cl {ac:.0f}% vs Ge {ag:.0f}%)"
    if remapped_from:
        return f"remapped {remapped_from}→{tier}: Agy Claude pool hot{pool_cmp}"
    if tier == "strong":
        return f"Argon > Opus: wide ideation/large context + non-Claude voice, saves Agy Claude pool for code/house-style{pool_cmp}"
    if tier == "opus":
        return f"Opus > Argon: deep technical judgement / strict house-style precision{pool_cmp}"
    if tier == "sonnet":
        return f"Sonnet > Flash/Opus: scoped code in repo conventions at lower cost than Opus{pool_cmp}"
    if tier == "fast":
        return f"Flash > Sonnet: fast bulk reading/mechanical work on Gemini pool{pool_cmp}"
    if tier == "cheap":
        return f"Flash-Low > Sonnet/Haiku: trivial lookup/check at lowest cost{pool_cmp}"
    return f"tier {tier}{pool_cmp}"


def intake_Banner(ctx, saver, balance=None, clear_at=100000, standby=None,
                  target_model=None, target_tier="fast", reason="", remapped_from=None):
    """Multi-line dynamic routing box for UserPromptSubmit (`╭─ ⇄ [prompt] ──► ...`)."""
    desk = os.environ.get("CR_FRONT_MODEL", "haiku")
    full_m = target_model or "gemini-3.8-flash-medium"
    short_m = short(full_m)
    remap = f" (remapped {remapped_from}→{short_m})" if remapped_from else ""
    sb_txt = ""
    if standby:
        s0 = standby[0]
        sb_txt = f" ♻ warm standby #{s0.get('turns', 1)} ({s0['conversation_id'][:8]})"

    head = f"⇄ [prompt] ──► [{desk} desk] ──► [Antigravity: {short_m}]{remap}{sb_txt}"
    cmp_txt = tier_comparison(target_tier or short_m, remapped_from=remapped_from, balance=balance)
    if reason and not reason.startswith("tier "):
        why_model = f"{reason} · {cmp_txt}"
    else:
        why_model = cmp_txt

    q_txt = quota_rationale(balance=balance, saver=saver)
    bill_txt = "$0 on Claude plan (billed to Antigravity)"
    why_quota = f"{q_txt} · {bill_txt}" if q_txt else bill_txt

    ctx_str = f"ctx {ctx // 1000}K" + ("  ⚠ large, /clear if unrelated" if ctx > clear_at else "")
    tier_str = f" (tier {target_tier})" if target_tier and target_tier != full_m else ""
    lines = [
        f"╭─ {head}",
        f"│  why model: {why_model}",
        f"│  why quota: {why_quota}",
        f"╰─ model:     {full_m}{tier_str} · {ctx_str}",
    ]
    return "\n".join(lines)


def dispatch_banner(target_label, provider, reason="", remapped_from=None, standby_info=None,
                    tier=None, balance=None, full_model=None):
    """Live TUI node printed when the front desk dispatches a task to cr agy or a subagent."""
    desk = os.environ.get("CR_FRONT_MODEL", "haiku")
    remap = f" (remapped {remapped_from}→{target_label})" if remapped_from else ""
    sb = ""
    if standby_info:
        sb = f" ♻ warm standby #{standby_info['turns']} ({standby_info['conversation_id'][:8]})"
    head = f"⇄ [prompt] ──► [{desk} desk] ──► [{provider}: {target_label}]{remap}{sb}"
    if balance is None and tier is None and full_model is None:
        why = f" │ why: {reason}" if reason else ""
        return f"{head}{why}"

    cmp_txt = tier_comparison(tier or target_label, remapped_from=remapped_from, balance=balance)
    if reason and not reason.startswith("tier "):
        why_model = f"{reason} · {cmp_txt}"
    else:
        why_model = cmp_txt
    q_txt = quota_rationale(balance)
    bill_txt = "$0 on Claude plan (billed to Antigravity)" if provider == "Antigravity" else "Claude subagent (isolated context)"
    lines = [
        f"╭─ {head}",
        f"│  why model: {why_model}",
    ]
    if q_txt:
        lines.append(f"│  why quota: {q_txt} · {bill_txt}")
    else:
        lines.append(f"│  billing:   {bill_txt}")
    if full_model:
        lines.append(f"╰─ model:     {full_model}" + (f" (tier {tier})" if tier and tier != full_model else ""))
    else:
        lines[-1] = "╰─" + lines[-1][2:]
    return "\n".join(lines)


def completion_box(model, provider, status, seconds, usage, reason="", remapped_from=None,
                   comp_stats=None, standby_info=None, tier=None, balance=None):
    """Multi-line dynamic flow graph rendered by `cr agy` / PostToolUse on completion, showing
    the route, why it was chosen vs alternatives, live quota pools, warm standby state,
    compression stats, and $ saved vs pure Opus.
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

    eff_tier = tier
    if not eff_tier:
        for t_name, fam in (("strong", "argon"), ("opus", "opus"), ("sonnet", "sonnet"), ("cheap", "flash-low"), ("fast", "flash")):
            if fam in model:
                eff_tier = t_name
                break
    cmp_txt = tier_comparison(eff_tier or short_m, remapped_from=remapped_from, balance=balance)
    if reason and not reason.startswith("tier "):
        why_line = f"{reason} · {cmp_txt}"
    else:
        why_line = cmp_txt
    q_txt = quota_rationale(balance)

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
        f"│  model:  {model}" + (f" (tier {eff_tier})" if eff_tier else ""),
        f"│  why:    {why_line}",
    ]
    if q_txt:
        lines.append(f"│  quota:  {q_txt}")
    lines.extend([
        f"│  tokens: {_fmt_tok(tot_tok)} ({_fmt_tok(inp + cached)} in, {_fmt_tok(out)} out){comp_note}",
        f"│  saved:  💰 ${saved_usd:.3f} saved vs pure Opus (${actual:.3f} actual vs ${pure_opus:.3f} Opus · −{saved_pct:.0f}%)",
        f"╰─────────────────────────────────────────────────────────────────────────────",
    ])
    return "\n".join(lines)
