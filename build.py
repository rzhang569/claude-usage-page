"""Build a local Claude Code and Codex usage card for this computer.

History comes from ~/.claude/stats-cache.json, the same totals `claude /stats`
shows (it keeps days whose transcripts Claude Code has since cleaned up).
Anything after that file was last written comes from
~/.claude/projects/**/*.jsonl. Both are only read. A parse cache next to this
script keeps what it has seen, so later cleanups lose nothing. index.html, and
the same data as data.json for the optional sidebar extension, are written next to this script. Nothing leaves the computer; deleting this folder
removes everything.

Codex comes from ~/.codex/sessions/**/*.jsonl, plus its state database for
threads with no readable token rows, such as its compressed session files. Only
counts are read, never conversations.
"""
import copy
import datetime as dt
import hashlib
import getpass
import glob
import json
import os
import pathlib
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
ROOT = os.path.join(BASE, "projects")
STATS = os.path.join(BASE, "stats-cache.json")
CODEX = os.path.abspath(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
CACHE = os.path.join(HERE, "cache-v4.json")
# Earlier releases keep a version 3 cache in cache.json, and can't read this one's entries. It is
# left as it was, so going back to one still works, and read once to carry its history over.
OLD_CACHE = os.path.join(HERE, "cache.json")
OUT = os.path.join(HERE, "index.html")
SNAPSHOT = os.path.join(HERE, "data.json")  # the same data on its own, for the sidebar extension
TEMPLATE = os.path.join(HERE, "template.html")
PRICING = os.path.join(HERE, "pricing.json")
CACHE_VERSION = 4
TAIL = 256  # bytes checked to tell a transcript appended to from one rewritten (only their hash is kept)
COPY_WINDOW = 5  # seconds: a fork's copy of its parent's history is written within this of the fork
IDLE = 1800  # a gap this long ends a task: waiting on the user, or the computer slept
# Session file rows worth decoding; the rest can be large and hold nothing to count.
CODEX_ROWS = (b'"event_msg"', b'"message"', b'"turn_context"', b'"session_meta"', b'"token_usage_record"',
              b'"web_search_call"')
CODEX_BACKGROUND = ("guardian_review", "subagent")  # threads that are not sessions a person started
# Cache entries parsed with the fields that pricing needs. Older entries of files still on disk
# are parsed again; files deleted since keep their counts and are left unpriced.
NEEDS = ("k", "p")
CODEX_NEEDS = ("k", "s", "rec", "mt", "wt", "inh", "recs")  # entries cached without these are read again
# Codex service tiers: standard ones price at the standard rates; any other tier is left unpriced.
TIERS = {None: "", "default": "", "auto": "", "standard": "", "priority": "|fast", "fast": "|fast",
         "ultrafast": "|ultrafast"}
FAST, US = 1, 2  # Claude reply flags: fast mode, US-only inference
OPENAI_LONG = 272000  # OpenAI prices a request over this many input tokens at its long-context rates


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


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()


def mark(f, offset):
    """Where parsing stopped, for resume_point to check next time: the length and a hash of the
    bytes just before it, never the bytes themselves, so the cache keeps no transcript text."""
    f.seek(max(0, offset - TAIL))
    tail = f.read(offset - f.tell())
    return {"offset": offset, "taillen": len(tail), "tailsum": fingerprint(tail)}


def resume_point(f, held):
    """Where a grown transcript can be picked up, or 0 if its known bytes are not still there."""
    held = held or {}
    offset, size = held.get("offset", 0), held.get("taillen", 0)
    if not offset or offset < size or "k" not in held or "tailsum" not in held:
        return 0
    f.seek(offset - size)
    return offset if fingerprint(f.read(size)) == held["tailsum"] else 0


def scrub(cache):
    """Replaces the raw tail bytes earlier versions kept with their length and hash. True if any."""
    changed = False
    for name in ("files", "codex"):
        for entry in (cache.get(name) or {}).values():
            if isinstance(entry, dict) and "tail" in entry:
                tail = bytes.fromhex(entry.pop("tail") or "")
                entry.update(taillen=len(tail), tailsum=fingerprint(tail))
                changed = True
    return changed


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
        # refresh only hands over an entry with this version's fields.
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
                split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
                tools = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
                flags = (FAST if usage.get("speed") == "fast" else 0) | (US if usage.get("inference_geo") == "us" else 0)
                reply = [t, session, side, model, usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
                         usage.get("cache_creation_input_tokens") or 0, usage.get("cache_read_input_tokens") or 0,
                         split.get("ephemeral_1h_input_tokens") or 0, flags, tools.get("web_search_requests") or 0]
                held = replies.get(key)
                if held:
                    merge_reply(held, reply)
                else:
                    replies[key] = reply
        end = mark(f, offset)
    return {"r": replies, "w": rows, "s": {k: list(v) for k, v in spans.items()}, "k": tasks, "p": 1, **end}


def merge_reply(held, reply):
    """One API response appears on a line per content block; streaming can leave smaller counts on
    the early ones, so each count keeps its largest value. Replies cached before pricing have only
    the first eight fields."""
    held[4:8] = [max(a, b) for a, b in zip(held[4:8], reply[4:8])]
    if len(reply) > 8 and len(held) > 8:
        held[8:] = [max(held[8], reply[8]), held[9] | reply[9], max(held[10], reply[10])]
    elif len(reply) > 8:
        held[8:] = reply[8:]


def apportion(total, weights):
    """total split in proportion to weights as whole numbers that add up to it exactly."""
    whole = sum(weights)
    exact = [total * w / whole for w in weights]
    shares = [int(x) for x in exact]
    for i in sorted(range(len(exact)), key=lambda i: shares[i] - exact[i])[:total - sum(shares)]:
        shares[i] += 1
    return shares


def days_between(start, end):
    return [utc_day(t) for t in range(int(start), int(max(start, end)) + 86400, 86400) if utc_day(t) <= utc_day(end)]


def model_id(model):
    """claude-opus-5-5-20261015, claude-opus-5-5[1m] and gpt-5.4-2026-03-05 are priced as their base model."""
    return re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2})$", "", re.sub(r"\[.*\]$", "", model))


