"""Замер точности подсказок на вашей истории (`tt eval`).

Считается на временной копии базы: ваша база не меняется, ничего никуда не отправляется. За каждый день трекер
угадывает задачи так, будто знает только то, что было до этого дня (без ваших ручных правок этого дня и без правил,
выученных позже), и сравнивает с тем, что реально залогировано в Jira за этот день. Нужна сверка с Jira за эти дни.
"""
import collections
import datetime as dt
import os
import shutil
import tempfile

from . import db, history
from .segment import build_day, day_bounds


def _prediction(b):
    """Что трекер предложил бы для блока: задача и откуда она."""
    if b.get("skip") or b.get("split"):
        return None, None
    if b.get("issue"):
        return b["issue"], "правило"
    if (b.get("suggestion") or {}).get("issue"):
        return b["suggestion"]["issue"], "нейросеть"
    h = b.get("hint") or {}
    if h.get("issue"):
        return h["issue"], "история" if h.get("source") == "history" else ("соседи" if h.get("here") else "слова")
    return None, None


def _score(pairs):
    """pairs: [(truth {issue: sec}, blocks)] → метрики."""
    tot = agree = rec = prec = bh = bok = 0.0
    src = collections.defaultdict(lambda: [0.0, 0.0])
    for tr, blocks in pairs:
        pred = collections.defaultdict(float)
        for b in blocks:
            issue, s = _prediction(b)
            if not issue:
                continue
            pred[issue] += b["seconds"]
            bh += b["seconds"]
            bok += b["seconds"] * (issue in tr)
            src[s][0] += b["seconds"]
            src[s][1] += b["seconds"] * (issue in tr)
        t = sum(tr.values())
        tot += t
        agree += sum(min(pred.get(k, 0), v) for k, v in tr.items())
        bt = {k for k, v in tr.items() if v >= 900}
        bp = {k for k, v in pred.items() if v >= 900}
        rec += len(bt & bp) / len(bt) if bt else 1
        prec += len(bt & bp) / len(bp) if bp else 1
    n = len(pairs) or 1
    return {"days": len(pairs), "jira_h": tot / 3600, "agree": agree / tot if tot else 0, "recall": rec / n,
            "precision": prec / n, "block_precision": bok / bh if bh else 0, "with_issue_h": bh / 3600,
            "sources": {k: {"h": h / 3600, "right": ok / h if h else 0} for k, (h, ok) in src.items()}}


def run(cfg, days=30, today=None, ai=False):
    today = today or dt.date.today()
    tmp = tempfile.mkdtemp(prefix="tt-eval-")
    path = os.path.join(tmp, "tt.db")
    try:
        src = db.connect()
        dst = db.connect(path)
        src.backup(dst)
        src.close()
        conn = dst
        lo = (today - dt.timedelta(days=days)).isoformat()
        truth = collections.defaultdict(dict)
        for r in conn.execute("SELECT date, issue, seconds FROM jira_logged WHERE date >= ? AND date < ? "
                              "AND issue != '*' AND seconds > 0", (lo, today.isoformat())):
            truth[r["date"]][r["issue"]] = r["seconds"]
        # Активности прошлых дней (для подсказок по истории) — от ручных правок не зависят, считаем заранее.
        history.fill_cache(conn, cfg, today, days + history.HORIZON_DAYS)
        res = {"base": [], "history": []}
        if ai:
            res["ai"] = []
        for day in sorted(truth, reverse=True):  # от поздних к ранним: удалённое «будущее» не возвращается
            date = dt.date.fromisoformat(day)
            t0, _ = day_bounds(date, cfg["day"]["start_hour"])
            conn.execute("DELETE FROM overrides WHERE start >= ?", (t0,))
            conn.execute("DELETE FROM mappings WHERE updated >= ?", (t0,))
            conn.execute("DELETE FROM suggestions WHERE created >= ?", (t0,))
            conn.execute("DELETE FROM event_marks WHERE start >= ?", (t0,))
            conn.execute("DELETE FROM parts WHERE date >= ?", (day,))
            conn.execute("DELETE FROM drafts WHERE date >= ?", (day,))
            conn.commit()
            on = dict(cfg, history={"enabled": True})
            base = build_day(conn, cfg, date, learn=False)["blocks"]
            if sum(b["seconds"] for b in base) < 3600:
                continue  # трекер в этот день почти не работал — сравнивать не с чем
            res["base"].append((truth[day], base))
            res["history"].append((truth[day], build_day(conn, on, date)["blocks"]))
            if ai:  # «Уточнить подсказки» локальной нейросетью — как если бы нажали кнопку в этот день
                from . import local_ai
                local_ai.run(conn, on, date)
                res["ai"].append((truth[day], build_day(conn, on, date)["blocks"]))
                conn.execute("DELETE FROM suggestions WHERE created >= ?", (t0,))
                conn.commit()
        conn.close()
        return {k: _score(v) for k, v in res.items()}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def report(r):
    if not r["base"]["days"]:
        return "Нет дней со сверкой с Jira — сначала откройте неделю в трекере (или «Сверить с Jira»)."
    lines = ["Дней со сверкой: %d, в Jira %.0f ч" % (r["base"]["days"], r["base"]["jira_h"]), "",
             "%-22s %8s %8s %10s %12s" % ("", "совпало", "найдено", "точн.задач", "точн.блоков")]
    for k, name in (("base", "без истории"), ("history", "с историей Jira"), ("ai", "+ нейросеть")):
        if k not in r:
            continue
        s = r[k]
        lines.append("%-22s %7.0f%% %7.0f%% %9.0f%% %11.0f%%" % (name, 100 * s["agree"], 100 * s["recall"],
                                                                100 * s["precision"], 100 * s["block_precision"]))
    lines += ["", "Откуда подсказки (с историей): часов · верно"]
    for k, v in sorted(r["history"]["sources"].items(), key=lambda kv: -kv[1]["h"]):
        lines.append("  %-10s %5.1f ч · %3.0f%%" % (k, v["h"], 100 * v["right"]))
    lines += ["", "совпало — доля часов Jira, которые трекер отнёс к той же задаче; найдено — задачи от 15 мин;",
              "точн.задач — предложенные задачи, которые оказались в Jira; точн.блоков — то же по времени."]
    return "\n".join(lines)
