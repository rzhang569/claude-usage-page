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
    def page(self, payload=None, language="en-US", storage=None, search="", clicks=(), system_dark=False):
        template = (ROOT / "template.html").read_text(encoding="utf-8")
        page = template.replace("/*__DATA__*/null", json.dumps(payload or data()))
        run = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=30, encoding="utf-8",
                             input=json.dumps({"html": page, "language": language, "storage": storage,
                                               "search": search, "clicks": list(clicks), "systemDark": system_dark}))
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

    def priced(self, **changes):
        days = data()["days"]
        days["2026-01-01"] = {**days["2026-01-01"], "c": {OPUS: 1234.5, SONNET: 0.004}, "ct": 2900}
        days["2025-12-31"] = {**days["2025-12-31"], "c": {OPUS: 8.25}, "u": 600, "ct": 1200}
        return data(days=days, pricing={"reviewed": "2026-11-20", "claude": "x", "openai": "y"}, **changes)

    def test_cost_line_tooltips_and_column_show_api_list_prices(self):
        result = self.page(self.priced())
        line = result["elements"]["costline"]
        self.assertFalse(line["hidden"])
        # 600 of the 4,100 tokens the estimate covers are unpriced (ct, not the page's token totals).
        self.assertEqual(html.unescape(re.sub(r"<[^>]+>", "", line["html"])),
                         "≈ $1.2k at Claude API list prices · 15% of tokens not priced")
        self.assertIn("Jan 1 — 12 · $1.2k", self.tips(result, "heat"))
        self.assertIn("Dec 31 — 7 · ≥ $8.25", self.tips(result, "heat"))  # 600 tokens there had no price
        table = self.page(self.priced(), storage={"cu.tab": "models"})["elements"]["mtable"]["html"]
        self.assertIn(">API cost</th>", table)
        self.assertIn("<td>$1.2k</td>", table)
        chart = self.tips(self.page(self.priced(), storage={"cu.tab": "models"}), "chart")
        self.assertTrue(any("Opus 5.5: 3.0k · $1.2k" in tip for tip in chart))

    def test_history_priced_from_surviving_transcripts_is_marked_partial(self):
        payload = self.priced()
        payload["pricing"]["partialBefore"] = "2025-12-31"
        line = self.page(payload)["elements"]["costline"]
        self.assertIn("history through Dec 31 partly estimated", line["html"])
        recent = self.page(payload, storage={"cu.range": "7"})  # today is Jan 1; 7d still includes Dec 31
        self.assertIn("partly estimated", recent["elements"]["costline"]["html"])
        # Between the November activity and the last 7 days: All reaches it, 7d doesn't.
        payload["pricing"]["partialBefore"] = "2025-11-30"
        self.assertIn("partly estimated", self.page(payload)["elements"]["costline"]["html"])
        self.assertNotIn("partly estimated", self.page(payload, storage={"cu.range": "7"})["elements"]["costline"]["html"])

    def test_cost_formats_cents_dollars_and_thousands(self):
        for dollars, shown in ((0.004, "<$0.01"), (8.25, "$8.25"), (123.4, "$123"), (12345, "$12.3k"), (2.5e6, "$2.5M")):
            days = {"2026-01-01": {"m": 1, "tm": {OPUS: 10}, "sp": [1, 1, 1, 7], "sc": 1, "c": {OPUS: dollars}}}
            result = self.page(data(days=days, pricing={"reviewed": "2026-11-20"}))
            self.assertIn(f"<b>{shown}</b>", result["elements"]["costline"]["html"], dollars)

    def test_cost_button_turns_cost_off_and_is_remembered(self):
        off = self.page(self.priced(), clicks=[("costbtn", {})])
        self.assertTrue(off["elements"]["costline"]["hidden"])
        self.assertEqual(off["storage"]["cu.cost"], "off")
        self.assertEqual(off["elements"]["costbtn"]["attrs"]["aria-pressed"], "false")
        self.assertNotIn("$", " ".join(self.tips(off, "heat")))
        table = self.page(self.priced(), storage={"cu.tab": "models", "cu.cost": "off"})["elements"]["mtable"]["html"]
        self.assertNotIn("API cost", table)
        self.assertEqual(self.page(self.priced())["elements"]["costbtn"]["attrs"]["aria-pressed"], "true")

    def test_no_prices_means_no_cost_anywhere(self):
        result = self.page()
        self.assertTrue(result["elements"]["costline"]["hidden"])
        self.assertTrue(result["elements"]["costseg"]["hidden"])
        self.assertTrue(self.page(self.priced(), storage={"cu.style": "profile"})["elements"]["costseg"]["hidden"])

    def test_a_day_with_only_a_search_fee_still_counts(self):
        days = data()["days"]
        days["2025-12-30"] = {"m": 0, "tm": {}, "sc": 0, "c": {OPUS: 5}}
        result = self.page(data(days=days, pricing={"reviewed": "2026-11-20"}))
        self.assertIn("<b>$5.00</b>", result["elements"]["costline"]["html"])

    def test_a_model_with_unpriced_tokens_shows_a_floor_rounded_down(self):
        def cost_cell(dollars, unpriced):
            days = {"2026-01-01": {"m": 1, "tm": {OPUS: 10}, "sp": [1, 1, 1, 7], "sc": 1, "c": {OPUS: dollars},
                                   "um": {OPUS: unpriced}, "u": unpriced, "ct": 10}}
            table = self.page(data(days=days, pricing={"reviewed": "2026-11-20"}), storage={"cu.tab": "models"})
            return html.unescape(re.findall(r"<td[^>]*>([^<]*)</td>", table["elements"]["mtable"]["html"])[-1])
        self.assertEqual(cost_cell(10.51, 5), "≥ $10")
        self.assertEqual(cost_cell(1234.5, 5), "≥ $1.2k")
        self.assertEqual(cost_cell(0.003, 5), "≥ $0.00")
        self.assertEqual(cost_cell(10.51, 0), "$11")  # fully priced: rounded as usual, no mark

    def test_a_model_with_few_tokens_but_real_spend_keeps_its_row(self):
        days = {"2026-01-01": {"m": 1, "tm": {OPUS: 10**7, SONNET: 100}, "sp": [1, 1, 1, 7], "sc": 1,
                               "c": {OPUS: 10, SONNET: 16}, "ct": 10**7 + 100}}
        payload = data(days=days, models={OPUS: [0, 0, 0, 10**7], SONNET: [0, 100, 0, 0]}, pricing={"reviewed": "x"})
        table = self.page(payload, storage={"cu.tab": "models"})["elements"]["mtable"]["html"]
        self.assertIn("Sonnet 5.5</td><td>100</td>", table)
        self.assertIn("<td>$16</td>", table)
        keys = self.page(payload, storage={"cu.tab": "models"})["elements"]["keys"]["html"]
        self.assertNotIn("Sonnet", keys)  # the chart still leaves out what it can't draw

    def test_a_tiny_unpriced_share_still_shows(self):
        days = {"2026-01-01": {"m": 1, "tm": {OPUS: 10**6}, "sp": [0, 0, 0, 10**6], "sc": 1, "c": {OPUS: 0.2},
                               "u": 400, "ct": 10**6 + 400}}
        line = self.page(data(days=days, pricing={"reviewed": "x"}))["elements"]["costline"]
        self.assertIn("<0.1% of tokens not priced", html.unescape(line["html"]))
        self.assertIn("Not priced:", line["dataset"]["tip"])

    def test_a_search_fee_with_no_tokens_in_range_keeps_its_row(self):
        for counters in ({}, {SONNET: 0}):  # no entry at all, or an explicit zero
            with self.subTest(counters=counters):
                days = {"2026-01-01": {"m": 1, "tm": {OPUS: 1000, **counters}, "sp": [1, 1, 1, 997], "sc": 1,
                                       "c": {OPUS: 100, SONNET: 0.01}, "ct": 1000}}
                payload = data(days=days, models={OPUS: [1, 1, 1, 997]}, pricing={"reviewed": "x"})
                table = self.page(payload, storage={"cu.tab": "models"})["elements"]["mtable"]["html"]
                self.assertRegex(table, r"Sonnet 5\.5</td><td>0</td>.*<td>\$0\.01</td></tr>")

    def test_a_mostly_unpriced_range_doesnt_round_to_all(self):
        days = {"2026-01-01": {"m": 1, "tm": {OPUS: 10**6}, "sp": None, "sc": 1, "c": {OPUS: 0.2}, "u": 999000, "ct": 10**6}}
        line = self.page(data(days=days, pricing={"reviewed": "x"}))["elements"]["costline"]["html"]
        self.assertIn(">99% of tokens not priced", html.unescape(line))

    def test_models_table_says_when_old_history_is_partly_priced(self):
        payload = self.priced()
        payload["pricing"]["partialBefore"] = "2025-12-31"
        table = self.page(payload, storage={"cu.tab": "models"})["elements"]["mtable"]["html"]
        self.assertIn("API cost through Dec 31 comes from the transcripts still on this computer", table)
        payload["pricing"]["partialBefore"] = "2025-10-01"
        self.assertNotIn("API cost through", self.page(payload, storage={"cu.tab": "models"})["elements"]["mtable"]["html"])

    def test_history_with_nothing_priced_says_so_instead_of_free(self):
        days = {"2026-01-01": {"m": 1, "tm": {OPUS: 10}, "sp": None, "sc": 1, "u": 10, "ct": 10}}
        line = self.page(data(days=days, pricing={"reviewed": "x"}))["elements"]["costline"]["html"]
        self.assertTrue(line.startswith("<b>No estimate</b>"), line)
        self.assertIn("100% of tokens not priced", line)

    def test_codex_cost_names_the_openai_api(self):
        codex = {**CODEX, "days": {"2026-01-02": {**CODEX["days"]["2026-01-02"], "c": {"gpt-6.1-sol": 4.2}}}}
        result = self.page(self.priced(codex=codex), storage={"cu.source": "codex"})
        self.assertIn("at OpenAI API list prices", result["elements"]["costline"]["html"])

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

    def test_a_task_under_half_a_minute_does_not_read_as_zero(self):
        short = data(tasks={"2026-01-01": 20})
        self.assertEqual(self.tiles(self.page(short))["Longest task"], "<1m")
        self.assertEqual(self.stats(self.page(short, language="zh-CN", storage={"cu.style": "profile"}))[2][0], "不到 1 分")

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

    def test_appearance_follows_the_computer_by_default(self):
        self.assertEqual(self.page()["classes"], [])
        self.assertEqual(self.page(system_dark=True)["classes"], ["dark"])

    def test_the_button_cycles_auto_light_and_dark_and_is_remembered(self):
        result = self.page(system_dark=True, clicks=[("appearance", {})])
        self.assertEqual((result["classes"], result["elements"]["appearance"]["text"]), ([], "☀︎ Light"))
        self.assertEqual(result["storage"]["cu.appearance"], "light")
        result = self.page(clicks=[("appearance", {}), ("appearance", {})])
        self.assertEqual((result["classes"], result["elements"]["appearance"]["text"]), (["dark"], "☾ Dark"))
        result = self.page(storage={"cu.appearance": "dark"}, clicks=[("appearance", {})])
        self.assertEqual(result["elements"]["appearance"]["text"], "◐ Auto")

    def test_model_colors_change_in_the_dark(self):
        html = self.page(storage={"cu.appearance": "dark"}, clicks=[("tabs", {"tab": "models"})])["elements"]["mtable"]["html"]
        self.assertIn("#e8875a", html)
        self.assertNotIn("#d97757", html)

    def test_profile_has_a_light_and_a_dark_look(self):
        self.assertEqual(self.page(storage={"cu.style": "profile"})["classes"], ["profile", "light"])
        self.assertEqual(self.page(storage={"cu.style": "profile"}, system_dark=True)["classes"], ["profile"])
        light = self.page(storage={"cu.style": "profile", "cu.appearance": "light"}, system_dark=True)
        self.assertEqual(light["classes"], ["profile", "light"])
        self.assertEqual(light["elements"]["appearance"]["text"], "☀︎ Light")

    def test_profile_follows_the_system_language(self):
        chinese = self.page(data(models={OPUS: [2480000000, 0, 0, 0]},
                                 days={"2026-01-01": {"m": 1, "tm": {OPUS: 2480000000}, "sp": None, "sc": 1}}),
                            language="zh-CN", clicks=[("styles", {"style": "profile"})])
        self.assertEqual(self.stats(chinese)[0], ("24.8亿", "累计 Token 数"))
        self.assertIn("更新于", chinese["elements"]["pfoot"]["text"])
        english = self.page(language="fr-FR", clicks=[("styles", {"style": "profile"})])
        self.assertEqual(self.stats(english)[3], ("3 days", "Longest streak"))
        self.assertIn("Updated", english["elements"]["pfoot"]["text"])

    def words(self, result):
        # Everything a reader can see: element text, plus the hover tips inside the markup.
        seen = []
        for el in result["elements"].values():
            if el["hidden"]:
                continue
            seen += [el["text"], re.sub(r"<[^>]+>", " ", el["html"])] + re.findall(r'data-tip="([^"]*)"', el["html"])
        return html.unescape(" ".join(seen + [result["title"]]))

    def test_each_view_keeps_to_one_language(self):
        both = data(codex=CODEX)
        for mode in ("daily", "weekly", "total"):
            for look in ("auto", "light", "dark"):
                chinese = self.page(both, language="zh-CN",
                                    storage={"cu.style": "profile", "cu.mode": mode, "cu.appearance": look})
                left = re.sub(r"Claude Code|Claude|Codex|Token|@?demo|DE", "", self.words(chinese))
                self.assertNotRegex(left, r"[A-Za-z]{2,}", (mode, look))
                self.assertEqual(chinese["lang"], "zh-CN")
        labels = lambda r: [r["elements"]["selector:[data-style=%s]" % k]["text"] for k in ("card", "profile")]
        self.assertEqual(labels(self.page(language="zh-CN", storage={"cu.style": "profile"})), ["经典", "个人主页"])
        self.assertEqual(labels(self.page(language="zh-CN")), ["Classic", "Profile"])
        self.assertEqual(labels(self.page(language="en-US", storage={"cu.style": "profile"})), ["Classic", "Profile"])
        cjk = r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]"
        for language in ("en-US", "zh-CN"):  # Classic stays English even on a Chinese system.
            for tab in ("overview", "models"):
                classic = self.page(both, language=language, storage={"cu.tab": tab})
                self.assertNotRegex(self.words(classic), cjk, (language, tab))
                self.assertEqual((classic["lang"], classic["title"]), ("en", "Claude Code Usage"))
        english = self.page(both, language="en-US", storage={"cu.style": "profile", "cu.mode": "weekly"})
        self.assertNotRegex(self.words(english), cjk)

    def test_profile_in_chinese_leaves_no_english_written_in_the_markup(self):
        # Words written into template.html itself, which the harness only sees once the script sets them.
        from html.parser import HTMLParser
        found = []

        class Static(HTMLParser):
            stack = []

            def handle_starttag(self, tag, attrs):
                if tag not in ("meta", "link", "br", "img", "input"):
                    self.stack.append(dict(attrs))

            def handle_endtag(self, tag):
                if tag not in ("meta", "link", "br", "img", "input") and self.stack:
                    self.stack.pop()

            def handle_data(self, text):
                if text.strip() and self.stack and any(a.get("id") == "profile" or a.get("class") == "top" for a in self.stack):
                    found.append((text.strip(), [dict(a) for a in self.stack]))

        template = (ROOT / "template.html").read_text(encoding="utf-8")
        Static().feed(template[template.index("<main>"):template.index('<div class="card" id="cardview">')])
        result = self.page(data(codex=CODEX), language="zh-CN", storage={"cu.style": "profile"})
        shown = result["elements"]

        def key(attrs):
            if attrs.get("id"):
                return attrs["id"]
            data_attr = next(((k, v) for k, v in attrs.items() if k.startswith("data-")), None)
            return data_attr and "selector:[%s=%s]" % data_attr

        self.assertTrue(found)
        for text, stack in found:
            if any(shown.get(a.get("id"), {}).get("hidden") for a in stack if a.get("id")):
                continue  # inside something the Profile view hides
            if re.fullmatch(r"Claude Code|Codex", text):
                continue  # product names
            el = shown.get(key(stack[-1]))
            self.assertIsNotNone(el, f"{text!r} is never rewritten for Chinese")
            self.assertNotRegex(re.sub(r"Token", "", el["text"]), r"[A-Za-z]{2,}", text)

    def test_profile_language_button_picks_auto_english_or_chinese(self):
        button = lambda r: r["elements"]["language"]["text"]
        profile = {"cu.style": "profile"}
        auto_zh = self.page(language="zh-CN", storage=profile)
        self.assertEqual((button(auto_zh), self.stats(auto_zh)[0][1]), ("🌐\ufe0e 自动", "累计 Token 数"))
        auto_en = self.page(language="en-US", storage=profile)
        self.assertEqual((button(auto_en), self.stats(auto_en)[0][1]), ("🌐\ufe0e Auto", "Total tokens"))
        english = self.page(language="zh-CN", storage={**profile, "cu.lang": "en"})
        self.assertEqual((button(english), self.stats(english)[0][1]), ("🌐\ufe0e English", "Total tokens"))
        self.assertEqual((english["lang"], english["title"]), ("en", "Claude Code Usage"))
        self.assertNotRegex(self.words(english), r"[\u4e00-\u9fff]")
        chinese = self.page(language="en-US", storage={**profile, "cu.lang": "zh"})
        self.assertEqual((button(chinese), self.stats(chinese)[0][1]), ("🌐\ufe0e 中文", "累计 Token 数"))
        self.assertEqual((chinese["lang"], chinese["title"]), ("zh-CN", "Claude Code 用量"))
        unknown = self.page(language="en-US", storage={**profile, "cu.lang": "fr"})
        self.assertEqual(button(unknown), "🌐\ufe0e Auto")
        # Each click moves Auto -> English -> 中文 -> Auto and is remembered.
        for clicks, stored, label in ((1, "en", "Total tokens"), (2, "zh", "累计 Token 数"), (3, "auto", "Total tokens")):
            result = self.page(language="en-US", storage=profile, clicks=[("language", {})] * clicks)
            self.assertEqual((result["storage"]["cu.lang"], self.stats(result)[0][1]), (stored, label), clicks)

    def test_cycle_buttons_tell_screen_readers_their_setting(self):
        said = lambda r, b: r["elements"][b]["attrs"].get("aria-label")
        profile = {"cu.style": "profile"}
        zh = self.page(language="zh-CN", storage=profile)
        self.assertEqual(said(zh, "language"), "语言：自动，点击切换")
        self.assertEqual(said(zh, "appearance"), "外观：自动，点击切换")
        en = self.page(language="en-US", storage={**profile, "cu.lang": "en", "cu.appearance": "dark"})
        self.assertEqual(said(en, "language"), "Language: English, click to change")
        self.assertEqual(said(en, "appearance"), "Appearance: Dark, click to change")
        picked = self.page(language="en-US", storage={**profile, "cu.lang": "zh"})
        self.assertEqual(said(picked, "language"), "语言：中文，点击切换")
        classic = self.page(language="zh-CN", storage={"cu.appearance": "light"})
        self.assertEqual(said(classic, "appearance"), "Appearance: Light, click to change")
        # Neither button claims to be a pressed toggle.
        template = (ROOT / "template.html").read_text(encoding="utf-8")
        self.assertNotRegex(template, r'id="(language|appearance)"[^>]*aria-pressed')
        self.assertNotIn("aria-pressed", zh["elements"]["language"]["attrs"])

    def test_unknown_stored_appearance_falls_back_to_auto(self):
        for style in ("card", "profile"):
            for value in ("system", "constructor", "toString", "__proto__"):
                result = self.page(storage={"cu.appearance": value, "cu.style": style, "cu.lang": value})
                self.assertEqual(result["elements"]["appearance"]["text"], "◐ Auto", (style, value))
                self.assertEqual(result["elements"]["appearance"]["attrs"]["aria-label"],
                                 "Appearance: Auto, click to change")

    def test_classic_hides_the_language_button_and_stays_english(self):
        classic = self.page(language="en-US", storage={"cu.lang": "zh"})
        self.assertTrue(classic["elements"]["langseg"]["hidden"])
        self.assertNotRegex(self.words(classic), r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
        self.assertFalse(self.page(storage={"cu.style": "profile"})["elements"]["langseg"]["hidden"])

    def test_the_tab_title_names_the_source(self):
        both = data(codex=CODEX)
        for language, style, claude, codex in (("en-US", "card", "Claude Code Usage", "Codex Usage"),
                                               ("zh-CN", "profile", "Claude Code 用量", "Codex 用量")):
            store = {"cu.style": style}
            self.assertEqual(self.page(both, language=language, storage=store)["title"], claude)
            self.assertEqual(self.page(both, language=language, storage={**store, "cu.source": "codex"})["title"], codex)
            switched = self.page(both, language=language, storage=store, clicks=[("sources", {"source": "codex"})])
            self.assertEqual(switched["title"], codex)

    def test_chinese_tips_use_a_chinese_colon(self):
        tips = self.tips(self.page(language="zh-CN", storage={"cu.style": "profile"}), "year")
        self.assertEqual(tips[-1], "1月1日：3001 Token")

    def test_profile_hides_the_theme_switch_and_ignores_the_theme(self):
        result = self.page(storage={"cu.theme": "blue", "cu.style": "profile"}, system_dark=True)
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
        # What build.py writes then: no history at all.
        empty = data(claude=False, days={}, hours={}, models={}, tasks={}, totals={"sessions": 0, "messages": 0})
        result = self.page(empty)
        self.assertEqual(result["elements"]["title"]["text"], "Claude Code usage")
        self.assertEqual((self.tiles(result)["Sessions"], self.tiles(result)["Messages"]), ("0", "0"))

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
