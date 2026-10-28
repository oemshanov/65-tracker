"""Напоминания о разборе времени и launchd-агенты."""
import datetime as dt
import os
import plistlib
import subprocess
import sys
import time

from . import config
from .segment import build_day, fmt_dur, propose

WEEKDAYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7,
            "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6, "вс": 7}
LAUNCH_DIR = os.path.expanduser("~/Library/LaunchAgents")
COLLECTOR_LABEL = "com.timetracker.collector"
REMIND_LABEL = "com.timetracker.remind"
COLLECTOR_APP = os.path.expanduser("~/Applications/65 tracker.app")  # установка через install.sh
OLD_APP = os.path.expanduser("~/Applications/Учёт времени.app")  # прежнее имя — install.sh удаляет
# Приложение из .dmg может лежать где угодно (обычно /Applications) и несёт свой Python:
# при запуске оно записывает пути к себе и к Python сюда.
APP_PATH_FILE = os.path.join(config.SUPPORT_DIR, "app_path")
PYTHON_PATH_FILE = os.path.join(config.SUPPORT_DIR, "python_path")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def collector_app():
    p = _read(APP_PATH_FILE)
    return p if p and os.path.isdir(p) else COLLECTOR_APP


def python_exe():
    p = _read(PYTHON_PATH_FILE)
    return p if p and os.access(p, os.X_OK) else "/usr/bin/python3"


def day_needs_review(conn, cfg, date, check_jira=True):
    """→ (нужно ли, сводка) для даты."""
    st = conn.execute("SELECT status FROM days WHERE date=?", (date.isoformat(),)).fetchone()
    if st and st["status"] in ("manual", "done"):
        return False, None
    day = build_day(conn, cfg, date)
    p = propose(conn, cfg, day)
    if p["total"] < 30 * 60:
        return False, None
    pending = [w for w in p["worklogs"] if w["status"] in ("draft", "changed")]
    pushed = st and st["status"] == "pushed"
    if not (pending or (p["unassigned"] >= 30 * 60 and not pushed)):
        return False, p
    # Если в Jira за этот день уже есть ваши часы (внесли сами или иначе) — не напоминаем.
    work = p["total"] - p["skipped"]
    if check_jira and jira_logged_total(conn, cfg, date) >= 0.9 * work:
        return False, p
    return True, p


def jira_logged_total(conn, cfg, date):
    """Сколько вы уже залогировали в Jira за дату (кэш на час). 0, если Jira не подключена или недоступна."""
    from . import jira
    rows = list(conn.execute("SELECT seconds, fetched FROM jira_logged WHERE date=?", (date.isoformat(),)))
    if rows and time.time() - max(r["fetched"] for r in rows) < 3600:
        return sum(r["seconds"] for r in rows)
    if not jira.Jira(cfg).configured:
        return 0
    try:
        return sum(jira.refresh_logged(conn, cfg, date).values())
    except jira.JiraError:
        return 0


def candidate_days(cfg, mode, today=None):
    today = today or dt.date.today()
    workdays = set(cfg["day"]["workdays"])
    if mode == "today":
        return [today]
    days = []
    d = today - dt.timedelta(days=1)
    span = 7 if mode == "week" else 10
    while len(days) < span and (today - d).days <= 21:
        if d.isoweekday() in workdays or mode == "week":
            days.append(d)
        d -= dt.timedelta(days=1)
    return days


def _current_mode(cfg):
    now = dt.datetime.now()
    for r in cfg.get("reminders", []):
        hh, mm = [int(x) for x in r["time"].split(":")]
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days = {WEEKDAYS[d.lower()] for d in r.get("days", [])} or set(range(1, 8))
        if now.isoweekday() in days and abs((now - at).total_seconds()) < 3 * 3600:
            return r.get("review", "previous_workday")
    return "previous_workday"


def osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip()


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run(conn, cfg, force=False, mode=None):
    from . import suggest
    db_retention = cfg.get("retention_days")
    if db_retention:
        from .db import purge
        purge(conn, db_retention)
    mode = mode or _current_mode(cfg)
    need = []
    for d in candidate_days(cfg, mode):
        ok, p = day_needs_review(conn, cfg, d)
        if ok:
            need.append((d, p))
    if not need and not force:
        return []
    if cfg["llm"].get("enabled") and cfg["llm"].get("pre_classify_on_remind"):
        for d, _ in need[:5]:
            try:
                suggest.run(conn, cfg, d, quiet=True)
            except Exception as e:  # подсказки — бонус, напоминание важнее
                print("suggest %s: %s" % (d, e), file=sys.stderr)
        need = [(d, propose(conn, cfg, build_day(conn, cfg, d))) for d, _ in need]
    if not need:
        text = "Всё разложено и отправлено 👌"
    else:
        lines = []
        for d, p in sorted(need)[:6]:
            wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][d.weekday()]
            tail = (", без задачи %s" % fmt_dur(p["unassigned"])) if p["unassigned"] >= 15 * 60 else ""
            lines.append("%s %s — %s%s" % (wd, d.strftime("%d.%m"), fmt_dur(p["total"]), tail))
        text = "Не залогировано:\n" + "\n".join(lines)
    target = sorted(need)[0][0] if need else dt.date.today()
    if cfg.get("reminder_style") == "notification":
        osa("display notification %s with title \"65 tracker\" subtitle \"Нажмите, чтобы разобрать\"" % _q(text))
        return need
    btn = osa(
        "display dialog %s with title \"65 tracker\" buttons {\"Позже\", \"Разобрать\"} "
        "default button \"Разобрать\" giving up after 900" % _q(text + "\n\nРазбор займёт пару минут."))
    if "Разобрать" in btn:
        from .server import open_ui
        open_ui(cfg, target)
    elif "Позже" in btn:
        snooze = int(cfg.get("snooze_min", 60)) * 60
        tt = os.path.join(config.APP_DIR, "tt.py")
        subprocess.Popen(["/bin/sh", "-c", "sleep %d; exec %s %s remind --mode %s" %
                          (snooze, _shq(python_exe()), _shq(tt), mode)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return need


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _write_plist(label, data):
    os.makedirs(LAUNCH_DIR, exist_ok=True)
    path = os.path.join(LAUNCH_DIR, label + ".plist")
    data = dict(data, Label=label)
    with open(path, "wb") as f:
        plistlib.dump(data, f)
    uid = os.getuid()
    target = "gui/%d/%s" % (uid, label)
    subprocess.run(["launchctl", "bootout", target], capture_output=True)
    # bootout асинхронный: ждём, пока сервис выгрузится, иначе bootstrap падает с I/O error.
    for _ in range(50):
        if subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0:
            break
        time.sleep(0.1)
    for attempt in range(5):
        r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, path], capture_output=True, text=True)
        if r.returncode == 0:
            return path
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("launchctl bootstrap %s: %s" % (label, r.stderr.strip()))


def install_agents(cfg, collector=True):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    out = []
    if collector:
        exe = os.path.join(collector_app(), "Contents/MacOS/ttcollect")
        out.append(_write_plist(COLLECTOR_LABEL, {
            # Перезапуск только при сбое: «Выйти» в меню завершает с кодом 0 и не воскресает.
            "ProgramArguments": [exe], "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Interactive",
            "Nice": 10, "LowPriorityIO": True, "ThrottleInterval": 30,
            "StandardErrorPath": os.path.join(config.LOG_DIR, "collector.log"),
        }))
    intervals = []
    for r in cfg.get("reminders", []):
        if r.get("enabled") is False:
            continue  # выключено в настройках
        hh, mm = [int(x) for x in r["time"].split(":")]
        for d in r.get("days", []) or list(WEEKDAYS)[:7]:
            iso = WEEKDAYS[d.lower()]
            intervals.append({"Weekday": iso % 7, "Hour": hh, "Minute": mm})
    path = os.path.join(LAUNCH_DIR, REMIND_LABEL + ".plist")
    if intervals:
        out.append(_write_plist(REMIND_LABEL, {
            "ProgramArguments": [python_exe(), os.path.join(config.APP_DIR, "tt.py"), "remind"],
            "StartCalendarInterval": intervals,
            "StandardOutPath": os.path.join(config.LOG_DIR, "remind.log"),
            "StandardErrorPath": os.path.join(config.LOG_DIR, "remind.log"),
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + os.path.expanduser("~/.local/bin"),
                                     "PYTHONPYCACHEPREFIX": os.path.join(config.SUPPORT_DIR, "pycache")},
        }))
    elif os.path.exists(path):
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), REMIND_LABEL)], capture_output=True)
        os.remove(path)
    return out


def uninstall_agents():
    for label in (COLLECTOR_LABEL, REMIND_LABEL):
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)], capture_output=True)
        p = os.path.join(LAUNCH_DIR, label + ".plist")
        if os.path.exists(p):
            os.remove(p)


def collector_running():
    # Сборщик может быть запущен и launchd, и вручную из Spotlight.
    return subprocess.run(["pgrep", "-x", "ttcollect"], capture_output=True).returncode == 0


def collector_paused():
    return os.path.exists(os.path.join(config.SUPPORT_DIR, "paused_until"))
