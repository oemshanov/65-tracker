"""Что именно делалось: из спана (app/title/url/mic) получаем «контекст» и решаем, к какой задаче он относится."""
import re
from urllib.parse import parse_qs, urlparse

JIRA_KEY = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9_]{1,9}-[1-9][0-9]{0,5})(?![A-Za-z0-9])")
NOT_PROJECTS = {"UTF", "ISO", "SHA", "MD", "RFC", "COVID", "GPT", "CVE", "RGB", "CMYK", "HTTP", "TLS",
                "SSL", "USB", "WCAG", "UX", "UI", "PDF", "IOS", "MAC", "ARM", "X86", "WIN", "DDR"}

BROWSERS = {
    "com.google.Chrome", "com.google.Chrome.beta", "com.brave.Browser", "com.microsoft.edgemac",
    "company.thebrowser.Browser", "com.vivaldi.Vivaldi", "ru.yandex.desktop.yandex-browser",
    "com.operasoftware.Opera", "com.apple.Safari", "org.mozilla.firefox",
}
DESIGN_APPS = {
    "com.figma.Desktop": "Figma", "com.adobe.illustrator": "Illustrator",
    "com.adobe.Photoshop": "Photoshop", "com.adobe.AfterEffects": "After Effects",
    "com.adobe.InDesign": "InDesign", "com.adobe.Premiere": "Premiere", "org.blenderfoundation.blender": "Blender",
    "com.bohemiancoding.sketch3": "Sketch",
}
CHAT_APPS = {
    "ru.keepcoder.Telegram": "Telegram", "org.telegram.desktop": "Telegram", "chat.rocket": "Rocket.Chat",
    "com.tinyspeck.slackmacgap": "Slack", "com.apple.mail": "Почта", "com.microsoft.Outlook": "Outlook",
    "com.apple.MobileSMS": "Сообщения",
}
CALL_APPS = {
    "us.zoom.xos": "Zoom", "com.microsoft.teams2": "Teams", "com.microsoft.teams": "Teams",
    "ru.keepcoder.Telegram": "Telegram", "org.telegram.desktop": "Telegram", "chat.rocket": "Rocket.Chat",
    "com.apple.FaceTime": "FaceTime", "com.tinyspeck.slackmacgap": "Slack", "com.hnc.Discord": "Discord",
    "ru.yandex.telemost": "Телемост",
}
# Сайты, где важен конкретный документ, а не домен целиком.
DOC_DOMAINS = ("docs.google.com", "figma.com", "miro.com", "notion.so", "confluence", "wiki",
               "drive.google.com", "github.com", "gitlab", "dribbble.com", "behance.net")
MEET_DOMAINS = {"meet.google.com": "Google Meet", "telemost.yandex.ru": "Телемост", "telemost.360.yandex.ru": "Телемост",
                "telemost.yandex.com": "Телемост", "zoom.us": "Zoom", "app.zoom.us": "Zoom",
                "teams.microsoft.com": "Teams", "teams.live.com": "Teams", "ktalk.ru": "Контур.Толк",
                "salutejazz.ru": "SaluteJazz", "jazz.sber.ru": "SaluteJazz", "calls.vk.com": "VK Звонки",
                "meet.jit.si": "Jitsi", "app.webinar.ru": "Webinar"}
# Подстрока в bundle id процесса с микрофоном → название сервиса (если точного совпадения нет).
CALL_HINTS = [("telemost", "Телемост"), ("zoom", "Zoom"), ("teams", "Teams"), ("ktalk", "Контур.Толк"),
              ("jazz", "SaluteJazz"), ("discord", "Discord"), ("slack", "Slack"), ("facetime", "FaceTime"),
              ("telegram", "Telegram"), ("rocket", "Rocket.Chat"), ("skype", "Skype"), ("whatsapp", "WhatsApp"),
              ("vk", "VK Звонки"), ("webex", "Webex")]

