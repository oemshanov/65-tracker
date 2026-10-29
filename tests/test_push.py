"""Прогон отправки ворклогов против мок-Jira. TT_HOME=<tmp> python3 tests/test_push.py"""
import datetime as dt
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Тест стирает и пишет данные — только во временную базу, не в настоящую.
if not os.environ.get("TT_HOME") or os.path.realpath(os.environ["TT_HOME"]) == os.path.realpath(
        os.path.expanduser("~/Library/Application Support/TimeTracker")):
    sys.exit("Нужна временная база: TT_HOME=$(mktemp -d) python3 " + " ".join(sys.argv))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from ttlib import config, db, jira  # noqa: E402
from ttlib.segment import build_day, propose  # noqa: E402

CALLS, WORKLOGS = [], {}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        CALLS.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path.startswith("/rest/api/2/myself"):
            return self._send({"name": "j.doe", "key": "JIRAUSER1", "displayName": "Test"})
        if self.path.startswith("/rest/api/2/search"):
            keys = sorted({k for k, _ in WORKLOGS})
            return self._send({"total": len(keys), "issues": [{"key": k, "fields": {}} for k in keys]})
        if "/worklog" in self.path:
            key = self.path.split("/issue/")[1].split("/")[0]
            wl = [dict(v, id=i) for (k, i), v in WORKLOGS.items() if k == key]
            return self._send({"worklogs": wl})
        self._send({}, 404)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_POST(self):
        body = self._body()
        CALLS.append(("POST", self.path, body))
        key = self.path.split("/issue/")[1].split("/")[0]
        wid = str(1000 + len(WORKLOGS))
        WORKLOGS[(key, wid)] = {"author": {"name": "j.doe"}, "started": body["started"],
                                "timeSpentSeconds": body["timeSpentSeconds"]}
        self._send({"id": wid}, 201)

    def do_PUT(self):
        body = self._body()
        CALLS.append(("PUT", self.path, body))
        key, wid = self.path.split("/issue/")[1].split("?")[0].split("/worklog/")
        WORKLOGS[(key, wid)].update(timeSpentSeconds=body["timeSpentSeconds"])
        self._send({"id": wid})


srv = HTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
jira.keychain_get = lambda account: "test-token"

cfg = config.load()
cfg["jira"].update(base_url="http://127.0.0.1:%d" % srv.server_port, username="j.doe")
conn = db.connect()
date = dt.date(2026, 10, 24)
conn.execute("DELETE FROM drafts")
conn.execute("DELETE FROM days")
conn.execute("DELETE FROM jira_logged")
conn.commit()

def show(title):
    p = propose(conn, cfg, build_day(conn, cfg, date))
    print(title, [(w["issue"], w["seconds"] // 60, w["status"]) for w in p["worklogs"]])

# Привязки задаёт сам тест: макеты и иконки «Банк Север» → SB-77, созвон в Meet → INT-5 (PL-142 узнаётся по ключу).
# Meet без события в календаре правилом не запоминается — задача ставится на само время созвона.
for b in build_day(conn, cfg, date)["blocks"]:
    for c in b["contexts"]:
        if "Банк Север" in c["label"] or "bank_icons" in c["label"]:
            db.set_mapping(conn, c["key"], "SB-77", None, c["label"])
        elif "Meet" in c["label"]:
            db.set_mapping(conn, c["key"], "INT-5", None, c["label"])  # старое правило — должно игнорироваться
            db.add_override(conn, b["start"], b["end"], "INT-5")

show("до:")
r = jira.push_day(conn, cfg, date)
print("push1:", [(x["issue"], x["action"]) for x in r])
post = [c for c in CALLS if c[0] == "POST"][0]
print("пример запроса:", post[1], post[2])
assert any(c[2] == "Bearer test-token" for c in CALLS if c[0] == "GET")
show("после:")
r = jira.push_day(conn, cfg, date)
print("push2 (повтор, дублей быть не должно):", [(x["issue"], x["action"]) for x in r])
assert not any(x["action"] == "create" for x in r)
conn.execute("UPDATE drafts SET seconds=? WHERE date=? AND issue=?", (3 * 3600, date.isoformat(), "SB-77"))
conn.commit()
show("правка SB-77 → 3ч:")
r = jira.push_day(conn, cfg, date)
print("push3:", [(x["issue"], x["action"]) for x in r])
assert [x["action"] for x in r if x["issue"] == "SB-77"] == ["update"]
# Ручной ворклог, внесённый мимо tt, должен распознаться как «уже в Jira».
conn.execute("DELETE FROM drafts WHERE issue='INT-5'")
conn.commit()
WORKLOGS[("INT-5", "9999")] = {"author": {"name": "j.doe"}, "started": "2026-10-24T11:40:00.000+0300",
                               "timeSpentSeconds": 1800}
jira.refresh_logged(conn, cfg, date)
show("INT-5 внесён руками:")
# Отдельная запись в той же задаче: иконки «Банк Север» уходят в SB-77 своим ворклогом со своим комментарием.
# Делят до отправки, поэтому начинаем день заново.
conn.execute("DELETE FROM drafts")
conn.execute("DELETE FROM jira_logged")
conn.commit()
WORKLOGS.clear()
day = build_day(conn, cfg, date)
icons = sorted({c.get("group") or c["key"] for b in day["blocks"] if b["issue"] == "SB-77" for c in b["contexts"]
                if "bank_icons" in c["label"]})
db.set_part(conn, date.isoformat(), "SB-77", icons, "icons")
show("иконки SB-77 отдельно:")
r = jira.push_day(conn, cfg, date)
print("push4:", [(x["issue"], x["action"]) for x in r])
assert [x["action"] for x in r if x["issue"] == "SB-77"] == ["create", "create"]
assert len([k for k, _ in WORKLOGS if k == "SB-77"]) == 2
r = jira.push_day(conn, cfg, date)
assert not any(x["action"] == "create" for x in r), r
print("OK: все проверки прошли")
