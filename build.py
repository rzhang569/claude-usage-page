"""Build a local Claude Code usage card for this computer.

History comes from ~/.claude/stats-cache.json, the same totals `claude /stats`
shows (it keeps days whose transcripts Claude Code has since cleaned up).
Anything after that file was last written comes from
~/.claude/projects/**/*.jsonl. Both are only read. A parse cache next to this
script keeps what it has seen, so later cleanups lose nothing. index.html is
written next to this script. Nothing leaves the computer; deleting this folder
removes everything.
"""
import datetime as dt
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
ROOT = os.path.join(BASE, "projects")
STATS = os.path.join(BASE, "stats-cache.json")
CACHE = os.path.join(HERE, "cache.json")
OUT = os.path.join(HERE, "index.html")
TEMPLATE = os.path.join(HERE, "template.html")
CACHE_VERSION = 3


def epoch(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def local_day(t):
    # stats-cache.json keys its days in UTC and can't be re-split; days after it use local time.
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def parse(path):
    """replies: one per API response; rows: user/assistant rows by uuid; spans: session first/last."""
    replies, rows, spans = {}, {}, {}
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"timestamp"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ts, kind, session = row.get("timestamp"), row.get("type"), row.get("sessionId")
            if not ts or not session or kind not in ("user", "assistant"):
                continue
            t = epoch(ts)
            side = 1 if row.get("isSidechain") else 0
            if not side:
                lo, hi = spans.get(session, (t, t))
                spans[session] = (min(lo, t), max(hi, t))
            if row.get("uuid") and not row.get("isMeta"):
                rows[row["uuid"]] = [t, session, side]
            message = row.get("message") or {}
            if kind == "assistant" and message.get("usage"):
                model = message.get("model") or "?"
                if model == "<synthetic>":
                    continue
                u = message["usage"]
                key = f'{message.get("id")}|{row.get("requestId")}'
                counts = [u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                          u.get("cache_creation_input_tokens") or 0,
                          u.get("cache_read_input_tokens") or 0]
                held = replies.get(key)
                if held:
                    held[4:] = [max(a, b) for a, b in zip(held[4:], counts)]
                else:
                    replies[key] = [t, session, side, model, *counts]
    return {"r": replies, "w": rows, "s": {k: list(v) for k, v in spans.items()}}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    cache = load_json(CACHE, {})
    if cache.get("v") != CACHE_VERSION:
        cache = {"v": CACHE_VERSION, "files": {}}
    files = cache["files"]
    # Files Claude Code has deleted stay in the cache on purpose.
    for path in glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        stamp = [st.st_size, int(st.st_mtime)]
        held = files.get(path)
        if held and held.get("stamp") == stamp:
            continue
        files[path] = {"stamp": stamp, **parse(path)}

    replies, rows, spans = {}, {}, {}
    for entry in files.values():
        replies.update(entry["r"])
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
    totals = {"sessions": 0, "messages": 0, "longest": 0.0}

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
        totals["longest"] = (stats.get("longestSession") or {}).get("duration", 0) / 1000

    # Everything after it, from transcripts.
    for t, session, side, model, i, o, cw, cr in replies.values():
        if t <= cutoff:
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
        if t > cutoff:
            day(local_day(t))["m"] += 1
            totals["messages"] += 1
    for sid, (lo, hi) in spans.items():
        if lo > cutoff:
            day(local_day(lo))["sc"] += 1
            totals["sessions"] += 1
        totals["longest"] = max(totals["longest"], hi - lo)

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
        "days": days,
        "hours": hours,
        "models": all_models,
        "totals": totals,
        "sessions": [[lo, hi] for lo, hi in spans.values()],
    }

    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    os.replace(tmp, CACHE)

    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, OUT)
    print(f"wrote {OUT}: {len(days)} days")


if __name__ == "__main__":
    main()
