"""День → блоки времени → черновики ворклогов.

Сутки режутся на слоты (по умолчанию 5 мин). Внутри слота побеждает задача, на которую
пришлось больше всего активных секунд, и забирает всё время слота: короткие переключения
(глянуть Telegram на минуту) не дробят день. Время блока — реальные активные секунды,
а не «слоты × 5 мин», поэтому ничего не накручивается.
"""
import datetime as dt
import math
import re
import sys
import time
from collections import defaultdict

from . import claude_sessions
from .context import Resolver, calendar_ctx, context_of
from .hints import Hinter, meeting_issue, theme_tokens

SKIP_CATEGORIES = {"personal"}
# «По задачам дня»: время блока делится между задачами дня пропорционально (переписка, общие созвоны).
SPLIT = "split"


def parse_date(s):
    s = (s or "today").strip().lower()
    today = dt.date.today()
    if s in ("today", "сегодня"):
        return today
    if s in ("yesterday", "вчера"):
        return today - dt.timedelta(days=1)
    if s.startswith("-") and s[1:].isdigit():
        return today - dt.timedelta(days=int(s[1:]))
    return dt.date.fromisoformat(s)


def day_bounds(date, start_hour):
    t0 = time.mktime((date.year, date.month, date.day, start_hour, 0, 0, 0, 0, -1))
    nxt = date + dt.timedelta(days=1)
    t1 = time.mktime((nxt.year, nxt.month, nxt.day, start_hour, 0, 0, 0, 0, -1))
    return t0, t1


def date_of_ts(ts, start_hour):
    return (dt.datetime.fromtimestamp(ts) - dt.timedelta(hours=start_hour)).date()


def assignment_key(res, ctx):
    if res["issue"]:
        return "issue:" + res["issue"]
    if res["category"] in SKIP_CATEGORIES:
        return "skip:" + ctx.group
    if res["category"] == SPLIT:
        return "split:" + ctx.group
    # Неразобранное группируем по уровню «запомнить» (файл Figma, проект Claude) — одним блоком.
    return "ctx:" + ctx.group


