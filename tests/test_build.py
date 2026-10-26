"""Parsing, aggregation, incremental rebuilds, and atomic output contracts."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.fixtures import Fixture, MODEL, OTHER_MODEL, fixed_local_time, row


class BuildCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-usage-test-")
        self.addCleanup(self.temp.cleanup)
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)
        self.fixture = Fixture(self.temp.name)
        self.build = self.fixture.load_build()
        self.resources.enter_context(fixed_local_time(self.build))

    def run_build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.build.main()
        html = Path(self.build.OUT).read_text(encoding="utf-8")
        self.assertNotIn("/*__DATA__*/null", html)
        # Decode the actual JS assignment; also verify its terminator so trailing junk fails.
        start = html.index("const DATA = ") + len("const DATA = ")
        data, end = json.JSONDecoder().raw_decode(html[start:])
        self.assertTrue(html[start + end:].startswith(";\n"))
        self.assertIsInstance(data, dict)
        return data

    def only(self, rows, name="only.jsonl"):
        self.fixture.clear_transcripts()
        return self.fixture.write_transcript(name, rows)

    def cache(self):
        return json.loads(Path(self.build.CACHE).read_text(encoding="utf-8"))


class ParsingTests(BuildCase):
    def test_config_and_all_outputs_are_inside_temporary_directory(self):
        for name in ("BASE", "ROOT", "STATS", "CACHE", "OUT", "TEMPLATE"):
            with self.subTest(path=name):
                self.assertEqual(os.path.commonpath([getattr(self.build, name), self.temp.name]), self.temp.name)
        self.assertEqual(self.build.BASE, str(self.fixture.config))

    def test_epoch_accepts_z_offsets_and_fractional_seconds(self):
        self.assertEqual(self.build.epoch("2026-01-03T00:00:00Z"), 1767398400)
        self.assertEqual(self.build.epoch("2026-01-02T19:00:00-05:00"), 1767398400)
        self.assertEqual(self.build.epoch("2026-01-03T00:00:00.125+00:00"), 1767398400.125)

    def test_local_day_changes_at_local_midnight(self):
        self.assertEqual(self.build.local_day(self.build.epoch("2026-01-03T04:59:59Z")), "2026-01-02")
        self.assertEqual(self.build.local_day(self.build.epoch("2026-01-03T05:00:00Z")), "2026-01-03")

    def test_dedup_takes_each_count_maximum_within_one_file(self):
        parsed = self.build.parse(self.fixture.recent)
        self.assertEqual(len(parsed["r"]), 1)
        self.assertEqual(parsed["r"]["response|request"][4:], [15, 20, 35, 40])
        # Distinct transcript UUIDs are messages even when they share an API response.
        self.assertIn("reply-a", parsed["w"])
        self.assertIn("reply-b", parsed["w"])

    def test_response_key_includes_both_message_and_request_id(self):
        path = self.only([
            row("a", "2026-01-03T01:00:00Z", kind="assistant", counts=(1, 2, 3, 4),
                message_id="same", request_id="first"),
            row("b", "2026-01-03T01:00:00Z", kind="assistant", counts=(5, 6, 7, 8),
                message_id="same", request_id="second"),
            row("c", "2026-01-03T01:00:00Z", kind="assistant", counts=(9, 0, 0, 0),
                message_id="other", request_id="first"),
        ])
        self.assertEqual(set(self.build.parse(path)["r"]), {"same|first", "same|second", "other|first"})

    def test_duplicate_uuid_counts_once(self):
        path = self.only([row("same", "2026-01-03T01:00:00Z")] * 2)
        self.assertEqual(len(self.build.parse(path)["w"]), 1)
        self.assertEqual(self.run_build()["totals"]["messages"], 101)

    def test_sidechains_have_messages_and_tokens_but_no_session_span(self):
        parsed = self.build.parse(self.fixture.sidechain)
        self.assertEqual(parsed["s"], {})
        self.assertEqual(len(parsed["w"]), 2)
        self.assertEqual(parsed["r"]["side-answer|side-answer"][2], 1)
        self.assertTrue(all(values[2] == 1 for values in parsed["w"].values()))

    def test_meta_and_synthetic_rows_still_extend_main_session(self):
        path = self.only([
            row("meta", "2026-01-03T01:00:00Z", isMeta=True),
            row("synthetic", "2026-01-03T04:00:00Z", kind="assistant",
                counts=(100, 100, 100, 100), model="<synthetic>"),
        ])
        parsed = self.build.parse(path)
        self.assertEqual(parsed["r"], {})
        self.assertEqual(set(parsed["w"]), {"synthetic"})
        self.assertEqual(parsed["s"]["new"][1] - parsed["s"]["new"][0], 10800)

    def test_meta_assistant_usage_is_included_and_uuidless_rows_extend_spans(self):
        path = self.only([
            row(None, "2026-01-03T00:30:00Z"),
            row("meta", "2026-01-03T01:00:00Z", kind="assistant", counts=(1, 2, 3, 4), isMeta=True),
        ])
        parsed = self.build.parse(path)
        self.assertEqual(parsed["w"], {})
        self.assertEqual(parsed["r"]["meta|meta"][4:], [1, 2, 3, 4])
        self.assertEqual(parsed["s"]["new"][1] - parsed["s"]["new"][0], 1800)

    def test_missing_model_and_usage_fields_default_to_question_mark_and_zero(self):
        item = row("answer", "2026-01-03T01:00:00Z", kind="assistant", counts=(2,))
        item["message"].pop("model")
        item["message"]["usage"]["output_tokens"] = None
        path = self.only([item])
        self.assertEqual(self.build.parse(path)["r"]["answer|answer"][3:], ["?", 2, 0, 0, 0])

    def test_no_usage_empty_message_and_nonassistant_usage_have_no_replies(self):
        path = self.only([
            row("user", "2026-01-03T01:00:00Z", counts=(1, 2, 3, 4)),
            row("empty", "2026-01-03T01:00:00Z", kind="assistant", message={}),
            row("null", "2026-01-03T01:00:00Z", kind="assistant", message=None),
        ])
        self.assertEqual(self.build.parse(path)["r"], {})
        self.assertEqual(len(self.build.parse(path)["w"]), 3)

    def test_malformed_and_irrelevant_lines_are_ignored(self):
        path = self.only([
            {"type": "user", "sessionId": "x"},
            {"type": "user", "timestamp": "", "sessionId": "x"},
            {"type": "user", "timestamp": "2026-01-03T00:00:00Z"},
            row("progress", "2026-01-03T00:00:00Z", kind="progress"),
        ])
        with path.open("ab") as stream:
            stream.write(b'not JSON\n{"timestamp": bad}\n\xff\xfe\n')
        self.assertEqual(self.build.parse(path), {"r": {}, "w": {}, "s": {}})

    def test_unsorted_rows_use_minimum_and_maximum_session_timestamps(self):
        path = self.only([row("late", "2026-01-03T04:00:00Z"),
                          row("early", "2026-01-03T01:00:00Z"),
                          row("middle", "2026-01-03T02:00:00Z")])
        self.assertEqual(self.build.parse(path)["s"]["new"],
                         [self.build.epoch("2026-01-03T01:00:00Z"), self.build.epoch("2026-01-03T04:00:00Z")])


class AggregationTests(BuildCase):
    def test_fixture_has_exact_daily_counts_and_token_splits(self):
        data = self.run_build()
        self.assertEqual(data["days"], {
            "2026-01-01": {"m": 40, "tm": {MODEL: 1000}, "sp": None, "sc": 4},
            "2026-01-02": {"m": 68, "tm": {MODEL: 2110, OTHER_MODEL: 510}, "sp": None, "sc": 7},
            "2026-01-03": {"m": 2, "tm": {MODEL: 26}, "sp": [5, 6, 7, 8], "sc": 1},
        })
        self.assertEqual(data["models"], {MODEL: [120, 226, 342, 2448], OTHER_MODEL: [51, 62, 73, 324]})
        self.assertEqual(data["totals"], {"messages": 110, "sessions": 12, "longest": 10800})

    def test_stats_only_retains_utc_dates_totals_and_milliseconds_conversion(self):
        self.fixture.clear_transcripts()
        data = self.run_build()
        self.assertEqual(data["totals"], {"messages": 100, "sessions": 10, "longest": 7200})
        self.assertEqual(data["days"]["2026-01-02"]["m"], 60)
        self.assertEqual(data["days"]["2026-01-02"]["tm"], {MODEL: 2000, OTHER_MODEL: 500})
        self.assertTrue(all(d["sp"] is None for d in data["days"].values()))
        self.assertEqual(data["hours"], {})
        self.assertEqual(data["sessions"], [])

    def test_cutoff_is_end_of_last_computed_utc_day_not_last_transcript(self):
        self.only([row("old", "2026-01-02T23:59:59.999999Z", "old", "assistant", counts=(1000, 0, 0, 0)),
                   row("fresh", "2026-01-03T00:00:00.000001Z", "fresh", "assistant", counts=(7, 0, 0, 0))])
        data = self.run_build()
        self.assertEqual(data["totals"]["messages"], 101)
        self.assertEqual(data["totals"]["sessions"], 11)
        self.assertEqual(data["models"][MODEL], [107, 200, 300, 2400])
        self.assertEqual(data["days"]["2026-01-02"]["m"], 61)

    def test_exact_cutoff_midnight_belongs_to_transcripts(self):
        # lastComputedDate covers Jan 2 only; midnight starting Jan 3 must count.
        self.only([row("boundary", "2026-01-03T00:00:00Z", "boundary", "assistant", counts=(7, 8, 9, 10))])
        data = self.run_build()
        self.assertEqual(data["totals"]["messages"], 101)
        self.assertEqual(data["totals"]["sessions"], 11)
        self.assertEqual(data["models"][MODEL], [107, 208, 309, 2410])

    def test_new_days_and_session_start_dates_use_local_time(self):
        self.only([row("before", "2026-01-03T04:59:59Z", "overnight"),
                   row("after", "2026-01-03T05:00:00Z", "overnight")])
        data = self.run_build()
        self.assertEqual(data["days"]["2026-01-02"]["m"], 61)
        self.assertEqual(data["days"]["2026-01-02"]["sc"], 7)
        self.assertEqual(data["days"]["2026-01-03"], {"m": 1, "tm": {}, "sp": [0, 0, 0, 0], "sc": 0})

    def test_cross_cutoff_session_is_not_counted_twice_but_extends_longest(self):
        self.only([row("start", "2026-01-02T23:00:00Z", "cross"),
                   row("end", "2026-01-03T04:00:00Z", "cross")])
        data = self.run_build()
        self.assertEqual(data["totals"], {"messages": 101, "sessions": 10, "longest": 18000})

    def test_session_spans_merge_across_files_and_projects(self):
        self.only([row("a", "2026-01-03T01:00:00Z", "shared")], "alpha/a.jsonl")
        self.fixture.write_transcript("beta/b.jsonl", [row("b", "2026-01-03T04:00:00Z", "shared")])
        data = self.run_build()
        self.assertEqual(data["totals"]["sessions"], 11)
        self.assertEqual(data["totals"]["longest"], 10800)
        self.assertEqual(data["sessions"], [[self.build.epoch("2026-01-03T01:00:00Z"),
                                            self.build.epoch("2026-01-03T04:00:00Z")]])

    def test_cross_file_response_dedup_uses_componentwise_max_in_either_order(self):
        a = self.only([row("a", "2026-01-03T01:00:00Z", kind="assistant", counts=(10, 2, 30, 4),
                           message_id="same", request_id="same")], "alpha/a.jsonl")
        b = self.fixture.write_transcript("beta/b.jsonl", [
            row("b", "2026-01-03T01:00:00Z", kind="assistant", counts=(1, 20, 3, 40),
                message_id="same", request_id="same")])
        for order in ([str(a), str(b)], [str(b), str(a)]):
            Path(self.build.CACHE).unlink(missing_ok=True)
            with self.subTest(order=order), mock.patch.object(self.build.glob, "glob", return_value=order):
                data = self.run_build()
                self.assertEqual(data["models"][MODEL], [110, 220, 330, 2440])
                self.assertEqual(data["days"]["2026-01-02"]["tm"][MODEL], 2100)
                # Aggregation must not overwrite each file's original cached counts.
                self.assertEqual(self.cache()["files"][str(a)]["r"]["same|same"][4:], [10, 2, 30, 4])
                self.assertEqual(self.cache()["files"][str(b)]["r"]["same|same"][4:], [1, 20, 3, 40])

    def test_build_does_not_modify_source_stats_or_transcripts(self):
        before = {str(path): path.read_bytes() for path in self.fixture.config.rglob("*") if path.is_file()}
        self.run_build()
        self.run_build()
        after = {str(path): path.read_bytes() for path in self.fixture.config.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_duplicate_uuid_across_files_counts_once_in_messages_and_hours(self):
        item = row("same", "2026-01-03T01:00:00Z")
        self.only([item], "a.jsonl")
        self.fixture.write_transcript("b.jsonl", [item])
        data = self.run_build()
        self.assertEqual(data["totals"]["messages"], 101)
        self.assertEqual(sum(data["hours"]["2026-01-02"]), 1)

    def test_hours_include_history_sidechains_and_synthetic_but_exclude_meta(self):
        data = self.run_build()
        expected = {"2026-01-01": [0] * 24, "2026-01-02": [0] * 24, "2026-01-03": [0] * 24}
        expected["2026-01-01"][7:9] = [1, 1]
        for hour, count in ((18, 1), (20, 4), (21, 3), (22, 1)):
            expected["2026-01-02"][hour] = count
        expected["2026-01-03"][1] = 2
        self.assertEqual(data["hours"], expected)

    def test_missing_and_corrupt_stats_fall_back_to_all_transcripts(self):
        stats = Path(self.build.STATS)
        for content in (None, "{bad json"):
            with self.subTest(content=content):
                if content is None:
                    stats.unlink()
                else:
                    stats.write_text(content, encoding="utf-8")
                data = self.run_build()
                self.assertEqual(data["totals"], {"messages": 13, "sessions": 4, "longest": 10800})
                self.assertEqual(data["models"][MODEL], [1019, 1025, 1041, 1047])
                self.assertTrue(all(d["sp"] is not None for d in data["days"].values()))

    def test_empty_config_produces_valid_empty_dashboard(self):
        self.fixture.clear_transcripts()
        Path(self.build.STATS).unlink()
        data = self.run_build()
        for name in ("days", "hours", "models"):
            self.assertEqual(data[name], {})
        self.assertEqual(data["totals"], {"messages": 0, "sessions": 0, "longest": 0})
        self.assertEqual(data["sessions"], [])
        dt.datetime.fromisoformat(data["generatedAt"])
        dt.datetime.strptime(data["today"], "%Y-%m-%d")

    def test_partial_stats_fields_default_and_daily_entries_accumulate(self):
        self.fixture.clear_transcripts()
        self.fixture.write_stats({"dailyActivity": [
            {"date": "2026-01-01", "messageCount": 3},
            {"date": "2026-01-01", "sessionCount": 2}],
            "dailyModelTokens": [{"date": "2026-01-01", "tokensByModel": {MODEL: 5}},
                                 {"date": "2026-01-01", "tokensByModel": {MODEL: 7}},
                                 {"date": "2026-01-02"}],
            "modelUsage": {MODEL: {"inputTokens": 12}}, "longestSession": None})
        data = self.run_build()
        self.assertEqual(data["days"]["2026-01-01"], {"m": 3, "tm": {MODEL: 12}, "sp": None, "sc": 2})
        self.assertIsNone(data["days"]["2026-01-02"]["sp"])
        self.assertEqual(data["models"][MODEL], [12, 0, 0, 0])
        self.assertEqual(data["totals"], {"messages": 0, "sessions": 0, "longest": 0})

    def test_stats_without_last_computed_date_does_not_exclude_transcripts(self):
        self.fixture.stats.pop("lastComputedDate")
        self.fixture.write_stats()
        self.assertEqual(self.run_build()["totals"]["messages"], 113)

    def test_index_contains_valid_json_and_preserves_rest_of_template(self):
        self.run_build()
        template = Path(self.build.TEMPLATE).read_text(encoding="utf-8")
        before, after = template.split("/*__DATA__*/null")
        html = Path(self.build.OUT).read_text(encoding="utf-8")
        self.assertTrue(html.startswith(before))
        self.assertTrue(html.endswith(after))
        payload = html[len(before):-len(after)]
        self.assertEqual(set(json.loads(payload)),
                         {"generatedAt", "today", "days", "hours", "models", "totals", "sessions"})

    def test_command_line_build_uses_config_override_and_script_relative_output(self):
        result = subprocess.run([sys.executable, str(self.fixture.app / "build.py")],
                                cwd=self.fixture.root, env=self.fixture.env(), capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"wrote {self.fixture.app / 'index.html'}", result.stdout)
        self.assertTrue((self.fixture.app / "cache.json").is_file())
        self.assertNotIn("/*__DATA__*/null", (self.fixture.app / "index.html").read_text(encoding="utf-8"))
        self.assertFalse((self.fixture.root / "index.html").exists())


class CacheTests(BuildCase):
    def test_unchanged_files_are_not_reparsed_and_rebuild_is_idempotent(self):
        first = self.run_build()
        with mock.patch.object(self.build, "parse", side_effect=AssertionError("unchanged file reparsed")):
            second = self.run_build()
        first.pop("generatedAt")
        second.pop("generatedAt")
        self.assertEqual(first, second)
        self.assertEqual(len(self.cache()["files"]), 4)
        self.assertEqual(self.cache()["v"], self.build.CACHE_VERSION)

    def test_changed_and_new_transcripts_are_parsed_but_unchanged_are_skipped(self):
        self.run_build()
        with self.fixture.recent.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row("added", "2026-01-03T03:30:00Z")) + "\n")
        added = self.fixture.write_transcript("delta/new.jsonl", [row("extra", "2026-01-03T07:00:00Z", "extra")])
        with mock.patch.object(self.build, "parse", wraps=self.build.parse) as parse:
            data = self.run_build()
        self.assertEqual({call.args[0] for call in parse.call_args_list}, {str(self.fixture.recent), str(added)})
        self.assertEqual(data["totals"]["messages"], 112)

    def test_same_size_edit_within_one_second_invalidates_parse_cache(self):
        path = self.only([row("a", "2026-01-03T01:00:00Z", kind="assistant", counts=(1, 0, 0, 0))])
        base_ns = 1767402000 * 1_000_000_000
        os.utime(path, ns=(base_ns, base_ns + 100_000_000))
        self.run_build()
        previous = path.stat()
        path.write_text(path.read_text(encoding="utf-8").replace('"input_tokens": 1', '"input_tokens": 9'), encoding="utf-8")
        os.utime(path, ns=(base_ns, base_ns + 200_000_000))
        self.assertEqual(previous.st_size, path.stat().st_size)
        self.assertEqual(int(previous.st_mtime), int(path.stat().st_mtime))
        self.assertNotEqual(previous.st_mtime_ns, path.stat().st_mtime_ns)
        with mock.patch.object(self.build, "parse", wraps=self.build.parse) as parse:
            data = self.run_build()
        parse.assert_called_once_with(str(path))
        self.assertEqual(data["models"][MODEL][0], 109)

    def test_deleted_transcripts_still_count_on_later_builds(self):
        first = self.run_build()
        self.fixture.clear_transcripts()
        with mock.patch.object(self.build, "parse", side_effect=AssertionError("deleted file parsed")):
            second = self.run_build()
        for key in ("days", "hours", "models", "totals", "sessions"):
            self.assertEqual(first[key], second[key])
        self.assertEqual(len(self.cache()["files"]), 4)

    def test_version_bump_resets_cache_and_drops_deleted_files(self):
        self.run_build()
        self.fixture.sidechain.unlink()
        with mock.patch.object(self.build, "CACHE_VERSION", self.build.CACHE_VERSION + 1), \
                mock.patch.object(self.build, "parse", wraps=self.build.parse) as parse:
            data = self.run_build()
            self.assertEqual(self.cache()["v"], self.build.CACHE_VERSION)
        self.assertEqual(parse.call_count, 3)
        self.assertNotIn(str(self.fixture.sidechain), self.cache()["files"])
        self.assertEqual(data["totals"]["messages"], 108)
        self.assertEqual(data["models"][OTHER_MODEL], [50, 60, 70, 320])

    def test_corrupt_or_old_cache_is_rebuilt(self):
        for content in ("{invalid", json.dumps({"v": -1, "files": {"bogus": {}}})):
            with self.subTest(content=content):
                Path(self.build.CACHE).write_text(content, encoding="utf-8")
                with mock.patch.object(self.build, "parse", wraps=self.build.parse) as parse:
                    self.assertEqual(self.run_build()["totals"]["messages"], 110)
                self.assertEqual(parse.call_count, 4)
                self.assertNotIn("bogus", self.cache()["files"])

    def test_seconds_precision_cache_migrates_without_losing_deleted_transcripts(self):
        self.run_build()
        cache = self.cache()
        for path, entry in cache["files"].items():
            entry["stamp"] = [Path(path).stat().st_size, int(Path(path).stat().st_mtime)]
        Path(self.build.CACHE).write_text(json.dumps(cache), encoding="utf-8")
        self.fixture.sidechain.unlink()
        with mock.patch.object(self.build, "parse", wraps=self.build.parse) as parse:
            data = self.run_build()
        self.assertEqual(parse.call_count, 3)
        self.assertEqual(data["totals"]["messages"], 110)
        self.assertIn(str(self.fixture.sidechain), self.cache()["files"])
        for path, entry in self.cache()["files"].items():
            if Path(path).exists():
                self.assertEqual(entry["stamp"], [Path(path).stat().st_size, Path(path).stat().st_mtime_ns])

    def test_disappearing_file_during_stat_is_skipped(self):
        real_stat = self.build.os.stat

        def stat(path, *args, **kwargs):
            if str(path) == str(self.fixture.sidechain):
                raise FileNotFoundError(path)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(self.build.os, "stat", side_effect=stat):
            data = self.run_build()
        self.assertEqual(data["totals"]["messages"], 108)
        self.assertNotIn(str(self.fixture.sidechain), self.cache()["files"])

    def test_advancing_stats_cutoff_does_not_add_cached_rows_twice(self):
        self.run_build()
        self.fixture.stats["lastComputedDate"] = "2026-01-03"
        self.fixture.stats["totalMessages"] = 110
        self.fixture.stats["totalSessions"] = 12
        self.fixture.write_stats()
        with mock.patch.object(self.build, "parse", side_effect=AssertionError("unchanged file reparsed")):
            data = self.run_build()
        self.assertEqual(data["totals"]["messages"], 110)
        self.assertEqual(data["totals"]["sessions"], 12)
        self.assertEqual(data["models"][MODEL], [100, 200, 300, 2400])


class IOTests(BuildCase):
    def test_load_json_returns_default_for_missing_invalid_and_unreadable_files(self):
        path = self.fixture.root / "sample.json"
        default = object()
        self.assertIs(self.build.load_json(path, default), default)
        path.write_text("{bad", encoding="utf-8")
        self.assertIs(self.build.load_json(path, default), default)
        with mock.patch("builtins.open", side_effect=PermissionError):
            self.assertIs(self.build.load_json(path, default), default)
        path.write_text('{"ok": [1, 2]}', encoding="utf-8")
        self.assertEqual(self.build.load_json(path, default), {"ok": [1, 2]})

    def test_cache_and_html_are_complete_before_atomic_replace(self):
        cache_path, out_path = Path(self.build.CACHE), Path(self.build.OUT)
        cache_path.write_text("old cache", encoding="utf-8")
        out_path.write_text("old page", encoding="utf-8")
        real_replace = self.build.os.replace
        replaced = []

        def inspect(src, dst):
            self.assertEqual(src, dst + ".tmp")
            self.assertEqual(Path(src).parent, Path(dst).parent)
            text = Path(src).read_text(encoding="utf-8")
            if dst == self.build.CACHE:
                self.assertEqual(cache_path.read_text(encoding="utf-8"), "old cache")
                self.assertEqual(json.loads(text)["v"], self.build.CACHE_VERSION)
            else:
                self.assertEqual(out_path.read_text(encoding="utf-8"), "old page")
                self.assertIn("const DATA = {", text)
                self.assertNotIn("/*__DATA__*/null", text)
                self.assertTrue(text.endswith("</html>\n"))
            replaced.append(dst)
            real_replace(src, dst)

        with mock.patch.object(self.build.os, "replace", side_effect=inspect):
            self.run_build()
        self.assertEqual(replaced, [self.build.CACHE, self.build.OUT])
        self.assertFalse(Path(self.build.CACHE + ".tmp").exists())
        self.assertFalse(Path(self.build.OUT + ".tmp").exists())

    def test_failed_cache_replace_preserves_previous_files(self):
        Path(self.build.CACHE).write_text("old cache", encoding="utf-8")
        Path(self.build.OUT).write_text("old page", encoding="utf-8")
        with mock.patch.object(self.build, "replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.run_build()
        self.assertEqual(Path(self.build.CACHE).read_text(encoding="utf-8"), "old cache")
        self.assertEqual(Path(self.build.OUT).read_text(encoding="utf-8"), "old page")
        self.assertEqual(json.loads(Path(self.build.CACHE + ".tmp").read_text(encoding="utf-8"))["v"], self.build.CACHE_VERSION)

    def test_failed_output_replace_preserves_previous_page(self):
        Path(self.build.OUT).write_text("old page", encoding="utf-8")
        real_replace = self.build.replace

        def replace(src, dst):
            if dst == self.build.OUT:
                raise OSError("disk error")
            real_replace(src, dst)

        with mock.patch.object(self.build, "replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.run_build()
        self.assertEqual(Path(self.build.OUT).read_text(encoding="utf-8"), "old page")
        self.assertEqual(self.cache()["v"], self.build.CACHE_VERSION)
        self.assertIn("const DATA = {", Path(self.build.OUT + ".tmp").read_text(encoding="utf-8"))

    def test_replace_retries_permission_errors_then_succeeds(self):
        with mock.patch.object(self.build.os, "replace", side_effect=[PermissionError, PermissionError, None]) as replace, \
                mock.patch.object(self.build.time, "sleep") as sleep:
            self.build.replace("source", "target")
        self.assertEqual(replace.call_args_list, [mock.call("source", "target")] * 3)
        self.assertEqual(sleep.call_args_list, [mock.call(0.1)] * 2)

    def test_replace_exhausts_twenty_attempts_and_raises(self):
        with mock.patch.object(self.build.os, "replace", side_effect=PermissionError("locked")) as replace, \
                mock.patch.object(self.build.time, "sleep") as sleep:
            with self.assertRaisesRegex(PermissionError, "locked"):
                self.build.replace("source", "target")
        self.assertEqual(replace.call_count, 20)
        self.assertEqual(sleep.call_count, 19)

    def test_replace_does_not_retry_other_errors(self):
        with mock.patch.object(self.build.os, "replace", side_effect=OSError("full")) as replace, \
                mock.patch.object(self.build.time, "sleep") as sleep:
            with self.assertRaisesRegex(OSError, "full"):
                self.build.replace("source", "target")
        replace.assert_called_once_with("source", "target")
        sleep.assert_not_called()
