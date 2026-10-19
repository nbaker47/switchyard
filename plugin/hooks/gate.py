"""PreToolUse & PostToolUse hook:
1. Enforces the thin-dispatcher rule on the front desk so it hands work off to `cr agy` or
   subagents instead of reading/editing files in the main conversation.
2. Wraps noisy Bash commands (`git diff/status/log`, `pytest`, etc.) with `rtk` when available.
3. Emits live Dynamic Flow Graph nodes (`systemMessage`) into the Claude Code TUI as routes
   start and finish, showing the active model, why it was chosen, and $ saved vs pure Opus.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import agy  # noqa: E402
import compress  # noqa: E402
import flow  # noqa: E402
import quota  # noqa: E402
from ledger import CACHE, STATE, Ledger, opus_cost, short  # noqa: E402

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
HEAVY_BASH_RE = re.compile(
    r"\b(pytest|npm\s+test|pnpm\s+test|cargo\s+test|go\s+test|make\b|sed\s+-i|perl\s+-i|python3?\s+-c|cat\s+<<)",
    re.IGNORECASE,
)


def is_subagent(data):
    """True when the hook fired inside a subagent rather than the front desk."""
    if data.get("agent_id") or data.get("agent_type"):
        return True
    tp = str(data.get("transcript_path") or "")
    return "/subagents/" in tp or "\\subagents\\" in tp


def _turn_state(session_id):
    if not session_id:
        return {"reads": 0, "bash": 0}
    p = CACHE / f"turn-{session_id}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"reads": 0, "bash": 0}


def _save_turn_state(session_id, st):
    if not session_id:
        return
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"turn-{session_id}.json").write_text(json.dumps(st))
    except OSError:
        pass


def _parse_agy_tier(cmd):
    m = re.search(r"--tier(?:=|\s+)([\w.\-]+)", cmd)
    return m.group(1) if m else "fast"


def _parse_agy_why(cmd):
    m = re.search(r"""--why(?:=|\s+)(?:"([^"]+)"|'([^']+)'|(\S+))""", cmd)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or m.group(3) or "").strip()


def _deny(reason, notice_msg=None):
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    if notice_msg and os.environ.get("CR_NOTICE", "1") == "1":
        out["systemMessage"] = notice_msg
    return out


