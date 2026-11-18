"""Подсказки по истории в Jira: на вымышленных днях. TT_HOME=<tmp> python3 tests/test_history.py"""
import datetime as dt
import os
import sys
import time

# Тест стирает и пишет данные — только во временную базу, не в настоящую.
if not os.environ.get("TT_HOME") or os.path.realpath(os.environ["TT_HOME"]) == os.path.realpath(
        os.path.expanduser("~/Library/Application Support/TimeTracker")):
    sys.exit("Нужна временная база: TT_HOME=$(mktemp -d) python3 " + " ".join(sys.argv))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from ttlib import config, db, history  # noqa: E402

conn = db.connect()
cfg = config.load()
today = dt.date(2026, 3, 20)
H = 3600


def day(n, logged, acts):
    d = (today - dt.timedelta(days=n)).isoformat()
    for issue, h in logged.items():
        conn.execute("INSERT OR REPLACE INTO jira_logged VALUES(?,?,?,?)", (d, issue, int(h * H), time.time()))
    conn.execute("INSERT OR REPLACE INTO jira_logged VALUES(?,?,?,?)", (d, "*", 0, time.time()))
    for key, label, h in acts:
        conn.execute("INSERT OR REPLACE INTO day_contexts VALUES(?,?,?,?,?)", (d, key, label, "file", int(h * H)))


# 12 дней назад: давняя задача OLD-1, к которой привязан «архивный» макет (потом в неё не логировали).
day(12, {"OLD-1": 8}, [("file:figma:архив", "Figma: Архив / Старые экраны", 6), ("app:chat", "Чат", 1)])
# Дальше 10 дней: в каждый — основная задача (6 ч) и второстепенная (2 ч), мессенджер каждый день,
# макеты и сессии — по своим задачам, примерно пропорционально залогированному.
pairs = [("OMG-1", "OMG-2"), ("OMG-3", "OMG-4"), ("OMG-2", "OMG-3"), ("OMG-4", "OMG-1"), ("OMG-1", "OMG-3"),
         ("OMG-2", "OMG-4"), ("OMG-3", "OMG-1"), ("OMG-4", "OMG-2"), ("OMG-1", "OMG-4"), ("OMG-2", "OMG-1")]
files = {"OMG-1": ("file:figma:омега/главная", "Figma: Омега / Главная"),
         "OMG-2": ("file:figma:омега/корзина", "Figma: Омега / Корзина"),
         "OMG-3": ("claude:/x/omega-api", "Claude Code: omega-api — Фильтры"),
         "OMG-4": ("web:docs/омега-спека", "Спецификация Омега — Документы")}
for i, (a, b) in enumerate(pairs):
    day(10 - i, {a: 6, b: 2}, [files[a] + (5.5,), files[b] + (1.5,), ("app:chat", "Чат", 1)])
conn.commit()

m = history.Model(conn, cfg, today)
print("дней:", m.days, "недавние:", sorted(m.recent))
# 1. Активность, которая приходится на одну задачу, — подсказка этой задачи.
for issue, (key, label) in files.items():
    p = m.predict(key, label)
    print(" ", label, "→", p)
    assert p and p[0] == issue, (label, p)
# 2. Новый файл того же раздела (точного совпадения нет) — узнаётся по словам.
p = m.predict("file:figma:омега/корзина v2", "Figma: Омега / Корзина v2")
assert p and p[0] == "OMG-2", p
# 3. То, что бывает каждый день при любых задачах, подсказки не даёт.
assert m.predict("app:chat", "Чат") is None, m.predict("app:chat", "Чат")
# 4. Давняя задача не подсказывается, даже если связь была сильной.
assert m.predict("file:figma:архив", "Figma: Архив / Старые экраны") is None
# 5. Будущее не подглядываем: модель на дату в прошлом видит только дни до неё.
early = history.Model(conn, cfg, today - dt.timedelta(days=9))
assert early.days == 2 and "OMG-3" not in early.recent, (early.days, early.recent)

# apply: правила не трогаем, подсказку «по словам» заменяет уверенная по истории, давнюю — убираем.
blocks = [
    {"id": "1", "issue": "RULE-1", "skip": False, "context_key": files["OMG-1"][0], "label": files["OMG-1"][1], "hint": None},
    {"id": "2", "issue": None, "skip": False, "context_key": files["OMG-2"][0], "label": files["OMG-2"][1],
     "hint": {"issue": "OMG-9", "reason": "похоже на «…»"}, "suggestion": {"issue": "OMG-8"}},
    {"id": "3", "issue": None, "skip": False, "context_key": "web:новое", "label": "Что-то новое",
     "hint": {"issue": "OLD-1", "reason": "похоже на «…»"}},
]
history.apply(conn, cfg, today, blocks, lambda k: "задача " + k)
assert blocks[0]["issue"] == "RULE-1" and blocks[0]["hint"] is None
assert blocks[1]["hint"]["issue"] == "OMG-2" and blocks[1]["hint"]["source"] == "history" and blocks[1]["suggestion"] is None
assert blocks[2]["hint"] is None, "подсказка в давнюю задачу убрана"
# Выключено в настройках — ничего не меняется.
b = [{"id": "4", "issue": None, "skip": False, "context_key": files["OMG-3"][0], "label": files["OMG-3"][1], "hint": None}]
history.apply(conn, dict(cfg, history={"enabled": False}), today, b, str)
assert b[0]["hint"] is None
print("OK")
