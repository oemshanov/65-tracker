"""Время в Claude: переключение сессий и ожидание, пока Claude работает. TT_HOME=<tmp> python3 tests/test_claude.py"""
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from ttlib import claude_sessions, config, db  # noqa: E402
from ttlib.segment import build_day  # noqa: E402

claude_sessions.update = lambda conn, **kw: conn.executescript(claude_sessions.SCHEMA)  # без реальных журналов
conn = db.connect()
conn.executescript(claude_sessions.SCHEMA)
date = dt.date(2026, 10, 22)
T = lambda h, m: time.mktime((date.year, date.month, date.day, h, m, 0, 0, 0, -1))  # noqa: E731
for t in ("spans", "claude_events", "claude_sessions"):
    conn.execute("DELETE FROM %s WHERE %s" % (t, "1" if t == "claude_sessions" else
                                            ("start >= %d AND start < %d" % (T(0, 0), T(23, 0)) if t == "spans"
                                             else "ts >= %d AND ts < %d" % (T(0, 0), T(23, 0)))))
CL = ("Claude", "com.anthropic.claudefordesktop")
spans = [(T(10, 0), T(10, 20), "active", CL), (T(10, 20), T(11, 10), "idle", (None, None)),
         (T(11, 10), T(11, 40), "active", ("Figma", "com.figma.Desktop"))]
for s, e, st, (app, b) in spans:
    conn.execute("INSERT INTO spans(start,end,state,app,bundle,title) VALUES(?,?,?,?,?,?)",
                 (s, e, st, app, b, "Alpha / Экран" if b == "com.figma.Desktop" else None))
conn.executemany("INSERT INTO claude_sessions VALUES(?,?,?,?,?,?,?)", [
    ("a" * 36, os.path.expanduser("~/Documents/Alpha Claude/alpha-proto/.claude/worktrees/x-1"), "feature/ALP-12-filters",
     "Фильтры операций", "claude-desktop", T(9, 0), T(11, 0)),
    ("b" * 36, os.path.expanduser("~/Documents/MTS"), "HEAD", "Лендинг тарифов", "claude-desktop", T(9, 0), T(11, 0)),
])
ev = [(T(10, 0), "a" * 36, "prompt"), (T(10, 12), "b" * 36, "prompt")]
ev += [(T(10, 12) + i * 30, "b" * 36, "activity") for i in range(0, 46)]   # Claude работает до ~10:35
conn.executemany("INSERT INTO claude_events VALUES(?,?,?)", ev)
conn.commit()
day = build_day(conn, config.load(), date)
for b in day["blocks"]:
    print(time.strftime("%H:%M", time.localtime(b["start"])), time.strftime("%H:%M", time.localtime(b["end"])),
          "%3dм" % (b["seconds"] // 60), b["issue"], b["context_key"], "|", b["label"])
blk = day["blocks"]
assert blk[0]["issue"] == "ALP-12", "ключ из ветки"
assert blk[0]["context_key"].endswith("alpha-proto"), "worktree → проект"
assert "MTS" in blk[1]["label"] and 20 <= blk[1]["seconds"] // 60 <= 30, "ожидание засчитано до конца активности"
print("OK")