def claude_cost(prices, model, i, o, cw, cr, cw1h, flags, searches):
    """Dollars for one Claude response at API list prices, or None if its price isn't known."""
    m = model_id(model)
    rates = prices["models"].get(m)
    if not rates:
        return None
    long = prices.get("long", {}).get(m)
    if long and i + cw + cr > long["over"]:
        rates = long["rates"]
    scale = 1.0
    if flags & FAST:
        if m not in prices.get("fast", {}):
            return None
        scale *= prices["fast"][m]
    if flags & US:
        scale *= prices["us_only"]
    cw1h = min(cw1h, cw)
    tokens = (i, o, cw - cw1h, cw1h, cr)
    return sum(n * r for n, r in zip(tokens, rates)) / 1e6 * scale + searches * prices["web_search"]


def tier_scale(spec, size, day):
    """A tier's multiplier from pricing.json: a number, per context size ({"short": 2.5}), or
    dated ({"until": "2027-02-28", "x": 3, "then": 6}). None when not listed."""
    if isinstance(spec, dict) and size in spec:
        spec = spec[size]
    elif isinstance(spec, dict) and ("short" in spec or "long" in spec):
        return None
    if isinstance(spec, dict):
        return spec["x"] if day <= spec["until"] else spec["then"]
    return spec


def codex_cost(prices, key, counts, day):
    """(dollars, unpriced tokens) for Codex usage under one price key (see codex_key) on one UTC day
    (a dated tier tag brings its own date).
    Each token type is priced on its own, so a type with no listed rate leaves only itself unpriced."""
    model, *tags = key.split("|")
    m = model_id(model)
    when = {}
    for n, tag in enumerate(tags):
        if tag.startswith("ultrafast@"):
            tags[n], when["ultrafast"] = tag.split("@")
    table = prices["models"].get(m)
    if not table or any(tag not in ("fast", "ultrafast", "long") for tag in tags):
        return 0.0, sum(counts)
    # Models listed with a single rate have no long-context price of their own.
    size = "long" if "long" in tags and "long" in table else "short"
    scale = 1.0
    for tier in ("fast", "ultrafast"):
        if tier in tags:
            scale = tier_scale(prices.get(tier, {}).get(m), size, when.get(tier, day))
            if scale is None:
                return 0.0, sum(counts)
    dollars = unpriced = 0
    for n, rate in zip(counts, table[size]):
        if rate is None:
            unpriced += n
        else:
            dollars += n * rate
    return dollars / 1e6 * scale, unpriced


def add_cost(day, model, dollars, unpriced, tokens):
    """Per day: c, dollars by model; u, tokens that could not be priced; um, the same by model;
    ct, the tokens the estimate covers (priced or not), each response counted once, so u / ct is
    the unpriced share."""
    if dollars:
        day.setdefault("c", {})
        day["c"][model] = day["c"].get(model, 0) + dollars
    if unpriced:
        day["u"] = day.get("u", 0) + unpriced
        if model:
            day.setdefault("um", {})
            day["um"][model] = day["um"].get(model, 0) + unpriced
    day["ct"] = day.get("ct", 0) + tokens


