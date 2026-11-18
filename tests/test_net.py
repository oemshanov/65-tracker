"""Выход в сеть при разных способах подключения. TT_HOME=<tmp> python3 tests/test_net.py"""
import os
import socket
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

# Тест стирает и пишет данные — только во временную базу, не в настоящую.
if not os.environ.get("TT_HOME") or os.path.realpath(os.environ["TT_HOME"]) == os.path.realpath(
        os.path.expanduser("~/Library/Application Support/TimeTracker")):
    sys.exit("Нужна временная база: TT_HOME=$(mktemp -d) python3 " + " ".join(sys.argv))
# Прокси из окружения разработчика не должен влиять на тест — он должен вести себя одинаково на любой машине.
for k in [k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
    del os.environ[k]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from ttlib import jira, net  # noqa: E402

net.RETRY_DELAY = 1.0
HITS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(self.path)
        code = 500 if self.path.endswith("/boom") else 200
        body = ("via-proxy " + self.path if self.path.startswith("http://") else "ok").encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port=0):
    s = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


srv = serve()
base = "http://127.0.0.1:%d" % srv.server_port

# 1. Напрямую (без прокси в настройках) — работает.
assert net.urlopen(base + "/x", {}).read() == b"ok"
# 2. Ответ сервера с ошибкой не повторяем: это не сбой сети.
HITS.clear()
try:
    net.urlopen(base + "/boom", {})
    raise AssertionError("ожидали HTTPError")
except urllib.error.HTTPError as e:
    assert e.code == 500 and len(HITS) == 1, HITS
# 3. Короткий сбой (VPN переподключается): первая попытка — отказ, сервис поднялся — повтор проходит.
port = free_port()
threading.Timer(0.3, lambda: serve(port)).start()
t = time.time()
assert net.urlopen("http://127.0.0.1:%d/late" % port, {}).read() == b"ok"
assert time.time() - t >= net.RETRY_DELAY * 0.9, "был повтор после паузы"
# 4. Прокси из настроек трекера: запрос идёт через него, даже к адресу, который напрямую не открывается.
cfg = {"network": {"proxy": base}}
assert net.urlopen("http://jira.example.invalid/rest/x", cfg).read() == b"via-proxy http://jira.example.invalid/rest/x"
# 5. Связи нет совсем — Jira говорит, что делать, а не код ошибки.
dead = "http://127.0.0.1:%d" % free_port()
j = jira.Jira({"jira": {"base_url": dead, "username": "u"}})
j._secret = "t"
try:
    j.req("GET", "/rest/api/2/myself")
    raise AssertionError("ожидали JiraError")
except jira.JiraError as e:
    assert "Настройки → Сеть" in str(e) and "Errno" not in str(e), e
# 6. Проверка адреса прокси в настройках.
assert net.valid_proxy("") and net.valid_proxy("http://127.0.0.1:8080") and net.valid_proxy("https://proxy.corp.example:3128")
assert not net.valid_proxy("127.0.0.1:8080") and not net.valid_proxy("socks5://127.0.0.1:1080")
print("OK")
