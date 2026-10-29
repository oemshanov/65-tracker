"""Локальный сервер таймлайна: http://127.0.0.1:8765 (только localhost)."""
import datetime as dt
import json
import re
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, icons, jira, local_ai, report, suggest, update
from .context import generic_call
from .remind import day_needs_review
from .segment import build_day, parse_date, parse_dur, propose

WEB = os.path.join(config.APP_DIR, "web")


def code_version():
    """Версия кода = время последнего изменения файлов; при обновлении старый сервер перезапускается."""
    paths = [os.path.join(config.APP_DIR, "ttlib", f) for f in os.listdir(os.path.join(config.APP_DIR, "ttlib"))]
    paths += [os.path.join(WEB, f) for f in ("index.html", "widget.html", "timeline.html")]
    return str(int(max(os.path.getmtime(p) for p in paths if os.path.isfile(p))))


VERSION = code_version()


def day_payload(conn, cfg, date):
    day = build_day(conn, cfg, date)
    prop = propose(conn, cfg, day)
    st = conn.execute("SELECT status FROM days WHERE date=?", (date.isoformat(),)).fetchone()
    return dict(day, proposal=prop, status=st["status"] if st else None,
                jira_configured=jira.Jira(cfg).configured, base_url=cfg["jira"]["base_url"],
                jira_user=cfg["jira"].get("username") or "",
                issues_count=conn.execute("SELECT COUNT(*) FROM issues WHERE summary IS NOT NULL").fetchone()[0],
                llm_enabled=bool(cfg["llm"].get("enabled")),
                # Норма дня для подсказки «добрать до 8 часов»; в выходные её нет.
                norm={"seconds": int(float(cfg["worklog"].get("day_hours") or 0) * 3600) if date.weekday() < 5 else 0,
                      "ends": cfg["worklog"].get("day_ends") or "17:00"})


def week_payload(conn, cfg, date):
    monday = date - dt.timedelta(days=date.weekday())
    out = []
    for i in range(7):
        d = monday + dt.timedelta(days=i)
        if d > dt.date.today():
            out.append({"date": d.isoformat(), "future": True})
            continue
        need, p = day_needs_review(conn, cfg, d, check_jira=False)
        if p is None:
            p = propose(conn, cfg, build_day(conn, cfg, d))
        st = conn.execute("SELECT status FROM days WHERE date=?", (d.isoformat(),)).fetchone()
        out.append({"date": d.isoformat(), "total": p["total"], "to_log": p["to_log"], "unassigned": p["unassigned"],
                    "needs_review": need, "status": st["status"] if st else None,
                    "pushed": sum(w["seconds"] for w in p["worklogs"] if w["status"] in ("pushed", "in_jira"))})
    return out


PAUSE_FILE = os.path.join(config.SUPPORT_DIR, "paused_until")


def recording_status(cfg):
    """Идёт ли запись: пауза хранится в файле, его же читает приложение в строке меню."""
    from .remind import collector_running
    until = None
    try:
        with open(PAUSE_FILE) as f:
            v = float(f.read().strip())
        until = -1 if v < 0 else (v if v > time.time() else None)
    except (OSError, ValueError):
        pass
    u = update.state(cfg)
    return {"running": collector_running(), "paused": until is not None, "paused_until": until,
            "missing": missing_permissions(cfg), "build": web_build(),
            "update": {"newer": u["newer"], "latest": u["latest"], "done": u.get("done")}}


