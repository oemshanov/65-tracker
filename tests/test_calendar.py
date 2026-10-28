"""Календарь не заполняет время, когда трекер не работал; перерывы — личное. TT_HOME=<tmp> python3 tests/test_calendar.py"""
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from ttlib import claude_sessions, config, db, remind  # noqa: E402
from ttlib.segment import build_day  # noqa: E402

claude_sessions.update = lambda conn, **kw: None
conn = db.connect()
cfg = config.load()
d_off, d_on = dt.date(2026, 10, 16), dt.date(2026, 10, 15)
for d in (d_off, d_on):
    T = lambda h, m: time.mktime((d.year, d.month, d.day, h, m, 0, 0, 0, -1))  # noqa: E731
    conn.execute("DELETE FROM spans WHERE start >= ? AND start < ?", (T(0, 0), T(23, 0)))
    conn.execute("DELETE FROM calendar_events WHERE start >= ? AND start < ?", (T(0, 0), T(23, 0)))
    conn.executemany("INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?)", [
        ("d%s" % d, T(10, 30), T(11, 0), "Альфа - Дейлик", "w", None, 0, 5),
        ("s%s" % d, T(12, 30), T(13, 30), "Синхронизация по дизайну", "w", None, 0, 4),
        ("c%s" % d, T(14, 0), T(14, 20), "Coffee Break☕️", "w", None, 0, 9),
        ("e%s" % d, T(19, 0), T(19, 30), "Вечерний созвон", "w", None, 0, 2)])
# 15.10: трекер работал 10:00–15:00, ноутбук простаивал во время встреч.
T = lambda h, m: time.mktime((d_on.year, d_on.month, d_on.day, h, m, 0, 0, 0, -1))  # noqa: E731
conn.executemany("INSERT INTO spans(start,end,state,app,bundle,title) VALUES(?,?,?,?,?,?)", [
    (T(10, 0), T(10, 30), "active", "Figma", "com.figma.Desktop", "Polka / Корзина"),
    (T(10, 30), T(14, 30), "idle", None, None, None),
    (T(14, 30), T(15, 0), "active", "Figma", "com.figma.Desktop", "Polka / Корзина")])
conn.commit()

off = build_day(conn, cfg, d_off)
print("день без трекера:", [b["label"] for b in off["blocks"]])
assert not off["blocks"], "без записей трекера календарь не должен ничего добавлять"
assert remind.day_needs_review(conn, cfg, d_off, check_jira=False)[0] is False

on = build_day(conn, cfg, d_on)
for b in on["blocks"]:
    print("  %s %3dм skip=%-5s %s" % (time.strftime("%H:%M", time.localtime(b["start"])), b["seconds"] // 60,
                                      b["skip"], b["label"]))
labels = {b["label"]: b for b in on["blocks"]}
assert "Альфа - Дейлик" in labels and "Синхронизация по дизайну" in labels, "встречи внутри рабочего дня — засчитаны"
assert labels["Coffee Break☕️"]["skip"], "перерыв — личное"
assert "Вечерний созвон" not in labels, "после выключения трекера календарь не добавляет"
print("OK")
