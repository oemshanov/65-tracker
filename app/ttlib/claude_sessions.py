"""Сессии Claude Code: к какой работе относится время, проведённое в Claude.

Читаем локальные журналы ~/.claude/projects/*/*.jsonl инкрементально (только новые байты)
и сохраняем лишь метаданные: время, сессию, папку проекта, ветку, название сессии.
Текст переписки не сохраняется и никуда не отправляется.
"""
import datetime as dt
import glob
import json
import os
import re
import time

from .context import Ctx, find_issue_key

ROOT = os.path.expanduser("~/.claude/projects")
# Приложения, где идёт работа с Claude Code.
HOSTS = {"com.anthropic.claudefordesktop", "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable",
         "com.mitchellh.ghostty", "net.kovidgoyal.kitty"}
BIG_LINE = 200_000          # длиннее — результаты инструментов/картинки, JSON не разбираем
ACTIVITY_STEP = 20          # не чаще одной отметки активности на сессию за 20 с
FOCUS_BACK = 30 * 60        # реплика пользователя «держит» фокус на сессии до 30 мин
FOCUS_AHEAD = 10 * 60       # длинное сообщение со скриншотами пишется и 10 минут — время до реплики её же
MAX_GAP = 5 * 60            # разрыв активности Claude, после которого «ожидание» заканчивается

SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_files(path TEXT PRIMARY KEY, offset INTEGER, size INTEGER);
CREATE TABLE IF NOT EXISTS claude_sessions(
  session TEXT PRIMARY KEY, cwd TEXT, branch TEXT, title TEXT, entrypoint TEXT, first_ts REAL, last_ts REAL);
