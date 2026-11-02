"""What the page's own script draws, run in Node against a stand-in for the browser."""
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "page_harness.js"
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def data(**changes):
    # Thursday 2026-01-01, so Friday and Saturday of this week are future cells on the heatmap.
    return {
        "generatedAt": "2026-01-03T12:00:00", "today": "2026-01-01", "user": "demo", "claude": True,
        "days": {
            "2025-12-30": {"m": 5, "tm": {OPUS: 1000}, "sp": [100, 100, 300, 500], "sc": 1},
            "2025-12-31": {"m": 7, "tm": {OPUS: 2000}, "sp": [200, 200, 600, 1000], "sc": 1},
            "2026-01-01": {"m": 12, "tm": {OPUS: 3000, SONNET: 1}, "sp": [300, 300, 900, 1501], "sc": 2},
            "2025-11-01": {"m": 3, "tm": {OPUS: 500}, "sp": [50, 50, 150, 250], "sc": 1},
        },
        "hours": {"2026-01-01": [0] * 18 + [4] + [0] * 5},
        "models": {OPUS: [650, 650, 1950, 3250], SONNET: [0, 0, 0, 1]},
        "totals": {"sessions": 5, "messages": 27},
        "tasks": {"2025-11-01": 5400, "2026-01-01": 3599},
        "codex": None,
        **changes,
    }


CODEX = {
    "today": "2026-01-02",
    "days": {"2026-01-02": {"m": 2, "tm": {"gpt-6.1-sol": 4000000}, "sp": None, "sc": 1}},
    "hours": {}, "models": {"gpt-6.1-sol": [1000000, 1000000, 0, 2000000]},
    "totals": {"sessions": 1, "messages": 2}, "tasks": {"2026-01-02": 5192.034}, "estimated": True,
}


