#!/bin/zsh
# Установка Time Tracker: сборщик + CLI tt + напоминания + скилл /timesheet для Claude.
set -euo pipefail
SRC="${0:A:h}"
DEST="$HOME/Library/Application Support/TimeTracker"
APP="$HOME/Applications/65 tracker.app"   # в ~/Applications — находится через Spotlight
OLD_APP="$HOME/Applications/Учёт времени.app"  # прежнее имя приложения
BIN="$HOME/.local/bin"
mkdir -p "$DEST/logs" "$BIN"

say() { print -P "%F{blue}==>%f $*"; }

# Приложение переименовано в «65 tracker»: старое останавливаем и удаляем.
if [[ -d "$OLD_APP" ]]; then
  launchctl bootout "gui/$(id -u)/com.timetracker.collector" 2>/dev/null || true
  pkill -x ttcollect 2>/dev/null || true
  rm -rf "$OLD_APP"
  RENAMED=1
fi

# 1. Сборщик (Swift). Пересобираем, только если поменялся исходник: иначе macOS
#    забудет выданное разрешение «Универсальный доступ» (оно привязано к подписи бинарника).
SRC_HASH=$(cat "$SRC/collector/main.swift" "$SRC/collector/Info.plist" "$SRC/collector/icon/AppIcon.icns" | shasum -a 256 | cut -d' ' -f1)
if [[ ! -x "$APP/Contents/MacOS/ttcollect" || "$(cat "$DEST/.collector.sha" 2>/dev/null)" != "$SRC_HASH" ]]; then
  say "Собираю сборщик…"
  TMP=$(mktemp -d)
  build() { swiftc -O "$@" "$SRC/collector/main.swift" -o "$TMP/ttcollect" 2>"$TMP/err.log"; }
  if ! build; then
    # Command Line Tools бывают рассинхронизированы: компилятор старше SDK и/или дублирующийся
    # modulemap SwiftBridging. Пробуем подходящий SDK и прячем дубликат через VFS-оверлей.
    CLT=/Library/Developer/CommandLineTools
    print '{"version":0,"case-sensitive":"false","roots":[{"type":"directory","name":"'$CLT'/usr/include/swift","contents":[{"type":"file","name":"bridging.modulemap","external-contents":"'$TMP'/empty.modulemap"}]}]}' > "$TMP/overlay.yaml"
    : > "$TMP/empty.modulemap"
    ok=0
    for sdk in $(ls -d $CLT/SDKs/MacOSX1[0-9].*.sdk 2>/dev/null | sort -rV); do
      if build -sdk "$sdk" -target arm64-apple-macos14.0 -vfsoverlay "$TMP/overlay.yaml" -Xcc -ivfsoverlay -Xcc "$TMP/overlay.yaml"; then
        ok=1; break
      fi
    done
    [[ $ok == 1 ]] || { cat "$TMP/err.log"; echo "Не удалось собрать сборщик (см. ошибку выше)"; exit 1; }
  fi
  rm -rf "$APP" "$DEST/TTCollector.app"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cp "$TMP/ttcollect" "$APP/Contents/MacOS/ttcollect"
  cp "$SRC/collector/Info.plist" "$APP/Contents/Info.plist"
  cp "$SRC/collector/icon/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
  # Есть постоянный сертификат «65 tracker» (Связка ключей → Ассистент сертификатов → «Подпись кода») —
  # подписываем им: тогда доступ к окнам переживает пересборки. Иначе — разовая подпись, доступ слетает.
  if security find-identity -v -p codesigning 2>/dev/null | grep -q '"65 tracker"'; then
    codesign --force --sign "65 tracker" --identifier com.timetracker.collector "$APP" >/dev/null 2>&1
  else
    codesign --force --sign - --identifier com.timetracker.collector "$APP" >/dev/null 2>&1
  fi
  print "$SRC_HASH" > "$DEST/.collector.sha"
  rm -rf "$TMP"
  NEW_COLLECTOR=1
else
  say "Сборщик не менялся — оставляю как есть (разрешения сохранятся)"
  NEW_COLLECTOR=0
fi

# 2. Python-часть (только стандартная библиотека, системный python3).
say "Копирую tt…"
rsync -a --delete --exclude '__pycache__' "$SRC/app/" "$DEST/app/"
cat > "$BIN/tt" <<WRAP
#!/bin/sh
exec /usr/bin/python3 "$DEST/app/tt.py" "\$@"
WRAP
chmod +x "$BIN/tt"
[[ -f "$DEST/config.json" ]] || cp "$SRC/config.example.json" "$DEST/config.json"

# 3. Скилл для Claude Code: /timesheet в любой папке.
if [[ -d "$HOME/.claude" ]]; then
  say "Ставлю скилл /timesheet для Claude…"
  mkdir -p "$HOME/.claude/skills"
  rsync -a --delete "$SRC/claude/skills/timesheet/" "$HOME/.claude/skills/timesheet/"
fi

# 4. launchd: сборщик (всегда работает) + напоминания по расписанию из config.json.
say "Запускаю агентов…"
"$BIN/tt" agents install

# Сервер таймлайна, если запущен, сразу перезапускаем на новом коде — достаточно обновить вкладку.
/usr/bin/python3 -c "
import sys; sys.path.insert(0, '$DEST/app')
from ttlib import config, server
cfg = config.load()
if server.port_open(cfg['server']['port']): server.ensure_server(cfg)" || true

if [[ $NEW_COLLECTOR == 1 ]]; then
  say "Приложение само попросит «Универсальный доступ»: нажмите «Открыть Системные настройки» и включите «65 tracker»."
  [[ ${RENAMED:-0} == 1 ]] && say "Старую запись «Учёт времени» в этом списке можно удалить кнопкой «−»."
  say "Затем разрешите доступ к Календарю и к браузеру, когда macOS спросит."
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true
fi
[[ ":$PATH:" == *":$BIN:"* ]] || say "Добавьте $BIN в PATH (echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc)"
say "Готово. Проверка: tt status · Таймлайн: tt open · Jira: tt setup"
