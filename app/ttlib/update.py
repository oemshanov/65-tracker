"""Обновление приложения из релизов GitHub: проверка, скачивание dmg, проверка подписи и замена себя.

Наружу уходит только запрос к api.github.com («какой последний релиз») и скачивание dmg. Новая версия ставится,
только если подписана тем же сертификатом, что и установленная: чужой dmg под видом релиза не встанет.
"""
import json
import os
import plistlib
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import config
from .remind import collector_app

DIR = os.path.join(config.SUPPORT_DIR, "update")
CACHE = os.path.join(DIR, "latest.json")
DONE = os.path.join(DIR, "done.json")
REOPEN = os.path.join(DIR, "reopen")
BUNDLE_ID = "com.timetracker.collector"
APP_NAME = "65 tracker.app"
EVERY = 20 * 3600  # «раз в день»: с запасом, чтобы утренний запуск не съезжал на час каждый день

JOB = {"stage": None}
_checking = threading.Lock()
_installing = threading.Lock()


def _ver(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def _info(app):
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f)
    except (OSError, ValueError):
        return {}


def current():
    return _info(collector_app()).get("CFBundleShortVersionString") or "0"


def _api(cfg):
    # Подменить адрес можно только на этот же Мак — для проверки обновления на тестовом сервере.
    a = (cfg.get("updates") or {}).get("api") or ""
    return a.rstrip("/") if re.match(r"http://127\.0\.0\.1:\d+$", a.rstrip("/")) else "https://api.github.com"


def _notes(md):
    """Описание релиза на GitHub в Markdown → простой текст для окна настроек."""
    out = []
    for line in (md or "").replace("\r", "").split("\n"):
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^\s*[-*]\s+", "— ", line)
        line = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        out.append(line.rstrip())
    # Абзац «Как обновиться: скачайте dmg…» — для тех, кто ставит руками; в приложении есть кнопка.
    text = "\n".join(out).strip()
    return re.split(r"\n\s*Как обновиться", text)[0].strip()


