#!/bin/zsh
# Сборка «65 tracker.dmg» для коллег: приложение со своим Python внутри — без Homebrew, Xcode и терминала.
# Ничего не ставит на этот Мак: собирает во временной папке и кладёт результат в dist/.
set -euo pipefail
setopt null_glob   # шаблоны без совпадений — пусто, а не ошибка
SRC="${0:A:h}"
DIST="$SRC/dist"
CACHE="$DIST/cache"
NAME="65 tracker"
# Python: python-build-standalone, сборка без отладочных символов. Версию фиксируем (не «latest»),
# архив проверяем по опубликованным SHA256SUMS.
PY_TAG="20261009"
PY_FILE="cpython-3.12.15+${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz"
PY_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}"

say() { print -P "%F{blue}==>%f $*"; }
mkdir -p "$DIST" "$CACHE"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
APP="$WORK/stage/$NAME.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 1. Python (кешируется в dist/cache, чтобы не качать при каждой сборке).
if [[ ! -f "$CACHE/$PY_FILE" ]]; then
  say "Скачиваю Python ($PY_FILE)…"
  curl -fsSL -o "$CACHE/$PY_FILE.part" "$PY_BASE/${PY_FILE//+/%2B}"
  curl -fsSL -o "$CACHE/SHA256SUMS-$PY_TAG" "$PY_BASE/SHA256SUMS"
  want=$(grep " $PY_FILE\$" "$CACHE/SHA256SUMS-$PY_TAG" | cut -d' ' -f1)
  got=$(shasum -a 256 "$CACHE/$PY_FILE.part" | cut -d' ' -f1)
  [[ -n "$want" && "$want" == "$got" ]] || { rm -f "$CACHE/$PY_FILE.part"; echo "Контрольная сумма Python не совпала"; exit 1; }
  mv "$CACHE/$PY_FILE.part" "$CACHE/$PY_FILE"
fi
say "Кладу Python в приложение…"
tar -xzf "$CACHE/$PY_FILE" -C "$WORK"          # → $WORK/python
PY="$WORK/python"
# Лишнее для трекера: тесты, IDLE, Tk, документация, заголовки.
rm -rf "$PY"/lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,unittest/test} \
       "$PY"/lib/{tcl*,tk*,itcl*,thread*,Tix*} "$PY"/include "$PY"/share
find "$PY" -name '__pycache__' -prune -exec rm -rf {} +
"$PY/bin/python3" -m compileall -q -j 0 "$PY/lib/python3.12" >/dev/null 2>&1 || true  # байт-код внутри — Python не пишет в приложение
mv "$PY" "$APP/Contents/Resources/python"

# 2. Сборщик (Swift) — те же обходные флаги, что в install.sh.
say "Собираю приложение…"
build() { swiftc -O "$@" "$SRC/collector/main.swift" -o "$APP/Contents/MacOS/ttcollect" 2>"$WORK/err.log"; }
if ! build; then
  CLT=/Library/Developer/CommandLineTools
  print '{"version":0,"case-sensitive":"false","roots":[{"type":"directory","name":"'$CLT'/usr/include/swift","contents":[{"type":"file","name":"bridging.modulemap","external-contents":"'$WORK'/empty.modulemap"}]}]}' > "$WORK/overlay.yaml"
  : > "$WORK/empty.modulemap"
  ok=0
  for sdk in $(ls -d $CLT/SDKs/MacOSX1[0-9].*.sdk 2>/dev/null | sort -rV); do
    if build -sdk "$sdk" -target arm64-apple-macos14.0 -vfsoverlay "$WORK/overlay.yaml" -Xcc -ivfsoverlay -Xcc "$WORK/overlay.yaml"; then
      ok=1; break
    fi
  done
  [[ $ok == 1 ]] || { cat "$WORK/err.log"; echo "Не удалось собрать приложение"; exit 1; }
fi
cp "$SRC/collector/Info.plist" "$APP/Contents/Info.plist"
cp "$SRC/collector/icon/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

# 3. Код трекера, пример настроек и скилл /timesheet. Ничего личного: без тестов, базы и config.json.
say "Кладу код трекера…"
rsync -a --exclude '__pycache__' --exclude '.DS_Store' "$SRC/app/" "$APP/Contents/Resources/app/"
cp "$SRC/config.example.json" "$APP/Contents/Resources/config.example.json"
[[ -d "$SRC/claude/skills" ]] && rsync -a --exclude '.DS_Store' "$SRC/claude/skills/" "$APP/Contents/Resources/skills/"
VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$SRC/collector/Info.plist")
STAMP="$VERSION $(cd "$APP/Contents/Resources/app" && find . -type f | sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-12)"
print "$STAMP" > "$APP/Contents/Resources/app/.build"   # по нему приложение понимает, что код обновился

# 4. Подпись: сертификат «65 tracker», если есть, иначе разовая.
if security find-identity -p codesigning 2>/dev/null | grep -q '"65 tracker"'; then SIGN="65 tracker"; else SIGN="-"; fi
say "Подписываю (${SIGN/-/разовая подпись})…"
find "$APP/Contents/Resources/python" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0 |
  xargs -0 -n 50 codesign --force --sign "$SIGN" --timestamp=none >/dev/null 2>&1 || true
codesign --force --deep --sign "$SIGN" --identifier com.timetracker.collector "$APP"

# 5. Образ диска: приложение, ярлык «Программы» и инструкция.
say "Собираю образ диска…"
ln -s /Applications "$WORK/stage/Программы"
cat > "$WORK/stage/Как установить.txt" <<'TXT'
65 tracker — учёт рабочего времени с отправкой в Jira

1. Перетащите «65 tracker» в папку «Программы».
2. Откройте «65 tracker». Приложение не из App Store, поэтому macOS один раз скажет,
   что не может его проверить. Нажмите «Готово» (не «В Корзину»), затем:
   Системные настройки → Конфиденциальность и безопасность → прокрутите вниз →
   «Всё равно открыть» у «65 tracker» → подтвердите паролем или Touch ID.
   Если macOS пишет, что приложение повреждено, выполните в Терминале:
   xattr -dr com.apple.quarantine "/Applications/65 tracker.app"
3. Вверху экрана появится значок 65. Откройте его и нажмите «Выдать доступ» —
   без «Универсального доступа» трекер не видит, в каком окне вы работаете.
   Затем разрешите Календарь и браузер, когда macOS спросит.
4. Подключите Jira: значок 65 → «Открыть таймлайн» → «Подключить Jira».
   Нужен Personal Access Token (Jira → Профиль → Personal Access Tokens).
   Токен хранится в Связке ключей macOS.

Все данные остаются на вашем Маке. В сеть трекер ходит только в вашу Jira.
TXT
rm -f "$DIST/$NAME.dmg"
hdiutil create -quiet -volname "$NAME" -srcfolder "$WORK/stage" -ov -format UDZO "$DIST/$NAME.dmg"
say "Готово: $DIST/$NAME.dmg ($(du -h "$DIST/$NAME.dmg" | cut -f1)), версия $STAMP"
