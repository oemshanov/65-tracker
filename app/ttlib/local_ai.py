"""Локальный подбор задач маленькой моделью (Ollama + bge-m3) — только по кнопке, без Claude и токенов.

Модель переводит название активности и названия задач Jira в векторы и ищет ближайшие по смыслу:
«checkout-redesign» ≈ «Редизайн оформления заказа», «созвон по ЛК» ≈ «Менеджмент». Ollama запускается на время
подбора и сразу закрывается, модель выгружается из памяти — в фоне ничего не работает.
Векторы задач кешируются в базе, поэтому повторный подбор занимает секунды.
"""
import array
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

from . import config
from .hints import meeting_issue, pretty
from .segment import build_day
from .suggest import unassigned_contexts

CLOSED = ("завершена", "закрыт", "готово", "done", "closed", "cancelled")
LOCAL = "локально: "  # префикс reason — отличает подсказки модели от подсказок Claude


class LocalAIError(RuntimeError):
    pass


def _cfg(cfg):
    return dict({"url": "http://127.0.0.1:11434", "model": "bge-m3", "min_score": 0.55, "margin": 0.02},
                **(cfg["llm"].get("local") or {}))


# Нейросеть не входит в приложение: по кнопке в настройках скачиваем Ollama (если её нет) в папку трекера
# и модель (~1,2 ГБ). «Удалить» убирает модель и скачанную нами Ollama; Ollama из Homebrew не трогаем.
AI_DIR = os.path.join(config.SUPPORT_DIR, "ollama")
OLLAMA_URL = "https://github.com/ollama/ollama/releases/latest/download/ollama-darwin.tgz"
_job = {"stage": None}  # binary → model → done | error; pct — проценты текущего шага


def ollama_bin():
    for p in (shutil.which("ollama"), "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
              "/Applications/Ollama.app/Contents/Resources/ollama", os.path.join(AI_DIR, "ollama")):
        if p and os.path.exists(p):
            return p
    return None


def _call(url, path, body=None, timeout=120):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class Ollama:
    """Поднимает `ollama serve`, если он не запущен, и гасит его после работы."""

    def __init__(self, cfg, need_model=True):
        self.c = _cfg(cfg)
        self.url, self.model, self.proc, self.need_model = self.c["url"], self.c["model"], None, need_model

    def __enter__(self):
        try:
            _call(self.url, "/api/version", timeout=2)
        except Exception:
            exe = ollama_bin()
            if not exe:
                raise LocalAIError("Нейросеть не установлена — скачайте её в настройках")
            self.proc = subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         env=dict(os.environ, OLLAMA_KEEP_ALIVE="0"))
            for _ in range(60):
                time.sleep(0.25)
                try:
                    _call(self.url, "/api/version", timeout=1)
                    break
                except Exception:
                    pass
            else:
                self.__exit__()
                raise LocalAIError("Ollama не запустилась")
        if not self.need_model:
            return self
        names = {m["name"].split(":")[0] for m in _call(self.url, "/api/tags").get("models", [])}
        if self.model.split(":")[0] not in names:
            self.__exit__()
            raise LocalAIError("Нейросеть не установлена — скачайте её в настройках")
        return self

    def embed(self, texts):
        out = []
        for i in range(0, len(texts), 64):
            r = _call(self.url, "/api/embed", {"model": self.model, "input": texts[i:i + 64], "keep_alive": "1m"},
                      timeout=300)
            out += r["embeddings"]
        return out

    def __exit__(self, *exc):
        try:  # выгрузить модель из памяти, даже если Ollama запускали не мы
            _call(self.url, "/api/embed", {"model": self.model, "input": [], "keep_alive": 0}, timeout=10)
        except Exception:
            pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


def has_model(cfg):
    """Модель скачана — смотрим манифест на диске (`ollama list` требует запущенного сервера)."""
    name = _cfg(cfg)["model"].split(":")[0]
    return os.path.isdir(os.path.expanduser("~/.ollama/models/manifests/registry.ollama.ai/library/" + name))


def status(cfg):
    ready = bool(ollama_bin()) and has_model(cfg)
    job = dict(_job) if _job["stage"] not in (None, "done") else None
    return {"ready": ready, "model_name": _cfg(cfg)["model"], "job": job,
            "ours": os.path.exists(os.path.join(AI_DIR, "ollama"))}


