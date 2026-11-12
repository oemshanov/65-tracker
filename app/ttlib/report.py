"""Выгрузка дня текстом: вставить в Claude, в чат менеджеру, в заметки — куда угодно."""
import time

from .segment import build_day, fmt_dur, propose


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def day_report(conn, cfg, date):
    day = build_day(conn, cfg, date)
    p = propose(conn, cfg, day)
    issues = {r["key"]: r["summary"] for r in conn.execute("SELECT key, summary FROM issues")}
    out = ["# Рабочий день %s" % date.strftime("%d.%m.%Y"), "",
           "Выгрузка из локального трекера времени (активные окна, созвоны, сессии Claude Code). "
           "Активно: %s, к логированию: %s, без задачи: %s." % (
               fmt_dur(sum(b["seconds"] for b in day["blocks"])), fmt_dur(p["to_log"]), fmt_dur(p["unassigned"])),
           "", "## Ворклоги", ""]
    if p["worklogs"]:
        for w in p["worklogs"]:
            out.append("- %s «%s» — %s: %s" % (w["issue"], (w.get("summary") or issues.get(w["issue"]) or "").strip(),
                                              fmt_dur(w["seconds"]), w["comment"]))
    else:
        out.append("- пока нет")
    out += ["", "## Хронология", ""]
    for b in day["blocks"]:
        what = b["issue"] or ("личное" if b["skip"] else "по задачам дня" if b.get("split") else "без задачи")
        if not b["issue"] and not b["skip"] and b.get("hint"):
            what += ", похоже на %s" % b["hint"]["issue"]
        details = []
        for c in b["contexts"][:4]:
            t = c["label"]
            if c.get("title") and c["title"] not in t:
                t += " («%s»)" % c["title"][:80]
            details.append("%s %s" % (t, fmt_dur(c["seconds"])))
        out.append("- %s–%s · %s · %s — %s" % (_hm(b["start"]), _hm(b["end"]), fmt_dur(b["seconds"]), what,
                                              "; ".join(details)))
    if day.get("events"):
        out += ["", "## Календарь", ""]
        out += ["- %s–%s %s" % (_hm(e["start"]), _hm(e["end"]), e["title"]) for e in day["events"]]
    return "\n".join(out) + "\n"
