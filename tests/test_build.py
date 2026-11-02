"""Parsing, aggregation, incremental rebuilds, and atomic output contracts."""
import contextlib
import datetime as dt
import getpass
import io
import json
import os
from pathlib import Path
import re
import sqlite3
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
        start = html.index("const ALL = ") + len("const ALL = ")
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
        for name in ("BASE", "ROOT", "STATS", "CODEX", "CACHE", "OUT", "TEMPLATE"):
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
        parsed = self.build.parse(path)
        self.assertEqual((parsed["r"], parsed["w"], parsed["s"]), ({}, {}, {}))

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
        self.assertEqual(data["totals"], {"messages": 110, "sessions": 12})

    def test_stats_only_retains_utc_dates_and_totals(self):
        self.fixture.clear_transcripts()
        data = self.run_build()
        self.assertEqual(data["totals"], {"messages": 100, "sessions": 10})
        self.assertEqual(data["days"]["2026-01-02"]["m"], 60)
        self.assertEqual(data["days"]["2026-01-02"]["tm"], {MODEL: 2000, OTHER_MODEL: 500})
        self.assertTrue(all(d["sp"] is None for d in data["days"].values()))
        self.assertEqual(data["hours"], {})
        self.assertEqual(data["tasks"], {})

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

    def test_cross_cutoff_session_is_not_counted_twice(self):
        self.only([row("start", "2026-01-02T23:00:00Z", "cross"),
                   row("end", "2026-01-03T04:00:00Z", "cross")])
        data = self.run_build()
        self.assertEqual(data["totals"], {"messages": 101, "sessions": 10})

    def test_session_spans_merge_across_files_and_projects(self):
        self.only([row("a", "2026-01-03T01:00:00Z", "shared")], "alpha/a.jsonl")
        self.fixture.write_transcript("beta/b.jsonl", [row("b", "2026-01-03T04:00:00Z", "shared")])
        data = self.run_build()
        self.assertEqual(data["totals"]["sessions"], 11)

    def test_cross_file_response_dedup_uses_componentwise_max_in_either_order(self):
        a = self.only([row("a", "2026-01-03T01:00:00Z", kind="assistant", counts=(10, 2, 30, 4),
                           message_id="same", request_id="same")], "alpha/a.jsonl")
        b = self.fixture.write_transcript("beta/b.jsonl", [
            row("b", "2026-01-03T01:00:00Z", kind="assistant", counts=(1, 20, 3, 40),
                message_id="same", request_id="same")])
        for order in ([str(a), str(b)], [str(b), str(a)]):
            Path(self.build.CACHE).unlink(missing_ok=True)
            with self.subTest(order=order), mock.patch.object(self.build.glob, "glob", side_effect=lambda pattern, **_: order if "projects" in pattern else []):
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
                self.assertEqual(data["totals"], {"messages": 13, "sessions": 4})
                self.assertEqual(data["models"][MODEL], [1019, 1025, 1041, 1047])
                self.assertTrue(all(d["sp"] is not None for d in data["days"].values()))

    def test_empty_config_produces_valid_empty_dashboard(self):
        self.fixture.clear_transcripts()
        Path(self.build.STATS).unlink()
        data = self.run_build()
        for name in ("days", "hours", "models"):
            self.assertEqual(data[name], {})
        self.assertEqual(data["totals"], {"messages": 0, "sessions": 0})
        self.assertEqual(data["tasks"], {})
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
        self.assertEqual(data["totals"], {"messages": 0, "sessions": 0})

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
        self.assertEqual(json.loads(payload)["user"], getpass.getuser())
        self.assertEqual(set(json.loads(payload)),
                         {"generatedAt", "today", "user", "days", "hours", "models", "totals", "tasks", "codex", "claude"})

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

    def test_longest_task_runs_from_prompt_to_last_row_and_ignores_idle_gaps(self):
        def text(uuid, ts, content):
            return {**row(uuid, ts, "task"), "message": {"content": content}}
        result = [{"type": "tool_result", "content": "ok"}]
        path = self.fixture.write_transcript("task/a.jsonl", [
            text("p1", "2026-01-03T01:00:00Z", "first"),
            row("a1", "2026-01-03T01:10:00Z", "task", "assistant"),
            text("r1", "2026-01-03T01:20:00Z", result),
            row("a2", "2026-01-03T01:40:00Z", "task", "assistant"),
            text("p2", "2026-01-03T02:00:00Z", "second"),
            row("a3", "2026-01-03T02:05:00Z", "task", "assistant"),
            row("a4", "2026-01-03T09:00:00Z", "task", "assistant"),
        ])
        start, last, best = self.build.parse(str(path))["k"]["task"]
        self.assertEqual(max(best.values()), 2400)
        self.assertEqual(last - start, 0)

    def test_notes_added_by_claude_code_do_not_start_a_task(self):
        def text(uuid, ts, **extra):
            return {**row(uuid, ts, "notes", **extra), "message": {"content": "note"}}
        self.only([text("a", "2026-01-03T01:00:00Z"), text("b", "2026-01-03T01:10:00Z", isMeta=True),
                   text("c", "2026-01-03T01:20:00Z", isCompactSummary=True)])
        self.assertEqual(list(self.run_build()["tasks"].values()), [1200])

    def test_a_task_past_midnight_stays_on_the_day_it_started(self):
        def text(uuid, stamp):
            return {**row(uuid, stamp, "late"), "message": {"content": "go"}}
        # 23:50 to 00:10 in the fixed local time of run_build, UTC-5.
        self.only([text("a", "2026-01-04T04:50:00Z"), row("b", "2026-01-04T05:10:00Z", "late"),
                   text("c", "2026-01-04T05:15:00Z")])
        self.assertEqual(self.run_build()["tasks"], {"2026-01-03": 1200})

    def test_a_gap_over_thirty_minutes_ends_a_task(self):
        self.only([row("a", "2026-01-03T01:00:00Z", "gap"), row("b", "2026-01-03T01:29:00Z", "gap"),
                   row("c", "2026-01-03T02:00:00Z", "gap"), row("d", "2026-01-03T02:05:00Z", "gap")])
        self.assertEqual(list(self.run_build()["tasks"].values()), [1740])

    def test_longest_tasks_keeps_the_best_of_a_day_across_sessions(self):
        start = self.build.epoch("2026-01-03T12:00:00Z")
        entries = [{"k": {"a": [start, start + 500, {}]}}, {"k": {"b": [start, start + 200, {}]}}]
        self.assertEqual(list(self.build.longest_tasks(entries).values()), [500])

    def test_a_grown_transcript_is_resumed_not_read_again(self):
        path = self.only([row("a", "2026-01-03T01:00:00Z", "grow")])
        self.run_build()
        self.write_rows(path, [row("b", "2026-01-03T01:01:00Z", "grow")])
        with mock.patch.object(self.build, "parse", wraps=self.build.parse) as parse:
            self.run_build()
        self.assertIsNotNone(parse.call_args.args[1])

    def test_a_grown_file_from_an_older_cache_is_read_from_the_start(self):
        path = self.only([row("a", "2026-01-03T01:00:00Z", "grow")])
        self.run_build()
        cache = json.loads(Path(self.build.CACHE).read_text(encoding="utf-8"))
        for entry in cache["files"].values():
            del entry["k"]
        Path(self.build.CACHE).write_text(json.dumps(cache), encoding="utf-8")
        self.write_rows(path, [row("b", "2026-01-03T01:01:00Z", "grow")])
        self.assertEqual(list(self.run_build()["tasks"].values()), [60])

    def test_old_cache_entries_are_reparsed_once_for_tasks(self):
        self.run_build()
        cache = json.loads(Path(self.build.CACHE).read_text(encoding="utf-8"))
        for entry in cache["files"].values():
            del entry["k"]
        Path(self.build.CACHE).write_text(json.dumps(cache), encoding="utf-8")
        with mock.patch.object(self.build, "parse", wraps=self.build.parse) as parse:
            self.run_build()
        self.assertEqual(parse.call_count, len(cache["files"]))
        with mock.patch.object(self.build, "parse", wraps=self.build.parse) as parse:
            self.run_build()
        parse.assert_not_called()

    def write_rows(self, path, rows, mode="a"):
        with Path(path).open(mode, encoding="utf-8") as stream:
            stream.write("".join(json.dumps(item) + "\n" for item in rows))

    def test_grown_transcript_is_read_from_where_it_stopped(self):
        path = self.fixture.write_transcript("grow/a.jsonl", [row("one", "2026-01-03T01:00:00Z", "grow")])
        held = self.build.parse(str(path))
        held["w"]["sentinel"] = [0, "kept", 0]  # Lost if the file were read from the start.
        self.write_rows(path, [row("two", "2026-01-03T02:00:00Z", "grow")])
        parsed = self.build.parse(str(path), held)
        self.assertEqual(set(parsed["w"]), {"one", "two", "sentinel"})
        self.assertEqual(parsed["offset"], path.stat().st_size)
        self.assertEqual(parsed["s"]["grow"][1] - parsed["s"]["grow"][0], 3600)

    def test_grown_reply_keeps_the_largest_counts_across_reads(self):
        path = self.fixture.write_transcript("grow/b.jsonl", [
            row("one", "2026-01-03T01:00:00Z", "grow", "assistant", counts=(1, 9, 1, 1),
                message_id="m", request_id="r")])
        held = self.build.parse(str(path))
        self.write_rows(path, [row("two", "2026-01-03T01:00:01Z", "grow", "assistant", counts=(5, 2, 5, 5),
                                   message_id="m", request_id="r")])
        self.assertEqual(self.build.parse(str(path), held)["r"]["m|r"][4:], [5, 9, 5, 5])

    def test_unfinished_last_line_is_read_again_once_complete(self):
        path = self.fixture.write_transcript("grow/c.jsonl", [row("one", "2026-01-03T01:00:00Z", "grow")])
        whole = json.dumps(row("two", "2026-01-03T02:00:00Z", "grow"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(whole[:20])
        held = self.build.parse(str(path))
        self.assertEqual(set(held["w"]), {"one"})
        with path.open("a", encoding="utf-8") as stream:
            stream.write(whole[20:] + "\n")
        self.assertEqual(set(self.build.parse(str(path), held)["w"]), {"one", "two"})

    def test_rewritten_transcript_is_read_again_from_the_start(self):
        path = self.fixture.write_transcript("grow/d.jsonl", [row("one", "2026-01-03T01:00:00Z", "grow")])
        held = self.build.parse(str(path))
        self.write_rows(path, [row("new", "2026-01-03T02:00:00Z", "other"),
                               row("more", "2026-01-03T03:00:00Z", "other")], mode="w")
        self.assertEqual(set(self.build.parse(str(path), held)["w"]), {"new", "more"})

    def test_appending_through_main_matches_a_full_rebuild(self):
        self.run_build()
        self.write_rows(self.fixture.recent, [row("late", "2026-01-03T04:00:00Z")])
        self.run_build()
        incremental = json.loads(Path(self.build.CACHE).read_text(encoding="utf-8"))["files"]
        Path(self.build.CACHE).unlink()
        self.run_build()
        full = json.loads(Path(self.build.CACHE).read_text(encoding="utf-8"))["files"]
        self.assertEqual(incremental, full)

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
        parse.assert_called_once_with(str(path), None)
        self.assertEqual(data["models"][MODEL][0], 109)

    def test_deleted_transcripts_still_count_on_later_builds(self):
        first = self.run_build()
        self.fixture.clear_transcripts()
        with mock.patch.object(self.build, "parse", side_effect=AssertionError("deleted file parsed")):
            second = self.run_build()
        for key in ("days", "hours", "models", "totals", "tasks", "codex"):
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


class CodexTests(BuildCase):
    def event(self, ts, kind, payload):
        return {"timestamp": ts, "type": kind, "payload": payload}

    def tokens(self, ts, spent, cached, output):
        usage = {"input_tokens": spent, "cached_input_tokens": cached, "output_tokens": output}
        return self.event(ts, "event_msg", {"type": "token_count", "info": {"total_token_usage": usage}})

    def session(self, thread="one", source="user"):
        return [
            self.event("2026-01-03T01:00:00Z", "session_meta", {"id": thread, "thread_source": source}),
            self.event("2026-01-03T01:00:01Z", "event_msg", {"type": "task_started"}),
            self.event("2026-01-03T01:00:02Z", "turn_context", {"model": "gpt-6.1-sol"}),
            self.event("2026-01-03T01:00:03Z", "response_item", {"type": "message", "role": "user"}),
            self.event("2026-01-03T01:00:04Z", "response_item", {"type": "message", "role": "developer"}),
            self.tokens("2026-01-03T01:10:00Z", 100, 40, 10),
            self.tokens("2026-01-03T01:20:00Z", 300, 140, 30),
            self.event("2026-01-03T01:20:01Z", "response_item", {"type": "message", "role": "assistant"}),
            self.event("2026-01-03T01:20:01Z", "event_msg", {"type": "task_complete", "duration_ms": 900000}),
        ]

    def write_session(self, rows, name="one.jsonl"):
        path = self.fixture.codex / "sessions" / "2026" / "01" / "03" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
        return path

    def write_threads(self, rows):
        self.fixture.codex.mkdir(exist_ok=True)
        db = sqlite3.connect(self.fixture.codex / "state_5.sqlite")
        db.execute("CREATE TABLE threads (id TEXT, created_at INT, tokens_used INT, model TEXT, thread_source TEXT,"
                   " updated_at INT)")
        db.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", [(*row, row[1]) for row in rows])
        db.commit()
        db.close()

    def test_no_codex_folder_means_no_codex_view(self):
        self.assertIsNone(self.run_build()["codex"])

    def test_session_counts_tokens_from_running_totals_and_skips_other_roles(self):
        self.write_session(self.session())
        codex = self.run_build()["codex"]
        self.assertEqual(codex["models"], {"gpt-6.1-sol": [160, 30, 0, 140]})
        self.assertEqual(codex["totals"], {"sessions": 1, "messages": 2})
        day = next(iter(codex["days"].values()))
        self.assertEqual(day, {"m": 2, "tm": {"gpt-6.1-sol": 330}, "sp": [160, 30, 0, 140], "sc": 1})
        self.assertEqual(list(codex["tasks"].values()), [900])

    def test_automatic_reviews_are_left_out(self):
        self.write_session(self.session("review", "guardian_review"))
        self.assertIsNone(self.run_build()["codex"])

    def test_reviews_marked_only_by_their_source_are_left_out_too(self):
        rows = self.session("review", "subagent")
        rows[0]["payload"]["source"] = {"subagent": {"other": "guardian"}}
        self.write_session(rows)
        self.assertIsNone(self.run_build()["codex"])

    def test_per_response_records_replace_running_totals_and_days_are_utc(self):
        record = self.event("2026-01-03T01:30:00Z", "token_usage_record", {
            "usage": {"input_tokens": 50, "cached_input_tokens": 20, "cache_write_input_tokens": 5, "output_tokens": 7}})
        self.write_session([*self.session()[:3], record, self.tokens("2026-01-03T01:30:01Z", 900, 0, 900)])
        codex = self.run_build()["codex"]
        self.assertEqual(codex["models"], {"gpt-6.1-sol": [25, 7, 5, 20]})
        self.assertEqual(list(codex["days"]), ["2026-01-03"])

    def test_threads_without_token_rows_fill_in_from_the_database(self):
        self.write_session(self.session())
        created = int(self.build.epoch("2026-01-01T12:00:00Z"))
        self.write_threads([("one", created, 330, "gpt-6.1-sol", "user"),
                            ("gone", created, 660, "gpt-6.1-sol", "user"),
                            ("review", created, 33, "codex-auto-review", "guardian_review"),
                            ("empty", created, 0, "gpt-6.1-sol", "user")])
        codex = self.run_build()["codex"]
        self.assertEqual(codex["totals"]["sessions"], 3)
        self.assertEqual(sum(codex["models"]["gpt-6.1-sol"]), 990)
        self.assertNotIn("codex-auto-review", codex["models"])
        history = [d for d in codex["days"].values() if d["sp"] is None]
        self.assertEqual(len(history), 1)
        self.assertTrue(codex["estimated"])
        self.assertEqual(history[0]["tm"], {"gpt-6.1-sol": 660})

    def test_a_lost_threads_total_is_shared_over_days_with_other_activity(self):
        self.write_session(self.session())
        self.write_session(json.loads(json.dumps(self.session("two")).replace("2026-01-03", "2026-01-04")), "two.jsonl")
        start = int(self.build.epoch("2026-01-02T12:00:00Z"))
        self.write_threads([("gone", start, 300, "gpt-6.1-sol", "user")])
        db = sqlite3.connect(self.fixture.codex / "state_5.sqlite")
        db.execute("UPDATE threads SET updated_at = ?", (start + 4 * 86400,))
        db.commit()
        db.close()
        days = self.run_build()["codex"]["days"]
        self.assertEqual({k: v["tm"]["gpt-6.1-sol"] for k, v in days.items() if v["sp"] is None},
                         {"2026-01-03": 480, "2026-01-04": 480})
        self.assertEqual(days["2026-01-02"]["tm"], {})

    def test_a_grown_session_only_adds_the_new_tokens_and_messages(self):
        rows = self.session()
        path = self.write_session(rows[:6])
        partial = self.run_build()["codex"]
        self.assertEqual(sum(partial["models"]["gpt-6.1-sol"]), 110)
        os.utime(path, None)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(item) + "\n" for item in rows[6:]))
        codex = self.run_build()["codex"]
        self.assertEqual(sum(codex["models"]["gpt-6.1-sol"]), 330)
        self.assertEqual(codex["totals"]["messages"], 2)
        self.assertEqual(list(codex["tasks"].values()), [900])

    def test_a_half_written_last_line_is_read_once_it_is_complete(self):
        rows = self.session()
        path = self.write_session(rows[:6])
        line = json.dumps(rows[6])
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line[:20])
        self.run_build()
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line[20:] + "\n")
        self.assertEqual(sum(self.run_build()["codex"]["models"]["gpt-6.1-sol"]), 330)

    def test_a_grown_session_is_resumed_not_read_again(self):
        rows = self.session()
        path = self.write_session(rows[:6])
        self.run_build()
        with path.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(item) + "\n" for item in rows[6:]))
        cache = self.cache()
        next(iter(cache["codex"].values()))["m"]["2000-01-01"] = 7  # Only survives if the entry is kept.
        Path(self.build.CACHE).write_text(json.dumps(cache), encoding="utf-8")
        self.assertEqual(self.run_build()["codex"]["days"]["2000-01-01"]["m"], 7)

    def test_a_task_keeps_the_longest_run_of_its_start_day(self):
        started = int(self.build.epoch("2026-01-03T23:50:00Z"))
        done = {"type": "task_complete", "started_at": started}
        self.write_session([*self.session()[:3],
                            self.event("2026-01-04T00:10:00Z", "event_msg", {**done, "duration_ms": 1200000}),
                            self.event("2026-01-04T00:20:00Z", "event_msg", {**done, "duration_ms": 300000})])
        self.assertEqual(self.run_build()["codex"]["tasks"], {"2026-01-03": 1200})

    def test_a_resumed_session_stays_on_the_day_it_started(self):
        rows = self.session()
        self.write_session([*rows, self.event("2026-01-05T01:00:00Z", "session_meta", {"id": "one", "thread_source": "user"})])
        days = self.run_build()["codex"]["days"]
        self.assertEqual(days["2026-01-03"]["sc"], 1)
        self.assertNotIn("2026-01-05", days)

    def test_a_file_with_messages_but_no_token_rows_shares_its_total_by_message(self):
        rows = [row for row in self.session() if row["type"] != "event_msg" or row["payload"]["type"] != "token_count"]
        later = self.event("2026-01-04T01:00:00Z", "response_item", {"type": "message", "role": "user"})
        self.write_session([*rows, later])
        self.write_threads([("one", int(self.build.epoch("2026-01-03T01:00:00Z")), 300, "gpt-6.1-sol", "user")])
        days = self.run_build()["codex"]["days"]
        self.assertEqual({k: v["tm"] for k, v in days.items()},
                         {"2026-01-03": {"gpt-6.1-sol": 200}, "2026-01-04": {"gpt-6.1-sol": 100}})

    def test_the_newest_state_database_wins(self):
        self.write_threads([("new", 1767400000, 70, "gpt-6.1-sol", "user")])
        old = self.fixture.codex / "state_1.sqlite"
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE threads (id TEXT, created_at INT, tokens_used INT, model TEXT, thread_source TEXT)")
        db.execute("INSERT INTO threads VALUES ('old', 1767400000, 900, 'gpt-6.1-sol', 'user')")
        db.commit()
        db.close()
        os.utime(old, (1000, 1000))
        self.assertEqual(sum(self.run_build()["codex"]["models"]["gpt-6.1-sol"]), 70)

    def test_a_damaged_state_database_leaves_the_session_files_counted(self):
        self.write_session(self.session())
        (self.fixture.codex / "state_5.sqlite").write_bytes(b"not a database")
        self.assertEqual(sum(self.run_build()["codex"]["models"]["gpt-6.1-sol"]), 330)

    def test_today_is_the_utc_date(self):
        self.write_session(self.session())
        with mock.patch.object(self.build.time, "time", return_value=self.build.epoch("2026-01-04T02:00:00Z")):
            self.assertEqual(self.run_build()["codex"]["today"], "2026-01-04")

    def test_running_totals_do_not_double_a_response_that_also_has_a_record(self):
        record = self.event("2026-01-03T01:30:01Z", "token_usage_record", {"usage": {"input_tokens": 100, "output_tokens": 10}})
        self.write_session([*self.session()[:3], self.tokens("2026-01-03T01:30:00Z", 100, 0, 10), record])
        self.assertEqual(self.run_build()["codex"]["models"], {"gpt-6.1-sol": [100, 10, 0, 0]})

    def test_a_file_with_records_ignores_its_running_totals_on_every_day(self):
        record = self.event("2026-01-03T01:30:01Z", "token_usage_record", {"usage": {"input_tokens": 100}})
        self.write_session([*self.session()[:3], self.tokens("2026-01-02T01:30:00Z", 900, 0, 0), record])
        self.assertEqual(list(self.run_build()["codex"]["days"]), ["2026-01-03"])

    def test_a_running_total_that_resets_adds_nothing_until_it_grows_again(self):
        self.write_session([*self.session()[:3], self.tokens("2026-01-03T01:10:00Z", 100, 0, 0),
                            self.tokens("2026-01-03T01:20:00Z", 50, 0, 0), self.tokens("2026-01-03T01:30:00Z", 120, 0, 0)])
        self.assertEqual(self.run_build()["codex"]["models"]["gpt-6.1-sol"][0], 170)

    def test_more_cached_than_input_never_counts_negative(self):
        self.write_session([*self.session()[:3], self.tokens("2026-01-03T01:10:00Z", 10, 40, 5)])
        self.assertEqual(self.run_build()["codex"]["models"]["gpt-6.1-sol"], [0, 5, 0, 40])

    def test_odd_rows_are_skipped_not_fatal(self):
        odd = [self.tokens("not a date", 5, 0, 5), {"timestamp": 7, "type": "event_msg", "payload": {}}, ["list"], "text",
               self.event("2026-01-03T01:10:00Z", "token_usage_record",
                          {"usage": {"input_tokens": 9, "cached_input_tokens": None, "output_tokens": None}})]
        self.write_session([*self.session()[:3], *odd])
        self.assertEqual(self.run_build()["codex"]["models"]["gpt-6.1-sol"], [9, 0, 0, 0])

    def test_two_files_of_one_thread_are_one_session_and_are_not_topped_up(self):
        full = self.write_session(self.session())
        header = self.write_session([row for row in self.session() if row["type"] == "session_meta"], "two.jsonl")
        self.write_threads([("one", 1767400000, 330, "gpt-6.1-sol", "user")])
        database = [str(self.fixture.codex / "state_5.sqlite")]
        for order in ([str(full), str(header)], [str(header), str(full)]):
            found = lambda pattern, **_: order if "sessions" in pattern else database
            with self.subTest(order=order), mock.patch.object(self.build.glob, "glob", side_effect=found):
                codex = self.build.codex_data({})
                self.assertEqual(sum(codex["models"]["gpt-6.1-sol"]), 330)
                self.assertEqual(codex["totals"]["sessions"], 1)

    def test_a_later_session_header_does_not_change_the_thread(self):
        other = self.event("2026-01-03T01:30:00Z", "session_meta", {"id": "other", "thread_source": "user"})
        self.write_session([*self.session(), other])
        self.write_threads([("one", 1767400000, 330, "gpt-6.1-sol", "user")])
        self.assertEqual(sum(self.run_build()["codex"]["models"]["gpt-6.1-sol"]), 330)

    def test_subagents_cost_tokens_and_tasks_but_are_not_sessions(self):
        self.write_session(self.session("helper", "subagent"))
        self.write_threads([("lost", 1767400000, 70, "gpt-6.1-sol", "subagent")])
        codex = self.run_build()["codex"]
        self.assertEqual(codex["totals"]["sessions"], 0)
        self.assertEqual(sum(codex["models"]["gpt-6.1-sol"]), 400)
        self.assertEqual(list(codex["tasks"].values()), [900])

    def test_messages_land_in_the_local_hour_for_peak_hour(self):
        self.write_session(self.session())
        self.write_session(self.session("two"), "two.jsonl")
        hours = self.run_build()["codex"]["hours"]["2026-01-03"]
        self.assertEqual((max(hours), sum(hours)), (4, 4))

    def test_a_turn_without_a_model_keeps_the_last_one(self):
        rows = self.session()[:3]
        self.write_session([*rows, self.event("2026-01-03T01:05:00Z", "turn_context", {}), self.tokens("2026-01-03T01:10:00Z", 10, 0, 0)])
        self.assertEqual(list(self.run_build()["codex"]["models"]), ["gpt-6.1-sol"])

    def test_a_file_without_a_header_still_counts_its_tasks(self):
        self.write_session(self.session()[1:])
        self.assertEqual(list(self.run_build()["codex"]["tasks"].values()), [900])

    def test_a_lost_threads_total_stops_at_its_last_day(self):
        self.write_session(self.session())
        self.write_session(json.loads(json.dumps(self.session("two")).replace("2026-01-03", "2026-01-04")), "two.jsonl")
        self.write_threads([("gone", int(self.build.epoch("2026-01-02T01:00:00Z")), 300, "gpt-6.1-sol", "user")])
        db = sqlite3.connect(self.fixture.codex / "state_5.sqlite")
        db.execute("UPDATE threads SET updated_at = ?", (int(self.build.epoch("2026-01-03T23:00:00Z")),))
        db.commit()
        db.close()
        days = self.run_build()["codex"]["days"]
        self.assertEqual(days["2026-01-03"]["tm"]["gpt-6.1-sol"], 630)
        self.assertEqual(days["2026-01-04"]["tm"]["gpt-6.1-sol"], 330)

    def test_building_changes_nothing_in_the_codex_folder(self):
        self.write_session(self.session())
        self.write_threads([("one", 1767400000, 330, "gpt-6.1-sol", "user")])
        before = {str(path): path.read_bytes() for path in self.fixture.codex.rglob("*") if path.is_file()}
        self.run_build()
        self.assertEqual({str(path): path.read_bytes() for path in self.fixture.codex.rglob("*") if path.is_file()}, before)

    def test_a_removed_codex_folder_hides_codex_until_it_comes_back(self):
        self.write_session(self.session())
        first = self.run_build()["codex"]
        Path(self.fixture.codex).rename(self.fixture.root / "away")
        self.assertIsNone(self.run_build()["codex"])
        self.assertTrue(self.cache()["codex"])
        Path(self.fixture.root / "away").rename(self.fixture.codex)
        self.assertEqual(self.run_build()["codex"], first)

    def test_claude_code_is_marked_absent_without_history_or_a_folder(self):
        self.assertTrue(self.run_build()["claude"])
        self.fixture.clear_transcripts()
        (self.fixture.config / "stats-cache.json").unlink()
        Path(self.build.CACHE).unlink()
        self.assertFalse(self.run_build()["claude"])

    def test_a_removed_claude_folder_is_marked_absent_even_with_a_cache(self):
        self.run_build()
        self.fixture.config.rename(self.fixture.root / "away")
        self.assertFalse(self.run_build()["claude"])

    def test_deleted_session_files_stay_counted(self):
        path = self.write_session(self.session())
        first = self.run_build()["codex"]
        path.unlink()
        self.assertEqual(self.run_build()["codex"], first)


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

    def leftovers(self):
        return list(self.fixture.app.glob("*.tmp"))

    def test_builds_running_at_the_same_time_all_succeed(self):
        for _ in range(6):
            runs = [subprocess.Popen([sys.executable, str(self.fixture.app / "build.py")], env=self.fixture.env(),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(4)]
            for run in runs:
                _, error = run.communicate(timeout=30)
                self.assertEqual(run.returncode, 0, error)
        self.assertEqual(self.cache()["v"], self.build.CACHE_VERSION)
        self.assertEqual(self.leftovers(), [])

    def test_a_damaged_cache_of_any_shape_starts_over(self):
        for content in ("[]", '{"v": %d}' % self.build.CACHE_VERSION, "null"):
            with self.subTest(content=content):
                Path(self.build.CACHE).write_text(content, encoding="utf-8")
                self.assertEqual(self.run_build()["totals"]["messages"], 110)

    def test_a_file_that_vanishes_mid_build_is_skipped(self):
        real = self.build.parse

        def parse(path, held=None):
            if path.endswith("history.jsonl"):
                raise FileNotFoundError(path)
            return real(path, held)

        with mock.patch.object(self.build, "parse", side_effect=parse):
            self.assertGreater(self.run_build()["totals"]["messages"], 0)

    def test_trouble_with_codex_leaves_the_claude_page_working(self):
        with mock.patch.object(self.build, "codex_data", side_effect=TypeError("odd file")):
            data = self.run_build()
        self.assertIsNone(data["codex"])
        self.assertEqual(data["totals"]["messages"], 110)

    def test_a_script_tag_in_a_model_name_stays_inside_the_data(self):
        self.only([row("a", "2026-01-03T01:00:00Z", kind="assistant", counts=(1, 1, 1, 1), model="</script><b>")])
        self.assertIn("</script><b>", self.run_build()["models"])
        page = Path(self.build.OUT).read_text(encoding="utf-8")
        self.assertNotIn("</script><b>", page)

    def test_cache_and_html_are_complete_before_atomic_replace(self):
        cache_path, out_path = Path(self.build.CACHE), Path(self.build.OUT)
        cache_path.write_text("old cache", encoding="utf-8")
        out_path.write_text("old page", encoding="utf-8")
        real_replace = self.build.os.replace
        replaced = []

        def inspect(src, dst):
            self.assertRegex(src, re.escape(dst) + r"\.\d+\.tmp$")
            self.assertEqual(Path(src).parent, Path(dst).parent)
            text = Path(src).read_text(encoding="utf-8")
            if dst == self.build.CACHE:
                self.assertEqual(cache_path.read_text(encoding="utf-8"), "old cache")
                self.assertEqual(json.loads(text)["v"], self.build.CACHE_VERSION)
            else:
                self.assertEqual(out_path.read_text(encoding="utf-8"), "old page")
                self.assertIn("const ALL = {", text)
                self.assertNotIn("/*__DATA__*/null", text)
                self.assertTrue(text.endswith("</html>\n"))
            replaced.append(dst)
            real_replace(src, dst)

        with mock.patch.object(self.build.os, "replace", side_effect=inspect):
            self.run_build()
        self.assertEqual(replaced, [self.build.CACHE, self.build.OUT])
        self.assertEqual(self.leftovers(), [])

    def test_failed_cache_replace_preserves_previous_files(self):
        Path(self.build.CACHE).write_text("old cache", encoding="utf-8")
        Path(self.build.OUT).write_text("old page", encoding="utf-8")
        with mock.patch.object(self.build, "replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.run_build()
        self.assertEqual(Path(self.build.CACHE).read_text(encoding="utf-8"), "old cache")
        self.assertEqual(Path(self.build.OUT).read_text(encoding="utf-8"), "old page")
        self.assertEqual(self.leftovers(), [])

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
        self.assertEqual(self.leftovers(), [])

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
