"""Jira Server/DC + Tempo. Токен лежит в Связке ключей macOS (service «tt-jira»)."""
import base64
import datetime as dt
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

KEYCHAIN_SERVICE = "tt-jira"


class JiraError(Exception):
    pass


def keychain_get(account):
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def keychain_store_interactive(account):
    """`security` сам спрашивает секрет в терминале — в Python он не проходит."""
    print("Вставьте токен (ввод скрыт), затем повторите:")
    return subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                           "-a", account, "-w"]).returncode == 0


def keychain_store(account, secret):
    """Пишет секрет в Связку ключей через `security -i` (stdin), чтобы он не светился в списке процессов."""
    q = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
    cmd = "add-generic-password -U -s %s -a %s -w %s\n" % (q(KEYCHAIN_SERVICE), q(account), q(secret))
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    return r.returncode == 0 and keychain_get(account) == secret


class Jira:
    def __init__(self, cfg):
        j = cfg["jira"]
        self.base = j["base_url"].rstrip("/")
        self.user = j.get("username") or ""
        self.auth = j.get("auth", "pat")
        self.mode = j.get("mode", "jira")
        self.tempo_attributes = j.get("tempo_attributes") or {}
        self._secret = None
        self._me = None

    @property
    def configured(self):
        return bool(self.user) and keychain_get(self.user) is not None

    def _headers(self):
        if self._secret is None:
            self._secret = keychain_get(self.user) if self.user else None
            if not self._secret:
                raise JiraError("Jira не настроена: выполните `tt setup`")
        h = {"Accept": "application/json", "Content-Type": "application/json", "X-Atlassian-Token": "no-check"}
        if self.auth == "basic":
            h["Authorization"] = "Basic " + base64.b64encode(
                ("%s:%s" % (self.user, self._secret)).encode()).decode()
        else:
            h["Authorization"] = "Bearer " + self._secret
        return h

    def req(self, method, path, body=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        rq = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(rq, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:600]
            try:
                j = json.loads(msg)
                msg = "; ".join((j.get("errorMessages") or []) + ["%s: %s" % kv for kv in
                                                                    (j.get("errors") or {}).items()]) or msg
            except ValueError:
                pass
            raise JiraError("HTTP %s %s: %s" % (e.code, path, msg))
        except urllib.error.URLError as e:
            raise JiraError("Нет связи с %s (%s). VPN?" % (self.base, e.reason))

    def myself(self):
        if self._me is None:
            self._me = self.req("GET", "/rest/api/2/myself")
        return self._me

    def search(self, jql, fields="summary,project,status", limit=300):
        out, start = [], 0
        while True:
            r = self.req("GET", "/rest/api/2/search",
                         params={"jql": jql, "fields": fields, "startAt": start, "maxResults": min(100, limit)})
            out += r.get("issues", [])
            start += len(r.get("issues", []))
            if start >= r.get("total", 0) or start >= limit or not r.get("issues"):
                return out

    def details(self, key):
        it = self.req("GET", "/rest/api/2/issue/" + key,
                      params={"fields": "summary,description,status,project,issuetype,parent"})
        f = it["fields"]
        return {"key": it["key"], "summary": f.get("summary"), "description": (f.get("description") or "").strip(),
                "status": (f.get("status") or {}).get("name"), "project": (f.get("project") or {}).get("name"),
                "type": (f.get("issuetype") or {}).get("name"),
                "parent": ((f.get("parent") or {}).get("fields") or {}).get("summary")}

    def issue(self, key):
        return self.req("GET", "/rest/api/2/issue/" + key, params={"fields": "summary,project,status"})

    @staticmethod
    def started(ts):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000%z", time.localtime(ts))

    def add_worklog(self, key, started_ts, seconds, comment):
        if self.mode == "tempo":
            me = self.myself()
            body = {"worker": me.get("key") or me.get("name"), "originTaskId": key,
                    "started": time.strftime("%Y-%m-%d %H:%M:%S.000", time.localtime(started_ts)),
                    "timeSpentSeconds": int(seconds), "billableSeconds": int(seconds), "comment": comment or "",
                    "attributes": self.tempo_attributes}
            r = self.req("POST", "/rest/tempo-timesheets/4/worklogs/", body)
            r = r[0] if isinstance(r, list) else r
            return "tempo:%s" % (r.get("tempoWorklogId") or r.get("id"))
        r = self.req("POST", "/rest/api/2/issue/%s/worklog" % key,
                     {"started": self.started(started_ts), "timeSpentSeconds": int(seconds), "comment": comment or ""},
                     params={"adjustEstimate": "auto"})
        return str(r["id"])

    def update_worklog(self, key, wid, started_ts, seconds, comment):
        if wid.startswith("tempo:"):
            me = self.myself()
            self.req("PUT", "/rest/tempo-timesheets/4/worklogs/%s/" % wid[6:], {
                "worker": me.get("key") or me.get("name"), "originTaskId": key,
                "started": time.strftime("%Y-%m-%d %H:%M:%S.000", time.localtime(started_ts)),
                "timeSpentSeconds": int(seconds), "billableSeconds": int(seconds), "comment": comment or "",
                "attributes": self.tempo_attributes})
            return wid
        self.req("PUT", "/rest/api/2/issue/%s/worklog/%s" % (key, wid),
                 {"started": self.started(started_ts), "timeSpentSeconds": int(seconds), "comment": comment or ""},
                 params={"adjustEstimate": "auto"})
        return wid

    def logged_on(self, date):
        """Сколько вы уже залогировали в Jira за дату: {issue: seconds}."""
        me = self.myself()
        ids = {me.get("name"), me.get("key")}
        d = date.isoformat()
        keys = [i["key"] for i in self.search('worklogAuthor = currentUser() AND worklogDate = "%s"' % d,
                                              fields="summary", limit=200)]
        out = {}
        for k in keys:
            wl = self.req("GET", "/rest/api/2/issue/%s/worklog" % k)
            for w in wl.get("worklogs", []):
                a = w.get("author") or {}
                if a.get("name") not in ids and a.get("key") not in ids:
                    continue
                if (w.get("started") or "")[:10] != d:
                    continue
                out[k] = out.get(k, 0) + int(w.get("timeSpentSeconds") or 0)
        return out

    def create_issue(self, project, summary, issuetype="Task", description=None):
        body = {"fields": {"project": {"key": project}, "summary": summary, "issuetype": {"name": issuetype}}}
        if description:
            body["fields"]["description"] = description
        me = self.myself()
        body["fields"]["assignee"] = {"name": me.get("name")}
        try:
            r = self.req("POST", "/rest/api/2/issue", body)
        except JiraError:
            body["fields"].pop("assignee")
            r = self.req("POST", "/rest/api/2/issue", body)
        return r["key"]


def sync_issues(conn, cfg, jira=None):
    jira = jira or Jira(cfg)
    found = jira.search(cfg["jira"]["issue_jql"])
    # Плюс открытые задачи тех же проектов, даже не назначенные на вас: «Менеджмент», «Внутренние встречи»
    # нужны, чтобы подсказать задачу для созвона между работой над проектом.
    projects = {(it["fields"].get("project") or {}).get("key") for it in found}
    projects |= {r[0].split("-")[0] for r in conn.execute(
        "SELECT issue FROM mappings WHERE issue IS NOT NULL UNION "
        "SELECT issue FROM drafts WHERE date >= date('now', '-60 days')")}
    mine = {it["key"] for it in found}
    seen = set(mine)
    for p in sorted(filter(None, projects)):
        try:
            more = jira.search('project = "%s" AND statusCategory != Done ORDER BY updated DESC' % p, limit=150)
        except JiraError:
            continue  # нет доступа к проекту — не страшно
        found += [it for it in more if it["key"] not in seen]
        seen |= {it["key"] for it in more}
    now = time.time()
    for it in found:
        f = it["fields"]
        conn.execute(
            "INSERT OR REPLACE INTO issues(key, summary, project, project_name, status, updated, mine) "
            "VALUES(?,?,?,?,?,?,?)",
            (it["key"], f.get("summary"), (f.get("project") or {}).get("key"),
             (f.get("project") or {}).get("name"), (f.get("status") or {}).get("name"), now, it["key"] in mine))
    conn.commit()
    return len(found)


def ensure_issue(conn, jira, key):
    row = conn.execute("SELECT * FROM issues WHERE key=?", (key,)).fetchone()
    if row:
        return dict(row)
    it = jira.issue(key)
    f = it["fields"]
    conn.execute(
        "INSERT OR REPLACE INTO issues(key, summary, project, project_name, status, updated) VALUES(?,?,?,?,?,?)",
        (it["key"], f.get("summary"), (f.get("project") or {}).get("key"), (f.get("project") or {}).get("name"),
         (f.get("status") or {}).get("name"), time.time()))
    conn.commit()
    return {"key": it["key"], "summary": f.get("summary")}


def refresh_logged(conn, cfg, date, jira=None):
    jira = jira or Jira(cfg)
    logged = jira.logged_on(date)
    conn.execute("DELETE FROM jira_logged WHERE date=?", (date.isoformat(),))
    for k, s in logged.items():
        conn.execute("INSERT INTO jira_logged VALUES(?,?,?,?)", (date.isoformat(), k, s, time.time()))
    # Отметка «проверено», чтобы и пустой день кэшировался.
    conn.execute("INSERT OR REPLACE INTO jira_logged VALUES(?,?,?,?)", (date.isoformat(), "*", 0, time.time()))
    conn.commit()
    return logged


def push_day(conn, cfg, date, dry_run=False, only=None):
    """Отправляет черновики. Возвращает список результатов по задачам."""
    jira = Jira(cfg)
    results = []
    try:
        refresh_logged(conn, cfg, date, jira)
    except JiraError as e:
        if not dry_run:
            raise
        results.append({"issue": "*", "action": "warn", "error": str(e)})
    from .segment import build_day, propose  # здесь, чтобы не было циклического импорта
    proposal = propose(conn, cfg, build_day(conn, cfg, date))
    d0 = dt.datetime.combine(date, dt.time(hour=10)).timestamp()
    for w in proposal["worklogs"]:
        if only and w["issue"] not in only:
            continue
        if w["status"] in ("pushed", "in_jira") or not w["seconds"]:
            results.append({"issue": w["issue"], "action": "skip", "status": w["status"]})
            continue
        started = w["start"] or d0
        action = "update" if w["status"] == "changed" else "create"
        if dry_run:
            results.append({"issue": w["issue"], "action": action, "seconds": w["seconds"], "dry_run": True})
            continue
        try:
            if action == "update":
                wid = jira.update_worklog(w["issue"], w["jira_id"], started, w["seconds"], w["comment"])
            else:
                wid = jira.add_worklog(w["issue"], started, w["seconds"], w["comment"])
            conn.execute(
                "INSERT INTO drafts(date, issue, jira_id, pushed_seconds, pushed_comment, pushed_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(date, issue) DO UPDATE SET jira_id=excluded.jira_id, "
                "pushed_seconds=excluded.pushed_seconds, pushed_comment=excluded.pushed_comment, "
                "pushed_at=excluded.pushed_at",
                (date.isoformat(), w["issue"], wid, w["seconds"], w["comment"], time.time()))
            # Фиксируем текущие значения, чтобы авто-пересчёт не менял уже отправленное.
            conn.execute("UPDATE drafts SET seconds=?, comment=? WHERE date=? AND issue=?",
                         (w["seconds"], w["comment"], date.isoformat(), w["issue"]))
            conn.commit()
            results.append({"issue": w["issue"], "action": action, "seconds": w["seconds"], "id": wid})
        except JiraError as e:
            results.append({"issue": w["issue"], "action": "error", "error": str(e)})
    if not dry_run and not any(r["action"] == "error" for r in results):
        conn.execute("INSERT OR REPLACE INTO days(date, status, updated) VALUES(?,?,?)",
                     (date.isoformat(), "pushed", time.time()))
        conn.commit()
    return results
