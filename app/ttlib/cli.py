"""tt — учёт рабочего времени без таймеров. `tt help` — список команд."""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

from . import config, db, jira, remind, suggest
from .segment import build_day, fmt_dur, parse_date, parse_dur, propose

STATUS_RU = {"draft": "черновик", "changed": "изменён после отправки", "pushed": "в Jira", "in_jira": "уже в Jira"}


def hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def cmd_status(a, conn, cfg):
    last = conn.execute("SELECT MAX(end) FROM spans").fetchone()[0]
    running = remind.collector_running()
    print("Сборщик:     %s" % (("на паузе" if remind.collector_paused() else "работает") if running
                                  else "НЕ запущен — откройте «65 tracker» через Spotlight"))
    if last:
        ago = time.time() - last
        print("Последняя запись: %s (%s назад)" % (time.strftime("%d.%m %H:%M:%S", time.localtime(last)),
                                                  fmt_dur(ago) if ago > 60 else "%dс" % ago))
    recent = conn.execute("SELECT COUNT(*), COUNT(title) FROM spans WHERE end > ? AND state = 'active'",
                          (time.time() - 600,)).fetchone()
    if recent[0] == 0:
        print("Заголовки окон: нет активности за 10 мин — не проверить")
    else:
        print("Заголовки окон: %s" % ("собираются" if recent[1] else
                                      "НЕ собираются — нет «Универсального доступа» у «65 tracker»"))
    day = build_day(conn, cfg, dt.date.today())
    p = propose(conn, cfg, day)
    print("Сегодня:     %s активно, к логированию %s, без задачи %s"
          % (fmt_dur(p["total"]), fmt_dur(p["to_log"]), fmt_dur(p["unassigned"])))
    j = jira.Jira(cfg)
    print("Jira:        %s (%s)" % (cfg["jira"]["base_url"], ("пользователь %s, токен в Связке ключей" % j.user)
                                    if j.configured else "токен не сохранён — tt setup"))
    n = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    print("Задач в кэше: %d, выученных привязок: %d" % (n, conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0]))
    print("База:        %s (%.1f МБ)" % (config.DB_PATH, os.path.getsize(config.DB_PATH) / 1e6))


def cmd_day(a, conn, cfg):
    date = parse_date(a.date)
    day = build_day(conn, cfg, date)
    p = propose(conn, cfg, day)
    if a.json:
        slim = []
        for b in day["blocks"]:
            slim.append({
                "id": b["id"], "time": "%s–%s" % (hm(b["start"]), hm(b["end"])), "start": b["start"],
                "end": b["end"], "duration": fmt_dur(b["seconds"]), "seconds": b["seconds"], "issue": b["issue"],
                "skip": b["skip"], "source": b["source"], "label": b["label"], "kind": b["kind"],
                "context_key": b["context_key"], "suggestion": b["suggestion"],
                "contexts": [{"key": c["key"], "label": c["label"], "duration": fmt_dur(c["seconds"]),
                              "title": c["title"], "url": c["url"], "issue": c["resolved_issue"]}
                             for c in b["contexts"][:4]],
            })
        print(json.dumps({"date": day["date"], "blocks": slim, "proposal": p, "events": day["events"]},
                         ensure_ascii=False, indent=1))
        return
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][date.weekday()]
    print("%s %s · активно %s · к логированию %s · без задачи %s · личное %s\n" % (
        wd, date.strftime("%d.%m.%Y"), fmt_dur(p["total"]), fmt_dur(p["to_log"]), fmt_dur(p["unassigned"]),
        fmt_dur(p["skipped"])))
    for b in day["blocks"]:
        tag = b["issue"] or ("личное" if b["skip"] else "по дню" if b.get("split") else "?")
        sug = ""
        if b["suggestion"] and b["suggestion"].get("issue"):
            sug = "  ← Claude: %s (%.0f%%)" % (b["suggestion"]["issue"], 100 * (b["suggestion"]["confidence"] or 0))
        print("  %s–%s %7s  %-12s %s%s" % (hm(b["start"]), hm(b["end"]), fmt_dur(b["seconds"]), tag,
                                          b["label"][:70], sug))
    if p["worklogs"]:
        print("\nВорклоги:")
        for w in p["worklogs"]:
            print("  %-12s %7s  [%s]  %s" % (w["issue"], fmt_dur(w["seconds"]), STATUS_RU[w["status"]],
                                             w["comment"][:80]))


