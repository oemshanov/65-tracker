"""Figma: «запомнить» работает на файл/проект (часть до « / »), экраны остаются в комментарии.
TT_HOME=<tmp> python3 tests/test_figma.py"""
import datetime as dt
import os
import sys
import time

# Тест стирает и пишет данные — только во временную базу, не в настоящую.
if not os.environ.get("TT_HOME") or os.path.realpath(os.environ["TT_HOME"]) == os.path.realpath(
        os.path.expanduser("~/Library/Application Support/TimeTracker")):
    sys.exit("Нужна временная база: TT_HOME=$(mktemp -d) python3 " + " ".join(sys.argv))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from ttlib import claude_sessions, config, db  # noqa: E402
from ttlib.segment import build_day, propose  # noqa: E402

claude_sessions.update = lambda conn, **kw: None
conn = db.connect()
date = dt.date(2026, 10, 20)
T = lambda h, m: time.mktime((date.year, date.month, date.day, h, m, 0, 0, 0, -1))  # noqa: E731
conn.execute("DELETE FROM spans WHERE start >= ? AND start < ?", (T(0, 0), T(23, 0)))
conn.execute("DELETE FROM mappings WHERE context_key LIKE 'file:figma:%'")
conn.execute("DELETE FROM suggestions WHERE context_key LIKE 'file:figma:%'")
FIG = "com.figma.Desktop"
day_plan = [  # разные проекты и экраны вперемешку
    (10, 0, 40, "Polka / Карточка товара"), (10, 40, 20, "Polka / Фильтры"),
    (11, 0, 30, "Связь / Лендинг тарифов"), (11, 30, 25, "Polka / Корзина"),
    (12, 0, 30, "Банк Север Онбординг"), (12, 30, 15, "Связь / Личный кабинет"),
]
for h, m, mins, title in day_plan:
    conn.execute("INSERT INTO spans(start,end,state,app,bundle,title) VALUES(?,?,?,?,?,?)",
                 (T(h, m), T(h, m) + mins * 60, "active", "Figma", FIG, title))
conn.commit()
cfg = config.load()


def show(title):
    print("\n" + title)
    day = build_day(conn, cfg, date)
    for b in day["blocks"]:
        print("  %s %3dм %-8s %s" % (time.strftime("%H:%M", time.localtime(b["start"])), b["seconds"] // 60,
                                      b["issue"] or "?", b["label"]))
    for w in propose(conn, cfg, day)["worklogs"]:
        print("  ворклог %-7s %3dм  %s" % (w["issue"], w["seconds"] // 60, w["comment"]))
    return day


day = show("До привязок (каждый проект — свой блок, экраны внутри):")
groups = {b["context_key"] for b in day["blocks"]}
assert groups == {"file:figma:polka", "file:figma:связь", "file:figma:банк север онбординг"}, groups
# «Запомнить» на одном экране Polka и одном экране Связь — как кнопка в таймлайне (сохраняет группу блока).
for b in day["blocks"]:
    if b["context_key"] == "file:figma:polka" and "Карточка" in b["label"]:
        db.set_mapping(conn, b["context_key"], "PL-142", None, b["label"])
    if b["context_key"] == "file:figma:связь" and "Лендинг" in b["label"]:
        db.set_mapping(conn, b["context_key"], "MTS-3", None, b["label"])
day = show("После «запомнить» на «Polka / Карточка товара» и «Связь / Лендинг тарифов»:")
by = {}
for b in day["blocks"]:
    by.setdefault(b["issue"], 0)
    by[b["issue"]] += b["seconds"] // 60
assert by.get("PL-142") == 85 and by.get("MTS-3") == 45 and by.get(None) == 30, by
print("\nOK: Фильтры и Корзина ушли в PL-142, Личный кабинет — в MTS-3, «Банк Север Онбординг» не тронут")
