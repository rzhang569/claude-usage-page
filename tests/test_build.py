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



def counts_only(days):
    """Days without their cost fields (c, u, um, ct), which the cost tests check on their own."""
    return {k: {f: v for f, v in d.items() if f not in ("c", "u", "um", "ct")} for k, d in days.items()}

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
        for name in ("BASE", "ROOT", "STATS", "CODEX", "CACHE", "OLD_CACHE", "OUT", "TEMPLATE"):
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
        self.assertEqual(parsed["r"]["response|request"][4:8], [15, 20, 35, 40])
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
        self.assertEqual(parsed["r"]["meta|meta"][4:8], [1, 2, 3, 4])
        self.assertEqual(parsed["s"]["new"][1] - parsed["s"]["new"][0], 1800)

    def test_missing_model_and_usage_fields_default_to_question_mark_and_zero(self):
        item = row("answer", "2026-01-03T01:00:00Z", kind="assistant", counts=(2,))
        item["message"].pop("model")
        item["message"]["usage"]["output_tokens"] = None
        path = self.only([item])
        self.assertEqual(self.build.parse(path)["r"]["answer|answer"][3:8], ["?", 2, 0, 0, 0])

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
        self.assertEqual(counts_only(data["days"]), {
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
                self.assertEqual(self.cache()["files"][str(a)]["r"]["same|same"][4:8], [10, 2, 30, 4])
                self.assertEqual(self.cache()["files"][str(b)]["r"]["same|same"][4:8], [1, 20, 3, 40])

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
        self.assertEqual(counts_only(data["days"])["2026-01-01"], {"m": 3, "tm": {MODEL: 12}, "sp": None, "sc": 2})
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
                         {"generatedAt", "today", "user", "days", "hours", "models", "totals", "tasks", "codex", "claude", "pricing"})

    def test_command_line_build_uses_config_override_and_script_relative_output(self):
        result = subprocess.run([sys.executable, str(self.fixture.app / "build.py")],
                                cwd=self.fixture.root, env=self.fixture.env(), capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"wrote {self.fixture.app / 'index.html'}", result.stdout)
        self.assertTrue((self.fixture.app / "cache-v4.json").is_file())
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
        self.assertEqual(self.build.parse(str(path), held)["r"]["m|r"][4:8], [5, 9, 5, 5])

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

    def as_v3(self, cache):
        """The Claude Code part of the cache as an earlier release wrote it: eight-field replies, no marker."""
        old = {"v": 3, "files": {}}
        for path, entry in cache["files"].items():
            old["files"][path] = {**{k: v for k, v in entry.items() if k != "p"},
                                  "r": {key: reply[:8] for key, reply in entry["r"].items()}}
        return old

    def test_an_earlier_release_cache_is_carried_over_and_left_for_it(self):
        self.run_build()
        old = json.dumps(self.as_v3(self.cache()))
        Path(self.build.OLD_CACHE).write_text(old, encoding="utf-8")
        Path(self.build.CACHE).unlink()
        self.fixture.sidechain.unlink()  # its history now lives only in the old cache
        data = self.run_build()
        self.assertEqual(data["totals"]["messages"], 110)
        self.assertEqual(Path(self.build.OLD_CACHE).read_text(encoding="utf-8"), old)  # byte for byte
        self.assertEqual(self.cache()["v"], self.build.CACHE_VERSION)

    def test_what_an_earlier_release_saw_while_back_survives_the_next_upgrade(self):
        self.run_build()
        before = self.cache()
        # Back on the earlier release, a new transcript is cached, then cleaned up.
        extra = self.fixture.write_transcript("extra.jsonl", [row("x1", "2026-01-03T15:00:00Z", "x"),
                                                               row("x2", "2026-01-03T15:00:01Z", "x", "assistant")])
        self.run_build()
        seen = self.cache()["files"][str(extra)]
        Path(self.build.CACHE).write_text(json.dumps(before), encoding="utf-8")
        os.utime(self.build.CACHE, (1, 1))
        Path(self.build.OLD_CACHE).write_text(json.dumps(self.as_v3({**before, "files": {**before["files"], str(extra): seen}})),
                                              encoding="utf-8")
        extra.unlink()
        self.assertEqual(self.run_build()["totals"]["messages"], 112)

    def test_a_fuller_earlier_release_entry_keeps_the_replies_already_priced(self):
        path = self.fixture.write_transcript("grow.jsonl", [row("g1", "2026-01-03T15:00:00Z", "g"),
                                                             row("g2", "2026-01-03T15:00:01Z", "g", "assistant",
                                                                 counts=(10**6, 0, 0, 0), model="claude-opus-5-5")])
        self.run_build()
        priced = self.cache()
        # Back on the earlier release the transcript grows, then it is cleaned up before the upgrade.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row("g3", "2026-01-03T15:00:02Z", "g")) + "\n")
        messages = self.run_build()["days"]["2026-01-03"]["m"]
        grown = self.as_v3(self.cache())
        Path(self.build.CACHE).write_text(json.dumps(priced), encoding="utf-8")
        os.utime(self.build.CACHE, (1, 1))
        Path(self.build.OLD_CACHE).write_text(json.dumps(grown), encoding="utf-8")
        path.unlink()
        day = self.run_build()["days"]["2026-01-03"]
        self.assertAlmostEqual(day["c"]["claude-opus-5-5"], 4.0)  # still priced
        self.assertEqual(day["m"], messages)  # and the earlier release's extra row is there

    def test_a_damaged_cache_falls_back_to_the_earlier_releases(self):
        self.run_build()
        Path(self.build.OLD_CACHE).write_text(json.dumps(self.as_v3(self.cache())), encoding="utf-8")
        os.utime(self.build.OLD_CACHE, (1, 1))  # older than the damaged one
        Path(self.build.CACHE).write_text("{damaged", encoding="utf-8")
        self.fixture.sidechain.unlink()  # its history now lives only in the earlier release's cache
        self.assertEqual(self.run_build()["totals"]["messages"], 110)

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
        day = next(iter(counts_only(codex["days"]).values()))
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


class CostTests(BuildCase):
    """API list-price estimates. Prices here are the ones in pricing.json."""

    def prices(self):
        return json.loads((self.fixture.app / "pricing.json").read_text(encoding="utf-8"))

    def test_claude_rates_split_cache_writes_by_duration(self):
        claude = self.prices()["claude"]
        # Opus 5.5: $4 in, $20 out, $5 5m write, $8 1h write, $0.20 read per million.
        cost = self.build.claude_cost(claude, "claude-opus-5-5", 1e6, 1e6, 3e6, 1e6, 2e6, 0, 0)
        self.assertAlmostEqual(cost, 4 + 20 + 5 + 16 + 0.2)
        dated = self.build.claude_cost(claude, "claude-opus-5-5-20261015", 1e6, 0, 0, 0, 0, 0, 0)
        self.assertAlmostEqual(dated, 4)
        self.assertAlmostEqual(self.build.claude_cost(claude, "claude-opus-5-5[1m]", 1e6, 0, 0, 0, 0, 0, 0), 4)
        self.assertIsNone(self.build.claude_cost(claude, "claude-unknown-9", 1e6, 0, 0, 0, 0, 0, 0))

    def test_claude_fast_us_only_long_prompts_and_web_search(self):
        claude, b = self.prices()["claude"], self.build
        self.assertAlmostEqual(b.claude_cost(claude, "claude-opus-5-5", 1e6, 1e6, 0, 0, 0, b.FAST, 0), 48)
        self.assertAlmostEqual(b.claude_cost(claude, "claude-opus-5-5", 1e6, 0, 0, 0, 0, b.US, 0), 4.4)
        self.assertAlmostEqual(b.claude_cost(claude, "claude-opus-5-5", 1e6, 0, 0, 0, 0, b.FAST | b.US, 0), 8.8)
        self.assertIsNone(b.claude_cost(claude, "claude-sonnet-5-5", 1, 0, 0, 0, 0, b.FAST, 0))  # no fast price
        # Haiku 5.5 charges more once the whole prompt (input + cache) passes 100K tokens.
        self.assertAlmostEqual(b.claude_cost(claude, "claude-haiku-5-5", 60000, 0, 0, 40000, 0, 0, 0), 0.0064)
        self.assertAlmostEqual(b.claude_cost(claude, "claude-haiku-5-5", 60000, 0, 0, 40001, 0, 0, 0),
                               (60000 * 0.5 + 40001 * 0.05) / 1e6)
        self.assertAlmostEqual(b.claude_cost(claude, "claude-opus-5-5", 0, 0, 0, 0, 0, 0, 3), 0.03)

    def test_openai_rates_tiers_long_context_and_missing_rates(self):
        openai = self.prices()["openai"]
        cost = lambda key, counts, day="2026-11-20": self.build.codex_cost(openai, key, counts, day)
        for (key, counts, day), (dollars, unpriced) in {
            ("gpt-6.2-astra", (1e6, 1e6, 0, 1e6), "2026-11-20"): (8 + 40 + 0.4, 0),
            ("gpt-6.2-astra|long", (1e6, 0, 0, 0), "2026-11-20"): (16, 0),
            ("gpt-6.2-astra|fast", (1e6, 0, 1e6, 0), "2026-11-20"): (2 * (8 + 10), 0),
            ("gpt-6.2-sol|fast|long", (0, 1e6, 0, 0), "2026-11-20"): (30, 0),
            # Ultrafast: 3x through Feb 28 2027, 6x after; 6x on GPT-6 Astra and GPT-6.1 Sol.
            ("gpt-6.2-astra|ultrafast", (1e5, 0, 0, 0), "2027-02-28"): (2.4, 0),
            ("gpt-6.2-astra|ultrafast", (1e5, 0, 0, 0), "2027-03-01"): (4.8, 0),
            ("gpt-6-astra|ultrafast", (1e6, 0, 0, 0), "2026-11-20"): (60, 0),
            # GPT-5.5 Fast is 2.5x and lists no long-context Fast rate.
            ("gpt-5.5|fast", (1e6, 0, 0, 0), "2026-11-20"): (12.5, 0),
            ("gpt-5.5|fast|long", (7, 0, 0, 0), "2026-11-20"): (0, 7),
            ("gpt-5.4|fast", (1e6, 0, 0, 0), "2026-11-20"): (5, 0),
            # GPT-5.2 has one rate whatever the request size; no cache-write price leaves only that unpriced.
            ("gpt-5.2|long", (1e6, 0, 0, 0), "2026-11-20"): (1.75, 0),
            ("gpt-5.2", (1e5, 0, 5, 0), "2026-11-20"): (0.175, 5),
            ("gpt-9-nova", (1, 2, 0, 0), "2026-11-20"): (0, 3),
            ("gpt-6.2-astra|tier-flex", (4, 0, 0, 0), "2026-11-20"): (0, 4),
        }.items():
            got = cost(key, list(counts), day)
            self.assertAlmostEqual(got[0], dollars, msg=(key, day))
            self.assertEqual(got[1], unpriced, (key, day))

    def test_transcript_replies_are_priced_once_per_response(self):
        usage = {"input_tokens": 1000000, "output_tokens": 1000000, "cache_creation_input_tokens": 2000000,
                 "cache_read_input_tokens": 0, "cache_creation": {"ephemeral_1h_input_tokens": 2000000},
                 "server_tool_use": {"web_search_requests": 1}}
        rows = [row("a", "2026-01-03T15:00:00Z", "s"),
                *[{**row(f"b{n}", "2026-01-03T15:00:01Z", "s", "assistant", counts=(0, 0, 0, 0),
                         model="claude-opus-5-5", message_id="m", request_id="r")} for n in range(2)]]
        for r in rows[1:]:
            r["message"]["usage"] = usage
        self.only(rows)
        day = self.run_build()["days"]["2026-01-03"]
        self.assertAlmostEqual(day["c"]["claude-opus-5-5"], 4 + 20 + 16 + 0.01)
        self.assertNotIn("u", day)

    def test_unknown_models_old_cache_entries_and_stats_only_days_are_unpriced(self):
        self.fixture.write_stats()
        self.only([row("a", "2026-01-03T15:00:00Z", "s"),
                   row("b", "2026-01-03T15:00:01Z", "s", "assistant", counts=(5, 5, 0, 0), model="claude-new-9")])
        data = self.run_build()
        self.assertEqual(data["days"]["2026-01-03"]["u"], 10)
        self.assertEqual(data["days"]["2026-01-01"]["u"], 1000)  # stats-cache totals only
        self.assertTrue(data["pricing"]["reviewed"])
        # A transcript cached before pricing and deleted since keeps its counts but no price.
        cache = json.loads(Path(self.build.CACHE).read_text(encoding="utf-8"))
        old = {**next(iter(cache["files"].values()))}
        old.pop("p")
        old["r"] = {"gone|gone": [old["r"]["b|b"][0], "s", 0, MODEL, 7, 0, 0, 0]}
        old["stamp"] = [1, 1]
        cache["files"]["/deleted.jsonl"] = old
        Path(self.build.CACHE).write_text(json.dumps(cache), encoding="utf-8")
        self.assertEqual(self.run_build()["days"]["2026-01-03"]["u"], 17)

    def test_cached_files_without_pricing_fields_are_parsed_again(self):
        path = self.only([row("a", "2026-01-03T15:00:00Z", "s"),
                          row("b", "2026-01-03T15:00:01Z", "s", "assistant", counts=(1000000, 0, 0, 0),
                              model="claude-opus-5-5")])
        self.run_build()
        cache = json.loads(Path(self.build.CACHE).read_text(encoding="utf-8"))
        entry = cache["files"][str(path)]
        entry.pop("p")
        entry["r"]["b|b"] = entry["r"]["b|b"][:8]
        Path(self.build.CACHE).write_text(json.dumps(cache), encoding="utf-8")
        day = self.run_build()["days"]["2026-01-03"]
        self.assertAlmostEqual(day["c"]["claude-opus-5-5"], 4)
        self.assertNotIn("u", day)

    def test_no_pricing_file_means_no_cost_fields(self):
        (self.fixture.app / "pricing.json").unlink()
        self.only([row("a", "2026-01-03T15:00:00Z", "s"),
                   row("b", "2026-01-03T15:00:01Z", "s", "assistant", counts=(5, 5, 0, 0))])
        data = self.run_build()
        self.assertIsNone(data["pricing"])
        self.assertFalse(any("c" in d or "u" in d for d in data["days"].values()))


class CodexCostTests(BuildCase):
    event, tokens, session = CodexTests.event, CodexTests.tokens, CodexTests.session
    write_session, write_threads = CodexTests.write_session, CodexTests.write_threads

    def test_fast_tier_and_per_response_size_set_the_codex_price(self):
        usage = lambda spent, cached, out: {"input_tokens": spent, "cached_input_tokens": cached, "output_tokens": out}
        rows = [
            self.event("2026-01-03T01:00:00Z", "session_meta", {"id": "fast", "thread_source": "user"}),
            self.event("2026-01-03T01:00:01Z", "event_msg",
                       {"type": "thread_settings_applied", "thread_settings": {"service_tier": "priority"}}),
            self.event("2026-01-03T01:00:02Z", "turn_context", {"model": "gpt-6.2-astra"}),
            self.event("2026-01-03T01:00:03Z", "token_usage_record", {"usage": usage(1000000, 0, 0)}),
            self.event("2026-01-03T01:00:04Z", "token_usage_record", {"usage": usage(300000, 0, 0)}),
        ]
        self.write_session(rows)
        day = next(iter(self.run_build()["codex"]["days"].values()))
        # 1M input that request alone is long context: $16 x 2 (Fast); 300K is long too: $4.80 x 2.
        self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 2 * (16 + 4.8))
        self.assertEqual(day["tm"], {"gpt-6.2-astra": 1300000})

    def test_running_totals_are_priced_and_database_only_threads_are_not(self):
        standard = self.event("2026-01-03T01:00:00Z", "event_msg", {"type": "thread_settings_applied", "thread_settings": {}})
        self.write_session([standard, *self.session()])
        self.write_threads([("other", 1767402000, 5000, "gpt-6.1-sol", "user")])
        day = self.run_build()["codex"]["days"]["2026-01-03"]
        # gpt-6.1-sol: 160 input, 30 output, 140 cached.
        self.assertAlmostEqual(day["c"]["gpt-6.1-sol"], (160 * 2 + 30 * 10 + 140 * 0.1) / 1e6)
        self.assertEqual(day["u"], 5000)