def codex_review(thread):
    """Automatic approval reviews, which Codex's own total leaves out. Some are marked only by
    their source, as a subagent of kind guardian."""
    return thread.get("thread_source") == "guardian_review" or "guardian" in str(thread.get("source"))


def codex_counts(usage):
    cached, written = usage.get("cached_input_tokens") or 0, usage.get("cache_write_input_tokens") or 0
    return [max(0, (usage.get("input_tokens") or 0) - cached - written), usage.get("output_tokens") or 0, written, cached]


def codex_key(entry, usage, t):
    """The model plus what changes its price: "|fast" for the Fast (formerly Priority) tier,
    "|ultrafast", "|tier-<name>" for a tier that isn't priced (including "unrecorded": older
    Codex versions saved no tier, so a Fast session can't be told from a standard one), and
    "|long" for a request over the long-context threshold."""
    tier = entry.get("tier")
    tag = TIERS.get(tier, f"|tier-{tier}")
    if tag == "|ultrafast":
        # OpenAI dates Ultrafast price changes in Pacific time; keep the response's Pacific date.
        # UTC-8: the promotion ends in winter, outside daylight saving time.
        tag += "@" + dt.datetime.fromtimestamp(t - 8 * 3600, dt.timezone.utc).strftime("%Y-%m-%d")
    key = entry["model"] + tag
    if (usage.get("input_tokens") or 0) > OPENAI_LONG:
        key += "|long"
    return key


def tally(usage, t, model, counts):
    by_model = usage.setdefault(utc_day(t), {})
    by_model[model] = add(by_model.get(model, [0, 0, 0, 0]), counts)