@unittest.skipUnless(shutil.which("node"), "needs Node to run the page's script")
class PageTests(unittest.TestCase):
    def page(self, payload=None, language="en-US", storage=None, search="", clicks=()):
        template = (ROOT / "template.html").read_text(encoding="utf-8")
        page = template.replace("/*__DATA__*/null", json.dumps(payload or data()))
        run = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=30, encoding="utf-8",
                             input=json.dumps({"html": page, "language": language, "storage": storage,
                                               "search": search, "clicks": list(clicks)}))
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def tiles(self, result):
        found = {}
        for box in ("tiles", "meta"):
            for key, value in re.findall(r'class="k">(.*?)</div><div class="v"[^>]*>(.*?)</div>',
                                         result["elements"][box]["html"]):
                found[key] = html.unescape(value)
        return found

    def stats(self, result):
        return re.findall(r'class="v">(.*?)</div><div class="k">(.*?)</div>', result["elements"]["stats"]["html"])

    def tips(self, result, box):
        return [html.unescape(t) for t in re.findall(r'data-tip="(.*?)"', result["elements"][box]["html"])]

    def test_heatmap_hover_is_the_date_and_count_with_no_future_days(self):
        tips = self.tips(self.page(), "heat")
        self.assertEqual(tips[-1], "Jan 1 — 12")
        self.assertIn("Dec 31 — 7", tips)
        self.assertFalse([t for t in tips if t.startswith("Jan 2")])

    def test_active_days_has_a_denominator_only_for_fixed_ranges(self):
        self.assertEqual(self.tiles(self.page())["Active days"], "4")
        self.assertEqual(self.tiles(self.page(clicks=[("ranges", {"range": "30"})]))["Active days"], "3/30")

    def test_streaks_ignore_the_range(self):
        for clicks in ((), [("ranges", {"range": "7"})]):
            found = self.tiles(self.page(clicks=clicks))
            self.assertEqual((found["Longest streak"], found["Current streak"]), ("3 days", "3 days"))

    def test_longest_task_follows_the_range_and_rounds_to_the_minute(self):
        self.assertEqual(self.tiles(self.page())["Longest task"], "1h 30m")
        self.assertEqual(self.tiles(self.page(clicks=[("ranges", {"range": "7"})]))["Longest task"], "1h 0m")

    def test_book_line_counts_input_and_output_but_not_cache(self):
        models = {OPUS: [20000, 10000, 0, 9000000]}
        self.assertEqual(self.page(data(models=models))["elements"]["fun"]["text"],
                         "You’ve used about as many tokens as The Little Prince.")
        models = {OPUS: [50000, 0, 0, 9000000]}
        line = self.page(data(models=models))["elements"]["fun"]["text"]
        self.assertRegex(line, r"^You’ve used ~2× more tokens than The Little Prince\.$")
        self.assertEqual(self.page(data(models={OPUS: [10, 0, 0, 9000000]}))["elements"]["fun"]["text"], "")

    def test_models_page_hides_tiny_models_and_hover_lists_the_whole_day(self):
        result = self.page(data(days={"2026-01-01": {"m": 1, "tm": {OPUS: 3000, SONNET: 2000}, "sp": None, "sc": 1}},
                                models={OPUS: [3000, 0, 0, 0], SONNET: [2000, 0, 0, 0],
                                        "claude-haiku-5-5": [1, 0, 0, 0]}),
                           clicks=[("tabs", {"tab": "models"})])
        self.assertIn("Opus 5.5", result["elements"]["mtable"]["html"])
        self.assertNotIn("Haiku", result["elements"]["mtable"]["html"])
        self.assertEqual(self.tips(result, "chart"), ["Jan 1\nOpus 5.5: 3.0k\nSonnet 5.5: 2.0k"])

    def test_model_colors_follow_the_theme(self):
        result = self.page(clicks=[("tabs", {"tab": "models"}), ("themes", {"theme": "blue"})])
        self.assertIn("#2566d0", result["elements"]["mtable"]["html"])
        self.assertEqual(result["classes"], ["blue"])
        self.assertEqual(result["storage"]["cu.theme"], "blue")

    def test_profile_follows_the_system_language(self):
        chinese = self.page(data(models={OPUS: [2480000000, 0, 0, 0]},
                                 days={"2026-01-01": {"m": 1, "tm": {OPUS: 2480000000}, "sp": None, "sc": 1}}),
                            language="zh-CN", clicks=[("styles", {"style": "profile"})])
        self.assertEqual(self.stats(chinese)[0], ("24.8亿", "累计 Token 数"))
        self.assertIn("更新于", chinese["elements"]["pfoot"]["text"])
        english = self.page(language="fr-FR", clicks=[("styles", {"style": "profile"})])
        self.assertEqual(self.stats(english)[3], ("3 days", "Longest streak"))
        self.assertIn("Updated", english["elements"]["pfoot"]["text"])

    def test_profile_hides_the_theme_switch_and_ignores_the_theme(self):
        result = self.page(storage={"cu.theme": "blue", "cu.style": "profile"})
        self.assertEqual(result["classes"], ["profile"])
        self.assertTrue(result["elements"]["themes"]["hidden"])
        self.assertFalse(result["elements"]["profile"]["hidden"])

    def test_profile_daily_tips_stop_today(self):
        self.assertEqual(self.tips(self.page(storage={"cu.style": "profile"}), "year")[-1], "Jan 1: 3.0k tokens")

    def test_profile_weekly_and_cumulative_tips(self):
        weekly = self.page(storage={"cu.style": "profile", "cu.mode": "weekly"})
        self.assertEqual(self.tips(weekly, "year")[-1], "Week of Dec 28: 6.0k tokens")
        total = self.page(storage={"cu.style": "profile", "cu.mode": "total"})
        self.assertEqual(self.tips(total, "year")[-1], "Through Jan 1: 6.5k tokens")

    def test_profile_translates_its_mode_labels_and_marks_the_chosen_one(self):
        result = self.page(language="zh-TW", storage={"cu.style": "profile"}, clicks=[("modes", {"mode": "weekly"})])
        self.assertEqual(result["elements"]["selector:[data-mode=total]"]["text"], "累计总量")
        self.assertEqual(result["pressed"]["modes"], {"daily": False, "weekly": True, "total": False})

    def test_profile_total_matches_classic_when_history_is_partial(self):
        payload = data(models={OPUS: [10000, 0, 0, 0]})
        self.assertEqual(self.tiles(self.page(payload))["Total tokens"], "10.0k")
        self.assertEqual(self.stats(self.page(payload, storage={"cu.style": "profile"}))[0][0], "10.0k")

    def test_a_day_with_no_activity_does_not_extend_the_current_streak(self):
        days = data()["days"]
        days["2025-12-31"] = {"m": 0, "tm": {}, "sp": None, "sc": 0}
        found = self.tiles(self.page(data(days=days)))
        self.assertEqual((found["Longest streak"], found["Current streak"]), ("1 day", "1 day"))

    def test_model_names_drop_release_dates_and_keep_every_word(self):
        cases = {"claude-opus-4-5-20251101": "Opus 4.5", "claude-sonnet-4-20250514": "Sonnet 4",
                 "gpt-5.1-codex-max": "GPT-5.1 Codex Max", "gpt-4o": "GPT-4o", "codex-auto-review": "codex-auto-review"}
        for model, name in cases.items():
            with self.subTest(model=model):
                self.assertEqual(self.tiles(self.page(data(models={model: [1, 1, 1, 1]})))["Favorite model"], name)

    def test_source_switch_is_hidden_without_codex(self):
        self.assertTrue(self.page()["elements"]["sources"]["hidden"])

    def test_a_computer_with_only_codex_shows_only_codex(self):
        result = self.page(data(claude=False, codex=CODEX))
        self.assertEqual(result["elements"]["title"]["text"], "Codex usage")
        self.assertTrue(result["elements"]["sources"]["hidden"])

    def test_both_tools_show_the_switch(self):
        self.assertFalse(self.page(data(codex=CODEX))["elements"]["sources"]["hidden"])

    def test_a_computer_with_neither_tool_shows_an_empty_page(self):
        result = self.page(data(claude=False))
        self.assertEqual(result["elements"]["title"]["text"], "Claude Code usage")

    def test_a_link_for_a_tool_that_is_gone_shows_the_other_one(self):
        result = self.page(data(claude=False, codex=CODEX), search="?source=claude")
        self.assertEqual(result["elements"]["title"]["text"], "Codex usage")

    def test_switching_tools_clears_the_link_so_a_reload_keeps_the_choice(self):
        result = self.page(data(codex=CODEX), search="?source=codex", clicks=[("sources", {"source": "claude"})])
        self.assertEqual(result["addressBar"], ["/"])
        self.assertEqual(result["storage"]["cu.source"], "claude")

    def test_a_saved_codex_choice_falls_back_when_codex_is_gone(self):
        result = self.page(storage={"cu.source": "codex"})
        self.assertEqual(result["elements"]["title"]["text"], "Claude Code usage")

    def test_codex_view_uses_its_own_data_and_today(self):
        result = self.page(data(codex=CODEX), clicks=[("sources", {"source": "codex"})])
        self.assertEqual(result["elements"]["title"]["text"], "Codex usage")
        self.assertIn("Codex history only", result["elements"]["foot"]["text"])
        found = self.tiles(result)
        self.assertEqual(found["Longest task"], "1h 27m")
        self.assertEqual(found["Favorite model"], "GPT-6.1 Sol")
        self.assertEqual(found["Current streak"], "1 day")
        self.assertEqual(self.tips(result, "heat")[-1], "Jan 2 — 2")

    def test_codex_all_time_split_is_marked_estimated(self):
        result = self.page(data(codex=CODEX), clicks=[("sources", {"source": "codex"})])
        self.assertTrue(result["elements"]["split"]["text"].startswith("≈ "))

    def test_the_address_can_pick_the_tool_without_changing_the_saved_choice(self):
        result = self.page(data(codex=CODEX), search="?source=codex", storage={"cu.source": "claude"})
        self.assertEqual(result["elements"]["title"]["text"], "Codex usage")
        self.assertEqual(result["storage"]["cu.source"], "claude")


if __name__ == "__main__":
    unittest.main()
