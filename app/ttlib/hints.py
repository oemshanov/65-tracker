"""Подсказки задач без Claude: по совпадению слов с прошлыми привязками и с названиями задач Jira.

«acme.shop» → ACME-1, потому что раньше «Claude Code: Acme» привязывали к ACME-1;
«Альфа - Дейлик» → задача про встречи в проекте «Разработка дизайна Альфа».
"""
import os
import re
from collections import Counter

from .context import generic_call

STOP = set("""
claude code figma google chrome safari com www http https html app apps the and for with new untitled mobile
экран раздел страница мп веб web проект проекта проектов анализ мобильного приложения приложение разработка
дизайна дизайн задача задачи работа работы общие для под при над про как что это все всё документы documents users
встреча встречи созвон mail gmail yandex search поиск screen page главная main telegram ios android
desktop apple google яндекс телемост zoom meet iphone ipad macbook
""".split())
# Имя пользователя Мака и его полное имя встречаются в путях («/Users/имя/…», «Personal/Имя») и ни о чём
# не говорят — тоже стоп-слова. Берём с этого Мака, а не вписываем в код.
def _me():
    import pwd
    try:
        pw = pwd.getpwuid(os.getuid())
        raw = " ".join((pw.pw_name, pw.pw_gecos, os.path.basename(os.path.expanduser("~"))))
    except (KeyError, OSError):
        raw = os.path.basename(os.path.expanduser("~"))
    return {w for w in re.split(r"[^a-zа-яё0-9]+", raw.lower()) if len(w) > 2}


STOP |= _me()
_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _add(out, w):
    if w in STOP or w.isdigit():
        return
    out.add(w)
    if re.search(r"[а-яё]", w):
        lat = "".join(_TR.get(ch, ch) for ch in w)  # альфа → alpha, ромашка → romashka
        if lat not in STOP:
            out.add(lat)


def tokens(text):
    out = set()
    words = re.findall(r"[a-zа-яё0-9]+", (text or "").lower())
    for i, w in enumerate(words):
        if len(w) >= 3:
            _add(out, w)
        # «5 элемент» → «5элемент»/«5element», чтобы совпасть с 5element.ru и файлом «5element/…».
        if i + 1 < len(words) and (len(w) < 3 or w.isdigit()) and not words[i + 1].isdigit():
            _add(out, w + words[i + 1])
    return out


def _words(common):
    """Сколько разных слов совпало: «редизайн» и его транслит «redizayn» — одно слово."""
    return len(common) - sum(1 for t in common
                             if re.search(r"[а-яё]", t) and "".join(_TR.get(ch, ch) for ch in t) in common)


def theme_tokens(context_key, label=None):
    """Слова, по которым активности одного проекта узнают друг друга: папка Claude, файл, домен, встреча."""
    kind, _, rest = (context_key or "").partition(":")
    if kind == "claude":
        text = rest.rstrip("/").rsplit("/", 1)[-1]
    elif kind == "file":
        text = rest.partition(":")[2].replace("/", " ")
    elif kind == "web":
        text = re.sub(r"\.[a-z]{2,6}$", "", rest.split("/")[0])  # 5element.ru → 5element
    elif kind == "meeting":
        text = rest
    else:
        return set()
    return {t for t in tokens(text) if len(t) >= 4}


def pretty(context_key, label=None):
    """Читаемое имя активности для пояснения подсказки."""
    if label:
        return label
    kind, _, rest = context_key.partition(":")
    if kind == "claude":
        return "Claude Code: " + rest.rstrip("/").rsplit("/", 1)[-1]
    if kind == "file":
        app, _, doc = rest.partition(":")
        return "%s: %s" % (app.capitalize(), doc)
    if kind == "meeting":
        return "встреча «%s»" % rest
    return rest