TITLE_NOISE = [
    re.compile(r"^\(\d+\)\s*"),  # (3) непрочитанные
    re.compile(r"\s*[-–—|]\s*(Google Chrome|Safari|Яндекс Браузер|Brave|Arc|Microsoft Edge)$"),
    re.compile(r"\s*[-–—]\s*(Edited|Изменено)$"),
    re.compile(r"\s*•\s*$"),
]


class Ctx:
    __slots__ = ("key", "label", "kind", "category", "issue", "app", "bundle", "title", "url", "group", "icon")

    def __init__(self, key, label, kind, category, issue=None, app=None, bundle=None, title=None, url=None,
                 group=None, icon=None):
        self.key, self.label, self.kind, self.category = key, label, kind, category
        self.issue, self.app, self.bundle, self.title, self.url = issue, app, bundle, title, url
        # group — уровень, на котором запоминается привязка (для Claude — проект, а не сессия).
        self.group = group or key
        # icon — чьей иконкой показывать: у созвона это программа с микрофоном, а не окно на переднем плане.
        self.icon = icon or bundle

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def clean_title(t):
    if not t:
        return ""
    for rx in TITLE_NOISE:
        t = rx.sub("", t)
    return t.strip()


def find_issue_key(text, project_keys=None):
    if not text:
        return None
    for m in JIRA_KEY.finditer(text):
        key = m.group(1)
        proj = key.split("-")[0]
        if proj in NOT_PROJECTS:
            continue
        if project_keys and proj not in project_keys:
            continue
        return key
    return None


def issue_from_url(url):
    if not url:
        return None
    try:
        u = urlparse(url)
    except ValueError:
        return None
    m = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)", u.path)
    if m:
        return m.group(1)
    q = parse_qs(u.query)
    for p in ("selectedIssue", "issueKey", "issue"):
        if p in q and JIRA_KEY.fullmatch(q[p][0] or ""):
            return q[p][0]
    return None


def _meet_domain(dom):
    if not dom:
        return None
    return MEET_DOMAINS.get(dom) or next((v for k, v in MEET_DOMAINS.items() if dom.endswith("." + k)), None)


def figma_ctx(doc, app, bundle, title, url):
    """Figma: ключ — конкретный экран/страница, группа — файл/проект (часть до « / »).
    «Запомнить» сохраняется на группу, поэтому новые экраны того же файла разложатся сами."""
    project = doc.split(" / ")[0].strip() if " / " in doc else doc
    return Ctx("file:figma:" + doc.lower(), "Figma: " + doc, "file", "design", None, app, bundle, title, url,
               group="file:figma:" + project.lower())


def domain_of(url):
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def doc_name(bundle, title):
    """Имя файла/документа из заголовка окна дизайн-приложения."""
    t = clean_title(title)
    t = re.sub(r"\s*[-–—]\s*(Figma|Blender.*|Sketch)$", "", t)
    t = t.split(" @ ")[0]  # Illustrator/Photoshop: «file.ai @ 66% (RGB/Preview)»
    t = re.sub(r"\s*\[[^\]]*\]$", "", t)
    return t.strip() or None