def parse_codex(path, held=None):
    """One Codex session file. recs: its token_usage_record rows, one per API response, by
    response id: [thread, time, price key, [input, output, cache write, cache read]], so a
    response that shows up in more than one file (a fork's copy, a reverted thread's replay)
    counts once; usage: those without an id, per UTC day and price key; running: the same
    from the running totals in token_count rows, for files older than those; mt: messages by
    id, [time, owner if copied from a parent], so one a fork copied counts once; wt: hosted web
    searches the same way, [time, model, owner]; m and h: messages
    without an id, per day and per day and local hour; k: tasks in the shape of track_task,
    timed by the run time Codex records for each, which is the figure its own profile page
    shows; last: when the thread itself last did something. Read incrementally like parse.

    A fork's file can open with its parent's history: records naming the parent, kept under the
    parent so they can price it if its own file is gone, and running totals that go on
    counting from the parent's. Codex writes that copy all at once when it forks, so it ends at
    the first row naming this thread or the first row written after that moment (the fork's own
    metadata, or for an old fork that copied the parent's metadata too, the time in its file
    name); inh is the running total it brought along, which Codex's database counts for the
    fork as well."""
    with open(path, "rb") as f:
        offset = resume_point(f, held)
        # The thread is the one the file is named after: a fork can start with its parent's metadata.
        # A reverted thread's file adds its own id after the thread's: <thread>_<rollout>.jsonl.
        named = re.search(r"(?:rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)-)?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
                          r"(?:_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})?\.jsonl$", os.path.basename(path))
        born = named and named.group(1) and dt.datetime.strptime(named.group(1), "%Y-%m-%dT%H-%M-%S").timestamp()
        entry = held if offset else {"id": named and named.group(2), "kind": None, "review": False, "first": None,
                                     "model": "?", "tier": "unrecorded", "seen": [0, 0, 0, 0], "usage": {},
                                     "running": {}, "recs": {}, "cp": {}, "inh": 0, "copied": None, "parent": None,
                                     "forked": None, "last": None,
                                     "m": {}, "h": {}, "k": {}, "wt": {}, "mt": {}, "s": 1, "rec": False}
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
            if kind == "session_meta" and not entry["first"]:
                header = body.get("id")
                if entry["id"] and header not in (None, entry["id"]):  # an old fork's copy of its parent's
                    entry.update(copied="header", parent=header, first=born or t, forked=born or t)
                else:
                    entry.update(id=entry["id"] or header, first=t, parent=body.get("forked_from_id"), forked=t,
                                 copied="fork" if body.get("forked_from_id") else None)
                entry.update(kind=body.get("thread_source"), review=codex_review(body))
            owner = body.get("thread_id") or (body.get("item") or {}).get("thread_id")
            if entry["copied"] and (owner == entry["id"] or t > entry["forked"] + COPY_WINDOW):
                entry["copied"] = None
                entry["fresh"] = True  # the next running total still carries what the fork inherited
            copied = bool(entry["copied"])
            if kind == "turn_context":
                entry["model"] = body.get("model") or entry["model"]
            elif event == "thread_settings_applied":
                # A full snapshot: Codex leaves service_tier out when it is cleared to the standard tier.
                entry["tier"] = (body.get("thread_settings") or {}).get("service_tier")
            if entry["review"]:
                continue
            if kind == "token_usage_record" and body.get("usage"):
                if not entry["rec"]:
                    entry.update(rec=True, running={})  # records from here on: running totals aren't needed
                key, counts = codex_key(entry, body["usage"], t), codex_counts(body["usage"])
                whose = entry["id"] if owner in (None, entry["id"]) and not copied else owner or entry["parent"] or "?"
                if body.get("response_id"):
                    entry["recs"].setdefault(body["response_id"], [whose, t, key, counts])
                elif whose == entry["id"]:
                    tally(entry["usage"], t, key, counts)
                else:  # a copy with no id to match it by: only for a parent with nothing else
                    tally(entry["cp"].setdefault(whose, {}), t, key, counts)
            elif event == "token_count" and (body.get("info") or {}).get("total_token_usage"):
                now = codex_counts(body["info"]["total_token_usage"])
                last = body["info"].get("last_token_usage")
                if not copied and entry.pop("fresh", False) and not any(entry["seen"]) and last:
                    # A fork that copied nothing starts its running totals from its parent's.
                    entry["seen"] = [max(0, a - b) for a, b in zip(now, codex_counts(last))]
                    entry["inh"] = sum(entry["seen"])
                spent = [max(0, a - b) for a, b in zip(now, entry["seen"])]
                if copied:
                    entry["inh"] = sum(now)
                elif not entry["rec"]:
                    # The request's size, for the long-context rate: its own usage, else this step's input.
                    size = last or {"input_tokens": spent[0] + spent[2] + spent[3]}
                    tally(entry["running"], t, codex_key(entry, size, t), spent)
                entry["seen"] = now
            if kind == "response_item" and body.get("type") == "message" and body.get("role") in ("user", "assistant"):
                key, hour = utc_day(t), dt.datetime.fromtimestamp(t).hour
                if body.get("id"):
                    # A copied one belongs to the parent, and is dated by when the copy was made.
                    entry["mt"].setdefault(body["id"], [t, entry["parent"] if copied else None])
                elif not copied:
                    entry["m"][key] = entry["m"].get(key, 0) + 1
                    entry["h"].setdefault(key, [0] * 24)[hour] += 1
            search = (body.get("item") or {}) if event == "item_completed" else body if kind == "response_item" else {}
            if (search.get("kind") == "web.search" or search.get("type") == "web_search_call") \
                    and (search.get("action") or {}).get("type") == "search" and search.get("status") in (None, "completed") \
                    and (search.get("id") or not copied):
                # Hosted web searches are charged per search; page opens and finds in a page aren't.
                # Kept by id, so a search a forked thread copies from its parent counts once.
                sid = search.get("id") or f"{path}:{offset}"
                entry["wt"].setdefault(sid, [t, entry["model"], entry["parent"] if copied else None])
            if copied:
                continue
            entry["last"] = max(entry["last"] or t, t)
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


