"""Build a local Claude Code and Codex usage card for this computer.

History comes from ~/.claude/stats-cache.json, the same totals `claude /stats`
shows (it keeps days whose transcripts Claude Code has since cleaned up).
Anything after that file was last written comes from
~/.claude/projects/**/*.jsonl. Both are only read. A parse cache next to this
script keeps what it has seen, so later cleanups lose nothing. index.html is
written next to this script. Nothing leaves the computer; deleting this folder
removes everything.

Codex comes from ~/.codex/sessions/**/*.jsonl, plus its state database for
threads with no readable token rows, such as its compressed session files. Only
counts are read, never conversations.
"""
import datetime as dt
import getpass
import glob
import json
import os
import pathlib
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
ROOT = os.path.join(BASE, "projects")
STATS = os.path.join(BASE, "stats-cache.json")
CODEX = os.path.abspath(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
CACHE = os.path.join(HERE, "cache.json")
OUT = os.path.join(HERE, "index.html")
TEMPLATE = os.path.join(HERE, "template.html")
CACHE_VERSION = 3
TAIL = 256  # bytes kept to check that a transcript was appended to, not rewritten
IDLE = 1800  # a gap this long ends a task: waiting on the user, or the computer slept
# Session file rows worth decoding; the rest can be large and hold nothing to count.
CODEX_ROWS = (b'"event_msg"', b'"message"', b'"turn_context"', b'"session_meta"', b'"token_usage_record"')
CODEX_BACKGROUND = ("guardian_review", "subagent")  # threads that are not sessions a person started


def epoch(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def local_day(t):
    # stats-cache.json keys its days in UTC and can't be re-split; days after it use local time.
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def utc_day(t):
    # Codex's profile page splits days in UTC, so its days here do too.
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")


def add(a, b):
    return [x + y for x, y in zip(a, b)]


def mark(f, offset):
    """Where parsing stopped, for resume_point to check next time."""
    f.seek(max(0, offset - TAIL))
    return {"offset": offset, "tail": f.read(offset - f.tell()).hex()}


def resume_point(f, held):
    """Where a grown transcript can be picked up, or 0 if its known bytes are not still there."""
    offset = (held or {}).get("offset", 0)
    tail = bytes.fromhex((held or {}).get("tail", ""))
    if not offset or offset < len(tail) or "k" not in held:
        return 0
    f.seek(offset - len(tail))
    return offset if f.read(len(tail)) == tail else 0


def is_prompt(row):
    """A message the user typed, not a tool result or something Claude Code added."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") != "tool_result" for b in content)
    return bool(content)


def track_task(tasks, session, t, starts):
    """Per session [start, last, best per local day]. A task runs from its start to its
    last row, and a gap of IDLE seconds ends it early."""
    task = tasks.setdefault(session, [t, t, {}])
    if starts or t - task[1] > IDLE:
        key = local_day(task[0])
        task[2][key] = max(task[2].get(key, 0), task[1] - task[0])
        task[:2] = [t, t]
    task[1] = t


def longest_tasks(entries):
    longest = {}
    for entry in entries:
        for start, last, best in entry.get("k", {}).values():
            for key, seconds in [*best.items(), (local_day(start), last - start)]:
                if seconds:
                    longest[key] = max(longest.get(key, 0), seconds)
    return longest


def parse(path, held=None):
    """replies: one per API response; rows: user/assistant rows by uuid; spans: session first/last;
    tasks: see track_task, where a task starts at a prompt.

    Transcripts only grow and can reach hundreds of MB, so with a held entry for a grown
    file only the new bytes are read. A last line without its newline is read again next time.
    """
    with open(path, "rb") as f:
        offset = resume_point(f, held)
        replies, rows, spans, tasks = (held["r"], held["w"], held["s"], held["k"]) if offset else ({}, {}, {}, {})
        f.seek(offset)
        for raw in f:
            if raw.endswith(b"\n"):
                offset += len(raw)
            if b'"timestamp"' not in raw:
                continue
            try:
                row = json.loads(raw.decode("utf-8", "ignore"))
                ts, kind, session = row.get("timestamp"), row.get("type"), row.get("sessionId")
                if not ts or not session or kind not in ("user", "assistant"):
                    continue
                t = epoch(ts)
                prompt = is_prompt(row)
                message = row.get("message") or {}
                usage = message.get("usage") if kind == "assistant" else None
            except (ValueError, AttributeError, TypeError):
                continue
            side = 1 if row.get("isSidechain") else 0
            if not side:
                lo, hi = spans.get(session, (t, t))
                spans[session] = (min(lo, t), max(hi, t))
                track_task(tasks, session, t, prompt)
            if row.get("uuid") and not row.get("isMeta"):
                rows[row["uuid"]] = [t, session, side]
            if usage:
                model = message.get("model") or "?"
                if model == "<synthetic>":
                    continue
                key = f'{message.get("id")}|{row.get("requestId")}'
                counts = [usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
                          usage.get("cache_creation_input_tokens") or 0,
                          usage.get("cache_read_input_tokens") or 0]
                held = replies.get(key)
                if held:
                    held[4:] = [max(a, b) for a, b in zip(held[4:], counts)]
                else:
                    replies[key] = [t, session, side, model, *counts]
        end = mark(f, offset)
    return {"r": replies, "w": rows, "s": {k: list(v) for k, v in spans.items()}, "k": tasks, **end}


def days_between(start, end):
    return [utc_day(t) for t in range(int(start), int(max(start, end)) + 86400, 86400) if utc_day(t) <= utc_day(end)]


def codex_review(thread):
    """Automatic approval reviews, which Codex's own total leaves out. Some are marked only by
    their source, as a subagent of kind guardian."""
    return thread.get("thread_source") == "guardian_review" or "guardian" in str(thread.get("source"))


def codex_counts(usage):
    cached, written = usage.get("cached_input_tokens") or 0, usage.get("cache_write_input_tokens") or 0
    return [max(0, (usage.get("input_tokens") or 0) - cached - written), usage.get("output_tokens") or 0, written, cached]


def tally(usage, t, model, counts):
    by_model = usage.setdefault(utc_day(t), {})
    by_model[model] = add(by_model.get(model, [0, 0, 0, 0]), counts)


def parse_codex(path, held=None):
    """One Codex session file. usage: per UTC day and model [input, output, cache write,
    cache read], from its token_usage_record rows, one per API response; running: the same
    from the running totals in token_count rows, for files older than those; m and h: messages
    per day and per day and local hour; k: tasks in the shape of track_task, timed by the run
    time Codex records for each, which is the figure its own profile page shows. Read
    incrementally like parse."""
    with open(path, "rb") as f:
        offset = resume_point(f, held)
        entry = held if offset else {"id": None, "kind": None, "review": False, "first": None, "model": "?",
                                     "seen": [0, 0, 0, 0], "usage": {}, "running": {},
                                     "m": {}, "h": {}, "k": {}}
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            if not any(mark in raw for mark in CODEX_ROWS):
                continue
            try:
                row = json.loads(raw.decode("utf-8", "ignore"))
                kind, body, ts = row.get("type"), row.get("payload"), row.get("timestamp")
                if not ts or not isinstance(body, dict):
                    continue
                t = epoch(ts)
            except (ValueError, AttributeError, TypeError):
                continue
            event = body.get("type") if kind == "event_msg" else None
            if kind == "session_meta":
                if not entry["first"]:
                    entry.update(id=body.get("id"), kind=body.get("thread_source"), first=t, review=codex_review(body))
            elif kind == "turn_context":
                entry["model"] = body.get("model") or entry["model"]
            if entry["review"]:
                continue
            if kind == "token_usage_record" and body.get("usage"):
                tally(entry["usage"], t, entry["model"], codex_counts(body["usage"]))
            elif event == "token_count" and (body.get("info") or {}).get("total_token_usage"):
                now = codex_counts(body["info"]["total_token_usage"])
                tally(entry["running"], t, entry["model"], [max(0, a - b) for a, b in zip(now, entry["seen"])])
                entry["seen"] = now
            if kind == "response_item" and body.get("type") == "message" and body.get("role") in ("user", "assistant"):
                key = utc_day(t)
                entry["m"][key] = entry["m"].get(key, 0) + 1
                entry["h"].setdefault(key, [0] * 24)[dt.datetime.fromtimestamp(t).hour] += 1
            if event == "task_complete" and body.get("duration_ms"):
                best = entry["k"].setdefault(entry["id"] or path, [t, t, {}])[2]
                key = utc_day(body.get("started_at") or t)
                best[key] = max(best.get(key, 0), body["duration_ms"] / 1000)
        entry.update(mark(f, offset))
    return entry


def codex_threads():
    """Rows of Codex's state database, which also covers the session files it has compressed."""
    found = glob.glob(os.path.join(CODEX, "state_*.sqlite"))
    if not found:
        return []
    uri = pathlib.Path(max(found, key=os.path.getmtime)).as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True)
        try:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM threads")]
        finally:
            db.close()
    except sqlite3.Error:
        return []


def codex_data(cache):
    """The same shape as the Claude Code data, or None without any Codex history."""
    if not os.path.isdir(CODEX):
        return None  # Removed altogether. The cache keeps its history in case it comes back.
    files = cache.setdefault("codex", {})
    refresh(files, glob.glob(os.path.join(CODEX, "sessions", "**", "*.jsonl"), recursive=True), parse_codex)

    days, hours, models = {}, {}, {}
    totals = {"sessions": 0, "messages": 0}

    def day(key):
        return days.setdefault(key, {"m": 0, "tm": {}, "sp": [0, 0, 0, 0], "sc": 0})

    counted, started = {}, set()
    for entry in files.values():
        spent = entry["usage"] or entry["running"]
        counted[entry["id"]] = counted.get(entry["id"], 0) + sum(sum(c) for by_model in spent.values() for c in by_model.values())
        for key, by_model in spent.items():
            d = day(key)
            for model, counts in by_model.items():
                d["tm"][model] = d["tm"].get(model, 0) + sum(counts)
                d["sp"] = add(d["sp"], counts)
                models[model] = add(models.get(model, [0, 0, 0, 0]), counts)
        for key, n in entry["m"].items():
            day(key)["m"] += n
            totals["messages"] += n
        for key, row in entry["h"].items():
            hours[key] = add(hours.get(key, [0] * 24), row)
        if entry["first"] and entry["kind"] not in CODEX_BACKGROUND and entry["id"] not in started:
            started.add(entry["id"])
            day(utc_day(entry["first"]))["sc"] += 1
            totals["sessions"] += 1

    # Threads with no token rows of their own: only a total. Share it by message where the
    # file has them, otherwise over the days in the thread's life with other activity, so it
    # doesn't invent active days. Split it like the rest of that model's usage.
    messages = {entry["id"]: entry["m"] for entry in files.values() if entry["m"]}
    worked = set(days)
    overall = [sum(c) for c in zip(*models.values())]
    overall = overall if any(overall) else [0, 0, 0, 1]
    for thread in codex_threads():
        tokens, created = thread.get("tokens_used") or 0, thread.get("created_at") or 0
        if codex_review(thread):
            continue
        if thread["id"] not in counted and thread.get("thread_source") not in CODEX_BACKGROUND:
            day(utc_day(created))["sc"] += 1
            totals["sessions"] += 1
        if tokens and not counted.get(thread["id"]):
            model = thread.get("model") or "?"
            mix = models[model] if any(models.get(model, ())) else overall
            active = (messages.get(thread["id"])
                      or dict.fromkeys(worked.intersection(days_between(created, thread.get("updated_at") or created)), 1)
                      or {utc_day(created): 1})
            for key, weight in active.items():
                d = day(key)
                d["tm"][model] = d["tm"].get(model, 0) + round(tokens * weight / sum(active.values()))
                d["sp"] = None
            models[model] = add(models.get(model, [0, 0, 0, 0]), [round(tokens * b / sum(mix)) for b in mix])

    if not days:
        return None
    return {"today": utc_day(time.time()), "days": days, "hours": hours, "models": models, "totals": totals,
            "tasks": longest_tasks(files.values()),
            "estimated": any(d["sp"] is None for d in days.values())}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_atomically(path, text):
    # The page server, the hourly job and /my-usage can all build at once.
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def replace(src, dst):
    # On Windows the target can't be replaced while serve.py has it open; retry briefly.
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)


