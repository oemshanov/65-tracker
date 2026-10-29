"""Генерирует синтетический рабочий день в тестовую базу: TT_HOME=/tmp/x python3 tests/fake_day.py 2026-10-24"""
import datetime as dt
import os
import random
import sys
import time

# Тест стирает и пишет данные — только во временную базу, не в настоящую.
if not os.environ.get("TT_HOME") or os.path.realpath(os.environ["TT_HOME"]) == os.path.realpath(
        os.path.expanduser("~/Library/Application Support/TimeTracker")):
    sys.exit("Нужна временная база: TT_HOME=$(mktemp -d) python3 " + " ".join(sys.argv))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from ttlib import db  # noqa: E402

date = dt.date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-10-24")
conn = db.connect()
t = time.mktime((date.year, date.month, date.day, 9, 40, 0, 0, 0, -1))
CH = ("Google Chrome", "com.google.Chrome")
FIG = ("Figma", "com.figma.Desktop")
TG = ("Telegram", "ru.keepcoder.Telegram")
plan = [
    (25, "active", CH, "[PL-142] Редизайн карточки товара - Jira", "https://jira.example.com/browse/PL-142", None),
    (95, "active", FIG, "Polka — Карточка товара v3", None, None),
    (30, "call", FIG, "Polka — Карточка товара v3", None, "us.zoom.xos"),
    (20, "active", TG, "Андрей PM Polka", None, None),
    (50, "idle", (None, None), None, None, None),
    (70, "active", FIG, "Банк Север Онбординг", None, None),
    (15, "active", ("Spotify", "com.spotify.client"), "Spotify", None, None),
    (40, "active", CH, "Material 3 guidelines", "https://m3.material.io/components", None),
    (45, "active", ("Adobe Illustrator", "com.adobe.illustrator"), "bank_icons.ai @ 66% (RGB/Preview)", None, None),
    (25, "call", CH, "Meet - Дизайн-ревью", "https://meet.google.com/abc-defg-hij", "com.google.Chrome"),
]
conn.execute("DELETE FROM spans WHERE start >= ? AND start < ?", (t - 3600 * 6, t + 3600 * 18))
for minutes, state, (app, bundle), title, url, mic in plan:
    end = t + minutes * 60
    # дробим на куски с короткими переключениями, как в жизни
    while t < end:
        step = min(end - t, random.randint(60, 600))
        conn.execute("INSERT INTO spans(start,end,state,app,bundle,title,url,mic_app) VALUES(?,?,?,?,?,?,?,?)",
                     (t, t + step, state, app, bundle, title, url, mic))
        t += step
        if state == "active" and random.random() < 0.25 and t < end - 120:
            conn.execute("INSERT INTO spans(start,end,state,app,bundle,title,url,mic_app) VALUES(?,?,?,?,?,?,?,?)",
                         (t, t + 50, "active", TG[0], TG[1], "Рабочий чат", None, None))
            t += 50
conn.execute("INSERT OR REPLACE INTO calendar_events VALUES(?,?,?,?,?,?,?,?)",
             ("ev1", time.mktime((date.year, date.month, date.day, 17, 30, 0, 0, 0, -1)),
              time.mktime((date.year, date.month, date.day, 18, 0, 0, 0, 0, -1)), "Синк по Банк Северу", "Работа", None, 0, 4))
conn.executemany("INSERT OR REPLACE INTO issues(key, summary, project, project_name, status, updated) VALUES(?,?,?,?,?,?)", [
    ("PL-142", "Редизайн карточки товара", "OZ", "Polka Seller", "In Progress", time.time()),
    ("SB-77", "Онбординг: новые экраны и иконки", "SB", "Банк СеверБизнес", "In Progress", time.time()),
    ("INT-5", "Внутренние встречи и ревью", "INT", "Internal", "Open", time.time()),
])
conn.commit()
print("ok", date)