def handle_pre_tool(data):
    tool = data.get("tool_name") or ""
    inp = data.get("tool_input") or {}
    sub = is_subagent(data)
    enforce = os.environ.get("CR_ENFORCE_DISPATCH", "1") == "1"

    # Inside a subagent: allow all tools, but wrap noisy Bash commands with RTK.
    if sub:
        if tool == "Bash":
            cmd = inp.get("command") or ""
            wrapped = compress.rtk_wrap(cmd)
            if wrapped != cmd:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "updatedInput": {**inp, "command": wrapped},
                    }
                }
        return {}

    # Front desk: check if this is a hand-off to `cr agy` or a Claude subagent (`Agent`/`Task`).
    if tool == "Bash":
        cmd = (inp.get("command") or "").strip()
        if re.search(r"(?:^|\s|/)cr\s+agy\b", cmd):
            tier = _parse_agy_tier(cmd)
            why_arg = _parse_agy_why(cmd)
            bal = quota.pool_balance()
            try:
                models = (CACHE / "agy-models.txt").read_text().split()
                model, eff, rem, why = agy.resolve(tier, models, task=cmd)
            except (OSError, SystemExit):
                model, eff, rem, why = tier, tier, None, f"tier {tier}"
            if why_arg:
                why = why_arg if why.startswith("tier ") else f"{why_arg} · {why}"
            sb = None
            if "--new" not in cmd:
                sb = agy.standby_get(model, session_id=data.get("session_id"), cwd=data.get("cwd"))
                if sb:
                    sb = {**sb, "turns": sb.get("turns", 1) + 1}
            msg = flow.dispatch_banner(
                short(model), "Antigravity", reason=why, remapped_from=rem,
                standby_info=sb, tier=eff, balance=bal, full_model=model,
            )
            return {"systemMessage": msg} if os.environ.get("CR_NOTICE", "1") == "1" else {}

    if tool in ("Agent", "Task"):
        sub_type = inp.get("subagent_type") or "subagent"
        desc = (inp.get("description") or "").strip()
        bal = quota.pool_balance()
        short_type = sub_type.split(":")[-1]
        sb = None
        updated_inp = None
        if (
            os.environ.get("CR_AGY_NO_STANDBY") != "1"
            and short_type in ("worker", "deep")
            and not inp.get("resume")
            and "--new" not in desc
        ):
            sb = agy.standby_get(f"claude:{sub_type}", session_id=data.get("session_id"), cwd=data.get("cwd"))
            if sb:
                if sb.get("autocompact"):
                    old_tok = sb.get("context_tokens", 0)
                    digest = sb.get("digest", "")
                    if digest and inp.get("prompt"):
                        updated_inp = {
                            **inp,
                            "prompt": f"[Autocompacted prior standby context (~{old_tok // 1000}K tok): {digest}]\n\n{inp['prompt']}",
                        }
                    sb = {**sb, "turns": 1, "resumed": False, "compacted_from": old_tok}
                else:
                    updated_inp = {**inp, "resume": sb["conversation_id"]}
                    sb = {**sb, "turns": sb.get("turns", 1) + 1, "resumed": True}
        msg = flow.dispatch_banner(
            sub_type, "Claude", reason=desc or "browser / Claude-only tooling or scout lookup",
            standby_info=sb if (sb and sb.get("resumed")) else None,
            tier=short_type, balance=bal,
        )
        out = {"systemMessage": msg} if os.environ.get("CR_NOTICE", "1") == "1" else {}
        if updated_inp:
            out["hookSpecificOutput"] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated_inp,
            }
        return out

    if not enforce:
        return {}

    sid = data.get("session_id")
    st = _turn_state(sid)
    saver_on, _ = quota.saver()
    pace_on = quota.pace_status().get("hot", False)
    strict_saver = saver_on or pace_on

    # 1. Never allow the front desk to edit/write files directly.
    if tool in EDIT_TOOLS:
        return _deny(
            "Switchyard front desk is dispatcher-only: never edit/write files in the main "
            "conversation. Hand this task to `cr agy --tier auto \"<brief>\"` (or `--tier sonnet` "
            "/ `--tier fast`), or `Agent(subagent_type='switchyard:worker')` if it needs browser/Claude-only MCP.",
            notice_msg="⇄ blocked front-desk edit ──► redirecting to `cr agy` hand-off",
        )

    # 2. Limit direct Read/Grep/Glob on the front desk so raw file contents don't bloat context.
    if tool in READ_TOOLS:
        max_reads = 0 if strict_saver else int(os.environ.get("CR_DESK_MAX_READS", "1"))
        st["reads"] = st.get("reads", 0) + 1
        _save_turn_state(sid, st)
        if st["reads"] > max_reads:
            return _deny(
                f"Switchyard front desk limit ({max_reads} direct read/search per turn): "
                "every file read in the main session is re-billed on every future turn. "
                "Hand this lookup/reading to `cr agy --tier fast \"<brief>\"` (or "
                "`Agent(subagent_type='switchyard:scout')` for a tiny lookup).",
                notice_msg="⇄ front-desk read cap reached ──► redirecting to `cr agy --tier fast` / `scout`",
            )

    # 3. Limit direct non-cr Bash commands on the front desk.
    if tool == "Bash":
        cmd = (inp.get("command") or "").strip()
        if not re.search(r"(?:^|\s|/)cr\b", cmd):
            max_bash = 0 if strict_saver else int(os.environ.get("CR_DESK_MAX_BASH", "1"))
            st["bash"] = st.get("bash", 0) + 1
            _save_turn_state(sid, st)
            if st["bash"] > max_bash or HEAVY_BASH_RE.search(cmd):
                return _deny(
                    "Switchyard front desk is dispatcher-only: do not run test suites, scripts, "
                    "or multi-step shell loops in the main conversation. Hand the task to "
                    "`cr agy --tier fast \"<brief>\"` or `cr agy --tier auto \"<brief>\"`.",
                    notice_msg="⇄ blocked front-desk shell loop ──► redirecting to `cr agy`",
                )
            wrapped = compress.rtk_wrap(cmd)
            if wrapped != cmd:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "updatedInput": {**inp, "command": wrapped},
                    }
                }
    return {}


def _latest_agy_run(session_id):
    try:
        with open(STATE / "agy.jsonl", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16 * 1024))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not session_id or r.get("session") == session_id or (time.time() - r.get("ts", 0) < 30):
            return r
    return None


