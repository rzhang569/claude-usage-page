#!/bin/sh
# Installs the usage page for the current macOS user:
#   ~/.claude-usage-page/          the scripts, the built page and its parse cache
#   ~/Library/LaunchAgents/        two jobs: rebuild hourly, serve on 127.0.0.1
#   ~/.claude/skills/my-usage/     the /my-usage command for Claude Code
# Run again to update. uninstall.sh removes all of it.
set -eu

PORT="${PORT:-8788}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude-usage-page"
AGENTS="$HOME/Library/LaunchAgents"
SKILL="$HOME/.claude/skills/my-usage"
BUILD=io.github.rzhang569.claude-usage-page.build
SERVE=io.github.rzhang569.claude-usage-page.serve

if [ "$(uname)" != Darwin ]; then
  echo "install.sh sets up macOS launchd jobs. On other systems run build.py on a timer"
  echo "and serve.py at login yourself (see README)."
  exit 1
fi
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found"; exit 1; }

mkdir -p "$DEST" "$AGENTS" "$SKILL"
cp "$SRC/build.py" "$SRC/serve.py" "$SRC/template.html" "$DEST/"

plist() { # label, interval-or-keepalive, script args...
  label=$1; mode=$2; shift 2
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo "  <key>ProgramArguments</key><array><string>$PY</string>"
    for a in "$@"; do echo "    <string>$a</string>"; done
    echo '  </array>'
    echo '  <key>RunAtLoad</key><true/>'
    if [ "$mode" = keepalive ]; then
      echo '  <key>KeepAlive</key><true/>'
    else
      echo "  <key>StartInterval</key><integer>$mode</integer>"
      echo '  <key>LowPriorityIO</key><true/>'
    fi
    echo '  <key>Nice</key><integer>10</integer>'
    echo "  <key>StandardOutPath</key><string>$DEST/$label.log</string>"
    echo "  <key>StandardErrorPath</key><string>$DEST/$label.log</string>"
    echo '</dict></plist>'
  } > "$AGENTS/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
}

"$PY" "$DEST/build.py"
plist "$BUILD" 3600 "$DEST/build.py"
plist "$SERVE" keepalive "$DEST/serve.py" "$PORT"

cat > "$SKILL/SKILL.md" <<SKILL
---
name: my-usage
description: Show the local Claude Code usage card (sessions, tokens, heatmap, per-model breakdown). Use only when the user types /my-usage or asks to see their usage card.
---

The card is served at http://127.0.0.1:$PORT/ and rebuilt every hour. Keep the turn short.

1. Refresh it so it includes this session: \`$PY $DEST/build.py\`
2. Open it in the default browser: \`open http://127.0.0.1:$PORT/\`
   If it does not load, run \`launchctl kickstart -k gui/\$(id -u)/$SERVE\` and open it again.
   If the user asks to see it beside the chat, open the same URL in the built-in browser pane instead.

Then say in one line that the card is open and when it was updated. Do not restate its numbers unless asked.
If build.py fails, show the last lines of its output and of $DEST/$BUILD.log, and stop.
SKILL

echo "Installed. Open http://127.0.0.1:$PORT/ or type /my-usage in a new Claude Code session"
echo "(sessions that were already open won't list it)."
