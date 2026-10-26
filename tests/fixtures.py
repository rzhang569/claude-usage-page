"""Fake Claude data and isolated copies of the application, never real user data."""
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import types
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
MODEL = "claude-sonnet-4-5"
OTHER_MODEL = "claude-haiku-4-5"
COUNTS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
          "cache_read_input_tokens")


def row(uuid, timestamp, session="new", kind="user", *, counts=None,
        model=MODEL, message_id=None, request_id=None, **extra):
    result = {"uuid": uuid, "timestamp": timestamp, "sessionId": session, "type": kind}
    if counts is not None:
        result["message"] = {"id": message_id or uuid, "model": model,
                             "usage": dict(zip(COUNTS, counts))}
        result["requestId"] = request_id or uuid
    result.update(extra)
    return result


class Fixture:
    """Generate stats plus nested project/subagent transcripts beneath a temp root."""

    def __init__(self, root):
        self.root = Path(root)
        self.config = self.root / "claude-config"
        self.app = self.root / "app"
        self.projects = self.config / "projects"
        self.projects.mkdir(parents=True)
        self.app.mkdir()
        for name in ("build.py", "serve.py", "template.html"):
            shutil.copyfile(REPO / name, self.app / name)
        self.stats = {
            "dailyActivity": [
                {"date": "2026-01-01", "messageCount": 40, "sessionCount": 4},
                {"date": "2026-01-02", "messageCount": 60, "sessionCount": 6},
            ],
            "dailyModelTokens": [
                {"date": "2026-01-01", "tokensByModel": {MODEL: 1000}},
                {"date": "2026-01-02", "tokensByModel": {MODEL: 2000, OTHER_MODEL: 500}},
            ],
            "modelUsage": {
                MODEL: dict(zip(("inputTokens", "outputTokens", "cacheCreationInputTokens",
                                 "cacheReadInputTokens"), (100, 200, 300, 2400))),
                OTHER_MODEL: dict(zip(("inputTokens", "outputTokens", "cacheCreationInputTokens",
                                       "cacheReadInputTokens"), (50, 60, 70, 320))),
            },
            "totalSessions": 10, "totalMessages": 100,
            "longestSession": {"duration": 7200000}, "lastComputedDate": "2026-01-02",
        }
        self.write_stats()
        self.history = self.write_transcript("alpha/history.jsonl", [
            row("old-user", "2026-01-01T12:00:00Z", "old"),
            row("old-answer", "2026-01-01T13:00:00Z", "old", "assistant", counts=(999, 999, 999, 999)),
            row("cross-start", "2026-01-02T23:00:00Z", "cross"),
            row("cross-end", "2026-01-03T02:00:00Z", "cross"),
        ])
        self.recent = self.write_transcript("beta/recent.jsonl", [
            row("new-user", "2026-01-03T01:00:00Z"),
            row("reply-a", "2026-01-03T01:10:00Z", kind="assistant", counts=(10, 20, 30, 40),
                message_id="response", request_id="request"),
            row("reply-b", "2026-01-03T01:11:00Z", kind="assistant", counts=(15, 18, 35, 39),
                message_id="response", request_id="request"),
            row("meta", "2026-01-03T01:20:00Z", isMeta=True),
            row("synthetic", "2026-01-03T01:30:00Z", kind="assistant", counts=(900, 900, 900, 900),
                model="<synthetic>"),
            row("new-end", "2026-01-03T03:00:00Z"),
        ])
        self.sidechain = self.write_transcript("beta/subagents/agent.jsonl", [
            row("side-user", "2026-01-03T02:00:00Z", "side", isSidechain=True),
            row("side-answer", "2026-01-03T02:30:00Z", "side", "assistant", counts=(1, 2, 3, 4),
                model=OTHER_MODEL, isSidechain=True),
        ])
        self.next_day = self.write_transcript("gamma/next.jsonl", [
            row("next-user", "2026-01-03T06:00:00Z", "next"),
            row("next-answer", "2026-01-03T06:30:00Z", "next", "assistant", counts=(5, 6, 7, 8)),
        ])
        with self.recent.open("a", encoding="utf-8") as stream:
            stream.write('not json\n{"timestamp": broken\n')
            stream.write(json.dumps({"type": "progress", "timestamp": "2026-01-03T01:00:00Z",
                                     "sessionId": "ignored"}) + "\n")
            stream.write(json.dumps({"type": "user", "timestamp": "2026-01-03T01:00:00Z"}) + "\n")

    def write_stats(self, stats=None):
        (self.config / "stats-cache.json").write_text(
            json.dumps(self.stats if stats is None else stats), encoding="utf-8")

    def write_transcript(self, name, rows):
        path = self.projects / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
        return path

    def clear_transcripts(self):
        for path in self.projects.rglob("*.jsonl"):
            path.unlink()

    def load_build(self):
        spec = importlib.util.spec_from_file_location("isolated_build", self.app / "build.py")
        module = importlib.util.module_from_spec(spec)
        # BASE is evaluated during import. Never let it fall back to ~/.claude.
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            spec.loader.exec_module(module)
        return module

    def env(self):
        return {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config),
                "PYTHONDONTWRITEBYTECODE": "1", "TZ": "EST5"}


@contextlib.contextmanager
def fixed_local_time(module):
    """Model the machine's local zone as UTC-05 without global TZ changes/tzdata."""
    class LocalDateTime(dt.datetime):
        @classmethod
        def fromtimestamp(cls, timestamp, tz=None):
            if tz is not None:
                return super().fromtimestamp(timestamp, tz)
            return super().fromtimestamp(timestamp, dt.timezone(dt.timedelta(hours=-5))).replace(tzinfo=None)

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(dt.datetime.now(dt.timezone.utc).timestamp(), tz)

    clock = types.SimpleNamespace(datetime=LocalDateTime, timezone=dt.timezone, timedelta=dt.timedelta)
    with mock.patch.object(module, "dt", clock):
        yield
