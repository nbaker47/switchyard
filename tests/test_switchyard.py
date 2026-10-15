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

MODELS = [
    "gemini-4-argon-medium", "gemini-4-argon-high", "gemini-4-argon-low",
    "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
    "claude-opus-5-5-low", "claude-opus-5-5-medium", "claude-opus-5-5-high",
    "claude-sonnet-5-5-medium", "gpt-oss-120b-medium",
]


class Classify(unittest.TestCase):
    def check(self, prompt, expected):
        self.assertEqual(route.classify(prompt)[0], expected, prompt)

    def test_routes(self):
        self.check("where is the session store defined?", "scout")
        self.check("find every call to fetchUser", "scout")
        self.check("fix the off-by-one in pagination", "worker")
        self.check("why does the strip flicker on wake", "deep")
        self.check("how should we split the billing service", "deep")
        self.check("generate tests for the parser", "agy-fast")
        self.check("migrate all files to the new logger", "agy-fast")
        self.check("get a second opinion on this design", "agy-strong")
        self.check("thanks, that looks good", "self")
        self.check("", "self")

    def test_long_paste_goes_to_bulk(self):
        self.assertEqual(route.classify("x " * 4000)[0], "agy-fast")

    def test_whole_words_only(self):
        # "address" contains "add", "planet" contains "plan"
        self.check("the address field looks off to me", "self")


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
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"message": {"usage": {"cache_read_input_tokens": 300000}}}) + "\n")
        self.assertIn("/clear", route.advise("hello", f.name)[2])
        self.assertNotIn("/clear", route.advise("hello", None)[2])


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

    def test_unknown_tier_fails_clearly(self):
        with self.assertRaises(SystemExit):
            agy.pick("opus", ["gemini-3.8-flash-low"])


if __name__ == "__main__":
    unittest.main()