def context_of(span, project_keys=None, calendar_title=None, personal_bundles=()):
    """Возвращает Ctx для активного спана или спана созвона."""
    bundle = span["bundle"] or ""
    app = span["app"] or bundle or "?"
    title = clean_title(span["title"])
    url = span["url"] or ""
    issue = issue_from_url(url) or find_issue_key(title, project_keys)
    dom = domain_of(url) if url else ""

    if span["state"] == "call":
        mic = span["mic_app"] or ""
        who = CALL_APPS.get(mic) or next((v for k, v in CALL_APPS.items() if mic.startswith(k)), None)
        if not who and _meet_domain(dom):
            who = _meet_domain(dom)
        if not who and mic.split(".helper")[0] in BROWSERS:
            who = "Созвон в браузере"
        if not who:
            low = mic.lower()
            who = next((v for k, v in CALL_HINTS if k in low), None)
        who = who or (mic or "Созвон")
        label = calendar_title or who
        key = "meeting:" + (calendar_title.lower() if calendar_title else who.lower())
        cat = "personal" if calendar_title and PERSONAL_EVENT.search(calendar_title) else "meeting"
        return Ctx(key, label, "meeting", cat, issue or find_issue_key(calendar_title, project_keys),
                   app, bundle, title, url, icon=mic.split(".helper")[0] or bundle)

    if bundle in personal_bundles:
        return Ctx("app:" + bundle, app, "app", "personal", None, app, bundle, title, url)

    if issue:
        return Ctx("issue:" + issue, title or issue, "issue", "work", issue, app, bundle, title, url)

    if bundle in DESIGN_APPS:
        doc = doc_name(bundle, title)
        name = DESIGN_APPS[bundle]
        if doc and doc.lower() not in (name.lower(), "home", "drafts", "recents", "недавние"):
            if name == "Figma":
                return figma_ctx(doc, app, bundle, title, url)
            return Ctx("file:%s:%s" % (name.lower(), doc.lower()), "%s: %s" % (name, doc), "file", "design",
                       None, app, bundle, title, url)
        return Ctx("app:" + bundle, name, "app", "design", None, app, bundle, title, url)

    if bundle in CHAT_APPS:
        name = CHAT_APPS[bundle]
        # «Telegram @ Ivan» — это имя аккаунта, а не чата: такие заголовки считаем одним контекстом.
        if title and title.lower() not in (name.lower(), "telegram") and not re.match(r"(?i)^telegram( @ .*)?$", title):
            return Ctx("chat:%s:%s" % (name.lower(), title.lower()), "%s: %s" % (name, title), "chat",
                       "communication", None, app, bundle, title, url)
        return Ctx("app:" + bundle, name, "app", "communication", None, app, bundle, title, url)

    if bundle in BROWSERS:
        if not dom:
            return Ctx("app:" + bundle, title or app, "web", "browsing", None, app, bundle, title, url)
        if dom == "figma.com" or dom.endswith(".figma.com"):
            doc = doc_name("com.figma.Desktop", title)
            if doc:
                return figma_ctx(doc, app, bundle, title, url)
        if _meet_domain(dom):
            name = _meet_domain(dom)
            return Ctx("meeting:" + name.lower(), name, "meeting", "meeting", None, app, bundle, title, url)
        if any(d in dom for d in DOC_DOMAINS) and title:
            return Ctx("web:%s:%s" % (dom, title.lower()), "%s — %s" % (title, dom), "web", "docs",
                       None, app, bundle, title, url)
        cat = "tracking" if "jira" in dom else "browsing"
        return Ctx("web:" + dom, dom, "web", cat, None, app, bundle, title, url)

    return Ctx("app:" + (bundle or app), app, "app", "other", None, app, bundle, title, url)


# События календаря, которые по умолчанию не работа (можно переопределить через «запомнить»).
PERSONAL_EVENT = re.compile(r"обед|перерыв|coffee|кофе|lunch|\bbreak\b|отпуск|отгул|day ?off|выходной|личное",
                            re.I)


def calendar_ctx(event, project_keys=None):
    t = event["title"] or "Встреча"
    cat = "personal" if PERSONAL_EVENT.search(t) else "meeting"
    return Ctx("meeting:" + t.lower(), t, "meeting", cat, find_issue_key(t, project_keys), "Календарь")


_PLATFORM = re.compile(r"^\s*(мп|веб|web|ios|android)[\s_:.\-]+", re.I)


def platform_of(summary):
    m = _PLATFORM.match(summary or "")
    return m.group(1).lower().replace("web", "веб") if m else None