def _record_claude_subagent_standby(data):
    """Record the latest Claude subagent (`worker`/`deep`) agentId & context tokens for warm standby."""
    if os.environ.get("CR_AGY_NO_STANDBY") == "1":
        return None
    inp = data.get("tool_input") or {}
    sub_type = inp.get("subagent_type") or "subagent"
    if sub_type.split(":")[-1] not in ("worker", "deep"):
        return None
    tp = data.get("transcript_path")
    agent_id = inp.get("resume")
    ctx_tok = 0
    if tp:
        sub_dir = Path(tp).with_suffix("") / "subagents"
        if sub_dir.is_dir():
            jfiles = sorted(sub_dir.glob("agent-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if jfiles:
                latest_f = jfiles[0]
                if not agent_id:
                    agent_id = latest_f.stem.replace("agent-", "", 1)
                try:
                    for line in reversed(latest_f.read_text(errors="ignore").splitlines()):
                        if '"usage"' in line:
                            u = json.loads(line).get("message", {}).get("usage") or {}
                            ctx_tok = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                                + (u.get("cache_creation_input_tokens") or 0)
                            break
                except (OSError, ValueError):
                    pass
    resp = data.get("tool_response")
    resp_txt = json.dumps(resp) if isinstance(resp, dict) else str(resp or "")
    if agent_id:
        return agy.standby_record(
            f"claude:{sub_type}", agent_id,
            session_id=data.get("session_id"), cwd=data.get("cwd"),
            context_tokens=ctx_tok, response_text=resp_txt[:1500], provider="Claude",
        )
    return None


def handle_post_tool(data):
    if is_subagent(data) or os.environ.get("CR_NOTICE", "1") != "1":
        return {}
    tool = data.get("tool_name") or ""
    inp = data.get("tool_input") or {}
    sid = data.get("session_id")
    tp = data.get("transcript_path")
    desk = os.environ.get("CR_FRONT_MODEL", "haiku")

    if tool == "Bash" and re.search(r"(?:^|\s|/)cr\s+agy\b", (inp.get("command") or "")):
        r = _latest_agy_run(sid)
        s = Ledger(sid, tp).refresh().summary() if sid else None
        bal = quota.pool_balance()
        if r:
            box = flow.completion_box(
                model=r.get("model", "?"),
                provider="Antigravity",
                status=r.get("status", "SUCCESS"),
                seconds=float(r.get("seconds", 0)),
                usage={
                    "input": r.get("input", 0),
                    "cached": r.get("cached", 0),
                    "output": r.get("output", 0),
                    "thinking": r.get("thinking", 0),
                },
                reason=r.get("reason") or _parse_agy_why(inp.get("command") or "") or "Antigravity hand-off",
                remapped_from=r.get("remapped_from"),
                comp_stats=r.get("comp_stats"),
                standby_info=r.get("standby_info"),
                balance=bal,
            )
            if s and s.get("baseline"):
                box += f"\n  session total saved vs all-Opus: 💰 ${s['saved']:.2f} (−{s['saved_pct']:.0f}%)"
            return {"systemMessage": box}

    if tool in ("Agent", "Task"):
        sb_rec = _record_claude_subagent_standby(data)
        s = Ledger(sid, tp).refresh().summary() if sid else None
        last = s.get("last") if s else None
        bal = quota.pool_balance()
        if last:
            call_saved = max(0.0, (last.get("opus") or 0.0) - (last.get("cost") or 0.0))
            sess_txt = (
                f" │ session saved: ${s['saved']:.2f} (−{s['saved_pct']:.0f}% vs all-Opus)"
                if s and s.get("baseline") else ""
            )
            sb_txt = f" ♻#{sb_rec['turns']}" if sb_rec and sb_rec.get("turns", 1) > 1 else ""
            q_txt = flow.quota_rationale(bal)
            q_line = f"│  quota: {q_txt}\n" if q_txt else ""
            msg = (
                f"╭─ ⇄ flow complete: [prompt] ──► [{desk} desk] ──► [Claude: {last.get('route')}{sb_txt} ({last.get('model')})] ──► [✓ {last.get('seconds', 0):.1f}s]\n"
                f"{q_line}"
                f"╰─ 💰 saved ${call_saved:.3f} vs pure Opus{sess_txt}"
            )
            return {"systemMessage": msg}
    return {}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pre"
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    out = handle_post_tool(data) if mode == "post" else handle_pre_tool(data)
    if out:
        if isinstance(out.get("systemMessage"), str) and out["systemMessage"].startswith("╭─"):
            out["systemMessage"] = "\n" + out["systemMessage"]
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
