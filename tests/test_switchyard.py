"""Run with: python3 -m unittest discover tests"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


route = load("route", "plugin/hooks/route.py")
agy = load("agy", "lib/agy.py")
ledger = load("ledger", "lib/ledger.py")

BROWSER_RULE = (
    'Browser work never goes to agy. Anything that needs Claude in Chrome, Google Flow, AI Studio, Meta Business Suite, YouTube Studio, Buffer/TikTok, or runs podvid live / live-take / live-build stays on this Claude session or a Claude '
    'subagent, in saver mode too, because agy has no browser and none of our hooks. Below 95% of the plan limit, do the browser work on the cheapest Claude route that can do it (a Sonnet subagent for the browser steps, not Opus) and say the plan percentage in one line first; at 95% or more, stop and tell the user instead of starting it, and never hand it to agy. Reading, bulk edits, brainstorming and code that need no browser still go to agy in saver mode.'
)

MODELS = [
    "gemini-4-argon-medium", "gemini-4-argon-high", "gemini-4-argon-low",
    "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
    "claude-opus-5-5-low", "claude-opus-5-5-medium", "claude-opus-5-5-high",
    "claude-sonnet-5-5-medium", "gpt-oss-120b-medium",
]


class Context(unittest.TestCase):
    def test_reads_last_usage(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for n in (1000, 52000):
                f.write(json.dumps({"message": {"usage": {
                    "input_tokens": 10, "cache_read_input_tokens": n,
                    "cache_creation_input_tokens": 90}}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        self.assertEqual(route.context_tokens(f.name), 52100)

    def test_missing_transcript(self):
        self.assertEqual(route.context_tokens("/nonexistent.jsonl"), 0)

    def test_nudges_only_when_large(self):
        off = {"active": False, "pct": None, "window": None, "resets": None}
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"message": {"usage": {"cache_read_input_tokens": 300000}}}) + "\n")
        self.assertIn("/clear", route.facts(f.name, off, {})[1])
        self.assertNotIn("/clear", route.facts(None, off, {})[1])


class Facts(unittest.TestCase):
    """The hook states facts; it never picks a route."""

    def test_saver_mode_is_spelled_out(self):
        on = {"active": True, "pct": 91, "window": "7-day", "resets": None}
        text = route.facts(None, on, {"fast": "gemini-3.8-flash-medium"})[1]
        self.assertIn("SAVER MODE ON", text)
        self.assertIn("7-day limit is at 91%", text)
        self.assertIn("fast=gemini-3.8-flash-medium", text)
        self.assertIn("saver mode (7-day 91%)", route.notice(0, on))

    def test_saver_mode_keeps_browser_work_off_agy(self):
        on = {"active": True, "pct": 91, "window": "7-day", "resets": None}
        text = route.facts(None, on, {})[1]
        self.assertIn(BROWSER_RULE, text)
        off = {"active": False, "pct": 40, "window": "5-hour", "resets": None}
        self.assertNotIn(BROWSER_RULE, route.facts(None, off, {})[1])

    def test_router_prompt_has_browser_rule_twice(self):
        router = (ROOT / "prompts/router.md").read_text()
        self.assertEqual(router.count(BROWSER_RULE), 2)
        saver = router.split("## Saver mode")[1].split("\n## ")[0]
        self.assertIn(BROWSER_RULE, saver)
        self.assertLess(router.index(BROWSER_RULE), router.index("## What each model is good at"))

    def test_agy_first_policy_in_facts_regardless_of_saver(self):
        for active in (True, False):
            st = {"active": active, "pct": 91 if active else 40, "window": "7-day", "resets": None}
            text = route.facts(None, st, {})[1]
            self.assertIn("--tier sonnet", text)
            self.assertIn("--tier opus", text)
            self.assertIn("browser integration", text)
            self.assertIn("fallback", text)

    def test_router_prompt_prefers_agy_with_fallback(self):
        router = (ROOT / "prompts/router.md").read_text()
        self.assertIn("Agy first for Sonnet/Opus-class work", router)
        self.assertIn("agy sonnet (claude-sonnet-5-5-medium)", router)
        self.assertIn("Fall back to Claude when agy fails", router)
        self.assertNotIn("with saver mode OFF the deep subagent", router)

    def test_saver_off_mentions_usage_only(self):
        off = {"active": False, "pct": 40, "window": "5-hour", "resets": None}
        text = route.facts(None, off, {})[1]
        self.assertNotIn("SAVER MODE ON", text)
        self.assertIn("40%", text)
        self.assertNotIn("saver", route.notice(0, off))

    def test_no_route_is_guessed(self):
        self.assertFalse(hasattr(route, "classify"))

    def test_reads_quota_and_ignores_reset_windows(self):
        import time
        q = {"updated": time.time(),
             "five_hour": {"used_percentage": 95, "resets_at": time.time() - 10},
             "seven_day": {"used_percentage": 50, "resets_at": time.time() + 999}}
        self.assertEqual(route.quota.used(q), 50)
        self.assertEqual(route.quota.window(q), "seven_day")
        self.assertEqual(route.quota.saver({**q, "updated": 0}), (False, None))


class Tiers(unittest.TestCase):
    def test_resolve_from_live_list(self):
        self.assertEqual(agy.pick("cheap", MODELS), "gemini-3.8-flash-low")
        self.assertEqual(agy.pick("fast", MODELS), "gemini-3.8-flash-medium")
        self.assertEqual(agy.pick("strong", MODELS), "gemini-4-argon-high")
        self.assertEqual(agy.pick("opus", MODELS), "claude-opus-5-5-medium")
        self.assertEqual(agy.pick("sonnet", MODELS), "claude-sonnet-5-5-medium")

    def test_exact_id_passes_through(self):
        self.assertEqual(agy.pick("gpt-oss-120b-medium", MODELS), "gpt-oss-120b-medium")

    def test_new_models_need_no_edit(self):
        future = ["gemini-5-flash-lite-low", "gemini-5-flash-lite-medium", "gemini-5-pro-high"]
        self.assertEqual(agy.pick("fast", future), "gemini-5-flash-lite-medium")
        self.assertEqual(agy.pick("strong", future), "gemini-5-pro-high")

    def test_stray_background_flag_is_dropped_not_fatal(self):
        cases = [
            (["--tier", "opus", "--run_in_background", "do it"], ["--tier", "opus", "do it"]),
            (["do it", "--run_in_background", "true"], ["do it"]),
            (["--background=true", "do it"], ["do it"]),
            (["do it"], ["do it"]),
        ]
        for argv, want in cases:
            self.assertEqual(agy.strip_background(argv)[0], want, argv)

    def test_stray_arguments_do_not_kill_the_hand_off(self):
        seen = {}
        agy.list_models = lambda refresh=False: ["gemini-3.8-flash-low"]
        agy.run = lambda task, model, *a, **k: seen.update(task=task, model=model) or {"status": "SUCCESS"}
        self.assertEqual(agy.main(["--tier", "cheap", "---", "--weird-flag", "brainstorm", "this"]), 0)
        self.assertEqual(seen, {"task": "brainstorm this", "model": "gemini-3.8-flash-low"})

    def test_unknown_tier_fails_clearly(self):
        with self.assertRaises(SystemExit):
            agy.pick("opus", ["gemini-3.8-flash-low"])


class Saving(unittest.TestCase):
    """A session with a Sonnet front desk, one Haiku subagent and one agy run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        ledger.CACHE = self.tmp / "cache"
        ledger.STATE = self.tmp / "state"
        ledger.STATE.mkdir()
        sid = "s1"
        main = self.tmp / f"{sid}.jsonl"

        def usage_line(model, read, out, mid):
            return json.dumps({"type": "assistant", "requestId": mid, "timestamp": "2026-10-15T12:00:00Z",
                               "message": {"id": mid, "model": model, "content": [], "usage": {
                                   "input_tokens": 0, "output_tokens": out,
                                   "cache_read_input_tokens": read, "cache_creation_input_tokens": 0}}})

        spawn = json.dumps({"type": "assistant", "timestamp": "2026-10-15T12:00:01Z", "message": {
            "content": [{"type": "tool_use", "id": "tu1", "name": "Agent",
                         "input": {"subagent_type": "switchyard:scout", "description": "find it"}}]}})
        # The same response logged twice must count once.
        main.write_text("\n".join([usage_line("claude-sonnet-5-5", 1_000_000, 10_000, "m1"),
                                   usage_line("claude-sonnet-5-5", 1_000_000, 10_000, "m1"),
                                   spawn]) + "\n")
        sub = self.tmp / sid / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.jsonl").write_text(usage_line("claude-haiku-5-5", 500_000, 5_000, "h1") + "\n")
        (sub / "agent-a1.meta.json").write_text(json.dumps({"toolUseId": "tu1", "agentType": "switchyard:scout"}))
        (ledger.STATE / "agy.jsonl").write_text(json.dumps({
            "ts": 1, "session": sid, "model": "gemini-3.8-flash-medium", "status": "SUCCESS",
            "input": 20_000, "output": 1_000, "thinking": 0, "cached": 0}) + "\n")
        self.s = ledger.Ledger(sid, main).refresh().summary()

    def test_prices_against_all_opus(self):
        # Sonnet: 1M read * 0.20 + 10K out * 10 = 0.30. Haiku: 500K * 0.01 + 5K * 0.5 = 0.0075.
        self.assertAlmostEqual(self.s["actual"], 0.3075, places=4)
        # Opus: 0.20 + 0.20 (sonnet work) + 0.10 + 0.10 (haiku work) + 0.08 + 0.02 (agy work)
        self.assertAlmostEqual(self.s["baseline"], 0.70, places=4)
        self.assertAlmostEqual(self.s["saved_pct"], 56.07, places=1)

    def test_feed_names_route_model_and_provider(self):
        routes = [(e["route"], e["provider"], e["model"]) for e in self.s["feed"] if e["kind"] == "route"]
        self.assertIn(("switchyard:scout", "Claude", "haiku"), routes)
        self.assertIn(("agy", "Antigravity", "gemini-3.8-flash-medium"), routes)

    def test_incremental_refresh_does_not_double_count(self):
        again = ledger.Ledger("s1", self.tmp / "s1.jsonl").refresh().summary()
        self.assertAlmostEqual(again["actual"], self.s["actual"])


if __name__ == "__main__":
    unittest.main()


def test_agy_quota_pools():
    import agy_quota
    data = {"models": [
        {"modelId": "claude-opus-5-5-high", "remainingPercentage": 0.8, "resetTime": "2026-10-19T19:00:00Z"},
        {"modelId": "claude-sonnet-5-5-low", "remainingPercentage": 0.7, "resetTime": "2026-10-19T19:00:00Z"},
        {"modelId": "gemini-3.6-flash-low", "remainingPercentage": 1, "resetTime": "2026-10-19T19:30:00Z"},
        {"modelId": "gemini-2.5-flash", "remainingPercentage": 0.1, "isAutocompleteOnly": True,
         "resetTime": "2026-10-19T19:30:00Z"},
        {"modelId": "gemini-pro-agent", "remainingPercentage": 0.5, "resetTime": "2026-10-19T19:30:00Z"},
    ]}
    got = {p["pool"]: round(p["used"]) for p in agy_quota.parse(data)}
    assert got == {"Claude": 30, "Gemini Pro": 50, "Gemini Flash": 0}
