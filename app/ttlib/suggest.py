"""Подсказки Claude для неразобранных блоков (через `claude -p`, без инструментов)."""
import json
import os
import re
import subprocess
import time
from collections import defaultdict

from .segment import build_day, fmt_dur

PROMPT = """Ты помогаешь дизайнеру разложить рабочее время по задачам Jira.
Ниже — фрагменты дня, которые не удалось отнести к задаче автоматически, список его задач в Jira
и примеры того, как он раньше сам относил похожую активность.

Для КАЖДОГО контекста реши:
- issue: ключ задачи из списка, если связь видна (название проекта/клиента/файла совпадает с задачей
  или проектом, встреча про этот проект, переписка по этому проекту). Не выдумывай ключи вне списка.
- category: "work" — рабочее, но задачу не определить; "personal" — явно личное (соцсети, развлечения,
  личные покупки); "meeting" — рабочая встреча без понятной задачи.
- confidence: 0..1, насколько ты уверен в issue (0.9+ только при явном совпадении названий).
- reason: коротко по-русски, почему.

Ответь ТОЛЬКО JSON без пояснений:
{"suggestions": [{"context": "<key>", "issue": "KEY-1" | null, "category": "work|personal|meeting",
  "confidence": 0.0, "reason": "..."}]}

ЗАДАЧИ ПОЛЬЗОВАТЕЛЯ:
%(issues)s

ПРИМЕРЫ ЕГО ПРИВЯЗОК (контекст → задача):
%(examples)s

НЕРАЗОБРАННЫЕ КОНТЕКСТЫ (%(date)s):
%(contexts)s
"""


def unassigned_contexts(day):
    agg = defaultdict(lambda: {"seconds": 0, "label": None, "kind": None, "titles": set(), "urls": set(),
                               "times": []})
    for b in day["blocks"]:
        if b["issue"] or b["skip"]:
            continue
        for c in b["contexts"]:
            if c["resolved_issue"] or c["category"] == "personal":
                continue
            a = agg[c.get("group") or c["key"]]
            a["seconds"] += c["seconds"]
            a["label"], a["kind"] = c["label"], c["kind"]
            if c["title"]:
                a["titles"].add(c["title"][:120])
            if c["url"]:
                a["urls"].add(re.sub(r"\?.*", "", c["url"])[:120])
            a["times"].append(time.strftime("%H:%M", time.localtime(b["start"])))
    return {k: v for k, v in agg.items() if v["seconds"] >= 120}


COMMENT_PROMPT = """Ты пишешь комментарии к ворклогам Jira за %(date)s для дизайнера.
Комментарий читает менеджер проекта: он должен коротко (до 150 символов, по-русски) описывать, ЧТО сделано по задаче,
в терминах самой задачи. Никаких названий программ (Figma, Claude, Telegram, Chrome, Видеоповтор и т.п.),
сайтов-инструментов, слов «блок», «активность», «трекер». Не выдумывай результатов, которых не видно из данных;
если данных мало — опиши работу через формулировку задачи.

Ответь ТОЛЬКО JSON: {"comments": {"KEY-1": "текст", ...}}

ВОРКЛОГИ:
%(items)s
"""


def comments(conn, cfg, date):
    """Claude пишет комментарии к черновикам, опираясь на саму задачу в Jira. Правленные вами — не трогает."""
    from . import jira
    from .segment import propose
    day = build_day(conn, cfg, date)
    p = propose(conn, cfg, day)
    edited = {r["issue"] for r in conn.execute("SELECT issue FROM drafts WHERE date=? AND comment IS NOT NULL",
                                                (date.isoformat(),))}
    # Записи, вынесенные из задачи отдельно, Claude не переписывает: у них свой комментарий по своим активностям.
    todo = [w for w in p["worklogs"] if w["status"] == "draft" and w["issue"] not in edited and not w["part"]]
    if not todo:
        return 0
    j = jira.Jira(cfg) if jira.Jira(cfg).configured else None
    items = []
    for w in todo:
        det = {}
        if j:
            try:
                det = j.details(w["issue"])
            except jira.JiraError:
                det = {}
        acts = defaultdict(int)
        for b in day["blocks"]:
            if b["issue"] != w["issue"] or (b.get("context_key") or b["id"]) not in {s["key"] for s in w["sources"]}:
                continue
            for c in b["contexts"]:
                if c.get("resolved_issue") in (None, w["issue"]):
                    acts[(c["label"] or "")[:90]] += c["seconds"]
        items.append(json.dumps({
            "key": w["issue"], "task": det.get("summary") or w["summary"], "project": det.get("project"),
            "parent": det.get("parent"), "description": (det.get("description") or "")[:600],
            "time": fmt_dur(w["seconds"]),
            "what_was_open": [k for k, _ in sorted(acts.items(), key=lambda kv: -kv[1])[:8]],
        }, ensure_ascii=False))
    out = _ask_claude(cfg, COMMENT_PROMPT % {"date": date.isoformat(), "items": "\n".join(items)})
    n = 0
    for key, text in (out.get("comments") or {}).items():
        if key in {w["issue"] for w in todo} and text:
            conn.execute("INSERT INTO drafts(date, issue, comment) VALUES(?,?,?) ON CONFLICT(date, issue, part) "
                         "DO UPDATE SET comment=excluded.comment", (date.isoformat(), key, text.strip()[:250]))
            n += 1
    conn.commit()
    return n