def codex_data(cache, prices=None):
    """The same shape as the Claude Code data, or None without any Codex history. With prices,
    days also get their cost (see add_cost)."""
    if not os.path.isdir(CODEX):
        return None  # Removed altogether. The cache keeps its history in case it comes back.
    files = cache.setdefault("codex", {})
    found = [path for folder in ("sessions", "archived_sessions")
             for path in glob.glob(os.path.join(CODEX, folder, "**", "*.jsonl"), recursive=True)]
    unread = refresh(files, found, parse_codex, CODEX_NEEDS)
    # Archiving moves a session file into archived_sessions under the same name, and the cache
    # keeps the old path's entry: count each file name once, from where it is now, else from the
    # copy that had seen the most of it.
    here, chosen = set(found) - unread, {}
    for path, entry in files.items():
        name = os.path.basename(path)
        rank = (path in here, entry["stamp"][0])
        if name not in chosen or rank > chosen[name][0]:
            chosen[name] = (rank, entry)
    entries = [entry for _, entry in chosen.values()]
    readable = {entry["id"] for (present, _), entry in chosen.values() if present}

    days, hours, models = {}, {}, {}
    totals = {"sessions": 0, "messages": 0}

    def day(key):
        return days.setdefault(key, {"m": 0, "tm": {}, "sp": [0, 0, 0, 0], "sc": 0})

    def count(spent, priced_ok):
        """Adds a file's usage to the days and models, and returns its token total."""
        whole = 0
        for key, by_model in spent.items():
            d = day(key)
            for priced, counts in by_model.items():
                model, total = priced.split("|")[0], sum(counts)
                whole += total
                d["tm"][model] = d["tm"].get(model, 0) + total
                d["sp"] = add(d["sp"], counts)
                split = d.setdefault("ms", {})  # the day's own split per model
                split[model] = add(split.get(model, [0, 0, 0, 0]), counts)
                models[model] = add(models.get(model, [0, 0, 0, 0]), counts)
                if prices:
                    dollars, unpriced = codex_cost(prices, priced, counts, key) if priced_ok else (0, total)
                    add_cost(d, model, dollars, unpriced, total)
        return whole

    # A copy is written when the fork is made: what it holds of the parent is dated no later
    # than the parent's own last activity, so old usage doesn't land in a recent range.
    threads = codex_threads()
    last_active = {row["id"]: row.get("updated_at") for row in threads if row.get("updated_at")}

    def when(t, owner):
        return min(t, last_active[owner]) if owner and owner in last_active else t

    # base: what the files account for in a thread's database total, its own usage plus what a
    # fork inherited; seen: when the cache last saw the thread do something; replies: each API
    # response once, from its own thread's file where that has it, else from a copy; legacy:
    # tokens of a thread cached by an earlier version, which kept no response ids.
    base, seen, replies, legacy, said = {}, {}, {}, {}, {}
    started, searched, spoken = set(), set(), set()
    ordered = sorted(entries, key=lambda e: e["first"] or 0)
    for entry in ordered:
        for rid, (owner, t, key, counts) in entry.get("recs", {}).items():
            if rid not in replies or owner == entry["id"] and not replies[rid][4]:
                replies[rid] = (owner, t, key, counts, owner == entry["id"], "s" in entry)
        if "recs" not in entry:
            spent = entry["usage"] or entry["running"]
            legacy[entry["id"]] = legacy.get(entry["id"], 0) + sum(sum(c) for by in spent.values() for c in by.values())
    # An earlier version of this page cached some threads without response ids: their totals,
    # already spread over the right days with their messages, stay; a fork's copy of such a
    # thread adds only what came after them.
    for entry in ordered:
        # A file with per-response records uses only those: its running totals repeat them.
        # Entries kept for deleted files predate the flag: they used records whenever there were any.
        spent = entry["usage"] if entry.get("rec", bool(entry["usage"])) else entry["running"]
        thread = entry["id"]
        base[thread] = base.get(thread, 0) + count(spent, "s" in entry) + count(entry.get("surplus", {}), False) \
            + entry.get("inh", 0)
        seen[thread] = max(seen.get(thread) or 0, entry.get("last") or entry["first"] or 0)
        if prices:
            fees = {**entry.get("ws", {}),  # cached before wt: [day, model]
                    **{sid: [utc_day(when(t, owner)), model] for sid, (t, model, owner) in entry.get("wt", {}).items()
                       if owner not in legacy}}
            for sid, (key, model) in fees.items():
                if sid not in searched:
                    searched.add(sid)
                    add_cost(day(key), model, prices["web_search"], 0, 0)
        mine = said.setdefault(thread, {})
        for key, n in entry["m"].items():
            mine[key] = mine.get(key, 0) + n
        for key, row in entry["h"].items():
            hours[key] = add(hours.get(key, [0] * 24), row)
        dated = [(mid, None, None, None, held) for mid, held in entry.get("mi", {}).items()]  # cached before mt
        dated += [(mid, t, owner, None, None) for mid, (t, owner) in entry.get("mt", {}).items()]
        for mid, t, owner, _, held in dated:
            # A copied message of a thread an earlier version cached is already in its counts.
            if mid in spoken or owner in legacy:
                continue
            spoken.add(mid)
            if held:
                key, hour = held
            else:
                t = when(t, owner)
                key, hour = utc_day(t), dt.datetime.fromtimestamp(t).hour
            theirs = said.setdefault(owner or thread, {})
            theirs[key] = theirs.get(key, 0) + 1
            hours.setdefault(key, [0] * 24)[hour] += 1
        if entry["first"] and entry["kind"] not in CODEX_BACKGROUND and thread not in started:
            started.add(thread)
            day(utc_day(entry["first"]))["sc"] += 1
            totals["sessions"] += 1
    messages = {thread: by_day for thread, by_day in said.items() if by_day}
    for by_day in messages.values():
        for key, n in by_day.items():
            day(key)["m"] += n
            totals["messages"] += n

    # Responses only a copy has: the earliest of them can be ones an earlier version of this page
    # already counted, without ids, from the parent's own file: skip that many tokens' worth.
    grouped = {}
    for owner, t, key, counts, own, priced_ok in replies.values():
        if not own and legacy.get(owner, 0) > 0:
            legacy[owner] -= sum(counts)
            continue
        t = t if own else when(t, owner)
        tally(grouped.setdefault((owner, priced_ok), {}), t, key, counts)
        seen[owner] = max(seen.get(owner) or 0, t)
    for (owner, priced_ok), spent in grouped.items():
        base[owner] = base.get(owner, 0) + count(spent, priced_ok)
    # Copies with no response id can't be matched to their parent's own records: they price
    # only a parent nothing else accounts for, from the fullest copy, dated like the rest.
    copies = {}
    for entry in entries:
        for owner, spent in entry.get("cp", {}).items():
            size = sum(sum(c) for by_model in spent.values() for c in by_model.values())
            if owner not in base and size > copies.get(owner, (0,))[0]:
                copies[owner] = (size, spent, "s" in entry)
    for owner, (size, spent, priced_ok) in copies.items():
        if owner in last_active:
            bound, moved = utc_day(last_active[owner]), {}
            for key, by_model in spent.items():
                for priced, counts in by_model.items():
                    held = moved.setdefault(min(key, bound), {})
                    held[priced] = add(held.get(priced, [0, 0, 0, 0]), counts)
            spent = moved
        base[owner] = count(spent, priced_ok)

    # The rest of a thread's database total is usage no file shows: a thread known only from the
    # database, or what one did after the cache last saw its file, before Codex compressed it.
    # A readable file with token rows is the whole story. The rest is only a total: shared by
    # message where the file had them, otherwise over the days with other activity in the part
    # of the thread's life the cache didn't see, so it doesn't invent active days, and split like
    # the rest of that model's usage.
    worked = set(days)
    overall = [sum(c) for c in zip(*models.values())]
    overall = overall if any(overall) else [0, 0, 0, 1]
    for thread in threads:
        tokens, created = thread.get("tokens_used") or 0, thread.get("created_at") or 0
        if codex_review(thread):
            continue
        tid = thread["id"]
        if tid not in started and thread.get("thread_source") not in CODEX_BACKGROUND:
            day(utc_day(created))["sc"] += 1
            totals["sessions"] += 1
        if tid in readable and base.get(tid):
            continue
        tokens = max(0, tokens - base.get(tid, 0))
        if not tokens:
            continue
        model = thread.get("model") or "?"
        mix = models[model] if any(models.get(model, ())) else overall
        # After a counted part, the rest came after the day the cache last saw the thread.
        later = bool(base.get(tid))
        start = seen.get(tid, created) if later else created
        end = max(start, thread.get("updated_at") or start)
        span = days_between(start, end)[1 if later else 0:]
        active = (None if later else messages.get(tid)) \
            or dict.fromkeys(sorted(worked.intersection(span)), 1) or {utc_day(end): 1}
        for key, share in zip(active, apportion(tokens, list(active.values()))):
            d = day(key)
            d["tm"][model] = d["tm"].get(model, 0) + share
            d["sp"] = None
            if prices:
                add_cost(d, model, 0, share, share)  # only a total: no mix to price
        models[model] = add(models.get(model, [0, 0, 0, 0]), apportion(tokens, mix))

    if not days:
        return None
    return {"today": utc_day(time.time()), "days": days, "hours": hours, "models": models, "totals": totals,
            "tasks": longest_tasks(entries),
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


def refresh(files, paths, parser, needs=NEEDS):
    """Parse new or changed files, and return the ones that couldn't be read. Files deleted since
    stay in the cache on purpose."""
    unread = set()
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            unread.add(path)
            continue
        stamp = [st.st_size, st.st_mtime_ns]
        held = files.get(path)
        if held and held.get("stamp") == stamp and all(k in held for k in needs):
            continue
        grew = held and held["stamp"][0] < st.st_size and all(k in held for k in needs)
        try:
            # A copy: a read that fails halfway must leave the cached entry as it was.
            files[path] = {**parser(path, copy.deepcopy(held) if grew else None), "stamp": stamp}
        except OSError:
            unread.add(path)  # Deleted or locked since the glob; the next build picks it up.
    return unread


def codex_surplus(old, new):
    """What an earlier release's entry for a Codex file holds beyond this version's, per UTC day
    and model: only totals, so it counts unpriced."""
    def plain(spent, into, sign):
        for key, by_model in spent.items():
            for priced, counts in by_model.items():
                held = into.setdefault(key, {}).setdefault(priced.split("|")[0], [0, 0, 0, 0])
                into[key][priced.split("|")[0]] = [a + sign * b for a, b in zip(held, counts)]
    left = {}
    plain(old["usage"] or old["running"], left, 1)
    mine = {}
    for owner, t, key, counts in new.get("recs", {}).values():
        if owner == new["id"]:
            tally(mine, t, key, counts)
    plain(mine, left, -1)
    plain(new["usage"] if new.get("rec") else new["running"], left, -1)
    return {key: {model: [max(0, n) for n in counts] for model, counts in by_model.items() if any(n > 0 for n in counts)}
            for key, by_model in left.items() if any(any(n > 0 for n in c) for c in by_model.values())}


def load_cache():
    """This version's cache, with what an earlier release has added to its own since: after going
    back to one and then forward again, files it saw that are gone now live only there."""
    def valid(cache, version):
        return isinstance(cache, dict) and cache.get("v") == version and isinstance(cache.get("files"), dict)
    cache = load_json(CACHE, None)
    ok = valid(cache, CACHE_VERSION)
    cache = cache if ok else {"v": CACHE_VERSION, "files": {}}
    try:
        newer = not ok or os.path.getmtime(OLD_CACHE) > os.path.getmtime(CACHE)
    except OSError:
        newer = False
    old = load_json(OLD_CACHE, None) if newer else None
    if valid(old, 3):
        for name in ("files", "codex"):
            mine = cache.setdefault(name, {})
            for path, entry in (old.get(name) or {}).items():
                # Its entries lack this version's fields, so a file still on disk is read again.
                if path not in mine:
                    mine[path] = entry
                elif entry.get("stamp", [0])[0] > mine[path]["stamp"][0] and not os.path.exists(path):
                    # It saw more of a file that is gone now: keep what this version priced, and
                    # add what it saw beyond that.
                    if name == "files":
                        mine[path] = {**entry, "r": {**entry["r"], **mine[path]["r"]}}
                    else:
                        mine[path] = {**mine[path], "surplus": codex_surplus(entry, mine[path])}
    scrub(cache)
    # The earlier release's cache gets the same clean-up once. It still reads it: with no tail
    # bytes it just stops checking a grown file before reading on. Its time stays as it was.
    if not cache.get("scrubbed") and os.path.exists(OLD_CACHE):
        old = old if old is not None else load_json(OLD_CACHE, None)
        if isinstance(old, dict) and scrub(old):
            try:
                when = os.stat(OLD_CACHE)
                write_atomically(OLD_CACHE, json.dumps(old, separators=(",", ":")))
                os.utime(OLD_CACHE, ns=(when.st_atime_ns, when.st_mtime_ns))
            except OSError:
                return cache  # try again next build
    cache["scrubbed"] = True
    return cache


def main():
    cache = load_cache()
    files = cache["files"]
    refresh(files, glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True), parse)

    replies, rows, spans = {}, {}, {}
    for entry in files.values():
        for key, reply in entry["r"].items():
            held = replies.get(key)
            if held:
                merge_reply(held, reply)
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
    stats_tokens = {}  # per stats-cache.json day and model, where no transcript is left to count instead

    def count(key, model, counts):
        d = day(key)
        d["tm"][model] = d["tm"].get(model, 0) + sum(counts)
        d["sp"] = [a + b for a, b in zip(d["sp"], counts)]
        split = d.setdefault("ms", {})  # the day's own split per model
        split[model] = [a + b for a, b in zip(split.get(model, [0, 0, 0, 0]), counts)]
        all_models[model] = [a + b for a, b in zip(all_models.get(model, [0, 0, 0, 0]), counts)]

    # Before the cutoff, stats-cache.json days are UTC days. Its token totals run about twice the
    # responses behind them (it counts some more than once), while Claude Code's own cost tracking
    # matches one count per response: so where transcripts survive for a day and model, they are
    # what counts, each response once, and stats-cache.json fills in only where none are left.
    before = {}
    for reply in replies.values():
        # With no cutoff, every transcript overlaps stats-cache.json's days.
        if reply[0] < cutoff or not cutoff:
            key = (utc_day(reply[0]), reply[3])
            before[key] = [a + b for a, b in zip(before.get(key, [0, 0, 0, 0]), reply[4:8])]
    for (key, model), counts in before.items():
        if cutoff:
            count(key, model, counts)

    # History up to the moment stats-cache.json was written.
    if stats:
        for a in stats.get("dailyActivity", []):
            d = day(a["date"])
            d["m"] += a.get("messageCount", 0)
            d["sc"] += a.get("sessionCount", 0)
        mix = {model: [u.get("inputTokens", 0), u.get("outputTokens", 0), u.get("cacheCreationInputTokens", 0),
                       u.get("cacheReadInputTokens", 0)] for model, u in stats.get("modelUsage", {}).items()}
        for a in stats.get("dailyModelTokens", []):
            for model, n in a.get("tokensByModel", {}).items():
                if (a["date"], model) in before or not n:
                    continue
                d = day(a["date"])
                d["tm"][model] = d["tm"].get(model, 0) + n
                d["split"] = False  # only a total: its input / output / cache split is estimated
                by_model = stats_tokens.setdefault(a["date"], {})
                by_model[model] = by_model.get(model, 0) + n
                # Split like that model's all-time mix, as the page does for the day.
                ratio = mix.get(model) if any(mix.get(model, ())) else [0, 0, 0, 1]
                all_models[model] = [a + b for a, b in zip(all_models.get(model, [0, 0, 0, 0]), apportion(n, ratio))]
        totals["sessions"] = stats.get("totalSessions", 0)
        totals["messages"] = stats.get("totalMessages", 0)

    # Everything after it, from transcripts.
    for reply in replies.values():
        if reply[0] >= cutoff:
            count(local_day(reply[0]), reply[3], reply[4:8])
    for t, session, side in rows.values():
        if t >= cutoff:
            day(local_day(t))["m"] += 1
            totals["messages"] += 1
    for sid, (lo, hi) in spans.items():
        if lo >= cutoff:
            day(local_day(lo))["sc"] += 1
            totals["sessions"] += 1

    # Cost at API list prices, each response once, on the day the page counts it. Usage known
    # only from stats-cache.json totals has no per-response detail, so it stays unpriced.
    prices = load_json(PRICING, None)
    if prices:
        for reply in replies.values():
            t, model, counts = reply[0], reply[3], reply[4:8]
            key = local_day(t) if t >= cutoff else utc_day(t)
            dollars = claude_cost(prices["claude"], model, *counts, *reply[8:]) if len(reply) > 8 else None
            if dollars is None:
                # Tokens without a known price stay unpriced; the flat search fee is still known.
                fee = (reply[10] if len(reply) > 10 else 0) * prices["claude"]["web_search"]
                add_cost(day(key), model, fee, sum(counts), sum(counts))
            else:
                add_cost(day(key), model, dollars, 0, sum(counts))
        for key, by_model in stats_tokens.items():
            for model, n in by_model.items():
                add_cost(day(key), model, 0, n, n)

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
        # Some all-time model totals include days known only as totals, split by estimate.
        "estimated": any(d["sp"] is None for d in days.values()),
        # History before the cutoff is priced from the transcripts still on this computer, which
        # may not hold all of it; stats-cache.json totals can't tell how much is missing.
        "pricing": prices and {"reviewed": prices.get("reviewed"), "claude": prices["claude"].get("source"),
                               "openai": prices["openai"].get("source"),
                               "partialBefore": stats.get("lastComputedDate") if stats else None},
    }
    try:
        data["codex"] = codex_data(cache, prices and prices["openai"])
    except Exception as error:  # Trouble with Codex's files must not take the Claude Code page down.
        print(f"skipped Codex: {error!r}", file=sys.stderr)
        data["codex"] = None
    if not os.path.isdir(BASE):
        # Its folder is gone: the history stays in the cache in case it comes back, but the page
        # and the sidebar extension, which hands data.json to whatever calls it, leave it out.
        data.update(days={}, hours={}, models={}, tasks={}, totals={k: 0 for k in data["totals"]})

    write_atomically(CACHE, json.dumps(cache, separators=(",", ":")))

    snapshot = json.dumps(data, separators=(",", ":"))
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read().replace("/*__DATA__*/null", snapshot.replace("<", "\\u003c"))
    write_atomically(OUT, html)
    write_atomically(SNAPSHOT, snapshot)
    print(f"wrote {OUT}: {len(days)} days")


if __name__ == "__main__":
    main()
