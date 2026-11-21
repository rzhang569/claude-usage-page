"""Build the optional Claude Desktop sidebar card into dist/:

  claude-usage-page.mcpb   the extension (open it with the Claude app to install)
  sidebar.html             the card, to publish as an artifact and pin to the sidebar

The card is template.html with its data supplied by the extension; see sidebar/loader.html.
Usage: python3 sidebar/make.py [output folder]
"""
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "sidebar"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist"


def card():
    page = (ROOT / "template.html").read_text(encoding="utf-8")
    # The card refreshes through the extension, so the page doesn't reload itself.
    refresh = '<meta http-equiv="refresh" content="600">\n'
    assert refresh in page and "/*__DATA__*/null" in page, "template.html changed; update sidebar/make.py"
    page = page.replace(refresh, "")
    loader = (HERE / "loader.html").read_text(encoding="utf-8")
    assert loader.count('/*__PAGE__*/""') == 1
    # A JSON string literal with every "<" escaped, so the template can't end or open a script.
    return loader.replace('/*__PAGE__*/""', json.dumps(page).replace("<", "\\u003c"))


def extension(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("manifest.json", "icon.png", "server/index.js"):
            z.write(HERE / name, name)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sidebar.html").write_text(card(), encoding="utf-8")
    extension(OUT / "claude-usage-page.mcpb")
    print(f"wrote {OUT / 'sidebar.html'} and {OUT / 'claude-usage-page.mcpb'}")


if __name__ == "__main__":
    main()
