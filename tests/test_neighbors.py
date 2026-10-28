"""Склейка одного проекта из разных мест и подсказки «по соседям» — без Claude."""
import os
import sys
import tempfile

os.environ.setdefault("TT_HOME", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from ttlib import db  # noqa: E402
from ttlib.segment import _between, _themes  # noqa: E402


def blk(start, minutes, key, label, kind, issue=None, hint=None):
    return {"start": start * 60, "end": (start + minutes) * 60, "seconds": minutes * 60, "issue": issue, "skip": False,
            "split": False, "suggestion": None, "hint": hint, "context_key": key, "label": label, "kind": kind}


conn = db.connect()
conn.executemany("INSERT OR REPLACE INTO issues(key, summary, project, status) VALUES(?,?,?,?)", [
    ("SHOP-2", "Редизайн оформления заказа", "SHOP", "В работе"),
    ("SHOP-3", "Менеджмент", "SHOP", "Backlog"),
    ("ZET-1", "Общие работы МП_Внутренние встречи", "ZET", "В работе"),
    ("ZET-2", "Общие работы МП_Внешние встречи", "ZET", "В работе"),
])
conn.commit()

lapy_hint = {"issue": "SHOP-2", "summary": "Редизайн оформления заказа", "reason": "похоже на «Редизайн оформления заказа»"}
blocks = [
    blk(0, 20, "claude:/users/x/documents/5 элемент", "Claude Code: 5 элемент — Редизайн", "claude", hint=lapy_hint),
    blk(20, 15, "meeting:телемост", "Телемост", "meeting"),
    blk(35, 10, "file:figma:5element/каталог", "Figma: 5element/Каталог", "file"),
    blk(45, 5, "web:5element.ru", "5element.ru", "web"),
    blk(50, 10, "app:com.anthropic.claudefordesktop", "Claude", "app"),
    blk(120, 10, "meeting:альфа - дейлик", "Альфа - Дейлик", "meeting", issue="ZET-1"),
    blk(130, 30, "file:figma:alpha", "Figma: alpha", "file", issue="ZET-5"),
]
_themes(blocks)
_between(conn, blocks)
by = {b["label"]: b for b in blocks}

# «Альфа» (Claude), «5element/…» (Figma) и 5element.ru — одна тема с общей подсказкой.
themes = {by[k]["theme"] for k in ("Claude Code: 5 элемент — Редизайн", "Figma: 5element/Каталог", "5element.ru")}
assert len(themes) == 1 and None not in themes, themes
assert by["5element.ru"]["hint"]["issue"] == "SHOP-2", by["5element.ru"]["hint"]
# Дейлик и макеты Альфы уже в разных задачах — не склеиваются.
assert by["Альфа - Дейлик"]["theme"] is None or by["Альфа - Дейлик"]["theme"] != by["Figma: alpha"]["theme"]
# Созвон посреди работы над Альфой → задача проекта про менеджмент, только для этого отрезка.
h = by["Телемост"]["hint"]
assert h and h["issue"] == "SHOP-3" and h["here"], h
# «Claude» без сессии сразу после Альфы → та же задача.
h = by["Claude"]["hint"]
assert h and h["issue"] == "SHOP-2" and h["here"], h
print("ok")
