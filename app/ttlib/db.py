"""SQLite: спаны пишет сборщик (Swift), остальное — этот модуль."""
import os
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans(
  id INTEGER PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL, state TEXT NOT NULL,
  app TEXT, bundle TEXT, title TEXT, url TEXT, mic_app TEXT);
CREATE INDEX IF NOT EXISTS spans_start ON spans(start);
CREATE TABLE IF NOT EXISTS calendar_events(
  uid TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL, title TEXT, calendar TEXT,
  location TEXT, all_day INTEGER, attendees INTEGER, PRIMARY KEY(uid, start));
-- Выученные привязки «контекст → задача» (из таймлайна, Claude, CLI).
CREATE TABLE IF NOT EXISTS mappings(
  context_key TEXT PRIMARY KEY, issue TEXT, category TEXT, label TEXT, updated REAL);
-- Ручная привязка конкретного интервала времени.
CREATE TABLE IF NOT EXISTS overrides(
  id INTEGER PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL, issue TEXT, category TEXT,
  created REAL);
CREATE TABLE IF NOT EXISTS suggestions(
  context_key TEXT PRIMARY KEY, issue TEXT, category TEXT, confidence REAL, reason TEXT,
  created REAL);
CREATE TABLE IF NOT EXISTS issues(
  key TEXT PRIMARY KEY, summary TEXT, project TEXT, project_name TEXT, status TEXT,
  updated REAL);
-- Ворклог дня. Обычно один на задачу; part — отдельная запись в той же задаче («Альфа» отдельно от остального).
CREATE TABLE IF NOT EXISTS drafts(
  date TEXT NOT NULL, issue TEXT NOT NULL, seconds INTEGER, comment TEXT,
  jira_id TEXT, pushed_seconds INTEGER, pushed_comment TEXT, pushed_at REAL, part TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(date, issue, part));
-- Какие активности дня вынесены в отдельную запись задачи (part — ключ темы, из которой она выросла).
CREATE TABLE IF NOT EXISTS parts(
  date TEXT NOT NULL, issue TEXT NOT NULL, context_key TEXT NOT NULL, part TEXT NOT NULL,
  PRIMARY KEY(date, issue, context_key));
CREATE TABLE IF NOT EXISTS days(date TEXT PRIMARY KEY, status TEXT, note TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS jira_logged(
  date TEXT NOT NULL, issue TEXT NOT NULL, seconds INTEGER, fetched REAL,
  PRIMARY KEY(date, issue));
"""


def connect():
    os.makedirs(config.SUPPORT_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    try:  # mine=0 — задача проекта, подтянутая не по вашему фильтру (для подсказок «встреч» проекта)
        conn.execute("ALTER TABLE issues ADD COLUMN mine INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    if "part" not in {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}:
        # До 1.4 ворклог был один на задачу в день: ключ таблицы меняется, поэтому пересоздаём её.
        conn.executescript("""BEGIN;
            CREATE TABLE drafts_new(
              date TEXT NOT NULL, issue TEXT NOT NULL, seconds INTEGER, comment TEXT,
              jira_id TEXT, pushed_seconds INTEGER, pushed_comment TEXT, pushed_at REAL, part TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(date, issue, part));
            INSERT INTO drafts_new(date, issue, seconds, comment, jira_id, pushed_seconds, pushed_comment, pushed_at)
              SELECT date, issue, seconds, comment, jira_id, pushed_seconds, pushed_comment, pushed_at FROM drafts;
            DROP TABLE drafts;
            ALTER TABLE drafts_new RENAME TO drafts;
            COMMIT;""")
    return conn


def set_part(conn, date, issue, context_keys, part):
    """Вынести активности в отдельную запись задачи (part) или вернуть в общую (part пустой)."""
    for k in context_keys:
        if part:
            conn.execute("INSERT OR REPLACE INTO parts VALUES(?,?,?,?)", (date, issue, k, part))
        else:
            conn.execute("DELETE FROM parts WHERE date=? AND issue=? AND context_key=?", (date, issue, k))
    conn.commit()


def set_mapping(conn, context_key, issue=None, category=None, label=None):
    conn.execute(
        "INSERT INTO mappings(context_key, issue, category, label, updated) VALUES(?,?,?,?,?) "
        "ON CONFLICT(context_key) DO UPDATE SET issue=excluded.issue, category=excluded.category, "
        "label=COALESCE(excluded.label, mappings.label), updated=excluded.updated",
        (context_key, issue, category, label, time.time()),
    )
    conn.commit()


def delete_mapping(conn, context_key):
    conn.execute("DELETE FROM mappings WHERE context_key=?", (context_key,))
    conn.commit()


def add_override(conn, start, end, issue=None, category=None):
    # Новая привязка интервала заменяет пересекающиеся старые.
    conn.execute("DELETE FROM overrides WHERE start < ? AND end > ?", (end, start))
    if issue or category:
        conn.execute(
            "INSERT INTO overrides(start, end, issue, category, created) VALUES(?,?,?,?,?)",
            (start, end, issue, category, time.time()),
        )
    conn.commit()


def purge(conn, older_than_days):
    cutoff = time.time() - older_than_days * 86400
    conn.execute("DELETE FROM spans WHERE end < ?", (cutoff,))
    conn.execute("DELETE FROM calendar_events WHERE end < ?", (cutoff,))
    conn.execute("DELETE FROM overrides WHERE end < ?", (cutoff,))
    conn.commit()