def install_async(cfg):
    if _job["stage"] in ("binary", "model"):
        return
    _job.update(stage="binary", pct=0, error=None)
    threading.Thread(target=_install, args=(cfg,), daemon=True).start()


def _install(cfg):
    try:
        if not ollama_bin():
            os.makedirs(AI_DIR, exist_ok=True)
            tgz = os.path.join(AI_DIR, "ollama.tgz")
            with urllib.request.urlopen(OLLAMA_URL, timeout=60) as r, open(tgz, "wb") as f:
                total, done = int(r.headers.get("Content-Length") or 0), 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    _job["pct"] = int(done * 100 / total) if total else 0
            subprocess.run(["tar", "-xzf", tgz, "-C", AI_DIR], check=True)
            os.remove(tgz)
            exe = os.path.join(AI_DIR, "ollama")
            if not os.path.exists(exe):
                raise LocalAIError("В архиве Ollama нет программы ollama")
            os.chmod(exe, 0o755)
        _job.update(stage="model", pct=0)
        with Ollama(cfg, need_model=False) as ai:
            req = urllib.request.Request(ai.url + "/api/pull", data=json.dumps({"model": ai.model}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3600) as r:
                for line in r:
                    ev = json.loads(line or b"{}")
                    if ev.get("error"):
                        raise LocalAIError(ev["error"])
                    if ev.get("total"):
                        _job["pct"] = int(ev.get("completed", 0) * 100 / ev["total"])
        _job.update(stage="done", pct=100)
    except Exception as e:  # покажем в настройках, кнопка «Скачать» станет «Повторить»
        _job.update(stage="error", error=str(e))


def remove(cfg):
    """Удалить модель и скачанную нами Ollama. Ollama из Homebrew или Ollama.app остаются."""
    if has_model(cfg) and ollama_bin():
        with Ollama(cfg, need_model=False) as ai:
            req = urllib.request.Request(ai.url + "/api/delete", data=json.dumps({"model": ai.model}).encode(),
                                         headers={"Content-Type": "application/json"}, method="DELETE")
            try:
                urllib.request.urlopen(req, timeout=60).read()
            except Exception:
                pass
    if os.path.isdir(AI_DIR):
        shutil.rmtree(AI_DIR, ignore_errors=True)
    _job.update(stage=None)


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def vectors(conn, ai, texts):
    """Векторы с кешем в базе (ключ — модель + текст)."""
    conn.execute("CREATE TABLE IF NOT EXISTS embeddings(hash TEXT PRIMARY KEY, vec BLOB)")
    keys = [hashlib.sha1((ai.model + "\n" + t).encode()).hexdigest() for t in texts]
    have = {}
    for i in range(0, len(keys), 500):
        part = keys[i:i + 500]
        for r in conn.execute("SELECT hash, vec FROM embeddings WHERE hash IN (%s)" % ",".join("?" * len(part)), part):
            have[r["hash"]] = list(array.array("f", r["vec"]))
    todo = sorted({t for t, k in zip(texts, keys) if k not in have})
    if todo:
        for t, v in zip(todo, ai.embed(todo)):
            k = hashlib.sha1((ai.model + "\n" + t).encode()).hexdigest()
            have[k] = _norm(v)
            conn.execute("INSERT OR REPLACE INTO embeddings VALUES(?, ?)", (k, array.array("f", have[k]).tobytes()))
        conn.commit()
    return [have[k] for k in keys]


NOISE = re.compile(r"\s*[—–-]\s*(Поиск в Google|Google Search|Яндекс|Google Chrome|Safari|Arc|Яндекс Браузер|Figma"
                   r"|YouTube|Gmail|Google Документы|Google Таблицы)\s*$|^(Claude Code|Figma|Sketch|Miro|Notion|Google Docs?):\s*",
                   re.I)
GENERIC = re.compile(r"^web:(mail\.|e\.mail|calendar\.|outlook\.|drive\.google)|:untitled$|:без названия$", re.I)
SITE = re.compile(r"^(www\.)?[a-z0-9.-]+\.[a-z]{2,6}$", re.I)


UNTITLED = ("untitled", "без названия", "новая вкладка", "new tab")


def _clean(t):
    t = (t or "").strip()
    for _ in range(3):
        t = NOISE.sub("", t).strip()
    return t


def context_text(v):
    """Что модель читает об активности: название + заголовки окон, без программ и поисковиков."""
    label = _clean(v["label"])
    parts = [] if (SITE.match(label) and re.search(r"google|yandex|bing|duckduckgo", label)) \
        or label.lower() in UNTITLED else [label]
    for t in sorted(v["titles"])[:3]:
        t = _clean(t)
        if t and t not in parts and t.lower() not in UNTITLED:
            parts.append(t)
    return " · ".join(p for p in parts if p)[:300]


def run(conn, cfg, date, dry_run=False):
    c = _cfg(cfg)
    day = build_day(conn, cfg, date)
    # «Claude», «Видеоповтор», почта, «Untitled» сами по себе ни о чём — их ведут соседи.
    mapped = {r[0] for r in conn.execute("SELECT context_key FROM mappings")}  # в т.ч. «личное»
    ctxs = {k: v for k, v in unassigned_contexts(day).items()
            if v["kind"] != "app" and k not in mapped and not GENERIC.search(k) and len(context_text(v)) >= 4}
    if not ctxs:
        return {"asked": 0, "saved": 0}
    issues = [r for r in conn.execute("SELECT key, summary, project_name, status, mine FROM issues "
                                      "WHERE summary IS NOT NULL") if (r["status"] or "").lower() not in CLOSED]
    history = [{"label": pretty(r["context_key"], r["label"]), "issue": r["issue"]}
               for r in conn.execute("SELECT context_key, label, issue FROM mappings WHERE issue IS NOT NULL")
               if not r["context_key"].startswith(("app:", "meeting:"))]  # «Телемост» ни о чём не говорит
    if not issues and not history:
        raise LocalAIError("Нет задач для сравнения — нажмите «↻ Задачи Jira»")
    summary = {r["key"]: r["summary"].strip() for r in issues}
    ckeys = list(ctxs)
    with Ollama(cfg) as ai:
        cv = vectors(conn, ai, [context_text(ctxs[k]) for k in ckeys])
        iv = vectors(conn, ai, ["%s · %s" % (r["summary"].strip(), r["project_name"] or "") for r in issues])
        hv = vectors(conn, ai, [context_text({"label": r["label"], "titles": ()}) or r["label"] for r in history])
    old = {r["context_key"]: r for r in conn.execute("SELECT * FROM suggestions")}
    saved, now = 0, time.time()
    for k, v in zip(ckeys, cv):
        cands = {}  # issue → (score, why)
        for r, w in zip(issues, iv):
            s = _dot(v, w) + 0.03 * (r["mine"] != 0) + 0.01 * ((r["status"] or "").lower() == "в работе")
            if s > cands.get(r["key"], (0,))[0]:
                cands[r["key"]] = (s, "похоже на «%s»" % summary[r["key"]][:60])
        for r, w in zip(history, hv):
            s = _dot(v, w) + 0.05  # то, что вы сами уже привязывали, весомее
            if s > cands.get(r["issue"], (0,))[0]:
                cands[r["issue"]] = (s, "как «%s»" % r["label"][:60])
        ranked = sorted(cands.items(), key=lambda kv: -kv[1][0])
        if dry_run:
            print("%-50s" % context_text(ctxs[k])[:50], " | ".join("%s %.3f %s" % (i, s, w[:35]) for i, (s, w) in ranked[:3]))
            continue
        if not ranked:
            continue
        (issue, (score, why)) = ranked[0]
        proj = issue.split("-")[0]
        # Отрыв считаем от лучшей задачи ДРУГОГО проекта: SHOP-1 и SHOP-2 почти равны — проект всё равно ясен.
        second = next((sc for i, (sc, _) in ranked[1:] if i.split("-")[0] != proj), 0)
        if score < c["min_score"] or score - second < c["margin"]:
            continue
        if ctxs[k]["kind"] == "meeting":
            # «Дейли альфы» модель с «Внутренними встречами» не связывает — берём от неё только проект.
            mi = meeting_issue(conn, proj)
            if mi and mi["issue"] != issue:
                issue, why = mi["issue"], "встреча по проекту «%s»" % (summary.get(issue) or proj)[:50]
        prev = old.get(k)
        if prev and not (prev["reason"] or "").startswith(LOCAL):
            continue  # подсказку Claude не перетираем
        # Уверенность ниже порога автопринятия: модель только подсказывает, решаете вы.
        conf = min(0.7, round(score, 2))
        conn.execute("INSERT OR REPLACE INTO suggestions VALUES(?,?,?,?,?,?)",
                     (k, issue, None, conf, LOCAL + why, now))
        saved += 1
    conn.commit()
    return {"asked": len(ckeys), "saved": saved}