def norm_screen(s):
    """Название экрана/задачи без платформы, кавычек и слов «экран», «раздел»."""
    s = _PLATFORM.sub("", (s or "").lower().replace("ё", "е"))
    s = re.sub(r"[«»\"“”„'`]", " ", s)
    s = re.sub(r"\b(экран|раздел|страница|главный экран раздела)\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class Resolver:
    """Контекст → задача. Приоритет: явный ключ > выученная привязка > правило > Claude."""

    def __init__(self, conn, cfg):
        self.cfg = cfg
        self.mappings = {r["context_key"]: r for r in conn.execute("SELECT * FROM mappings")}
        self.suggestions = {r["context_key"]: r for r in conn.execute("SELECT * FROM suggestions")}
        self.rules = []
        for r in cfg.get("rules", []):
            cond = {k: re.compile(v, re.I) for k, v in (r.get("if") or {}).items()}
            self.rules.append((cond, r))
        self.auto_accept = float(cfg["llm"].get("auto_accept", 1.01))
        self.issues = {}
        self.by_project = {}
        for r in conn.execute("SELECT key, summary, project FROM issues WHERE summary IS NOT NULL"):
            self.issues[r["key"]] = r["summary"]
            self.by_project.setdefault(r["key"].split("-")[0], []).append((r["key"], r["summary"]))

    def match_screen(self, screen, base_issue):
        """«Экран «Мои обращения»» + ALPHA-5 («МП_…») → ALPHA-60 «МП_Мои обращения»."""
        cands = self.by_project.get(base_issue.split("-")[0]) or []
        target = norm_screen(screen)
        if not cands or len(target) < 4:
            return None
        plat = platform_of(self.issues.get(base_issue) or "")
        best, best_score = None, 0.0
        for key, summary in cands:
            n = norm_screen(summary)
            if not n:
                continue
            if n == target:
                score = 2.0
            elif min(len(n), len(target)) >= 8 and (target in n or n in target):
                score = 1.0
            else:
                continue
            if plat and platform_of(summary) == plat:
                score += 0.5
            if score > best_score:
                best, best_score = key, score
        return best

    def _rule(self, ctx):
        for cond, r in self.rules:
            ok = True
            for field, rx in cond.items():
                if field == "any":
                    val = " ".join(filter(None, [ctx.app, ctx.title, ctx.url, ctx.label]))
                else:
                    val = getattr(ctx, field, None) or ""
                if not rx.search(val):
                    ok = False
                    break
            if ok:
                return r
        return None

    def resolve(self, ctx):
        """→ dict(issue, category, source, confidence, suggestion)"""
        sug = self.suggestions.get(ctx.group) or self.suggestions.get(ctx.key)
        sug_d = dict(sug) if sug else None
        m = self.mappings.get(ctx.key)
        mg = self.mappings.get(ctx.group) if not m and ctx.group != ctx.key else None
        if mg and mg["issue"]:
            # Файл Figma привязан целиком — экран уточняем по названию задачи в том же проекте.
            screen = ctx.label.split(" / ", 1)[1] if ctx.key.startswith("file:figma:") and " / " in ctx.label else None
            hit = self.match_screen(screen, mg["issue"]) if screen else None
            if hit:
                return {"issue": hit, "category": ctx.category, "source": "screen", "confidence": 1.0,
                        "suggestion": None}
        m = m or mg
        if m:
            return {"issue": m["issue"], "category": m["category"] or ctx.category, "source": "learned",
                    "confidence": 1.0, "suggestion": None}
        if ctx.issue:
            return {"issue": ctx.issue, "category": ctx.category, "source": "explicit", "confidence": 1.0,
                    "suggestion": None}
        r = self._rule(ctx)
        if r:
            return {"issue": r.get("issue"), "category": r.get("category") or ctx.category, "source": "rule",
                    "confidence": 1.0, "suggestion": None}
        if ctx.category == "personal":
            return {"issue": None, "category": "personal", "source": "builtin", "confidence": 1.0,
                    "suggestion": None}
        if sug and (sug["confidence"] or 0) >= self.auto_accept and (sug["issue"] or sug["category"]):
            return {"issue": sug["issue"], "category": sug["category"] or ctx.category, "source": "claude",
                    "confidence": sug["confidence"], "suggestion": sug_d}
        return {"issue": None, "category": ctx.category, "source": None, "confidence": 0.0,
                "suggestion": sug_d}
