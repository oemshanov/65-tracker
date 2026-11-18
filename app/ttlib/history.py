"""Подсказки по вашей истории в Jira: какие активности приходились на дни, когда вы логировали ту или иную задачу.

Считается на этом Маке: по уже записанным трекером активностям и по времени, которое вы залогировали в Jira
(сколько в какую задачу за день — без комментариев). Ничего никуда не отправляется, нейросеть не используется.

Как: за каждый прошлый день время каждой активности делится между задачами, залогированными в этот день,
пропорционально их часам. Активность, которая день за днём приходится на одну задачу, связывается с ней;
то, что бывает каждый день при любых задачах (мессенджер, почта), размазывается и подсказки не даёт.
Подсказываем только задачи, в которые логировали недавно, и только при достаточной уверенности —
лучше без подсказки, чем мимо. Пороги подобраны честным замером (`tt eval`).
"""
import collections
import datetime as dt
import re
import time

HORIZON_DAYS = 42   # сколько прошлых дней учитывать
RECENT_DAYS = 10    # «недавние» задачи — из стольких последних дней с записями в Jira
MIN_CONF = 0.45     # доля лучшей задачи в оценке; ниже — не подсказываем
W_CTX, W_TOK = 2.0, 0.5  # точное совпадение активности весомее совпадения отдельных слов

# Слова, которые есть почти в любом заголовке и о задаче не говорят: названия программ, служебные слова.
STOP = set("the and for with from this that что как это для при над под или все всё без она они его "
           "новый новая claude code figma google chrome safari telegram https http www com".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_contexts(
  date TEXT NOT NULL, context_key TEXT NOT NULL, label TEXT, kind TEXT, seconds INTEGER,
  PRIMARY KEY(date, context_key));
"""


def enabled(cfg):
    return (cfg.get("history") or {}).get("enabled", True)


def tokens(s):
    return {t for t in re.findall(r"[a-zа-я0-9]{4,}", (s or "").lower().replace("ё", "е"))
            if t not in STOP and not t.isdigit()}


def store_day(conn, date, blocks):
    """Активности прошедшего дня (без личного) — для подсказок по следующим дням."""
    agg = {}
    for b in blocks:
        if b.get("skip"):
            continue
        k = b.get("context_key") or b["id"]
        a = agg.setdefault(k, [b.get("label") or "", b.get("kind") or "", 0])
        a[2] += int(b["seconds"])
    conn.execute("DELETE FROM day_contexts WHERE date=?", (date.isoformat(),))
    conn.executemany("INSERT INTO day_contexts VALUES(?,?,?,?,?)",
                     [(date.isoformat(), k, l, kd, s) for k, (l, kd, s) in agg.items()]
                     + [(date.isoformat(), "*", None, None, 0)])  # отметка «день посчитан», даже пустой
    conn.commit()


def fill_cache(conn, cfg, upto, horizon=HORIZON_DAYS):
    """Досчитать активности прошлых дней, которых ещё нет в кэше. Тяжело (разбор дня) — вызывать в фоне."""
    from .segment import build_day
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM day_contexts")}
    n = 0
    for i in range(1, horizon + 1):
        d = upto - dt.timedelta(days=i)
        if d.isoformat() in have:
            continue
        t = time.mktime(d.timetuple())
        if not conn.execute("SELECT 1 FROM spans WHERE start >= ? AND start < ? LIMIT 1", (t, t + 86400)).fetchone():
            continue
        store_day(conn, d, build_day(conn, cfg, d, learn=False)["blocks"])
        n += 1
    return n


class Model:
    """Связи «активность → задача» по дням до date."""

    def __init__(self, conn, cfg, date, horizon=HORIZON_DAYS):
        lo = (date - dt.timedelta(days=horizon)).isoformat()
        logged = collections.defaultdict(dict)
        for r in conn.execute("SELECT date, issue, seconds FROM jira_logged WHERE date >= ? AND date < ? "
                              "AND issue != '*' AND seconds > 0", (lo, date.isoformat())):
            logged[r["date"]][r["issue"]] = r["seconds"]
        acts = collections.defaultdict(list)
        for r in conn.execute("SELECT date, context_key, label, seconds FROM day_contexts WHERE date >= ? AND date < ? "
                              "AND context_key != '*'", (lo, date.isoformat())):
            acts[r["date"]].append(r)
        days = sorted(d for d in logged if d in acts)
        self.days = len(days)
        self.recent = set()
        for d in days[-RECENT_DAYS:]:
            self.recent |= set(logged[d])
        self.labels = {}
        self.ctx = collections.defaultdict(lambda: collections.defaultdict(float))
        self.tok = collections.defaultdict(lambda: collections.defaultdict(float))
        for d in days:
            total = sum(logged[d].values())
            share = {k: v / total for k, v in logged[d].items()}
            for a in acts[d]:
                self.labels[a["context_key"]] = a["label"]
                tk = tokens(a["label"])
                for k, s in share.items():
                    self.ctx[a["context_key"]][k] += a["seconds"] * s
                    for t in tk:
                        self.tok[t][k] += a["seconds"] * s

    def examples(self, min_share=0.5):
        """Прошлые активности, которые уверенно приходились на одну недавнюю задачу: [(название, задача)]."""
        out = []
        for ck, assoc in self.ctx.items():
            total = sum(assoc.values())
            k, v = max(assoc.items(), key=lambda kv: kv[1])
            if total and v / total >= min_share and k in self.recent and self.labels.get(ck):
                out.append((self.labels[ck], k))
        return out

    def predict(self, context_key, label):
        """→ (задача, уверенность 0..1) или None."""
        sc = collections.defaultdict(float)
        ca = self.ctx.get(context_key)
        if ca:
            s = sum(ca.values())
            for k, v in ca.items():
                sc[k] += W_CTX * v / s
        for t in tokens(label):
            ta = self.tok.get(t)
            if ta:
                s = sum(ta.values())
                for k, v in ta.items():
                    sc[k] += W_TOK * v / s
        sc = {k: v for k, v in sc.items() if k in self.recent}
        if not sc:
            return None
        k, v = max(sc.items(), key=lambda kv: kv[1])
        conf = v / sum(sc.values())
        return (k, conf) if conf >= MIN_CONF else None


def apply(conn, cfg, date, blocks, summary):
    """Подсказки по истории для блоков без задачи. Уверенная подсказка по истории заменяет подсказки
    «по словам», «по соседям» и старые подсказки нейросети — по замеру они ошибаются чаще.
    Подсказки «по словам» в задачи, куда давно не логировали, убираем."""
    if not enabled(cfg):
        return
    m = Model(conn, cfg, date)
    if not m.days:
        return
    for b in blocks:
        if b.get("issue") or b.get("skip"):
            continue
        p = m.predict(b.get("context_key") or b["id"], b.get("label"))
        if p:
            issue, conf = p
            b["hint"] = {"issue": issue, "summary": summary(issue), "source": "history", "confidence": round(conf, 2),
                         "reason": "так вы логировали раньше"}
            b["suggestion"] = None
        elif b.get("hint") and m.recent and b["hint"].get("issue") not in m.recent:
            b["hint"] = None
