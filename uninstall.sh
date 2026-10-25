#!/bin/sh
# Removes everything install.sh added. Claude Code's own files are never touched.
for label in io.github.rzhang569.claude-usage-page.build io.github.rzhang569.claude-usage-page.serve; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
rm -rf "$HOME/.claude-usage-page" "$HOME/.claude/skills/my-usage"
echo "Removed."