def cmd_export(a, conn, cfg):
    date = parse_date(a.date)
    p = propose(conn, cfg, build_day(conn, cfg, date))
    if a.format == "day":
        from .report import day_report
        print(day_report(conn, cfg, date), end="")
        return
    if a.format == "json":
        print(json.dumps(p["worklogs"], ensure_ascii=False, indent=1))
    elif a.format == "csv":
        import csv
        w = csv.writer(sys.stdout)
        w.writerow(["date", "issue", "hours", "comment"])
        for r in p["worklogs"]:
            w.writerow([date.isoformat(), r["issue"], "%.2f" % (r["seconds"] / 3600), r["comment"]])
    else:
        print("%s — итого %s" % (date.strftime("%d.%m.%Y"), fmt_dur(p["to_log"])))
        for r in p["worklogs"]:
            print("%s\t%s\t%s" % (r["issue"], fmt_dur(r["seconds"]), r["comment"]))
        if p["unassigned"]:
            print("(без задачи: %s)" % fmt_dur(p["unassigned"]))


def _find_block(conn, cfg, block_id):
    ts = float(block_id)
    date = (dt.datetime.fromtimestamp(ts) - dt.timedelta(hours=cfg["day"]["start_hour"])).date()
    day = build_day(conn, cfg, date)
    for b in day["blocks"]:
        if b["id"] == block_id:
            return date, b
    raise SystemExit("Блок %s не найден (id = начало блока из `tt day --json`)" % block_id)


def cmd_assign(a, conn, cfg):
    issue = a.issue.upper()
    skip = issue in ("-", "SKIP", "PERSONAL", "ЛИЧНОЕ")
    split = issue in ("*", "SPLIT", "ДЕНЬ")
    clear = issue in ("NONE", "AUTO")
    issue_v = None if (skip or clear or split) else issue
    cat = "split" if split else ("personal" if skip else None)
    if a.context:
        if clear:
            db.delete_mapping(conn, a.context)
        else:
            db.set_mapping(conn, a.context, issue_v, cat, a.label)
        print("Запомнил: %s → %s" % (a.context, issue_v or ("по задачам дня" if split else "личное" if skip else "авто")))
        return
    for bid in a.block:
        date, b = _find_block(conn, cfg, bid)
        if a.remember and b["context_key"]:
            if clear:
                db.delete_mapping(conn, b["context_key"])
            else:
                db.set_mapping(conn, b["context_key"], issue_v, cat, b["label"])
        else:
            db.add_override(conn, b["start"], b["end"], issue_v, cat)
        print("%s–%s %s → %s%s" % (hm(b["start"]), hm(b["end"]), b["label"][:50],
                                   issue_v or ("личное" if skip else "авто"), " (запомнил)" if a.remember else ""))


def cmd_draft(a, conn, cfg):
    date = parse_date(a.date)
    issue = a.issue.upper()
    if a.reset:
        conn.execute("UPDATE drafts SET seconds=NULL, comment=NULL WHERE date=? AND issue=?", (date.isoformat(), issue))
    else:
        secs = parse_dur(a.time) if a.time else None
        conn.execute(
            "INSERT INTO drafts(date, issue, seconds, comment) VALUES(?,?,?,?) ON CONFLICT(date, issue) DO UPDATE "
            "SET seconds=COALESCE(excluded.seconds, drafts.seconds), comment=COALESCE(excluded.comment, drafts.comment)",
            (date.isoformat(), issue, secs, a.comment))
    conn.commit()
    print("OK")