def refresh(files, paths, parser):
    """Parse new or changed files. Files deleted since stay in the cache on purpose."""
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        stamp = [st.st_size, st.st_mtime_ns]
        held = files.get(path)
        if held and held.get("stamp") == stamp and "k" in held:
            continue
        grew = held and held["stamp"][0] < st.st_size
        try:
            files[path] = {**parser(path, held if grew else None), "stamp": stamp}
        except OSError:
            continue  # Deleted or locked since the glob; the next build picks it up.


def main():
    cache = load_json(CACHE, {})
    if not isinstance(cache, dict) or cache.get("v") != CACHE_VERSION or not isinstance(cache.get("files"), dict):
        cache = {"v": CACHE_VERSION, "files": {}}
    files = cache["files"]
    refresh(files, glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True), parse)

    replies, rows, spans = {}, {}, {}
    for entry in files.values():
        for key, reply in entry["r"].items():
            held = replies.get(key)
            if held:
                held[4:] = [max(a, b) for a, b in zip(held[4:], reply[4:])]
            else:
                replies[key] = reply[:]
        rows.update(entry["w"])
        for sid, (lo, hi) in entry["s"].items():
            a, b = spans.get(sid, (lo, hi))
            spans[sid] = (min(a, lo), max(b, hi))

    stats = load_json(STATS, None)
    # The cache covers whole UTC days through lastComputedDate; later rows come from transcripts.
    cutoff = 0.0
    if stats and stats.get("lastComputedDate"):
        last = dt.datetime.strptime(stats["lastComputedDate"], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        cutoff = (last + dt.timedelta(days=1)).timestamp()

    days = {}

    def day(key):
        return days.setdefault(key, {"m": 0, "tm": {}, "sp": [0, 0, 0, 0], "split": True, "sc": 0})

    all_models = {}
    totals = {"sessions": 0, "messages": 0}

    # History up to the moment stats-cache.json was written.
    if stats:
        for a in stats.get("dailyActivity", []):
            d = day(a["date"])
            d["m"] += a.get("messageCount", 0)
            d["sc"] += a.get("sessionCount", 0)
            d["split"] = False
        for a in stats.get("dailyModelTokens", []):
            d = day(a["date"])
            for model, n in a.get("tokensByModel", {}).items():
                d["tm"][model] = d["tm"].get(model, 0) + n
            d["split"] = False
        for model, u in stats.get("modelUsage", {}).items():
            all_models[model] = [u.get("inputTokens", 0), u.get("outputTokens", 0),
                                 u.get("cacheCreationInputTokens", 0), u.get("cacheReadInputTokens", 0)]
        totals["sessions"] = stats.get("totalSessions", 0)
        totals["messages"] = stats.get("totalMessages", 0)

    # Everything after it, from transcripts.
    for t, session, side, model, i, o, cw, cr in replies.values():
        if t < cutoff:
            continue
        d = day(local_day(t))
        counts = (i, o, cw, cr)
        d["tm"][model] = d["tm"].get(model, 0) + sum(counts)
        for n, v in enumerate(counts):
            d["sp"][n] += v
        acc = all_models.setdefault(model, [0, 0, 0, 0])
        for n, v in enumerate(counts):
            acc[n] += v
    for t, session, side in rows.values():
        if t >= cutoff:
            day(local_day(t))["m"] += 1
            totals["messages"] += 1
    for sid, (lo, hi) in spans.items():
        if lo >= cutoff:
            day(local_day(lo))["sc"] += 1
            totals["sessions"] += 1

    # Hours of day from every transcript row seen (history included), for peak hour.
    hours = {}
    for t, session, side in rows.values():
        local = dt.datetime.fromtimestamp(t)
        h = hours.setdefault(local.strftime("%Y-%m-%d"), [0] * 24)
        h[local.hour] += 1

    for d in days.values():
        if not d["split"]:
            d["sp"] = None
        del d["split"]

    data = {
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "today": dt.datetime.now().strftime("%Y-%m-%d"),
        "user": getpass.getuser(),
        "claude": os.path.isdir(BASE) and bool(days),
        "days": days,
        "hours": hours,
        "models": all_models,
        "totals": totals,
        "tasks": longest_tasks(files.values()),
    }
    try:
        data["codex"] = codex_data(cache)
    except Exception as error:  # Trouble with Codex's files must not take the Claude Code page down.
        print(f"skipped Codex: {error!r}", file=sys.stderr)
        data["codex"] = None

    write_atomically(CACHE, json.dumps(cache, separators=(",", ":")))

    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    write_atomically(OUT, html)
    print(f"wrote {OUT}: {len(days)} days")


if __name__ == "__main__":
    main()