def check(cfg):
    """Спрашивает GitHub о последнем релизе и запоминает ответ (для плашки и автопроверки)."""
    repo = (cfg.get("updates") or {}).get("repo")
    if not repo:
        return {"error": "Не указан репозиторий с обновлениями"}
    req = urllib.request.Request("%s/repos/%s/releases/latest" % (_api(cfg), repo),
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "65-tracker"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return {"error": "GitHub ответил %s" % e.code}
        rel = {}
    except (OSError, ValueError):
        return {"error": "Нет связи с GitHub"}
    dmg = next((a for a in rel.get("assets") or [] if a.get("name", "").endswith(".dmg")), {})
    data = {"checked_at": time.time(), "latest": (rel.get("tag_name") or "").lstrip("vV") or None,
            "dmg": dmg.get("browser_download_url"), "size": dmg.get("size"), "page": rel.get("html_url"),
            "notes": _notes(rel.get("body"))}
    os.makedirs(DIR, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def _cached():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def auto_enabled(cfg):
    return (cfg.get("updates") or {}).get("auto", True) is not False


def _designated(app):
    """Требование подписи установленного приложения: «этот идентификатор и этот сертификат»."""
    r = subprocess.run(["codesign", "-d", "-r-", app], capture_output=True, text=True)
    m = re.search(r"designated => (.+)", r.stdout + r.stderr)
    return m.group(1).strip() if m else ""


def can_install():
    """Обновлять сами можем, если приложение подписано постоянным сертификатом и папку с ним можно менять."""
    app = collector_app()
    return "certificate leaf" in _designated(app) and os.access(os.path.dirname(app), os.W_OK)


def state(cfg, background=True):
    """Что известно об обновлениях — без сети. Раз в день (если не выключено) проверяет GitHub в фоне."""
    c, cur = _cached(), current()
    if background and auto_enabled(cfg) and time.time() - c.get("checked_at", 0) > EVERY and _checking.acquire(False):
        def run():
            try:
                check(cfg)
            finally:
                _checking.release()
        threading.Thread(target=run, daemon=True).start()
    out = {"current": cur, "latest": c.get("latest"), "newer": _ver(c.get("latest")) > _ver(cur),
           "notes": c.get("notes") or "", "page": c.get("page"), "dmg": c.get("dmg"), "size": c.get("size"),
           "checked_at": c.get("checked_at"), "auto": auto_enabled(cfg), "job": dict(JOB)}
    try:
        with open(DONE, encoding="utf-8") as f:
            out["done"] = json.load(f)
    except (OSError, ValueError):
        pass
    return out


def ack():
    try:
        os.remove(DONE)
    except OSError:
        pass


def _set(**kw):
    JOB.clear()
    JOB.update(kw)


def _download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "65-tracker"})
    with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
        total, got = int(r.headers.get("Content-Length") or 0), 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                JOB["pct"] = min(99, got * 100 // total)


def _install(cfg, server_pid):
    c = check(cfg)
    if c.get("error"):
        raise RuntimeError(c["error"])
    cur, latest = current(), c.get("latest")
    if _ver(latest) <= _ver(cur):
        raise RuntimeError("Новой версии нет")
    repo, url = cfg["updates"]["repo"], c.get("dmg") or ""
    allowed = "https://github.com/%s/releases/download/" % repo if _api(cfg).startswith("https://") else _api(cfg) + "/"
    if not url.startswith(allowed):
        raise RuntimeError("В релизе нет dmg")
    app = collector_app()
    req = _designated(app)
    if "certificate leaf" not in req:
        raise RuntimeError("Эта сборка подписана без сертификата — обновите вручную")

    shutil.rmtree(DIR + "/work", ignore_errors=True)
    work, mnt = DIR + "/work", DIR + "/work/mnt"
    os.makedirs(mnt)
    dmg = work + "/new.dmg"
    _set(stage="download", pct=0, version=latest)
    _download(url, dmg)

    _set(stage="verify", version=latest)
    r = subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mnt, dmg],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("dmg не открылся")
    new = work + "/" + APP_NAME
    try:
        src = os.path.join(mnt, APP_NAME)
        if not os.path.isdir(src):
            raise RuntimeError("В dmg нет «65 tracker»")
        subprocess.run(["ditto", src, new], check=True, capture_output=True)
    finally:
        subprocess.run(["hdiutil", "detach", mnt, "-force"], capture_output=True)
    info = _info(new)
    if info.get("CFBundleIdentifier") != BUNDLE_ID or info.get("CFBundleShortVersionString") != latest:
        raise RuntimeError("В dmg другое приложение")
    # Та же подпись, что у установленного: и целостность всех файлов, и тот же сертификат.
    v = subprocess.run(["codesign", "--verify", "--deep", "--strict", "-R=" + req, new], capture_output=True, text=True)
    if v.returncode != 0:
        raise RuntimeError("Подпись новой версии не совпала с установленной — обновление не поставлено")

    _set(stage="restart", version=latest)
    script = work + "/swap.sh"
    with open(script, "w") as f:
        f.write(SWAP)
    os.chmod(script, 0o755)
    try:
        os.remove(DONE)
    except OSError:
        pass
    log = open(os.path.join(config.LOG_DIR, "update.log"), "a")
    subprocess.Popen(["/bin/sh", script, app, new, work + "/old.app", str(server_pid), DONE, REOPEN, cur, latest],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def install_async(cfg, server_pid):
    if not _installing.acquire(False):
        return dict(JOB)
    _set(stage="download", pct=0)

    def run():
        try:
            _install(cfg, server_pid)
        except Exception as e:  # noqa: BLE001 — любую ошибку показываем в настройках
            _set(stage="error", error=str(e))
            shutil.rmtree(DIR + "/work", ignore_errors=True)
        finally:
            _installing.release()
    threading.Thread(target=run, daemon=True).start()
    return dict(JOB)


# Замена себя: дождаться выхода приложения, переложить новую версию на место старой и запустить.
# Отдельным процессом — приложение и сервер, которые его запустили, к этому моменту уже закрыты.
SWAP = r'''#!/bin/sh
APP="$1"; NEW="$2"; OLD="$3"; SRV="$4"; DONE="$5"; REOPEN="$6"; FROM="$7"; TO="$8"
echo "$(date '+%F %T') обновление $FROM → $TO"
pkill -HUP -x ttcollect
i=0; while pgrep -x ttcollect >/dev/null && [ $i -lt 50 ]; do sleep 0.2; i=$((i+1)); done
pkill -KILL -x ttcollect 2>/dev/null
rm -rf "$OLD"
ok=0
if mv "$APP" "$OLD"; then
  if mv "$NEW" "$APP"; then ok=1; else mv "$OLD" "$APP"; fi
fi
if [ $ok = 1 ]; then
  printf '{"from":"%s","to":"%s","at":%s}' "$FROM" "$TO" "$(date +%s)" > "$DONE"
else
  printf '{"from":"%s","to":"%s","error":"macOS не дала заменить приложение"}' "$FROM" "$TO" > "$DONE"
fi
touch "$REOPEN"
kill "$SRV" 2>/dev/null
launchctl kickstart "gui/$(id -u)/com.timetracker.collector" 2>/dev/null || open "$APP"
sleep 2
pgrep -x ttcollect >/dev/null || open "$APP"
rm -rf "$(dirname "$NEW")"  # рабочая папка: старая версия, dmg, этот скрипт
echo "готово: ok=$ok"
'''