def cmd_push(a, conn, cfg):
    date = parse_date(a.date)
    p = propose(conn, cfg, build_day(conn, cfg, date))
    todo = [w for w in p["worklogs"] if w["status"] in ("draft", "changed") and (not a.only or w["issue"] in a.only)]
    if not todo:
        print("Нечего отправлять за %s." % date.isoformat())
        return
    print("К отправке за %s:" % date.strftime("%d.%m.%Y"))
    for w in todo:
        print("  %-12s %7s  %s%s" % (w["issue"], fmt_dur(w["seconds"]), w["comment"][:70],
                                     " (обновление)" if w["status"] == "changed" else ""))
    if p["unassigned"]:
        print("  (без задачи %s — не отправляется)" % fmt_dur(p["unassigned"]))
    if not a.dry_run and not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Нужен --yes для отправки без терминала")
        if input("Отправить в Jira? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
            return
    res = jira.push_day(conn, cfg, date, dry_run=a.dry_run, only=a.only)
    for r in res:
        if r["action"] == "error":
            print("  ✗ %s: %s" % (r["issue"], r["error"]))
        elif r["action"] in ("create", "update"):
            print("  %s %s %s%s" % ("·" if r.get("dry_run") else "✓", r["issue"], fmt_dur(r["seconds"]),
                                    " (dry-run)" if r.get("dry_run") else ""))
    if any(r["action"] == "error" for r in res):
        return 1


def cmd_issues(a, conn, cfg):
    if a.sync:
        print("Загружено задач: %d" % jira.sync_issues(conn, cfg), file=sys.stderr)
    q = "%" + (a.search or "") + "%"
    rows = [dict(r) for r in conn.execute(
        "SELECT key, summary, project, project_name, status FROM issues WHERE key LIKE ? OR summary LIKE ? "
        "OR project_name LIKE ? ORDER BY updated DESC", (q, q, q))]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            print("%-12s %-14s %s" % (r["key"], (r["status"] or "")[:14], r["summary"] or ""))


def cmd_issue(a, conn, cfg):
    d = jira.Jira(cfg).details(a.key.upper())
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return
    print("%s · %s · %s · %s" % (d["key"], d["summary"], d["project"], d["status"]))
    if d["parent"]:
        print("Родитель: %s" % d["parent"])
    print(d["description"][:2000] or "(описания нет)")


def cmd_suggest(a, conn, cfg):
    date = parse_date(a.date)
    r = suggest.run(conn, cfg, date)
    print("Claude разобрал контекстов: %d, подсказок сохранено: %d" % (r["asked"], r["saved"]))


def cmd_create_issue(a, conn, cfg):
    j = jira.Jira(cfg)
    if not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Нужен --yes")
        if input("Создать %s «%s» (%s)? [y/N] " % (a.project, a.summary, a.type)).lower() not in ("y", "д", "да"):
            return
    key = j.create_issue(a.project.upper(), a.summary, a.type, a.description)
    jira.ensure_issue(conn, j, key)
    print(key)


def cmd_setup(a, conn, cfg):
    if not a.cli:
        from .server import open_ui
        open_ui(cfg, extra="connect=1")
        print("Открыл в браузере окно подключения Jira — вставьте токен там. Это окно можно закрыть.")
        return
    base = input("Адрес Jira [%s]: " % cfg["jira"]["base_url"]).strip() or cfg["jira"]["base_url"]
    user = input("Логин Jira [%s]: " % (cfg["jira"]["username"] or "")).strip() or cfg["jira"]["username"]
    print("Способ входа: 1 — Personal Access Token (Профиль → Personal Access Tokens), 2 — пароль")
    auth = "basic" if input("[1]: ").strip() == "2" else "pat"
    config.save_user({"jira": {"base_url": base, "username": user, "auth": auth}})
    if not jira.keychain_store_interactive(user):
        raise SystemExit("Не удалось сохранить в Связку ключей")
    cfg = config.load()
    try:
        me = jira.Jira(cfg).myself()
        print("✓ Вход выполнен: %s (%s)" % (me.get("displayName"), me.get("name")))
        print("Задач загружено: %d" % jira.sync_issues(conn, cfg))
    except jira.JiraError as e:
        print("✗ %s" % e)
        return 1


def cmd_mark(a, conn, cfg):
    date = parse_date(a.date)
    if a.status == "open":
        conn.execute("DELETE FROM days WHERE date=?", (date.isoformat(),))
    else:
        conn.execute("INSERT OR REPLACE INTO days(date, status, updated) VALUES(?,?,?)",
                     (date.isoformat(), a.status, time.time()))
    conn.commit()
    print("%s: %s" % (date.isoformat(), a.status))


def cmd_rules(a, conn, cfg):
    rows = list(conn.execute("SELECT * FROM mappings ORDER BY updated DESC"))
    for r in rows:
        print("%-12s %s" % (r["issue"] or r["category"], r["label"] or r["context_key"]))
    if not rows:
        print("Пока нет выученных привязок.")


def cmd_agents(a, conn, cfg):
    if a.action == "install":
        for p in remind.install_agents(cfg, collector=not a.reminders_only):
            print("✓", p)
    else:
        remind.uninstall_agents()
        print("Агенты удалены")


def main(argv):
    ap = argparse.ArgumentParser(prog="tt", description=__doc__)
    sp = ap.add_subparsers(dest="cmd")

    sp.add_parser("status", help="состояние сборщика и сегодняшний день")
    p = sp.add_parser("day", help="блоки дня и черновики ворклогов")
    p.add_argument("date", nargs="?", default="today")
    p.add_argument("--json", action="store_true")
    p = sp.add_parser("open", help="открыть таймлайн в браузере")
    p.add_argument("date", nargs="?")
    sp.add_parser("ensure-server", help="поднять сервер таймлайна в фоне и напечатать адрес (для окна приложения)")
    p = sp.add_parser("serve", help="запустить сервер таймлайна")
    p.add_argument("--quiet", action="store_true")
    p = sp.add_parser("assign", help="привязать блок(и) или контекст к задаче (ISSUE | - личное | none)")
    p.add_argument("issue")
    p.add_argument("block", nargs="*", help="id блоков из `tt day --json`")
    p.add_argument("--context", help="ключ контекста вместо блока")
    p.add_argument("--label")
    p.add_argument("--remember", action="store_true", help="запомнить для этого контекста навсегда")
    p = sp.add_parser("draft", help="поправить часы/комментарий ворклога")
    p.add_argument("date")
    p.add_argument("issue")
    p.add_argument("--time")
    p.add_argument("--comment")
    p.add_argument("--reset", action="store_true")
    p = sp.add_parser("push", help="отправить ворклоги в Jira")
    p.add_argument("date", nargs="?", default="today")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--only", nargs="*")
    p = sp.add_parser("export", help="ворклоги для ручного ввода")
    p.add_argument("date", nargs="?", default="today")
    p.add_argument("--format", choices=["text", "csv", "json", "day"], default="text",
                   help="day — весь день текстом (для Claude, чата, заметок)")
    p = sp.add_parser("issues", help="задачи из Jira")
    p.add_argument("--sync", action="store_true")
    p.add_argument("--search")
    p.add_argument("--json", action="store_true")
    p = sp.add_parser("issue", help="задача из Jira: название, проект, описание (контекст для комментария)")
    p.add_argument("key")
    p.add_argument("--json", action="store_true")
    p = sp.add_parser("suggest", help="попросить Claude разобрать неясные блоки")
    p.add_argument("date", nargs="?", default="today")
    p = sp.add_parser("create-issue", help="создать задачу в Jira")
    p.add_argument("project")
    p.add_argument("summary")
    p.add_argument("--type", default="Task")
    p.add_argument("--description")
    p.add_argument("--yes", action="store_true")
    p = sp.add_parser("setup", help="подключить Jira (токен — в Связку ключей)")
    p.add_argument("--cli", action="store_true", help="спросить логин/токен в терминале, а не в браузере")
    p = sp.add_parser("mark", help="статус дня: manual (внёс сам), done, open")
    p.add_argument("date")
    p.add_argument("status", choices=["manual", "done", "open"])
    sp.add_parser("rules", help="выученные привязки")
    p = sp.add_parser("remind", help="проверить, есть ли что разобрать, и напомнить")
    p.add_argument("--force", action="store_true")
    p.add_argument("--mode", choices=["previous_workday", "today", "week"])
    p = sp.add_parser("agents", help="установить/удалить launchd-агенты (после правки reminders)")
    p.add_argument("action", choices=["install", "uninstall"])
    p.add_argument("--reminders-only", action="store_true")
    sp.add_parser("config", help="открыть config.json")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    cfg = config.load()
    if a.cmd == "serve":
        from .server import serve
        return serve(cfg, foreground=not a.quiet)
    if a.cmd == "ensure-server":
        from .server import ensure_server
        print("http://127.0.0.1:%d/" % ensure_server(cfg))
        return 0
    if a.cmd == "config":
        if not os.path.exists(config.CONFIG_PATH):
            config.save_user({})
        print(config.CONFIG_PATH)
        subprocess.run(["open", "-t", config.CONFIG_PATH])
        return 0
    conn = db.connect()
    if a.cmd == "open":
        from .server import open_ui
        print(open_ui(cfg, parse_date(a.date) if a.date else None))
        return 0
    if a.cmd == "remind":
        remind.run(conn, cfg, force=a.force, mode=a.mode)
        return 0
    fn = {"status": cmd_status, "day": cmd_day, "assign": cmd_assign, "draft": cmd_draft, "push": cmd_push,
          "export": cmd_export, "issues": cmd_issues, "issue": cmd_issue, "suggest": cmd_suggest, "create-issue": cmd_create_issue,
          "setup": cmd_setup, "mark": cmd_mark, "rules": cmd_rules, "agents": cmd_agents}[a.cmd]
    try:
        return fn(a, conn, cfg) or 0
    except jira.JiraError as e:
        print("Jira: %s" % e, file=sys.stderr)
        return 2