def build_day(conn, cfg, date):
    start_hour = cfg["day"]["start_hour"]
    t0, t1 = day_bounds(date, start_hour)
    slot = cfg["segment"]["slot_min"] * 60
    nslots = int(math.ceil((t1 - t0) / slot))
    project_keys = set(cfg["jira"].get("project_keys") or []) or {
        r[0] for r in conn.execute("SELECT DISTINCT project FROM issues WHERE project IS NOT NULL")
    } or None
    personal = set(cfg.get("personal_bundles") or [])
    resolver = Resolver(conn, cfg)

    events = [dict(r) for r in conn.execute(
        "SELECT * FROM calendar_events WHERE start < ? AND end > ? AND all_day = 0 ORDER BY start", (t1, t0))]

    def event_at(ts):
        # Самое конкретное событие: короткий «Дейлик» важнее многочасового блока «alpha» вокруг него.
        hits = [e for e in events if e["start"] <= ts < e["end"] and e["end"] - e["start"] <= 4 * 3600]
        return min(hits, key=lambda e: e["end"] - e["start"]) if hits else None

    # Созвон целиком (микрофон занят, разрывы ≤ 2 мин) называем по событию календаря, с которым он пересёкся:
    # подключились к «Дейлику» в 10:25, а в календаре он с 10:30 — первые минуты тоже дейлик.
    calls = []  # [start, end, event]
    for r in conn.execute("SELECT start, end, mic_app FROM spans WHERE state='call' AND start < ? AND end > ? "
                          "ORDER BY start", (t1, t0)):
        if calls and r["start"] - calls[-1][1] <= 120:
            calls[-1][1] = max(calls[-1][1], r["end"])
        else:
            calls.append([r["start"], r["end"], None, (r["mic_app"] or "").split(".helper")[0] or None])
    for c in calls:
        best = 0  # noqa: c = [начало, конец, событие, программа звонка]
        for ev in events:
            if ev["end"] - ev["start"] > 4 * 3600:
                continue
            ov = min(c[1], ev["end"]) - max(c[0], ev["start"])
            # пересеклись, либо созвон начался в пределах 10 мин от начала события
            if ov > best or (ov <= 0 and not best and abs(c[0] - ev["start"]) <= 600 and c[2] is None):
                best, c[2] = max(ov, 0), ev

    # Были на встрече (звонок пересёкся с событием календаря) — засчитываем её целиком по календарю,
    # даже если микрофон выключали или параллельно печатали в других окнах: дейли 30 мин — это 30 мин.
    attended = []  # (начало, конец, контекст встречи)
    for a, b, ev, mic in calls:
        if ev:
            mctx = calendar_ctx(ev, project_keys)
            mctx.icon = mic
            attended.append((min(a, ev["start"]), max(b, ev["end"]), mctx))

    def call_event(ts):
        for a, b, ev, _mic in calls:
            if a <= ts <= b:
                return ev or event_at(ts)
        return event_at(ts)

    overrides = [dict(r) for r in conn.execute(
        "SELECT * FROM overrides WHERE start < ? AND end > ?", (t1, t0))]

    slot_asg = [defaultdict(float) for _ in range(nslots)]   # assignment key -> seconds
    slot_ctx = [defaultdict(float) for _ in range(nslots)]   # ctx key -> seconds
    slot_span = [[None, None] for _ in range(nslots)]        # first/last active ts
    ctxs, asgs = {}, {}
    state_secs = defaultdict(float)
    tracked_any = False

    def add(s, e, ctx):
        res = resolver.resolve(ctx)
        ak = assignment_key(res, ctx)
        ctxs.setdefault(ctx.key, ctx)
        asgs.setdefault(ak, res)
        i = int((s - t0) // slot)
        while s < e and i < nslots:
            seg_end = min(e, t0 + (i + 1) * slot)
            d = seg_end - s
            slot_asg[i][ak] += d
            slot_ctx[i][ctx.key] += d
            a, b = slot_span[i]
            slot_span[i] = [s if a is None else min(a, s), seg_end if b is None else max(b, seg_end)]
            s = seg_end
            i += 1

    ccfg = cfg.get("claude") or {}
    claude_tl = None
    if ccfg.get("sessions", True):
        try:
            claude_sessions.update(conn)
        except Exception as e:  # журналы Claude — бонус, день должен строиться и без них
            print("claude sessions: %s" % e, file=sys.stderr)
        claude_tl = claude_sessions.Timeline(conn, t0, t1)
    prev_active = None  # последний активный спан: (bundle, end)
    rec_first = rec_last = None  # когда трекер в этот день вообще работал

    def handle(sp, s, e, prev_active):
        if sp["state"] == "idle" and claude_tl and ccfg.get("count_waiting", True) and prev_active \
                and prev_active[0] in claude_sessions.HOSTS and s - prev_active[1] < 60:
            # Ждали, пока Claude работает: засчитываем, пока в журналах идёт активность.
            sid = claude_tl.session_at(s)
            if sid:
                until = claude_tl.busy_until(s, min(e, s + ccfg.get("waiting_cap_min", 30) * 60))
                if until - s >= 60:
                    add(s, until, claude_tl.ctx(sid, project_keys))
            return prev_active
        if sp["state"] not in ("active", "call"):
            return prev_active
        prev_active = (sp["bundle"], e)
        if sp["state"] == "active" and claude_tl and sp["bundle"] in claude_sessions.HOSTS:
            for a, b, sid in claude_tl.split(s, e):
                if sid:
                    add(a, b, claude_tl.ctx(sid, project_keys))
                else:
                    add(a, b, context_of(sp, project_keys, None, personal))
            return prev_active
        ev = call_event((s + e) / 2) if sp["state"] == "call" else None
        add(s, e, context_of(sp, project_keys, ev["title"] if ev else None, personal))
        return prev_active

    for sp in conn.execute("SELECT * FROM spans WHERE start < ? AND end > ? ORDER BY start", (t1, t0)):
        s, e = max(sp["start"], t0), min(sp["end"], t1)
        if e <= s:
            continue
        tracked_any = True
        rec_first = s if rec_first is None else min(rec_first, s)
        rec_last = e if rec_last is None else max(rec_last, e)
        state_secs[sp["state"]] += e - s
        if sp["state"] in ("active", "call", "idle"):
            rest = [(s, e)]
            for wa, wb, mctx in attended:
                nxt = []
                for x, y in rest:
                    ia, ib = max(x, wa), min(y, wb)
                    if ia < ib:
                        add(ia, ib, mctx)
                        nxt += [(p0, p1) for p0, p1 in ((x, ia), (ib, y)) if p1 > p0]
                    else:
                        nxt.append((x, y))
                rest = nxt
            for x, y in rest:
                prev_active = handle(sp, x, y, prev_active)
        else:
            prev_active = handle(sp, s, e, prev_active)

    min_active = cfg["segment"]["min_active_sec"]
    winners = [None] * nslots
    for i in range(nslots):
        total = sum(slot_asg[i].values())
        if total >= min_active:
            winners[i] = max(slot_asg[i].items(), key=lambda kv: kv[1])[0]

    # Встречи из календаря, когда ноутбук простаивал (офлайн-встреча).
    # Только внутри времени, когда трекер работал: дни до установки и выключенный трекер календарём не заполняем.
    if cfg["segment"].get("calendar_fills_idle") and events and rec_first is not None:
        for i in range(nslots):
            if winners[i] is not None:
                continue
            mid = t0 + i * slot + slot / 2
            if not rec_first <= mid <= rec_last:
                continue
            ev = event_at(mid)
            if not ev or (ev["end"] - ev["start"]) > 4 * 3600:
                continue
            ctx = calendar_ctx(ev, project_keys)
            res = resolver.resolve(ctx)
            ak = assignment_key(res, ctx)
            ctxs.setdefault(ctx.key, ctx)
            asgs.setdefault(ak, res)
            a = max(t0 + i * slot, ev["start"])
            b = min(t0 + (i + 1) * slot, ev["end"])
            slot_asg[i][ak] += b - a
            slot_ctx[i][ctx.key] += b - a
            slot_span[i] = [a, b]
            winners[i] = ak

    # Ручные привязки интервалов перекрывают всё.
    for i in range(nslots):
        if winners[i] is None:
            continue
        mid = t0 + i * slot + slot / 2
        for ov in overrides:
            if ov["start"] <= mid < ov["end"]:
                if ov["issue"]:
                    ak = "issue:" + ov["issue"]
                    asgs.setdefault(ak, {"issue": ov["issue"], "category": "work", "source": "manual",
                                         "confidence": 1.0, "suggestion": None})
                else:
                    cat = ov["category"] or "personal"
                    ak = ("split:" if cat == SPLIT else "skip:") + "override"
                    asgs.setdefault(ak, {"issue": None, "category": cat,
                                         "source": "manual", "confidence": 1.0, "suggestion": None})
                winners[i] = "ovr|" + ak
                break

    blocks = []
    cur = None
    for i in range(nslots):
        w = winners[i]
        if w is None:
            # Разрыв до 2 слотов внутри одной задачи не рвёт блок.
            if cur is not None and cur["_gap"] < 2:
                cur["_gap"] += 1
                continue
            if cur is not None:
                blocks.append(cur)
                cur = None
            continue
        manual = w.startswith("ovr|")
        ak = w[4:] if manual else w
        if cur is not None and cur["_ak"] == ak and cur["_manual"] == manual:
            cur["_gap"] = 0
        else:
            if cur is not None:
                blocks.append(cur)
            cur = {"_ak": ak, "_manual": manual, "_gap": 0, "start": slot_span[i][0], "end": slot_span[i][1],
                   "seconds": 0.0, "_ctx": defaultdict(float)}
        cur["end"] = max(cur["end"], slot_span[i][1])
        cur["seconds"] += sum(slot_asg[i].values())
        for ck, v in slot_ctx[i].items():
            cur["_ctx"][ck] += v
    if cur is not None:
        blocks.append(cur)

    out = []
    hinter = None
    for b in blocks:
        ak = b["_ak"]
        res = asgs[ak]
        top = sorted(b["_ctx"].items(), key=lambda kv: -kv[1])
        contexts = []
        for ck, secs in top[:6]:
            c = ctxs[ck]
            cres = resolver.resolve(c)
            contexts.append({"key": ck, "group": c.group, "label": c.label, "kind": c.kind, "category": c.category,
                             "seconds": int(secs), "issue": c.issue, "title": c.title, "url": c.url, "icon": c.icon,
                             "resolved_issue": cres["issue"], "source": cres["source"],
                             "suggestion": cres["suggestion"]})
        main = ctxs[top[0][0]] if top else None
        kind = "meeting" if main and main.kind == "meeting" else (main.kind if main else "app")
        source = res["source"]
        if main and not b["_manual"]:
            # Источник показываем по главному контексту блока, а не по первому за день.
            mres = resolver.resolve(main)
            if mres["issue"] == res["issue"] and (res["issue"] or mres["category"] == res["category"]):
                source = mres["source"]
        out.append({
            "id": "%d" % int(b["start"]),
            "start": b["start"], "end": b["end"], "seconds": int(b["seconds"]),
            "issue": res["issue"],
            "category": res["category"],
            "skip": ak.startswith("skip:"),
            "split": ak.startswith("split:"),
            "source": "manual" if b["_manual"] else source,
            "confidence": res["confidence"],
            "suggestion": res["suggestion"] if not res["issue"] else None,
            "context_key": main.group if main else None,
            "hint": None,
            "label": main.label if main else "?",
            "icon": main.icon if main else None,
            "kind": kind,
            "contexts": contexts,
        })
    # Подсказки из истории/названий задач — только для неразобранного (без Claude, без токенов).
    for b in out:
        if b["issue"] or b["skip"] or b.get("split") or (b["suggestion"] and b["suggestion"].get("issue")):
            continue
        hinter = hinter or Hinter(conn)
        c = b["contexts"][0] if b["contexts"] else {}
        b["hint"] = hinter.hint({"key": b["context_key"], "label": b["label"], "title": c.get("title"),
                                 "kind": b["kind"]})
    _themes(out)
    _between(conn, out)
    _auto_split(out, (cfg["segment"].get("auto_split_min") or 0) * 60)
    return {
        "date": date.isoformat(), "t0": t0, "t1": t1, "blocks": out, "tracked": tracked_any,
        "state_seconds": {k: int(v) for k, v in state_secs.items()},
        "events": [{"title": e["title"], "start": e["start"], "end": e["end"], "calendar": e["calendar"]}
                   for e in events],
    }


def _state(b):
    return b["issue"] or ("skip" if b["skip"] else "split" if b.get("split") else None)


def _themes(blocks):
    """Склеивает в одну тему активности одного проекта из разных мест: Claude «Альфа», Figma «5element/…», 5element.ru.

    Слово должно встретиться у 2–4 активностей; темы с разными уже выбранными задачами не склеиваются
    (дейлик Альфы и макеты Альфы остаются раздельно). Подсказка задачи расходится на всю тему.
    """
    groups = {}  # context_key → {"tokens", "states", "seconds", "label"}
    for b in blocks:
        k = b["context_key"]
        if not k or b["kind"] == "app":
            continue
        g = groups.setdefault(k, {"tokens": theme_tokens(k, b["label"]), "states": set(), "seconds": 0,
                                  "label": b["label"]})
        g["seconds"] += b["seconds"]
        if _state(b):
            g["states"].add(_state(b))
    by_token = defaultdict(list)
    for k, g in groups.items():
        for t in g["tokens"]:
            by_token[t].append(k)
    parent = {k: k for k in groups}
    states = {k: set(g["states"]) for k, g in groups.items()}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for t, ks in sorted(by_token.items(), key=lambda kv: (len(kv[1]), -len(kv[0]))):
        if not 2 <= len(ks) <= 4:
            continue
        for k in ks[1:]:
            a, b = find(ks[0]), find(k)
            if a != b and len(states[a] | states[b]) <= 1:
                parent[b] = a
                states[a] |= states.pop(b)
    # Модель (кнопка «Подобрать локально») отнесла к одной задаче — тоже одна тема.
    by_sug = defaultdict(set)
    for b in blocks:
        sg = b.get("suggestion") or {}
        if b["context_key"] in groups and sg.get("issue") and (sg.get("reason") or "").startswith("локально"):
            by_sug[sg["issue"]].add(b["context_key"])
    for ks in by_sug.values():
        ks = sorted(ks)
        for k in ks[1:]:
            a, b = find(ks[0]), find(k)
            if a != b and len(states[a] | states[b]) <= 1:
                parent[b] = a
                states[a] |= states.pop(b)
    members = defaultdict(list)
    for k in groups:
        members[find(k)].append(k)
    theme_of = {}
    for root, ks in members.items():
        if len(ks) > 1:
            for k in ks:
                theme_of[k] = "theme:" + "+".join(sorted(ks))
    hint = {}
    for root, ks in members.items():
        if len(ks) < 2:
            continue
        st = states[root]
        if st and next(iter(st)) not in ("skip", "split"):
            src = max((k for k in ks if groups[k]["states"]), key=lambda k: groups[k]["seconds"])
            hint[root] = {"issue": next(iter(st)), "summary": None, "reason": "как «%s» сегодня" % groups[src]["label"][:60]}
        else:
            best = None
            for b in blocks:
                if b.get("hint") and b["context_key"] in ks and (not best or b["seconds"] > best[0]):
                    best = (b["seconds"], b["hint"])
            if best:
                hint[root] = best[1]
    for b in blocks:
        k = b["context_key"]
        b["theme"] = theme_of.get(k)
        if k in theme_of and not _state(b) and not (b["suggestion"] and b["suggestion"].get("issue")):
            b["hint"] = hint.get(find(k)) or b["hint"]


def _between(conn, blocks, near=30 * 60, touch=10 * 60):
    """Безымянное время посреди работы над проектом (или вплотную к ней) — скорее всего тот же проект.

    Созвон: Альфа → Телемост → снова Альфа — подсказываем задачу проекта про встречи/менеджмент.
    Приложение без названия проекта («Claude», «Просмотр», «Finder») сразу после Acme — ту же задачу.
    Явное совпадение слов («Альфа - Дейлик») важнее, его не трогаем.
    """
    def issue_of(b):
        return b["issue"] or (b.get("hint") or {}).get("issue") or (b.get("suggestion") or {}).get("issue")

    def project(b):
        if b["kind"] == "meeting" or b["skip"] or b.get("split"):
            return None
        k = issue_of(b)
        return (k.split("-")[0], b) if k else None

    work = [(b["start"], b["end"], p) for b in blocks for p in [project(b)] if p]
    cache = {}
    for b in blocks:
        if b["kind"] not in ("meeting", "app") or _state(b) or (b["suggestion"] and b["suggestion"].get("issue")):
            continue
        if b.get("hint") and not b["hint"]["reason"].startswith("проект"):
            continue
        before = max((w for w in work if w[1] <= b["start"] + 60 and b["start"] - w[1] <= near), key=lambda w: w[1], default=None)
        after = min((w for w in work if w[0] >= b["end"] - 60 and w[0] - b["end"] <= near), key=lambda w: w[0], default=None)
        if before and after and before[2][0] == after[2][0]:
            proj, nb, why = before[2][0], before[2][1], "между работой над"
        elif before and b["start"] - before[1] <= touch and not (after and after[0] - b["end"] <= touch):
            proj, nb, why = before[2][0], before[2][1], "сразу после работы над"
        elif after and after[0] - b["end"] <= touch and not (before and b["start"] - before[1] <= touch):
            proj, nb, why = after[2][0], after[2][1], "прямо перед работой над"
        else:
            continue
        same = {"issue": issue_of(nb), "summary": (nb.get("hint") or {}).get("summary")}
        if b["kind"] == "meeting":
            if proj not in cache:
                cache[proj] = meeting_issue(conn, proj)
            mi, what = cache[proj] or same, "созвон"
        else:
            mi, what = same, b["label"][:30]
        # here=True: подсказка про этот отрезок, а не про приложение — назначается без «запомнить».
        b["hint"] = {"issue": mi["issue"], "summary": mi["summary"], "here": True,
                     "reason": "%s %s «%s»" % (what, why, nb["label"][:50])}


def _auto_split(blocks, limit):
    """Мелочь без задачи и без подсказки — по задачам дня: определить её невозможно, а разбирать вручную долго.

    Считается по активности за весь день (Telegram 4 раза по 3 мин = 12 мин — мелочь), с учётом тем.
    Что вы разложили сами, не трогаем. Подсказка («похоже на ALP-12») мелочь не спасает — две минуты
    Telegram не стоят решения. Исключение — созвоны: даже короткий созвон — конкретная работа.
    """
    if not limit:
        return
    total, hinted = defaultdict(float), set()
    for b in blocks:
        if _state(b):
            continue
        k = b.get("theme") or b["context_key"] or b["id"]
        total[k] += b["seconds"]
        if b["kind"] == "meeting" and (b.get("hint") or (b["suggestion"] and b["suggestion"].get("issue"))):
            hinted.add(k)
    for b in blocks:
        k = b.get("theme") or b["context_key"] or b["id"]
        if not _state(b) and k not in hinted and total[k] < limit:
            b["split"] = b["auto_split"] = True


def _round_largest_remainder(values, unit):
    """Округляет к кратному unit так, чтобы сумма совпала с округлённой суммой."""
    if not values:
        return {}
    total = sum(values.values())
    target = int(round(total / unit))
    floors = {k: int(v // unit) for k, v in values.items()}
    rest = target - sum(floors.values())
    order = sorted(values, key=lambda k: -(values[k] / unit - floors[k]))
    for k in order[:max(0, rest)]:
        floors[k] += 1
    return {k: n * unit for k, n in floors.items()}


def propose(conn, cfg, day):
    """Черновики ворклогов: по записи на задачу (или несколько, если вы вынесли часть активностей
    отдельной записью) — авто-расчёт + ручные правки из drafts."""
    unit = cfg["worklog"]["round_min"] * 60
    min_sec = cfg["worklog"]["min_min"] * 60
    parts = {(r["issue"], r["context_key"]): r["part"]
             for r in conn.execute("SELECT * FROM parts WHERE date=?", (day["date"],))}
    # Ключ записи — (задача, part); part пустой у общей записи задачи.
    per = defaultdict(lambda: {"seconds": 0, "start": None, "labels": defaultdict(int), "sources": defaultdict(float),
                               "names": defaultdict(float), "icons": {}, "split": 0, "folded": 0})
    unassigned = 0
    skipped = 0
    split = 0
    for b in day["blocks"]:
        if b["skip"]:
            skipped += b["seconds"]
            continue
        if b.get("split"):
            split += b["seconds"]
            continue
        if not b["issue"]:
            unassigned += b["seconds"]
            continue
        # Блок назван по главному окну, но внутри него бывает и другая работа (полчаса Claude Code посреди
        # «Figma: Альфа»). Поэтому время блока делим по окнам: вынесенное отдельной записью уходит туда
        # своей долей, остальное — в запись главного окна блока.
        ck = b.get("context_key") or b["id"]
        home = parts.get((b["issue"], ck), "")
        ctx_sec = sum(c["seconds"] for c in b["contexts"]) or 1
        for c in b["contexts"] or [{"key": ck, "label": b["label"], "kind": b["kind"], "seconds": 1}]:
            g = c.get("group") or c.get("key") or ck
            p = per[(b["issue"], parts.get((b["issue"], g), home))]
            share = b["seconds"] * c["seconds"] / ctx_sec if b["contexts"] else b["seconds"]
            p["seconds"] += share
            p["start"] = b["start"] if p["start"] is None else min(p["start"], b["start"])
            p["sources"][g] += share
            p["names"][c.get("label") or ""] += share
            p["icons"].setdefault(g, (c.get("label") or "", c.get("icon")))
            part = comment_part(c)
            # Чужое (экран Альфы, мелькнувший внутри блока Acme) в комментарий этой задачи не берём.
            if part and c.get("resolved_issue") in (None, b["issue"]):
                p["labels"][part] += c["seconds"]
    work = sum(v["seconds"] for v in per.values())
    if split and work:
        # Делим «по задачам дня» пропорционально уже разложенному времени.
        for v in per.values():
            v["split"] = split * v["seconds"] / work
            v["seconds"] += v["split"]
    # Задача, назначенная вами или правилом, — конкретная работа: логируется отдельно, сколько бы ни длилась.
    # Растворяются только мелькания короче min_min (случайно открыли окно с ключом задачи на минуту).
    by_issue = defaultdict(float)
    for (k, _), v in per.items():
        by_issue[k] += v["seconds"]
    small = sorted((k for k in by_issue if by_issue[k] < min_sec), key=lambda k: by_issue[k])
    big = [k for k in by_issue if k not in small]
    if not big and small:
        big = [small.pop()]
    folded = []
    for k in small:
        folded.append({"issue": k, "seconds": int(by_issue[k]), "into": None})
        dst = [x for x in per if x[0] in big]
        base = sum(per[x]["seconds"] for x in dst)
        for x in dst:
            add = by_issue[k] * per[x]["seconds"] / base
            per[x]["seconds"] += add
            per[x]["folded"] += add
        for x in per:
            if x[0] == k:
                per[x]["seconds"] = 0
    raw = {x: v["seconds"] for x, v in per.items() if x[0] in big}
    rounded = _round_largest_remainder(raw, unit)
    for x in raw:  # 15 минут дейли не превращаются в 0 при округлении
        rounded[x] = max(rounded.get(x, 0), unit)

    # Тема — окна одного проекта («Figma: Альфа» + сессия Claude «alpha»): в списке источников они одной строкой.
    themes = {k: b["theme"] for b in day["blocks"] if b.get("theme") for k in b["theme"][len("theme:"):].split("+")}
    issues = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM issues")}
    drafts = {(r["issue"], r["part"]): dict(r) for r in conn.execute("SELECT * FROM drafts WHERE date=?",
                                                                      (day["date"],))}
    logged = {r["issue"]: r["seconds"] for r in conn.execute("SELECT * FROM jira_logged WHERE date=?",
                                                                (day["date"],))}
    rows = []
    for x in set(rounded) | set(drafts):
        key, part = x
        p = per.get(x)
        auto_secs = rounded.get(x, 0)
        labels = sorted(p["labels"].items(), key=lambda kv: -kv[1]) if p else []
        # В комментарий — только то, что заметно по времени, без минутных переключений.
        auto_comment = build_comment(labels, (issues.get(key) or {}).get("summary"))
        d = drafts.get(x) or {}
        secs = d.get("seconds") if d.get("seconds") is not None else auto_secs
        comment = d.get("comment") if d.get("comment") is not None else auto_comment
        if not secs and not d.get("jira_id"):
            continue
        iss = issues.get(key) or {}
        rows.append({
            "issue": key, "part": part, "id": key + ("|" + part if part else ""),
            "part_label": max(p["names"], key=p["names"].get) if p and part else None,
            "summary": iss.get("summary"), "project": iss.get("project") or key.split("-")[0],
            "seconds": int(secs), "auto_seconds": int(auto_secs), "raw_seconds": int(p["seconds"]) if p else 0,
            "comment": comment, "auto_comment": auto_comment, "edited": bool(d.get("seconds") is not None
                                                                              or d.get("comment") is not None),
            "start": p["start"] if p else None, "jira_id": d.get("jira_id"), "jira_logged": logged.get(key, 0),
            # Из чего сложилось время: активности (ключ → секунды) и добавки «поделить» / мелкие задачи.
            "sources": _sources(p, themes) if p else [], "split_seconds": int(p["split"]) if p else 0, "folded_seconds": int(p["folded"]) if p else 0,
            "_d": d,
        })
    total_by_issue = defaultdict(int)
    for r in rows:
        total_by_issue[r["issue"]] += r["seconds"]
    for r in rows:
        d = r.pop("_d")
        if d.get("jira_id"):
            r["status"] = "pushed" if (d.get("pushed_seconds") == r["seconds"] and d.get("pushed_comment") == r["comment"]) \
                else "changed"
        else:  # Внесено руками мимо трекера: сверяем со всей задачей за день, а не с отдельной записью.
            tot = total_by_issue[r["issue"]]
            r["status"] = "in_jira" if logged.get(r["issue"], 0) >= tot * 0.9 and tot else "draft"
    # Записи одной задачи — рядом: сначала общая, потом вынесенные.
    rows.sort(key=lambda r: (-total_by_issue[r["issue"]], r["issue"], r["part"] != "", -r["seconds"]))
    total = sum(b["seconds"] for b in day["blocks"])
    if split and not work:
        unassigned += split  # делить не на что — остаётся неразобранным
    return {"worklogs": rows, "folded": [f for f in folded if f["seconds"] >= 60], "min_sec": min_sec,
            "unassigned": int(unassigned), "skipped": int(skipped), "split": int(split),
            "total": int(total),
            "to_log": sum(r["seconds"] for r in rows)}


def _sources(p, themes):
    """Из чего сложилась запись: темы/окна по убыванию времени. keys — что выносить отдельной записью."""
    out = {}
    for k, sec in p["sources"].items():
        t = themes.get(k, k)
        o = out.setdefault(t, {"key": t, "keys": [], "seconds": 0, "label": None, "icon": None, "_top": -1})
        o["keys"].append(k)
        o["seconds"] += sec
        if sec > o["_top"]:
            o["_top"], (o["label"], o["icon"]) = sec, p["icons"].get(k, (k, None))
    res = sorted(out.values(), key=lambda o: -o["seconds"])
    for o in res:
        o.pop("_top")
        o["seconds"] = int(o["seconds"])
    return res


def comment_part(c):
    """Что из активности годится в комментарий ворклога — только суть работы, без названий программ.
    → (вид, текст) или None."""
    kind, label, title = c["kind"], c["label"] or "", (c.get("title") or "").strip()
    if kind == "claude":
        t = label.split(" — ", 1)[1] if " — " in label else ""
        return ("work", t) if t else None
    if kind == "file" and label.startswith("Figma: "):
        doc = label[len("Figma: "):]
        if " / " in doc:
            screen = re.sub(r"(?i)^(экран|раздел|страница)\s+", "", doc.split(" / ", 1)[1])
            return ("screen", re.sub(r"[«»\"“”]", "", screen).strip())
        return None if doc.lower() in ("untitled", "без названия") else ("file", doc)
    if kind == "file":  # Illustrator/Photoshop/…: имя файла без расширения
        doc = re.sub(r"\.[a-z0-9]{2,4}$", "", label.split(": ", 1)[-1], flags=re.I)
        return ("file", doc) if doc and not doc.lower().startswith("untitled") else None
    if kind == "meeting" and c.get("category") != "personal":
        if re.fullmatch(r"(?i)zoom|teams|телемост|google meet|созвон.*|контур\.толк|salutejazz|vk звонки|jitsi", label):
            return ("call", "созвон")
        return ("meeting", label)
    if kind == "web" and c.get("category") == "docs" and title:
        return ("doc", title.split(" - ")[0].split(" – ")[0][:60])
    return None  # чаты, сайты, приложения, Jira-страницы — в комментарий не идут


def build_comment(parts, summary=None):
    """«Логотип Альфа с интеграцией текста. Встреча «Обзор макетов». Макеты: Корзина, Фильтры».

    Только заметное по времени (минута в чужом файле Figma — не работа), без повторов: файл, о котором
    уже говорит сессия Claude («Логотип Альфа…» и макет «Альфа»), и название задачи во встрече не дублируются.
    """
    clean = lambda t: re.sub(r"\s+", " ", t or "").strip(" .;,—-")
    total = sum(sec for _, sec in parts) or 1
    floor = max(180, total * 0.08)
    items = []  # [вид, текст, секунды]
    for i, ((k, t), sec) in enumerate(parts):
        t = clean(t)
        if t and k != "screen" and (i == 0 or sec >= floor):
            items.append([k, t, sec])
    # Файл, который называет описание работы, — та же работа: время к ней, файл отдельно не пишем.
    for it in [x for x in items if x[0] == "file"]:
        name = it[1].split("/")[-1].strip().lower()
        host = next((x for x in items if x[0] in ("work", "doc") and len(name) >= 3
                     and re.search(r"(?<!\w)%s(?!\w)" % re.escape(name), x[1].lower())), None)
        if host:
            host[2] += it[2]
            items.remove(it)
    items.sort(key=lambda x: -x[2])
    has_meeting = any(x[0] == "meeting" for x in items)
    out = []
    for k, t, _ in items:
        if k == "meeting":
            # «Обзор макетов Корзина 2.0» в задаче «Корзина 2.0» → «Обзор макетов».
            short = clean(re.sub(re.escape(clean(summary)), " ", t, flags=re.I)) if summary and clean(summary) else t
            t = "встреча «%s»" % (short if len(short) >= 4 else t)
        elif k == "file":
            t = "макет «%s»" % t.split("/")[-1].strip()
        elif k == "call":
            if has_meeting:
                continue
            t = "созвон"
        out.append(t)
    screens = [clean(t) for (k, t), sec in parts if k == "screen" and sec >= 30]
    if screens:
        # Экраны — после основной работы и не больше трёх (по времени), иначе вытесняют главное.
        uniq = list(dict.fromkeys(screens))
        out.append("макеты: " + ", ".join(uniq[:3]) + (" и др." if len(uniq) > 3 else ""))
    out = list(dict.fromkeys(x for x in out if x))
    if not out:
        return summary or ""
    return ". ".join(x[0].upper() + x[1:] for x in out)[:250]


def merge_labels(labels):
    """«Figma: Alpha / A» + «Figma: Alpha / B» → «Figma: Alpha — A, B» (время суммируется, порядок по времени)."""
    out, groups = [], {}
    for lbl, sec in labels:
        if lbl.startswith("Figma: ") and " / " in lbl:
            proj, screen = lbl[len("Figma: "):].split(" / ", 1)
            g = groups.get(proj)
            if g is None:
                g = groups[proj] = ["Figma: " + proj, 0, []]
                out.append(g)
            g[1] += sec
            g[2].append(screen.strip())
        else:
            out.append([lbl, sec, None])
    res = []
    for lbl, sec, screens in out:
        if screens:
            lbl = "%s — %s" % (lbl, ", ".join(screens[:4]) + (" и др." if len(screens) > 4 else ""))
        res.append((lbl, sec))
    return sorted(res, key=lambda kv: -kv[1])


def _short_label(c):
    lbl = c["label"] or ""
    if c["kind"] == "issue":
        # «[ABC-1] Summary - Jira» → «Jira: Summary»
        lbl = lbl.replace(" - Jira", "")
        if c["issue"]:
            lbl = lbl.replace("[%s]" % c["issue"], "").replace(c["issue"], "").strip(" :-–")
        lbl = ("Jira: " + lbl) if lbl else ""
    elif c["kind"] == "meeting":
        lbl = "Встреча: " + lbl
    elif c["kind"] == "claude":
        lbl = lbl.replace("Claude Code: ", "Claude: ")
    elif c["kind"] == "web":
        lbl = lbl.split(" — ")[0]
    return lbl[:80]


def fmt_dur(sec):
    m = int(round(sec / 60.0))
    h, m = divmod(m, 60)
    if h and m:
        return "%dч %02dм" % (h, m)
    return "%dч" % h if h else "%dм" % m


def parse_dur(s):
    """«1h30m», «1.5h», «90m», «1:30», «1ч30м» → секунды."""
    import re
    s = s.strip().lower().replace(",", ".")
    if ":" in s:
        h, m = s.split(":", 1)
        return int(h) * 3600 + int(m) * 60
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*[hч])?\s*(?:(\d+)\s*[mм])?", s)
    if m and (m.group(1) or m.group(2)):
        return int(float(m.group(1) or 0) * 3600) + int(m.group(2) or 0) * 60
    if re.fullmatch(r"\d+(\.\d+)?", s):
        v = float(s)
        return int(v * 3600) if v <= 12 else int(v * 60)
    raise ValueError("Не понял длительность: %s" % s)