CREATE TABLE IF NOT EXISTS claude_events(ts REAL NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS claude_events_ts ON claude_events(ts);
"""

_TAIL_TS = re.compile(rb'"timestamp":"([0-9T:\.\-]+Z)"')
_TAIL_SID = re.compile(rb'"sessionId":"([0-9a-f\-]{36})"')
_last_update = 0.0


def _ts(s):
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _is_prompt(d):
    """Реплика человека (а не результат инструмента, служебное сообщение или субагент)."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return False
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        return not content.startswith("<task-notification>")
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        return "tool_result" not in kinds and bool(kinds & {"text", "image"})
    return False


def update(conn, max_age_days=60, force=False):
    """Дочитывает новые строки журналов. Быстро, если ничего не поменялось."""
    global _last_update
    if not force and time.time() - _last_update < 30:
        return
    _last_update = time.time()
    if not os.path.isdir(ROOT):
        return
    conn.executescript(SCHEMA)
    known = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, offset, size FROM claude_files")}
    cutoff = time.time() - max_age_days * 86400
    sessions, last_act = {}, {}
    events = []
    for path in glob.glob(os.path.join(ROOT, "*", "*.jsonl")) + glob.glob(os.path.join(ROOT, "*", "*", "subagents", "*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        offset, size = known.get(path, (0, 0))
        if st.st_size == offset:
            continue
        if st.st_size < offset:
            offset = 0  # файл переписан
        if offset == 0 and st.st_mtime < cutoff:
            conn.execute("INSERT OR REPLACE INTO claude_files VALUES(?,?,?)", (path, st.st_size, st.st_size))
            continue
        sub = "/subagents/" in path
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        end = data.rfind(b"\n") + 1  # последнюю недописанную строку оставляем на потом
        for line in data[:end].splitlines():
            if not line:
                continue
            if len(line) > BIG_LINE:
                # Служебные поля идут после содержимого сообщения — берём их из хвоста строки.
                tail = line[-4000:]
                m_ts, m_sid = _TAIL_TS.search(tail), _TAIL_SID.search(tail)
                if m_ts and m_sid:
                    ts, sid = _ts(m_ts.group(1).decode()), m_sid.group(1).decode()
                    if ts and ts - last_act.get(sid, 0) >= ACTIVITY_STEP:
                        last_act[sid] = ts
                        events.append((ts, sid, "activity"))
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            sid = d.get("sessionId")
            if not sid:
                continue
            if d.get("type") == "custom-title" and not sub:
                sessions.setdefault(sid, {})["title"] = d.get("customTitle")
                continue
            if d.get("type") == "summary" and not sub:
                sessions.setdefault(sid, {}).setdefault("summary", d.get("summary"))
                continue
            ts = _ts(d.get("timestamp"))
            if ts is None:
                continue
            s = sessions.setdefault(sid, {})
            if d.get("cwd") and not sub:
                # Проект — папка, где сессию открыли (cwd потом меняется вслед за командами).
                s.setdefault("cwd", d["cwd"])
                s["branch"] = d.get("gitBranch")
                s["entrypoint"] = d.get("entrypoint")
            s["first_ts"] = min(s.get("first_ts", ts), ts)
            s["last_ts"] = max(s.get("last_ts", ts), ts)
            if not sub and _is_prompt(d):
                events.append((ts, sid, "prompt"))
            elif ts - last_act.get(sid, 0) >= ACTIVITY_STEP:
                last_act[sid] = ts
                events.append((ts, sid, "activity"))
        conn.execute("INSERT OR REPLACE INTO claude_files VALUES(?,?,?)", (path, offset + end, st.st_size))
    for sid, s in sessions.items():
        title = s.get("title") or s.get("summary")
        conn.execute(
            "INSERT INTO claude_sessions(session, cwd, branch, title, entrypoint, first_ts, last_ts) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(session) DO UPDATE SET cwd=COALESCE(cwd, excluded.cwd), "
            "branch=COALESCE(excluded.branch, branch), title=COALESCE(excluded.title, title), "
            "entrypoint=COALESCE(excluded.entrypoint, entrypoint), "
            "first_ts=MIN(COALESCE(first_ts, excluded.first_ts), COALESCE(excluded.first_ts, first_ts)), "
            "last_ts=MAX(COALESCE(last_ts, excluded.last_ts), COALESCE(excluded.last_ts, last_ts))",
            (sid, s.get("cwd"), s.get("branch"), title, s.get("entrypoint"), s.get("first_ts"), s.get("last_ts")))
    conn.executemany("INSERT INTO claude_events VALUES(?,?,?)", events)
    conn.commit()


def project_of(cwd):
    root = re.sub(r"/\.claude/worktrees/[^/]+.*$", "", cwd or "")
    home = os.path.expanduser("~")
    rel = root[len(home) + 1:] if root.startswith(home + "/") else root
    if "scratch-workspaces" in rel:
        return root, "черновая сессия"
    rel = re.sub(r"^Documents/", "", rel)
    return root, rel or "~"


class Timeline:
    """Реплики и активность Claude за интервал; отвечает «какая сессия была в фокусе в момент t»."""

    def __init__(self, conn, t0, t1):
        try:
            rows = list(conn.execute("SELECT ts, session, kind FROM claude_events WHERE ts >= ? AND ts < ? ORDER BY ts",
                                     (t0 - FOCUS_BACK, t1 + FOCUS_AHEAD)))
            self.sessions = {r["session"]: dict(r) for r in conn.execute("SELECT * FROM claude_sessions")}
        except Exception:  # таблиц ещё нет
            rows, self.sessions = [], {}
        self.prompts = [(r["ts"], r["session"]) for r in rows if r["kind"] == "prompt"]
        self.activity = [r["ts"] for r in rows]
        self._ctx = {}

    def session_at(self, t):
        prev = None
        for ts, sid in self.prompts:
            if ts <= t:
                prev = (ts, sid)
            else:
                if prev and t - prev[0] <= FOCUS_BACK:
                    return prev[1]
                return sid if ts - t <= FOCUS_AHEAD else None
        if prev and t - prev[0] <= FOCUS_BACK:
            return prev[1]
        return None

    def split(self, s, e):
        """Режет интервал [s, e) на куски по сменам сессии в фокусе."""
        cuts = [s] + [ts for ts, _ in self.prompts if s < ts < e] + [e]
        out = []
        for a, b in zip(cuts, cuts[1:]):
            sid = self.session_at(a + min(1.0, (b - a) / 2))
            if out and out[-1][2] == sid:
                out[-1] = (out[-1][0], b, sid)
            else:
                out.append((a, b, sid))
        return out

    def busy_until(self, s, limit):
        """До какого момента после s Claude непрерывно что-то делал (разрывы ≤ 5 мин)."""
        last = s
        for ts in self.activity:
            if ts <= s:
                continue
            if ts > limit or ts - last > MAX_GAP:
                break
            last = ts
        return min(limit, last + 30) if last > s else s

    def ctx(self, sid, project_keys=None):
        if sid in self._ctx:
            return self._ctx[sid]
        sess = self.sessions.get(sid) or {}
        root, project = project_of(sess.get("cwd"))
        title = (sess.get("title") or "").strip() or None
        branch = sess.get("branch")
        branch = branch if branch not in (None, "", "HEAD", "main", "master", "develop") else None
        # Ключ в ветке или папке (feature/ALP-12-…, «… PRJ-718») — надёжный признак, берём без фильтра проектов;
        # в свободном названии сессии — только ключи известных проектов.
        issue = find_issue_key(" ".join(filter(None, [branch, os.path.basename(root)]))) \
            or find_issue_key(title, project_keys)
        label = "Claude Code: %s" % project + (" — %s" % title if title else "")
        c = Ctx("claude:%s|%s" % (root.lower(), sid), label, "claude", "work", issue, "Claude", None,
                title or branch, None, group="claude:" + root.lower(), icon="com.anthropic.claudefordesktop")
        self._ctx[sid] = c
        return c
