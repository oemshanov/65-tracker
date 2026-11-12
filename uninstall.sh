#!/bin/zsh
# Удаление Time Tracker. С флагом --purge удаляет и собранные данные.
set -euo pipefail
DEST="$HOME/Library/Application Support/TimeTracker"
[[ -x "$HOME/.local/bin/tt" ]] && "$HOME/.local/bin/tt" agents uninstall || true
pkill -f "TimeTracker/app/tt.py serve" 2>/dev/null || true
pkill -x ttcollect 2>/dev/null || true
rm -rf "$HOME/Applications/65 tracker.app" "$HOME/Applications/Учёт времени.app"
[[ -w "/Applications/65 tracker.app" ]] && rm -rf "/Applications/65 tracker.app" || true  # установка из .dmg
rm -f "$HOME/.local/bin/tt"
rm -rf "$HOME/.claude/skills/timesheet"
if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$DEST"
  security delete-generic-password -s tt-jira >/dev/null 2>&1 || true
  echo "Удалено вместе с данными и токеном Jira."
else
  rm -rf "$DEST/app" "$DEST/TTCollector.app" "$DEST/.collector.sha"
  echo "Удалено. Данные остались в $DEST (для полного удаления: ./uninstall.sh --purge)"
fi
