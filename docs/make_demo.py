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
CODEX_MIX = {"gpt-6.1-sol": 0.75, "gpt-6.1-terra": 0.25}
SPLIT = (0.0003, 0.004, 0.04, 0.9557)  # input, output, cache write, cache read
PRICES = json.loads((ROOT / "pricing.json").read_text(encoding="utf-8"))


def demo_cost(name, parts):
    """The day's made-up usage at the list prices in pricing.json (Claude cache writes as 1-hour ones)."""
    i, o, cw, cr = parts
    if name.startswith("claude-"):
        r = PRICES["claude"]["models"][name]
        return round((i * r[0] + o * r[1] + cw * r[3] + cr * r[4]) / 1e6, 4)
    r = PRICES["openai"]["models"][name]["short"]
    return round((i * r[0] + o * r[1] + cw * r[2] + cr * r[3]) / 1e6, 4)


def demo_tool(today, seed, mix, activity, volume):
    rng = random.Random(seed)
    days, hours, tasks = {}, {}, {}
    models = {name: [0, 0, 0, 0] for name in mix}
    messages = total_sessions = 0
    for back in range(150):
        day = today - dt.timedelta(days=back)
        if rng.random() > (activity if back < 70 else activity * 0.4):
            continue
        count, day_sessions = int(rng.lognormvariate(6.2, 0.7) * volume), rng.randint(1, 6)
        tokens = count * rng.randint(70000, 140000)
        by_model, split, cost, own = {}, [0, 0, 0, 0], {}, {}
        for name, share in mix.items():
            value = int(tokens * share * rng.uniform(0.6, 1.4))
            parts = [int(value * part) for part in SPLIT]
            by_model[name], own[name] = sum(parts), parts
            cost[name] = demo_cost(name, parts)
            for i, part in enumerate(parts):
                models[name][i] += part
                split[i] += part
        key = day.isoformat()
        days[key] = {"m": count, "tm": by_model, "sp": split, "ms": own, "sc": day_sessions, "c": cost}
        hours[key] = [0] * 24
        for _ in range(count):
            hours[key][min(23, max(0, int(rng.gauss(16, 4))))] += 1
        tasks[key] = rng.randint(300, 7000)
        messages += count
        total_sessions += day_sessions
    return {"today": today.isoformat(), "days": days, "hours": hours, "models": models, "tasks": tasks,
            "totals": {"sessions": total_sessions, "messages": messages}}


def demo_data(today):
    pricing = {"reviewed": PRICES["reviewed"], "claude": PRICES["claude"]["source"], "openai": PRICES["openai"]["source"]}
    return {"generatedAt": f"{today.isoformat()}T21:30:00", "user": "demo", "claude": True, "pricing": pricing,
            **demo_tool(today, 7, MIX, 0.85, 1),
            "codex": demo_tool(today, 11, CODEX_MIX, 0.6, 0.4)}


def main():
    html = (ROOT / "template.html").read_text(encoding="utf-8")
    # The day to draw: today, or a fixed one (YYYY-MM-DD) to rebuild a checked-in demo exactly.
    day = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else dt.date.today()
    data = json.dumps(demo_data(day), separators=(",", ":"))
    for old, new in (("/*__DATA__*/null", data),
                     ('<meta http-equiv="refresh" content="600">\n', ""),
                     ('<main>\n',
                      '<main>\n  <style>html:lang(zh) .demo-en, html:not(:lang(zh)) .demo-zh { display: none; }</style>\n'
                      '  <p style="color:var(--muted);margin:0 0 16px">'
                      '<span class="demo-en">Live demo with made-up data. '
                      '<a href="https://github.com/rzhang569/claude-usage-page" style="color:var(--accent)">'
                      'Get it for your own usage</a></span>'
                      '<span class="demo-zh">在线演示，数据是编的。'
                      '<a href="https://github.com/rzhang569/claude-usage-page" style="color:var(--accent)">'
                      '装一个看看你自己的用量</a></span></p>'),
                     ("from this computer's ${TOOL[source]} history only", "demo data"),
                     ("仅统计这台电脑的 ${TOOL[source]} 记录", "演示数据")):
        assert old in html, old
        html = html.replace(old, new)
    OUT.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