def _read_permissions():
    try:
        with open(PERM_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def app_version():
    """Версия приложения из Info.plist и дата сборки страниц — чтобы понимать, какая сборка стоит."""
    import plistlib
    from .remind import collector_app
    app_dir = collector_app()
    v = {"app": None, "build": None, "web": dt.datetime.fromtimestamp(web_build()).strftime("%d.%m.%Y %H:%M")}
    try:
        with open(os.path.join(app_dir, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        v["app"], v["build"] = info.get("CFBundleShortVersionString"), info.get("CFBundleVersion")
        v["app_date"] = dt.datetime.fromtimestamp(
            os.path.getmtime(os.path.join(app_dir, "Contents", "MacOS", "ttcollect"))).strftime("%d.%m.%Y %H:%M")
    except (OSError, ValueError):
        pass
    return v


def _du(path):
    total = 0
    for dp, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def storage(conn):
    """Сколько места занимает трекер: история (база) и кеш (векторы нейросети, иконки, журналы)."""
    conn.execute("CREATE TABLE IF NOT EXISTS embeddings(hash TEXT PRIMARY KEY, vec BLOB)")  # на новой установке её ещё нет
    vec = conn.execute("SELECT COALESCE(SUM(LENGTH(vec)), 0) FROM embeddings").fetchone()[0]
    db = sum(os.path.getsize(os.path.join(config.SUPPORT_DIR, f)) for f in os.listdir(config.SUPPORT_DIR)
             if f.startswith("tt.db"))
    cache = vec + _du(icons.DIR) + _du(config.LOG_DIR)
    first = conn.execute("SELECT MIN(start) FROM spans").fetchone()[0]
    return {"history": max(0, db - vec), "cache": cache,
            "since": dt.date.fromtimestamp(first).strftime("%d.%m.%Y") if first else None}


def clear_cache(conn):
    """Чистит то, что соберётся заново само: векторы нейросети, иконки приложений, аватар, журналы.
    История записи, задачи, привязки и ворклоги не трогаются."""
    conn.execute("CREATE TABLE IF NOT EXISTS embeddings(hash TEXT PRIMARY KEY, vec BLOB)")
    conn.execute("DELETE FROM embeddings")
    conn.commit()
    conn.execute("VACUUM")
    for d in (icons.DIR,):
        if os.path.isdir(d):
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
    if os.path.isdir(config.LOG_DIR):
        for f in os.listdir(config.LOG_DIR):
            try:
                open(os.path.join(config.LOG_DIR, f), "w").close()
            except OSError:
                pass
    return storage(conn)


def jira_avatar(cfg):
    """Аватар из профиля Jira (нужна авторизация, поэтому через сервер). Кеш на сутки."""
    out = os.path.join(icons.DIR, "jira-avatar")
    if os.path.exists(out) and time.time() - os.path.getmtime(out) < 86400:
        return out
    try:
        j = jira.Jira(cfg)
        if not j.configured:
            return None
        url = (j.myself().get("avatarUrls") or {}).get("48x48")
        if not url:
            return None
        h = j._headers()
        h["Accept"] = "image/*"
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=10) as r:
            data = r.read()
        os.makedirs(icons.DIR, exist_ok=True)
        with open(out, "wb") as f:
            f.write(data)
        return out
    except Exception:
        return out if os.path.exists(out) else None


def web_build():
    """Версия страниц: время последней правки файлов. Панель и окно перезагружаются, когда она меняется."""
    return max((os.path.getmtime(os.path.join(WEB, f)) for f in os.listdir(WEB) if f.endswith(".html")), default=0)


PERM_FILE = os.path.join(config.SUPPORT_DIR, "permissions.json")


def missing_permissions(cfg):
    """Каких доступов не хватает трекеру. Состояние пишет приложение в строке меню (раз в минуту)."""
    try:
        with open(PERM_FILE) as f:
            p = json.load(f)
    except (OSError, ValueError):
        return []
    if time.time() - p.get("updated", 0) > 600:
        return []  # приложение не запущено — судить не по чему
    out = []
    if not p.get("accessibility"):
        out.append({"action": "accessibility", "title": "Нет доступа к окнам", "button": "Выдать доступ",
                    "text": "Без «Универсального доступа» трекер не видит, в каком окне вы работаете, "
                            "и не может разложить время по задачам"})
    if cfg["collector"].get("calendar", True) and p.get("calendar") in ("ask", "denied"):
        out.append({"action": "calendar", "title": "Нет доступа к календарю", "button": "Разрешить",
                    "text": "Встречи не подписываются названиями, а созвоны не считаются по времени из календаря"})
    for b in (p.get("browsers") or []) if cfg["collector"].get("browser_urls", True) else []:
        if b.get("state") in ("ask", "denied"):
            out.append({"action": "automation", "bundle": b["bundle"], "title": "Нет доступа к %s" % b["name"],
                        "button": "Разрешить",
                        "text": "Трекер не видит адрес вкладки, поэтому не узнаёт задачи по сайтам вроде Jira и Figma"})
    return out


def set_pause(cfg, mode):
    """mode: минуты ("30"), "eod" — до конца рабочего дня (начало следующего), "inf" — пока не включат, "resume"."""
    if mode in (None, "resume"):
        try:
            os.remove(PAUSE_FILE)
        except OSError:
            pass
        return
    if mode == "inf":
        v = -1
    elif mode == "eod":
        h = cfg["day"]["start_hour"]
        now = dt.datetime.now()
        nxt = (now - dt.timedelta(hours=h)).date() + dt.timedelta(days=1)
        v = dt.datetime.combine(nxt, dt.time(h)).timestamp()
    else:
        v = time.time() + int(mode) * 60
    with open(PAUSE_FILE, "w") as f:
        f.write(str(v))


_syncing = threading.Lock()


def _auto_sync_issues(conn, cfg, every=12 * 3600):
    """Список задач обновляется сам, если ему больше 12 часов: в фоне, страница не ждёт."""
    last = conn.execute("SELECT MAX(updated) FROM issues").fetchone()[0] or 0
    if time.time() - last < every or not jira.Jira(cfg).configured or not _syncing.acquire(blocking=False):
        return

    def run():
        try:
            jira.sync_issues(db.connect(), cfg)
        except Exception as e:  # нет сети или Jira недоступна — попробуем при следующем открытии
            print("auto sync issues: %s" % e, file=sys.stderr)
        finally:
            _syncing.release()
    threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "tt/1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        self.server.touch()
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        conn, cfg = self.server.conn, config.load()
        try:
            static = {"/": "index.html", "/index.html": "index.html", "/new": "index.html", "/widget": "widget.html",
                      "/legacy": "timeline.html"}
            fname = static.get(u.path)
            if not fname and u.path.startswith("/fonts/") and re.fullmatch(r"/fonts/[a-z0-9-]+\.woff2", u.path):
                fname = u.path.lstrip("/")
            if fname:
                with open(os.path.join(WEB, fname), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2" if fname.endswith(".woff2") else "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400" if fname.endswith(".woff2") else "no-store")
                self.end_headers()
                return self.wfile.write(body)
            if u.path == "/api/ping":
                return self._json({"ok": True, "version": VERSION})
            if u.path == "/api/day":
                return self._json(day_payload(conn, cfg, parse_date(q.get("date"))))
            if u.path == "/api/week":
                return self._json(week_payload(conn, cfg, parse_date(q.get("date"))))
            if u.path == "/api/status":
                return self._json(recording_status(cfg))
            if u.path == "/api/jira/avatar":
                path = jira_avatar(cfg)
                if not path:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with open(path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml" if data.lstrip()[:4] in (b"<svg", b"<?xm") else "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/icon":
                path = icons.jira_icon(cfg["jira"]["base_url"]) if q.get("jira") else icons.app_icon(q.get("b"))
                if not path:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with open(path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/x-icon" if path.endswith(".ico") else "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=604800")
                self.end_headers()
                return self.wfile.write(body)
            if u.path == "/api/settings":
                rem = (cfg.get("reminders") or [{}])[0]
                return self._json({
                    "data_dir": config.SUPPORT_DIR,
                    "jira": {"configured": jira.Jira(cfg).configured, "base_url": cfg["jira"]["base_url"],
                             "username": cfg["jira"].get("username") or "",
                             "issues": conn.execute("SELECT COUNT(*) FROM issues WHERE summary IS NOT NULL").fetchone()[0]},
                    "reminder": {"time": rem.get("time", "10:00"), "days": rem.get("days", []),
                                 "enabled": rem.get("enabled", True) is not False and bool(cfg.get("reminders")),
                                 "style": cfg.get("reminder_style", "dialog")},
                    "local": local_ai.status(cfg),
                    "permissions": _read_permissions(),
                    "version": app_version(),
                    "update": dict(update.state(cfg, background=False), can_install=update.can_install()),
                    "storage": storage(conn),
                    "day_hours": cfg["worklog"].get("day_hours") or 0,
                })
            if u.path == "/api/update/check":
                c = update.check(cfg)
                return self._json(dict(update.state(cfg, background=False), error=c.get("error"), can_install=update.can_install()))
            if u.path == "/api/update/state":
                return self._json(dict(update.state(cfg, background=False), can_install=update.can_install()))
            if u.path == "/api/local/status":
                return self._json(local_ai.status(cfg))
            if u.path == "/api/report":
                return self._json({"text": report.day_report(conn, cfg, parse_date(q.get("date")))})
            if u.path == "/api/issues":
                _auto_sync_issues(conn, cfg)
                rows = [dict(r) for r in conn.execute("SELECT key, summary, project, project_name, status, mine FROM issues "
                                                      "ORDER BY updated DESC, key")]
                return self._json(rows)
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        self.server.touch()
        if not self._host_ok() or self.headers.get("X-TT") != "1":
            return self._json({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        conn, cfg = self.server.conn, config.load()
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/shutdown":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path == "/api/claude_login":
            # Вход в программу claude (для кнопки «Спросить Claude»): окно Терминала + страница входа в браузере.
            import tempfile
            exe = os.path.expanduser(cfg["llm"]["command"])
            fd, script = tempfile.mkstemp(prefix="tt-claude-login-", suffix=".command")
            with os.fdopen(fd, "w") as f:
                f.write("#!/bin/zsh\nclear\necho 'Вход в Claude для кнопки «Спросить Claude». Подтвердите вход в открывшемся браузере.'\n"
                        "%s auth login\necho\nread '?Готово. Нажмите Enter, чтобы закрыть окно.'\nrm -f \"$0\"\n"
                        % json.dumps(exe))
            os.chmod(script, 0o755)
            subprocess.run(["open", script])
            return self._json({"ok": True})
        if path == "/api/jira/logout":
            # Отключить Jira: убрать токен из Связки ключей и логин из настроек. Задачи и история остаются.
            user = cfg["jira"].get("username")
            if user:
                subprocess.run(["security", "delete-generic-password", "-s", jira.KEYCHAIN_SERVICE, "-a", user],
                               capture_output=True)
            config.save_user({"jira": {"username": ""}})
            return self._json({"ok": True})
        if path == "/api/jira/connect":
            user = (body.get("username") or "").strip()
            token = (body.get("token") or "").strip()
            base = (body.get("base_url") or cfg["jira"]["base_url"]).strip().rstrip("/")
            if not user or not token:
                return self._json({"error": "Нужны логин и токен"}, 400)
            config.save_user({"jira": {"base_url": base, "username": user,
                                       "auth": "basic" if body.get("password") else "pat"}})
            if not jira.keychain_store(user, token):
                return self._json({"error": "Не удалось сохранить токен в Связку ключей"}, 500)
            cfg = config.load()
            try:
                me = jira.Jira(cfg).myself()
                n = jira.sync_issues(conn, cfg)
            except jira.JiraError as e:
                return self._json({"error": str(e)}, 502)
            return self._json({"ok": True, "name": me.get("displayName") or me.get("name"), "issues": n})
        try:
            date = parse_date(body.get("date"))
            if path == "/api/assign":
                issue = (body.get("issue") or "").strip().upper() or None
                skip = body.get("skip")
                cat = "split" if body.get("split") else ("personal" if skip else None)
                ck = body.get("context_key")
                if ck and generic_call(ck) and issue:
                    # «Телемост» без события в календаре — задача только на это время, без правила на будущее.
                    db.delete_mapping(conn, ck)
                    body["remember"] = False
                if body.get("remember") and ck:
                    if issue or cat:
                        db.set_mapping(conn, body["context_key"], issue, cat, body.get("label"))
                    else:
                        db.delete_mapping(conn, body["context_key"])
                    for a, b in body.get("intervals") or ([[body["start"], body["end"]]] if body.get("start") else []):
                        db.add_override(conn, a, b)  # снять ручные привязки интервалов
                elif body.get("intervals"):
                    for a, b in body["intervals"]:
                        db.add_override(conn, a, b, issue, cat)
                elif body.get("start") and body.get("end"):
                    db.add_override(conn, body["start"], body["end"], issue, cat)
                if issue:
                    conn.execute("INSERT OR IGNORE INTO issues(key, updated) VALUES(?, 0)", (issue,))
                    conn.commit()
                return self._json(day_payload(conn, cfg, date))
            if path == "/api/accept":
                for key in body.get("context_keys", []):
                    s = conn.execute("SELECT * FROM suggestions WHERE context_key=?", (key,)).fetchone()
                    if s:
                        db.set_mapping(conn, key, s["issue"], s["category"], body.get("labels", {}).get(key))
                return self._json(day_payload(conn, cfg, date))
            if path == "/api/draft":
                issue, part = body["issue"], body.get("part") or ""
                secs = parse_dur(body["time"]) if body.get("time") not in (None, "") else None
                if body.get("reset"):
                    conn.execute("UPDATE drafts SET seconds=NULL, comment=NULL WHERE date=? AND issue=? AND part=?",
                                 (date.isoformat(), issue, part))
                else:
                    conn.execute(
                        "INSERT INTO drafts(date, issue, part, seconds, comment) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(date, issue, part) DO UPDATE SET "
                        "seconds=COALESCE(excluded.seconds, drafts.seconds), "
                        "comment=COALESCE(excluded.comment, drafts.comment)",
                        (date.isoformat(), issue, part, secs, body.get("comment")))
                conn.commit()
                return self._json(day_payload(conn, cfg, date))
            if path == "/api/part":
                # Вынести активности отдельной записью задачи (part — ключ темы) или вернуть в общую (part пустой).
                old = conn.execute("SELECT DISTINCT part FROM parts WHERE date=? AND issue=? AND context_key IN (%s)"
                                   % ",".join("?" * len(body["keys"])), [date.isoformat(), body["issue"], *body["keys"]]).fetchall()
                # Отправленная запись в Jira заморожена: перенос времени из неё или в неё задвоил бы или потерял часы.
                touched = {p for (p,) in old} | {"", body.get("part") or ""}
                if conn.execute("SELECT 1 FROM drafts WHERE date=? AND issue=? AND jira_id IS NOT NULL AND part IN (%s)"
                                % ",".join("?" * len(touched)), [date.isoformat(), body["issue"], *touched]).fetchone():
                    return self._json({"error": "Запись уже в Jira — делить её можно только до отправки"}, 409)
                db.set_part(conn, date.isoformat(), body["issue"], body["keys"], body.get("part") or "")
                # Правки записи, которой больше нет, не нужны (если её ещё не отправили в Jira).
                for (p,) in [*old, (body.get("part") or "",)]:
                    if p and not conn.execute("SELECT 1 FROM parts WHERE date=? AND issue=? AND part=?",
                                              (date.isoformat(), body["issue"], p)).fetchone():
                        conn.execute("DELETE FROM drafts WHERE date=? AND issue=? AND part=? AND jira_id IS NULL",
                                     (date.isoformat(), body["issue"], p))
                conn.commit()
                return self._json(day_payload(conn, cfg, date))
            if path == "/api/push":
                res = jira.push_day(conn, cfg, date, dry_run=bool(body.get("dry_run")), only=body.get("only"))
                return self._json({"results": res, "day": day_payload(conn, cfg, date)})
            if path == "/api/refresh_logged":
                jira.refresh_logged(conn, cfg, date)
                return self._json(day_payload(conn, cfg, date))
            if path == "/api/suggest":
                r = suggest.run(conn, cfg, date)
                r["comments"] = suggest.comments(conn, cfg, date)
                return self._json({"result": r, "day": day_payload(conn, cfg, date)})
            if path == "/api/pause":
                set_pause(cfg, body.get("mode"))
                return self._json(recording_status(cfg))
            if path == "/api/settings":
                rems = list(cfg.get("reminders") or [{"review": "previous_workday"}])
                t = body.get("reminder_time") or rems[0].get("time", "10:00")
                if not re.fullmatch(r"\d{1,2}:\d{2}", t):
                    return self._json({"error": "Время напоминания — в виде 10:00"}, 400)
                rems[0] = dict(rems[0], time=t, days=body.get("reminder_days") or rems[0].get("days", []))
                # Утром напоминаем про вчерашний рабочий день, с 15:00 — про сегодняшний.
                rems[0]["review"] = "today" if int(t.split(":")[0]) >= 15 else "previous_workday"
                if "reminder_enabled" in body:
                    rems[0]["enabled"] = bool(body["reminder_enabled"])
                upd = {"reminders": rems}
                if body.get("reminder_style") in ("dialog", "notification"):
                    upd["reminder_style"] = body["reminder_style"]
                config.save_user(upd)
                from .remind import install_agents
                install_agents(config.load(), collector=False)
                return self._json({"ok": True})
            if path == "/api/settings/workday":
                h = float(body.get("hours") or 0)
                if not 0 <= h <= 14:
                    return self._json({"error": "Норма дня — от 0 до 14 часов"}, 400)
                config.save_user({"worklog": {"day_hours": h}})
                return self._json({"ok": True})
            if path == "/api/update/install":
                update.install_async(cfg, os.getpid())
                return self._json(update.state(cfg, background=False))
            if path == "/api/update/ack":
                update.ack()
                return self._json({"ok": True})
            if path == "/api/update/auto":
                config.save_user({"updates": {"auto": bool(body.get("on"))}})
                return self._json(update.state(config.load(), background=False))
            if path == "/api/cache/clear":
                return self._json(clear_cache(conn))
            if path == "/api/local/install":
                local_ai.install_async(cfg)
                return self._json(local_ai.status(cfg))
            if path == "/api/local/remove":
                local_ai.remove(cfg)
                return self._json(local_ai.status(cfg))
            if path == "/api/local_suggest":
                if not local_ai.status(cfg)["ready"]:
                    return self._json({"error": "Нейросеть не установлена", "need_install": True}, 400)
                try:
                    r = local_ai.run(conn, cfg, date)
                except local_ai.LocalAIError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"result": r, "day": day_payload(conn, cfg, date)})
            if path == "/api/issues/sync":
                n = jira.sync_issues(conn, cfg)
                return self._json({"synced": n})
            if path == "/api/mark":
                status = body.get("status")
                if status:
                    conn.execute("INSERT OR REPLACE INTO days(date, status, updated) VALUES(?,?,?)",
                                 (date.isoformat(), status, time.time()))
                else:
                    conn.execute("DELETE FROM days WHERE date=?", (date.isoformat(),))
                conn.commit()
                return self._json(day_payload(conn, cfg, date))
            return self._json({"error": "not found"}, 404)
        except jira.JiraError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, idle_exit_sec):
        super().__init__(addr, Handler)
        self._local = threading.local()
        self.last = time.time()
        self.idle_exit_sec = idle_exit_sec

    @property
    def conn(self):
        # Своё соединение на поток: одно общее sqlite3-соединение ломается при параллельных запросах.
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = db.connect()
        return c

    def touch(self):
        self.last = time.time()


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def serve(cfg, foreground=True):
    port = cfg["server"]["port"]
    srv = Server(("127.0.0.1", port), cfg["server"]["idle_exit_min"] * 60)

    def watchdog():
        tick = time.time()
        while True:
            time.sleep(30)
            now = time.time()
            if now - tick > 120:  # Мак спал: сон не считаем простоем, иначе после пробуждения окно теряет сервер
                srv.touch()
            tick = now
            if srv.idle_exit_sec and now - srv.last > srv.idle_exit_sec:
                srv.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    if foreground:
        print("Таймлайн: http://127.0.0.1:%d" % port)
    srv.serve_forever()


def _running_version(port):
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=2) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None


def ensure_server(cfg):
    port = cfg["server"]["port"]
    if port_open(port):
        if _running_version(port) == code_version():
            return port
        # Запущен сервер со старым кодом — останавливаем и поднимаем заново.
        import urllib.request
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:%d/api/shutdown" % port, data=b"{}", method="POST",
                headers={"X-TT": "1", "Content-Type": "application/json"}), timeout=2).read()
        except Exception:
            subprocess.run(["pkill", "-f", "tt.py serve"], capture_output=True)
        for _ in range(30):
            if not port_open(port):
                break
            time.sleep(0.1)
    os.makedirs(config.LOG_DIR, exist_ok=True)
    log = open(os.path.join(config.LOG_DIR, "server.log"), "a")
    subprocess.Popen([sys.executable, os.path.join(config.APP_DIR, "tt.py"), "serve", "--quiet"],
                     stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        if port_open(port):
            break
        time.sleep(0.1)
    return port


def open_ui(cfg, date=None, extra=""):
    port = ensure_server(cfg)
    q = "&".join(filter(None, ["date=%s" % date.isoformat() if date else "", extra]))
    url = "http://127.0.0.1:%d/" % port + ("?" + q if q else "")
    subprocess.run(["open", url])
    return url