class Hinter:
    def __init__(self, conn):
        self.history = []  # (tokens, issue, label)
        usage = Counter()
        for r in conn.execute("SELECT context_key, label, issue FROM mappings WHERE issue IS NOT NULL"):
            if generic_call(r["context_key"]):
                continue
            kp = "" if r["context_key"].startswith("app:") else r["context_key"].split(":", 1)[-1]
            self.history.append((tokens((r["label"] or "") + " " + kp), r["issue"],
                                 pretty(r["context_key"], r["label"])))
            usage[r["issue"]] += 3
        for r in conn.execute("SELECT issue, COUNT(*) n FROM drafts GROUP BY issue"):
            usage[r["issue"]] += r["n"]
        self.usage = usage
        self.issues = []  # (key, summary, project_name, summary_tokens, project_tokens)
        self.active = set()
        self.mine = set()
        for r in conn.execute("SELECT key, summary, project_name, status, mine FROM issues WHERE summary IS NOT NULL"):
            if r["mine"] != 0:
                self.mine.add(r["key"])
            st = (r["status"] or "").lower()
            if st in ("завершена", "закрыт", "готово", "done", "closed", "cancelled"):
                continue
            if st in ("в работе", "in progress", "review", "на ревью", "в процессе"):
                self.active.add(r["key"])  # из двух одинаково похожих задач берём ту, что в работе
            self.issues.append((r["key"], r["summary"], r["project_name"] or "", tokens(r["summary"]),
                                tokens(r["project_name"])))

    def hint(self, ctx):
        """ctx: dict с key/label/title/url/kind → {"issue", "summary", "reason"} или None."""
        key = ctx.get("key") or ""
        # Технический id приложения (app:com.figma.Desktop) не несёт смысла — не используем.
        key_part = "" if key.startswith("app:") else key.split(":", 1)[-1]
        t = tokens(" ".join(filter(None, [ctx.get("label"), ctx.get("title"), key_part])))
        if not t:
            return None
        best = None  # (score, issue, reason)
        for toks, issue, label in self.history:
            common = t & toks
            if common:
                score = 3 * len(common) + 0.01 * self.usage[issue]
                if not best or score > best[0]:
                    best = (score, issue, "как «%s»" % label[:60])
        by_project = {}
        for key, summary, project, st, pt in self.issues:
            cs, cp = t & st, t & pt
            if key not in self.mine and _words(cs) < 2:
                continue  # чужие задачи проекта (баги и т.п.) — только при сильном совпадении
            if cs:
                # Совпадение с названием задачи (UI-kit → «Работа на UI KIT») весомее совпадения только по проекту.
                score = 3 * len(cs) + len(cp) + 0.01 * self.usage[key] + 0.5 * (key in self.active)
                if not best or score > best[0]:
                    best = (score, key, "похоже на «%s»" % summary[:60])
            elif cp:
                by_project.setdefault(project, []).append((key, summary))
        if not best and by_project:
            # Совпал только проект: берём задачу про встречи для созвонов, иначе самую используемую.
            project, cands = max(by_project.items(), key=lambda kv: len(kv[1]))
            pick = None
            if ctx.get("kind") == "meeting":
                pick = next((c for c in cands if re.search(r"встреч|созвон|митинг", c[1], re.I)), None)
            if not pick:
                used = [c for c in cands if self.usage[c[0]]]
                pick = max(used, key=lambda c: self.usage[c[0]]) if used else None
            if pick:
                best = (1, pick[0], "проект «%s»" % project[:50])
        if not best:
            return None
        summary = next((s for k, s, *_ in self.issues if k == best[1]), None)
        return {"issue": best[1], "summary": summary, "reason": best[2]}


MEETING_ISSUE = re.compile(r"встреч|созвон|митинг|коммуникац|обсужд|менеджмент|управлени|синк|sync|meeting", re.I)


def meeting_issue(conn, project):
    """Задача проекта для созвонов: «Внутренние встречи», иначе «Менеджмент»/«Коммуникации»."""
    rows = conn.execute("SELECT key, summary, status FROM issues WHERE (project=? OR key LIKE ?) AND summary IS NOT NULL",
                        (project, project + "-%")).fetchall()
    best = None
    for r in rows:
        st = (r["status"] or "").lower()
        if st in ("завершена", "закрыт", "готово", "done", "closed", "cancelled") or not MEETING_ISSUE.search(r["summary"]):
            continue
        score = 2 * bool(re.search(r"внутрен", r["summary"], re.I)) - bool(re.search(r"внешн", r["summary"], re.I))
        if not best or score > best[0]:
            best = (score, r["key"], r["summary"])
    return best and {"issue": best[1], "summary": best[2]}
