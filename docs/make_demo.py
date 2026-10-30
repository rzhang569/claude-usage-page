"""Build docs/index.html: the real page filled with made-up data, for GitHub Pages.
Usage: python3 docs/make_demo.py [output path]"""
import datetime as dt
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "index.html"
MIX = {"claude-opus-5-5": 0.7, "claude-sonnet-5-5": 0.25, "claude-haiku-5-5": 0.05}
SPLIT = (0.0003, 0.004, 0.04, 0.9557)  # input, output, cache write, cache read


def demo_data(today):
    rng = random.Random(7)
    days, hours, sessions = {}, {}, []
    models = {name: [0, 0, 0, 0] for name in MIX}
    messages = total_sessions = 0
    for back in range(150):
        day = today - dt.timedelta(days=back)
        if rng.random() > (0.85 if back < 70 else 0.35):
            continue
        count, day_sessions = int(rng.lognormvariate(6.2, 0.7)), rng.randint(1, 6)
        tokens = count * rng.randint(70000, 140000)
        by_model, split = {}, [0, 0, 0, 0]
        for name, share in MIX.items():
            value = int(tokens * share * rng.uniform(0.6, 1.4))
            by_model[name] = value
            for i, part in enumerate(SPLIT):
                models[name][i] += int(value * part)
                split[i] += int(value * part)
        key = day.isoformat()
        days[key] = {"m": count, "tm": by_model, "sp": split, "sc": day_sessions}
        hours[key] = [0] * 24
        for _ in range(count):
            hours[key][min(23, max(0, int(rng.gauss(16, 4))))] += 1
        start = dt.datetime(day.year, day.month, day.day, 14).timestamp()
        sessions += [[start, start + rng.randint(600, 30000)] for _ in range(day_sessions)]
        messages += count
        total_sessions += day_sessions
    return {"generatedAt": f"{today.isoformat()}T21:30:00", "today": today.isoformat(), "days": days,
            "hours": hours, "models": models, "sessions": sessions,
            "totals": {"sessions": total_sessions, "messages": messages, "longest": 3 * 86400 + 5400}}


def main():
    html = (ROOT / "template.html").read_text(encoding="utf-8")
    data = json.dumps(demo_data(dt.date.today()), separators=(",", ":"))
    for old, new in (("/*__DATA__*/null", data),
                     ('<meta http-equiv="refresh" content="600">\n', ""),
                     ("<h1>Claude Code usage</h1>",
                      '<h1>Claude Code usage</h1>\n  <p style="color:var(--muted);margin:-8px 0 16px">'
                      'Live demo with made-up data. '
                      '<a href="https://github.com/rzhang569/claude-usage-page" style="color:var(--accent)">'
                      'Get it for your own usage</a></p>'),
                     ("from this computer's Claude Code history only", "demo data")):
        assert old in html, old
        html = html.replace(old, new)
    OUT.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