class CostRegressionTests(BuildCase):
    """Each case a review found wrong in the first version of the cost estimate."""
    event, tokens, write_session, write_threads = (CodexTests.event, CodexTests.tokens, CodexTests.write_session,
                                                   CodexTests.write_threads)

    def codex_day(self, rows):
        self.write_session(rows)
        return next(iter(self.run_build()["codex"]["days"].values()))

    def start(self, model="gpt-6.2-astra"):
        # A settings snapshot with no tier: the standard one.
        return [self.event("2026-01-03T01:00:00Z", "session_meta", {"id": "s", "thread_source": "user"}),
                self.event("2026-01-03T01:00:00Z", "event_msg", {"type": "thread_settings_applied", "thread_settings": {}}),
                self.event("2026-01-03T01:00:01Z", "turn_context", {"model": model})]

    def tier(self, ts, value):
        return self.event(ts, "event_msg", {"type": "thread_settings_applied", "thread_settings": {"service_tier": value}})

    def record(self, ts, spent, owner=None, rid=None):
        body = {"usage": {"input_tokens": spent}}
        if owner:
            body["thread_id"] = owner  # the thread the response belongs to
        if rid:
            body["response_id"] = rid
        return self.event(ts, "token_usage_record", body)

    def test_unpriced_share_counts_stats_only_tokens_on_a_shared_date(self):
        # stats-cache ends 2026-01-02 (UTC); its tokens for that day have no transcript left,
        # while a transcript reply after the cutoff lands on the same local date.
        self.fixture.write_stats()
        self.only([row("a", "2026-01-03T01:00:00Z", "s"),
                   row("b", "2026-01-03T01:00:01Z", "s", "assistant", counts=(100, 0, 0, 0), model=MODEL)])
        days = self.run_build()["days"]
        # 01:00 UTC on Jan 3 is still Jan 2 in UTC-05, with the same model as stats-cache's Jan 2:
        # that reply doesn't stand for those stats tokens, which stay unpriced.
        shared = days["2026-01-02"]
        self.assertEqual(shared["u"], 2500)
        self.assertEqual(shared["ct"], 2600)
        for d in days.values():
            self.assertLessEqual(d.get("u", 0), d.get("ct", 0))

    def test_without_a_cutoff_transcripts_cover_their_stats_days(self):
        stats = {**self.fixture.stats, "dailyModelTokens": [{"date": "2026-01-02", "tokensByModel": {MODEL: 100}}]}
        del stats["lastComputedDate"]
        self.fixture.write_stats(stats)
        self.only([row("a", "2026-01-02T15:00:00Z", "s"),
                   row("b", "2026-01-02T15:00:01Z", "s", "assistant", counts=(100, 0, 0, 0))])
        self.assertFalse(any(d.get("u") for d in self.run_build()["days"].values()))

    def test_running_totals_without_a_request_size_still_find_long_context(self):
        day = self.codex_day([*self.start("gpt-6.1-sol"), self.tokens("2026-01-03T01:10:00Z", 300000, 0, 0)])
        self.assertAlmostEqual(day["c"]["gpt-6.1-sol"], 300000 * 4 / 1e6)

    def test_a_reply_before_the_cutoff_on_a_day_stats_lacks_is_not_priced(self):
        self.fixture.write_stats()  # days Jan 1 and Jan 2, cutoff at the end of Jan 2 (UTC)
        self.only([row("a", "2025-12-30T15:00:00Z", "s"),
                   row("b", "2025-12-30T15:00:01Z", "s", "assistant", counts=(10**6, 0, 0, 0), model="claude-opus-5-5")])
        days = self.run_build()["days"]
        self.assertFalse(any(d.get("c") for d in days.values()))

    def test_a_reply_before_the_cutoff_for_a_model_stats_lacks_is_not_priced(self):
        self.fixture.write_stats()  # Jan 1 has only MODEL
        self.only([row("a", "2026-01-01T15:00:00Z", "s"),
                   row("b", "2026-01-01T15:00:01Z", "s", "assistant", counts=(10**6, 0, 0, 0), model="claude-opus-5-5")])
        data = self.run_build()
        self.assertNotIn("c", data["days"]["2026-01-01"])
        self.assertEqual(data["pricing"]["partialBefore"], "2026-01-02")

    def test_dated_snapshots_are_priced_as_their_model(self):
        self.assertEqual(self.build.model_id("gpt-5.4-2026-03-05"), "gpt-5.4")
        self.assertEqual(self.build.model_id("claude-opus-5-5-20261015"), "claude-opus-5-5")

    def test_hosted_web_searches_cost_a_cent_each_once(self):
        def found(ts, sid, action="search"):
            return self.event(ts, "event_msg", {"type": "item_completed", "item": {
                "type": "Extension", "kind": "web.search", "id": sid, "action": {"type": action}}})
        call = self.event("2026-01-03T01:40:00Z", "response_item", {
            "type": "web_search_call", "id": "ws_9", "status": "completed", "action": {"type": "search"}})
        day = self.codex_day([*self.start(), found("2026-01-03T01:10:00Z", "a"), found("2026-01-03T01:10:01Z", "a"),
                              found("2026-01-03T01:20:00Z", "b"), found("2026-01-03T01:30:00Z", "c", "openPage"), call])
        self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 0.03)

    def test_a_session_with_no_recorded_tier_is_unpriced(self):
        # Older Codex versions saved no tier, so Fast and standard look alike there.
        rows = [r for r in self.start() if r["payload"].get("type") != "thread_settings_applied"]
        day = self.codex_day([*rows, self.record("2026-01-03T01:10:00Z", 100000)])
        self.assertNotIn("c", day)
        self.assertEqual(day["u"], 100000)

    def test_a_search_a_fork_copied_from_its_parent_counts_once(self):
        def found(ts, sid):
            return self.event(ts, "event_msg", {"type": "item_completed", "item": {
                "type": "Extension", "kind": "web.search", "id": sid, "action": {"type": "search"}}})
        self.write_session([*self.start(), found("2026-01-03T01:10:00Z", "a")], name="parent.jsonl")
        child = [self.event("2026-01-03T02:00:00Z", "session_meta", {"id": "child", "thread_source": "user"}),
                 found("2026-01-03T01:10:00Z", "a")]
        day = self.codex_day(child)
        self.assertAlmostEqual(sum(day["c"].values()), 0.01)

    def test_records_a_fork_copied_from_its_parent_count_once(self):
        # Codex can copy the parent's records into the fork; they still name the parent's thread.
        self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 100000, "s")], name="parent.jsonl")
        child = [self.event("2026-01-03T02:00:00Z", "session_meta", {"id": "child", "thread_source": "user"}),
                 self.event("2026-01-03T02:00:00Z", "event_msg", {"type": "thread_settings_applied", "thread_settings": {}}),
                 self.event("2026-01-03T02:00:01Z", "turn_context", {"model": "gpt-6.2-astra"}),
                 self.record("2026-01-03T01:10:00Z", 100000, "s"), self.record("2026-01-03T02:10:00Z", 50000, "child")]
        day = self.codex_day(child)
        self.assertEqual(day["tm"], {"gpt-6.2-astra": 150000})
        self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 150000 * 8 / 1e6)

    def test_a_fork_before_its_first_response_counts_nothing_of_its_parent(self):
        self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 100000, "s")], name="parent.jsonl")
        child = [self.event("2026-01-03T02:00:00Z", "session_meta", {"id": "child", "forked_from_id": "s", "thread_source": "user"}),
                 self.event("2026-01-03T02:00:01Z", "turn_context", {"model": "gpt-6.2-astra"}),
                 self.record("2026-01-03T01:10:00Z", 100000, "s"), self.tokens("2026-01-03T02:00:02Z", 100000, 0, 0)]
        # Codex's database gives the child the running total it inherited.
        self.write_threads([("child", int(self.build.epoch("2026-01-03T02:00:00Z")), 100000, "gpt-6.2-astra", "user")])
        self.assertEqual(self.codex_day(child)["tm"], {"gpt-6.2-astra": 100000})
        # Later the child works on and its rollout is compressed: only what it added is new.
        (self.fixture.codex / "sessions" / "2026" / "01" / "03" / "one.jsonl").unlink()
        (self.fixture.codex / "state_5.sqlite").unlink()
        self.write_threads([("child", int(self.build.epoch("2026-01-03T02:00:00Z")), 150000, "gpt-6.2-astra", "user")])
        day = next(iter(self.run_build()["codex"]["days"].values()))
        self.assertEqual(day["tm"], {"gpt-6.2-astra": 150000})

    def test_codex_entries_cached_before_the_fork_filter_are_read_again(self):
        self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 100000)])
        self.run_build()
        cache = self.cache()
        for entry in cache["codex"].values():
            entry.pop("rec", None)  # as an entry cached by the previous version
            entry["usage"] = {day: {m: [n * 2 for n in c] for m, c in by.items()} for day, by in entry["usage"].items()}
        Path(self.build.CACHE).write_text(json.dumps(cache), encoding="utf-8")
        day = next(iter(self.run_build()["codex"]["days"].values()))
        self.assertEqual(day["tm"], {"gpt-6.2-astra": 100000})

    def test_entries_kept_for_deleted_files_still_count(self):
        path = self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 100000)])
        self.run_build()
        cache = self.cache()
        for entry in cache["codex"].values():
            entry.pop("rec", None)  # cached by the previous version
        Path(self.build.CACHE).write_text(json.dumps(cache), encoding="utf-8")
        Path(path).unlink()
        day = next(iter(self.run_build()["codex"]["days"].values()))
        self.assertEqual(day["tm"], {"gpt-6.2-astra": 100000})

    def test_database_usage_beyond_a_gone_files_records_counts_unpriced(self):
        created = int(self.build.epoch("2026-01-03T01:00:00Z"))
        path = self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 40000)])
        self.write_threads([("s", created, 40000, "gpt-6.2-astra", "user")])
        self.run_build()
        # The thread works on, then Codex compresses its file before the next build.
        path.unlink()
        (self.fixture.codex / "state_5.sqlite").unlink()
        self.write_threads([("s", created, 100000, "gpt-6.2-astra", "user")])
        day = next(iter(self.run_build()["codex"]["days"].values()))
        self.assertEqual(day["tm"], {"gpt-6.2-astra": 100000})
        self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 40000 * 8 / 1e6)
        self.assertEqual(day["um"], {"gpt-6.2-astra": 60000})

    def test_a_readable_file_outranks_a_larger_database_total(self):
        # The database can run ahead of the last build, or count a fork's inherited usage.
        self.write_threads([("s", int(self.build.epoch("2026-01-03T01:00:00Z")), 100000, "gpt-6.2-astra", "user")])
        day = self.codex_day([*self.start(), self.record("2026-01-03T01:10:00Z", 40000)])
        self.assertEqual(day["tm"], {"gpt-6.2-astra": 40000})
        self.assertNotIn("u", day)

    def test_a_database_only_total_splits_into_whole_tokens_that_add_up(self):
        self.assertEqual(self.build.apportion(1001, [1, 1]), [501, 500])
        self.assertEqual(sum(self.build.apportion(1000, [3, 3, 3])), 1000)
        self.assertEqual(self.build.apportion(7, [0, 5, 0, 2]), [0, 5, 0, 2])

    def test_an_archived_session_counts_once_and_keeps_its_newest_detail(self):
        path = self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 100000)], name="rollout-x.jsonl")
        self.run_build()
        with path.open("a", encoding="utf-8") as f:  # it grows, then Codex archives it
            f.write(json.dumps(self.record("2026-01-03T01:20:00Z", 50000)) + "\n")
        archived = self.fixture.codex / "archived_sessions" / "rollout-x.jsonl"
        archived.parent.mkdir(parents=True)
        path.rename(archived)
        for _ in ("archived", "then compressed away"):
            day = next(iter(self.run_build()["codex"]["days"].values()))
            self.assertEqual(day["tm"], {"gpt-6.2-astra": 150000})
            self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 1.2)
            archived.unlink(missing_ok=True)
    def test_an_archived_session_is_read_on_a_fresh_install(self):
        archived = self.fixture.codex / "archived_sessions" / "2026" / "01" / "03" / "rollout-y.jsonl"
        archived.parent.mkdir(parents=True)
        archived.write_text("".join(json.dumps(r) + "\n" for r in [*self.start(), self.record("2026-01-03T01:10:00Z", 100000)]),
                            encoding="utf-8")
        day = next(iter(self.run_build()["codex"]["days"].values()))
        self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 0.8)

    def test_a_fork_with_its_parents_copied_history_counts_it_once(self):
        # Older Codex copied the parent's file into the fork, its metadata first, then the fork's own rows.
        parent, child = "01a00000-0000-7000-8000-000000000001", "01a00000-0000-7000-8000-000000000002"
        def said(ts, mid, role="user"):
            return self.event(ts, "response_item", {"type": "message", "role": role, "id": mid, "content": []})
        history = [self.event("2026-01-01T15:00:00Z", "session_meta", {"id": parent, "thread_source": "user"}),
                   self.event("2026-01-01T15:00:00Z", "event_msg", {"type": "thread_settings_applied", "thread_settings": {}}),
                   self.event("2026-01-01T15:00:01Z", "turn_context", {"model": "gpt-6.2-astra"}),
                   said("2026-01-01T15:05:00Z", "m1"), said("2026-01-01T15:06:00Z", "m2", "assistant"),
                   self.record("2026-01-01T15:06:00Z", 100000, parent)]
        self.write_session(history, name=f"rollout-2026-01-01T10-00-00-{parent}.jsonl")
        # Its file is named for the (local) time of the fork, before its own rows in any time zone.
        self.write_session([*history, said("2026-01-09T23:05:00Z", "m3"), self.record("2026-01-09T23:06:00Z", 50000, child)],
                           name=f"rollout-2026-01-09T08-00-00-{child}.jsonl")
        # Codex's database counts the fork's inherited usage in its total.
        self.write_threads([(parent, int(self.build.epoch("2026-01-01T15:00:00Z")), 100000, "gpt-6.2-astra", "user"),
                            (child, int(self.build.epoch("2026-01-09T15:00:00Z")), 150000, "gpt-6.2-astra", "user")])
        codex = self.run_build()["codex"]
        days = codex["days"]
        self.assertEqual({k: (d["sc"], d["m"], d["tm"]) for k, d in days.items()},
                         {"2026-01-01": (1, 2, {"gpt-6.2-astra": 100000}), "2026-01-09": (1, 1, {"gpt-6.2-astra": 50000})})
        self.assertEqual(codex["totals"], {"sessions": 2, "messages": 3})
        self.assertEqual(sum(sum(row) for row in codex["hours"].values()), 3)
        self.assertFalse(any(d.get("u") for d in days.values()))
    def fork_with_copies(self, parent, child):
        """Today's Codex: the fork's metadata, then the parent's history it copied, then its own rows."""
        def said(ts, mid=None, role="user"):
            return self.event(ts, "response_item", {"type": "message", "role": role, "content": [], **({"id": mid} if mid else {})})
        def total(ts, n, last):
            return self.event(ts, "event_msg", {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": n}, "last_token_usage": {"input_tokens": last}}})
        return [self.event("2026-01-09T15:00:00Z", "session_meta", {"id": child, "forked_from_id": parent, "thread_source": "user"}),
                self.event("2026-01-09T15:00:00Z", "event_msg", {"type": "thread_settings_applied", "thread_id": parent,
                                                                 "thread_settings": {}}),
                self.event("2026-01-09T15:00:00Z", "turn_context", {"model": "gpt-6.2-astra"}),
                said("2026-01-09T15:00:00Z"), self.record("2026-01-09T15:00:00Z", 100000, parent, "r-mom-1"),
                total("2026-01-09T15:00:00Z", 100000, 100000),
                self.event("2026-01-09T15:00:01Z", "event_msg", {"type": "thread_settings_applied", "thread_id": child,
                                                                 "thread_settings": {}}),
                said("2026-01-09T15:05:00Z", "own"), self.record("2026-01-09T15:06:00Z", 50000, child, "r-kid-1"),
                total("2026-01-09T15:06:00Z", 150000, 50000)]

    def test_a_fork_prices_its_gone_parent_from_the_copy_and_counts_the_copy_once(self):
        parent, child = "01a00000-0000-7000-8000-00000000000a", "01a00000-0000-7000-8000-00000000000b"
        self.write_session(self.fork_with_copies(parent, child), name=f"rollout-2026-01-09T10-00-00-{child}.jsonl")
        # The parent's own file was compressed before this page ever read it.
        for parent_total in (100000, 125000):  # the copy is all of the parent, or the parent went on after it
            with self.subTest(parent_total=parent_total):
                (self.fixture.codex / "state_5.sqlite").unlink(missing_ok=True)
                self.write_threads([(parent, int(self.build.epoch("2026-01-01T15:00:00Z")), parent_total, "gpt-6.2-astra", "user"),
                                    (child, int(self.build.epoch("2026-01-09T15:00:00Z")), 150000, "gpt-6.2-astra", "user")])
                codex = self.run_build()["codex"]
                days = codex["days"]
                self.assertEqual({k: d["tm"] for k, d in days.items()},
                                 {"2026-01-01": {"gpt-6.2-astra": parent_total}, "2026-01-09": {"gpt-6.2-astra": 50000}})
                self.assertAlmostEqual(sum(sum(d.get("c", {}).values()) for d in days.values()), 150000 * 8 / 1e6)
                self.assertEqual(sum(d.get("u", 0) for d in days.values()), parent_total - 100000)
                self.assertEqual(codex["totals"], {"sessions": 2, "messages": 1})  # the copied message has no id

    def test_a_fork_whose_file_is_later_compressed_counts_only_what_it_added(self):
        parent, child = "01a00000-0000-7000-8000-00000000000c", "01a00000-0000-7000-8000-00000000000d"
        self.write_session([self.event("2026-01-01T15:00:00Z", "session_meta", {"id": parent, "thread_source": "user"}),
                            *self.start()[1:], self.record("2026-01-01T15:06:00Z", 100000, parent, "r-mom-1")],
                           name=f"rollout-2026-01-01T10-00-00-{parent}.jsonl")
        path = self.write_session(self.fork_with_copies(parent, child), name=f"rollout-2026-01-09T10-00-00-{child}.jsonl")
        self.write_threads([(parent, int(self.build.epoch("2026-01-01T15:00:00Z")), 100000, "gpt-6.2-astra", "user"),
                            (child, int(self.build.epoch("2026-01-09T15:00:00Z")), 150000, "gpt-6.2-astra", "user")])
        self.run_build()
        path.unlink()
        (self.fixture.codex / "state_5.sqlite").unlink()
        self.write_threads([(parent, int(self.build.epoch("2026-01-01T15:00:00Z")), 100000, "gpt-6.2-astra", "user"),
                            (child, int(self.build.epoch("2026-01-09T15:00:00Z")), 170000, "gpt-6.2-astra", "user")])
        days = self.run_build()["codex"]["days"]
        self.assertEqual(sum(sum(d["tm"].values()) for d in days.values()), 170000)

    def test_usage_after_the_last_build_lands_after_it(self):
        created = int(self.build.epoch("2026-01-01T15:00:00Z"))
        path = self.write_session([self.event("2026-01-01T15:00:00Z", "session_meta", {"id": "s", "thread_source": "user"}),
                                   self.event("2026-01-01T15:00:00Z", "event_msg", {"type": "thread_settings_applied",
                                                                                    "thread_settings": {}}),
                                   self.event("2026-01-01T15:00:01Z", "turn_context", {"model": "gpt-6.2-astra"}),
                                   self.record("2026-01-01T15:10:00Z", 40000), self.record("2026-01-02T15:10:00Z", 40000)])
        self.write_threads([("s", created, 80000, "gpt-6.2-astra", "user")])
        self.run_build()
        # Resumed on Jan 15, then compressed before the next build.
        path.unlink()
        db = sqlite3.connect(self.fixture.codex / "state_5.sqlite")
        db.execute("UPDATE threads SET tokens_used = 140000, updated_at = ?", (int(self.build.epoch("2026-01-15T15:00:00Z")),))
        db.commit()
        db.close()
        days = self.run_build()["codex"]["days"]
        self.assertEqual({k: sum(d["tm"].values()) for k, d in days.items()},
                         {"2026-01-01": 40000, "2026-01-02": 40000, "2026-01-15": 60000})

    def test_a_file_that_cant_be_read_now_isnt_taken_as_the_whole_story(self):
        path = self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 100000)])
        self.write_threads([("s", int(self.build.epoch("2026-01-03T01:00:00Z")), 160000, "gpt-6.2-astra", "user")])
        self.run_build()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.record("2026-01-03T01:20:00Z", 60000)) + "\n")
        with mock.patch.object(self.build, "parse_codex", side_effect=OSError("locked")):
            day = next(iter(self.run_build()["codex"]["days"].values()))
        self.assertEqual(sum(day["tm"].values()), 160000)

    def test_a_shared_total_splits_the_same_way_every_build(self):
        start, end = (int(self.build.epoch(t)) for t in ("2026-01-01T15:00:00Z", "2026-01-02T15:00:00Z"))
        self.write_session([self.event("2026-01-01T15:00:00Z", "session_meta", {"id": "busy", "thread_source": "user"}),
                            *self.start()[1:], self.record("2026-01-01T15:10:00Z", 10),
                            self.record("2026-01-02T15:10:00Z", 10)], name="busy.jsonl")
        self.fixture.codex.mkdir(exist_ok=True)
        db = sqlite3.connect(self.fixture.codex / "state_5.sqlite")
        db.execute("CREATE TABLE threads (id TEXT, created_at INT, tokens_used INT, model TEXT, thread_source TEXT, updated_at INT)")
        db.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                       [("busy", start, 20, "gpt-6.2-astra", "user", end), ("lost", start, 1001, "gpt-6.2-astra", "user", end)])
        db.commit()
        db.close()
        days = self.run_build()["codex"]["days"]
        self.assertEqual((days["2026-01-01"]["tm"]["gpt-6.2-astra"], days["2026-01-02"]["tm"]["gpt-6.2-astra"]), (511, 510))

    def test_an_older_fork_without_rows_naming_it_still_counts_its_own_usage(self):
        # Older Codex wrote the copy at fork time and the fork's own turns later, naming no thread.
        def total(ts, n, last):
            return self.event(ts, "event_msg", {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": n}, "last_token_usage": {"input_tokens": last}}})
        rows = [self.event("2026-01-09T15:00:00Z", "session_meta", {"id": "kid", "forked_from_id": "mom", "thread_source": "user"}),
                self.event("2026-01-09T15:00:00Z", "turn_context", {"model": "gpt-6.1-sol"}),
                total("2026-01-09T15:00:01Z", 100000, 100000),
                self.event("2026-01-09T15:20:00Z", "response_item", {"type": "message", "role": "user", "content": []}),
                total("2026-01-09T15:21:00Z", 150000, 50000)]
        day = self.codex_day(rows)
        self.assertEqual((day["tm"], day["m"]), ({"gpt-6.1-sol": 50000}, 1))

    def test_a_read_that_fails_halfway_is_redone_without_counting_twice(self):
        path = self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 10)])
        self.run_build()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.record("2026-01-03T01:20:00Z", 5)) + "\n")
        # The new rows are read, then the file fails before the read can finish.
        with mock.patch.object(self.build, "mark", side_effect=OSError("read failed")):
            self.run_build()
        self.assertEqual(sum(next(iter(self.run_build()["codex"]["days"].values()))["tm"].values()), 15)

    def test_a_partly_cached_parent_takes_the_rest_from_a_fuller_copy(self):
        parent, child = "01a00000-0000-7000-8000-00000000000e", "01a00000-0000-7000-8000-00000000000f"
        mom = self.write_session([self.event("2026-01-01T15:00:00Z", "session_meta", {"id": parent, "thread_source": "user"}),
                                  *self.start()[1:], self.record("2026-01-01T15:06:00Z", 100000, parent, "r-mom-1")],
                                 name=f"rollout-2026-01-01T10-00-00-{parent}.jsonl")
        self.run_build()
        mom.unlink()  # it went on to 130,000, then Codex compressed it before the page looked again
        rows = self.fork_with_copies(parent, child)
        rows.insert(5, self.record("2026-01-09T15:00:00Z", 30000, parent, "r-mom-2"))
        self.write_session(rows, name=f"rollout-2026-01-09T10-00-00-{child}.jsonl")
        self.write_threads([(parent, int(self.build.epoch("2026-01-01T15:00:00Z")), 130000, "gpt-6.2-astra", "user"),
                            (child, int(self.build.epoch("2026-01-09T15:00:00Z")), 180000, "gpt-6.2-astra", "user")])
        days = self.run_build()["codex"]["days"]
        self.assertEqual(days["2026-01-01"]["tm"], {"gpt-6.2-astra": 130000})
        self.assertAlmostEqual(days["2026-01-01"]["c"]["gpt-6.2-astra"], 130000 * 8 / 1e6)
        self.assertFalse(any(d.get("u") for d in days.values()))

    def test_an_earlier_releases_codex_entry_still_counts_after_the_upgrade(self):
        # As cache.json held it before this version: plain model keys, no tier, no records flag.
        old = {"v": 3, "files": {}, "codex": {str(self.fixture.codex / "sessions" / "gone.jsonl"): {
            "id": "gone", "kind": "user", "review": False, "first": int(self.build.epoch("2026-01-03T01:00:00Z")),
            "model": "gpt-6.1-sol", "seen": [0, 0, 0, 0], "usage": {"2026-01-03": {"gpt-6.1-sol": [700, 100, 0, 200]}},
            "running": {}, "m": {"2026-01-03": 2}, "h": {"2026-01-03": [0] * 24}, "k": {}, "stamp": [10, 1],
            "offset": 10, "tail": ""}}}
        self.fixture.codex.mkdir(exist_ok=True)
        Path(self.build.OLD_CACHE).write_text(json.dumps(old), encoding="utf-8")
        day = next(iter(self.run_build()["codex"]["days"].values()))
        self.assertEqual((day["tm"], day["m"], day["u"]), ({"gpt-6.1-sol": 1000}, 2, 1000))  # counted, not priced

    def test_a_parent_seen_before_and_gone_now_isnt_counted_again_from_a_later_copy(self):
        parent, child = "01a00000-0000-7000-8000-000000000010", "01a00000-0000-7000-8000-000000000011"
        mom = self.write_session([self.event("2026-01-01T15:00:00Z", "session_meta", {"id": parent, "thread_source": "user"}),
                                  *self.start()[1:], self.record("2026-01-01T15:06:00Z", 100000, parent, "r-mom-1")],
                                 name=f"rollout-2026-01-01T10-00-00-{parent}.jsonl")
        self.write_session(self.fork_with_copies(parent, child), name=f"rollout-2026-01-09T10-00-00-{child}.jsonl")
        self.write_threads([(parent, int(self.build.epoch("2026-01-01T15:00:00Z")), 100000, "gpt-6.2-astra", "user"),
                            (child, int(self.build.epoch("2026-01-09T15:00:00Z")), 150000, "gpt-6.2-astra", "user")])
        first = self.run_build()["codex"]["days"]
        mom.unlink()  # the copy, dated Jan 9, is of the response the cache saw on Jan 1
        for days in (first, self.run_build()["codex"]["days"]):
            self.assertEqual({k: sum(d["tm"].values()) for k, d in days.items()}, {"2026-01-01": 100000, "2026-01-09": 50000})
            self.assertAlmostEqual(sum(sum(d.get("c", {}).values()) for d in days.values()), 1.2)

    def test_a_reverted_thread_counts_its_replay_once_and_its_new_usage(self):
        # Editing an earlier message makes a new file named <thread>_<rollout> that replays the history.
        thread, rollout = "01a00000-0000-7000-8000-000000000012", "01a00000-0000-7000-8000-000000000013"
        head = [self.event("2026-01-03T01:00:00Z", "session_meta", {"id": thread, "thread_source": "user"}), *self.start()[1:]]
        self.write_session([*head, self.record("2026-01-03T01:10:00Z", 100000, thread, "r1")],
                           name=f"rollout-2026-01-03T01-00-00-{thread}.jsonl")
        self.write_session([*head, self.record("2026-01-03T01:10:00Z", 100000, thread, "r1"),
                            self.record("2026-01-03T02:10:00Z", 50000, thread, "r2")],
                           name=f"rollout-2026-01-03T02-00-00-{thread}_{rollout}.jsonl")
        codex = self.run_build()["codex"]
        self.assertEqual(sum(next(iter(codex["days"].values()))["tm"].values()), 150000)
        self.assertEqual(codex["totals"]["sessions"], 1)

    def test_an_upgrade_keeps_the_old_parent_totals_and_a_fork_adds_only_what_came_after(self):
        parent, child = "01a00000-0000-7000-8000-000000000014", "01a00000-0000-7000-8000-000000000015"
        mom = self.fixture.codex / "sessions" / "2026" / "01" / "01" / f"rollout-2026-01-01T10-00-00-{parent}.jsonl"
        # The parent as an earlier version cached it: totals by day and model, messages counted,
        # no response ids; its file is gone now.
        old = {"v": 3, "files": {}, "codex": {str(mom): {
            "id": parent, "kind": "user", "review": False, "first": int(self.build.epoch("2026-01-01T15:00:00Z")),
            "model": "gpt-6.2-astra", "seen": [0, 0, 0, 0], "running": {}, "k": {}, "stamp": [10, 1], "offset": 10, "tail": "",
            "usage": {"2026-01-01": {"gpt-6.2-astra": [50000, 0, 0, 0]}, "2026-01-02": {"gpt-6.2-astra": [50000, 0, 0, 0]}},
            "m": {"2026-01-01": 1, "2026-01-02": 1}, "h": {"2026-01-01": [0] * 24, "2026-01-02": [0] * 24}}}}
        Path(self.build.OLD_CACHE).write_text(json.dumps(old), encoding="utf-8")
        rows = self.fork_with_copies(parent, child)
        # The fork's copy holds those two responses, the second with a message, and one more after them.
        rows[4] = self.record("2026-01-09T15:00:00Z", 50000, parent, "r-mom-1")
        rows[5:5] = [self.record("2026-01-09T15:00:00Z", 50000, parent, "r-mom-2"),
                     self.event("2026-01-09T15:00:00Z", "response_item", {"type": "message", "role": "user", "id": "m-mom",
                                                                          "content": []}),
                     self.record("2026-01-09T15:00:00Z", 20000, parent, "r-mom-3")]
        self.write_session(rows, name=f"rollout-2026-01-09T10-00-00-{child}.jsonl")
        self.write_threads([(parent, int(self.build.epoch("2026-01-02T15:00:00Z")), 120000, "gpt-6.2-astra", "user"),
                            (child, int(self.build.epoch("2026-01-09T15:00:00Z")), 170000, "gpt-6.2-astra", "user")])
        codex = self.run_build()["codex"]
        days = codex["days"]
        self.assertEqual({k: (sum(d["tm"].values()), d["m"]) for k, d in days.items()},
                         {"2026-01-01": (50000, 1), "2026-01-02": (70000, 1), "2026-01-09": (50000, 1)})
        self.assertEqual(sum(d.get("u", 0) for d in days.values()), 100000)  # the old totals: no prices kept
        self.assertAlmostEqual(sum(sum(d.get("c", {}).values()) for d in days.values()), 70000 * 8 / 1e6)

    def test_what_a_fork_copied_of_a_gone_parent_keeps_the_parents_dates(self):
        parent, child = "01a00000-0000-7000-8000-000000000018", "01a00000-0000-7000-8000-000000000019"
        copy = [self.event("2026-01-09T15:00:00Z", "session_meta", {"id": child, "forked_from_id": parent, "thread_source": "user"}),
                self.event("2026-01-09T15:00:00Z", "event_msg", {"type": "thread_settings_applied", "thread_id": parent,
                                                                 "thread_settings": {}}),
                self.event("2026-01-09T15:00:00Z", "turn_context", {"model": "gpt-6.2-astra"}),
                self.event("2026-01-09T15:00:00Z", "response_item", {"type": "message", "role": "user", "id": "m1", "content": []}),
                self.event("2026-01-09T15:00:00Z", "token_usage_record", {"usage": {"input_tokens": 100000}}),  # no id
                self.event("2026-01-09T15:00:00Z", "event_msg", {"type": "item_completed", "item": {
                    "type": "Extension", "kind": "web.search", "id": "ws1", "action": {"type": "search"}}}),
                self.event("2026-01-09T15:00:01Z", "event_msg", {"type": "thread_settings_applied", "thread_id": child,
                                                                 "thread_settings": {}}),
                self.record("2026-01-09T15:06:00Z", 50000, child, "r-kid")]
        self.write_session(copy, name=f"rollout-2026-01-09T10-00-00-{child}.jsonl")
        self.write_threads([(parent, int(self.build.epoch("2026-01-01T15:00:00Z")), 100000, "gpt-6.2-astra", "user"),
                            (child, int(self.build.epoch("2026-01-09T15:00:00Z")), 150000, "gpt-6.2-astra", "user")])
        days = self.run_build()["codex"]["days"]
        self.assertEqual(days["2026-01-01"]["tm"], {"gpt-6.2-astra": 100000})
        self.assertEqual(days["2026-01-01"]["m"], 1)
        self.assertAlmostEqual(days["2026-01-01"]["c"]["gpt-6.2-astra"], 0.8 + 0.01)  # the copied search too
        self.assertEqual((days["2026-01-09"]["tm"], days["2026-01-09"]["m"]), ({"gpt-6.2-astra": 50000}, 0))

    def test_an_earlier_releases_fuller_codex_entry_adds_only_its_surplus(self):
        path = self.write_session([*self.start(), self.record("2026-01-03T01:10:00Z", 100000, None, "r1")], name="grow.jsonl")
        self.run_build()
        priced = self.cache()
        entry = priced["codex"][str(path)]
        # Back on the earlier release, the file grew to 130,000 and was then cleaned up.
        old = {"v": 3, "files": {}, "codex": {str(path): {**{k: v for k, v in entry.items() if k not in ("recs", "s", "rec")},
                                                          "usage": {"2026-01-03": {"gpt-6.2-astra": [130000, 0, 0, 0]}},
                                                          "stamp": [entry["stamp"][0] + 50, 1]}}}
        Path(self.build.CACHE).write_text(json.dumps(priced), encoding="utf-8")
        os.utime(self.build.CACHE, (1, 1))
        Path(self.build.OLD_CACHE).write_text(json.dumps(old), encoding="utf-8")
        path.unlink()
        day = next(iter(self.run_build()["codex"]["days"].values()))
        self.assertEqual(day["tm"], {"gpt-6.2-astra": 130000})
        self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 0.8)  # what this version priced stays priced
        self.assertEqual(day["u"], 30000)

    def test_a_search_by_a_model_without_a_price_still_costs_its_fee(self):
        reply = row("b", "2026-01-03T15:00:01Z", "s", "assistant", counts=(5, 5, 0, 0), model="claude-3-5-sonnet")
        reply["message"]["usage"]["server_tool_use"] = {"web_search_requests": 2}
        self.only([row("a", "2026-01-03T15:00:00Z", "s"), reply])
        day = self.run_build()["days"]["2026-01-03"]
        self.assertAlmostEqual(day["c"]["claude-3-5-sonnet"], 0.02)
        self.assertEqual(day["u"], 10)

    def test_the_ultrafast_promotion_ends_at_pacific_midnight(self):
        for ts, dollars in (("2027-03-01T07:59:00Z", 2.4), ("2027-03-01T08:01:00Z", 4.8)):
            with self.subTest(ts=ts):
                self.write_session([*self.start(), self.tier("2026-01-03T01:00:02Z", "ultrafast"),
                                    self.record(ts, 100000)])
                day = self.run_build()["codex"]["days"]["2027-03-01"]
                self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], dollars)

    def test_history_coverage_is_per_model(self):
        # stats-cache: Jan 1 has 1,000 of MODEL; Jan 2 has 2,000 of MODEL and 500 of OTHER_MODEL.
        # Only a MODEL transcript survives from Jan 2, so OTHER_MODEL's 500 stay unpriced.
        self.fixture.write_stats()
        self.only([row("a", "2026-01-02T15:00:00Z", "s"),
                   row("b", "2026-01-02T15:00:01Z", "s", "assistant", counts=(100, 0, 0, 0))])
        days = self.run_build()["days"]
        self.assertEqual((days["2026-01-02"]["u"], days["2026-01-02"]["ct"]), (500, 600))
        self.assertEqual(days["2026-01-01"]["u"], 1000)

    def test_a_tier_cleared_either_way_is_standard_again(self):
        # Codex clears the tier with an explicit null or by leaving the key out of the snapshot.
        omitted = self.event("2026-01-03T01:15:00Z", "event_msg", {"type": "thread_settings_applied", "thread_settings": {}})
        for cleared in (self.tier("2026-01-03T01:15:00Z", None), omitted):
            with self.subTest(cleared=cleared["payload"]["thread_settings"]):
                day = self.codex_day([*self.start(), self.tier("2026-01-03T01:00:02Z", "priority"),
                                      self.record("2026-01-03T01:10:00Z", 100000), cleared,
                                      self.record("2026-01-03T01:20:00Z", 100000)])
                self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 1.6 + 0.8)
    def test_ultrafast_and_unknown_tiers(self):
        day = self.codex_day([*self.start(), self.tier("2026-01-03T01:00:02Z", "ultrafast"),
                              self.record("2026-01-03T01:10:00Z", 100000),
                              self.tier("2026-01-03T01:15:00Z", "flex"),
                              self.record("2026-01-03T01:20:00Z", 7)])
        self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 2.4)
        self.assertEqual(day["u"], 7)

    def test_long_context_is_per_request_not_per_day(self):
        day = self.codex_day([*self.start(), self.record("2026-01-03T01:10:00Z", 200000),
                              self.record("2026-01-03T01:20:00Z", 200000)])
        self.assertAlmostEqual(day["c"]["gpt-6.2-astra"], 400000 * 8 / 1e6)

    def test_an_unpriced_token_type_does_not_unprice_other_responses(self):
        write = self.event("2026-01-03T01:20:00Z", "token_usage_record",
                           {"usage": {"input_tokens": 5, "cache_write_input_tokens": 5}})
        day = self.codex_day([*self.start("gpt-5.2"), self.record("2026-01-03T01:10:00Z", 100000), write])
        self.assertAlmostEqual(day["c"]["gpt-5.2"], 0.175)
        self.assertEqual((day["u"], day["ct"]), (5, 100005))