def _ask_claude(cfg, prompt):
    llm = cfg["llm"]
    cmd = [os.path.expanduser(llm["command"]), "-p", "--tools", "", "--no-session-persistence",
           "--output-format", "text"]
    if llm.get("model"):
        cmd += ["--model", llm["model"]]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=llm.get("timeout_sec", 180))
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if "authenticate" in err.lower() or "login" in err.lower():
            raise RuntimeError("Claude CLI не авторизован: запустите `claude` в терминале и выполните /login")
        raise RuntimeError("claude -p завершился с ошибкой: %s" % err[:400])
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise RuntimeError("Claude вернул не JSON: %s" % r.stdout[:300])
    return json.loads(m.group(0))


def run(conn, cfg, date, quiet=False):
    day = build_day(conn, cfg, date)
    ctxs = unassigned_contexts(day)
    if not ctxs:
        return {"asked": 0, "saved": 0}
    issues = list(conn.execute("SELECT key, summary, project_name FROM issues ORDER BY updated DESC LIMIT 200"))
    examples = list(conn.execute(
        "SELECT context_key, label, issue, category FROM mappings ORDER BY updated DESC LIMIT 60"))
    issues_txt = "\n".join("%s — %s [%s]" % (r["key"], r["summary"], r["project_name"] or "") for r in issues) \
        or "(список задач пуст — выполните tt issues --sync; ставь issue только если ключ виден явно)"
    ex_txt = "\n".join("%s → %s" % (r["label"] or r["context_key"], r["issue"] or r["category"]) for r in examples) \
        or "(пока нет)"
    lines = []
    for k, v in sorted(ctxs.items(), key=lambda kv: -kv[1]["seconds"]):
        lines.append(json.dumps({
            "key": k, "label": v["label"], "kind": v["kind"], "time": fmt_dur(v["seconds"]),
            "when": sorted(set(v["times"]))[:6], "titles": sorted(v["titles"])[:5], "urls": sorted(v["urls"])[:3],
        }, ensure_ascii=False))
    prompt = PROMPT % {"issues": issues_txt, "examples": ex_txt, "contexts": "\n".join(lines),
                       "date": date.isoformat()}
    llm = cfg["llm"]
    cmd = [os.path.expanduser(llm["command"]), "-p", "--tools", "", "--no-session-persistence",
           "--output-format", "text"]
    if llm.get("model"):
        cmd += ["--model", llm["model"]]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=llm.get("timeout_sec", 180))
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if "authenticate" in err.lower() or "login" in err.lower():
            raise RuntimeError("Claude CLI не авторизован: запустите `claude` в терминале и выполните /login")
        raise RuntimeError("claude -p завершился с ошибкой: %s" % err[:400])
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise RuntimeError("Claude вернул не JSON: %s" % r.stdout[:300])
    data = json.loads(m.group(0))
    valid = {row["key"] for row in issues}
    saved = 0
    now = time.time()
    for s in data.get("suggestions", []):
        key = s.get("context")
        if key not in ctxs:
            continue
        issue = s.get("issue")
        if issue and valid and issue not in valid:
            issue = None
        cat = s.get("category") or "work"
        conn.execute("INSERT OR REPLACE INTO suggestions VALUES(?,?,?,?,?,?)",
                     (key, issue, cat if cat == "personal" else None,
                      float(s.get("confidence") or 0), s.get("reason"), now))
        saved += 1
    conn.commit()
    return {"asked": len(ctxs), "saved": saved}
