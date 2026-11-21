"""The GitHub Pages demo is the real template filled with fake data."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def build(self, *day):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "index.html"
            subprocess.run([sys.executable, str(ROOT / "docs" / "make_demo.py"), str(out), *day], check=True, timeout=30)
            return out.read_text(encoding="utf-8")

    def test_demo_embeds_valid_data_and_labels_itself(self):
        html = self.build()
        data = json.loads(re.search(r"const ALL = (\{.*?\});\n", html).group(1))
        self.assertGreater(len(data["days"]), 30)
        self.assertIn("Live demo with made-up data", html)
        self.assertNotIn("/*__DATA__*/null", html)
        self.assertNotIn('http-equiv="refresh"', html)

    def test_checked_in_demo_matches_the_template_and_generator(self):
        shipped = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        day = re.search(r'"today":"(\d{4}-\d\d-\d\d)"', shipped).group(1)
        self.assertEqual(self.build(day), shipped, "run: python3 docs/make_demo.py docs/index.html " + day)
