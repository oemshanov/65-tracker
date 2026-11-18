"""Выход в интернет (Jira, обновления, загрузки) — одинаково на любой машине и при любом способе выхода в сеть.

1. По умолчанию — как в системе: прокси берётся из «Системные настройки → Сеть → Подробнее → Прокси»
   (и из переменных окружения), без прокси — напрямую. Корпоративный VPN, который просто прокладывает маршрут,
   работает так же.
2. Если в «Настройки → Сеть» трекера указан прокси — всегда через него. Это для VPN-клиентов, которые дают
   локальный прокси, но не прописывают его в системе: адрес человек берёт из своего клиента.
3. Сбой соединения (VPN переподключается, Мак только что проснулся) — один повтор через пару секунд.

Портов и адресов конкретных программ не подбираем.
"""
import re
import time
import urllib.error
import urllib.request

RETRY_DELAY = 2.0
_PROXY_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+:\d{1,5}/?$")


def proxy(cfg):
    return ((cfg or {}).get("network") or {}).get("proxy") or ""


def valid_proxy(p):
    return not p or bool(_PROXY_RE.match(p.strip()))


def opener(cfg=None):
    p = proxy(cfg)
    if p:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
    return urllib.request.build_opener()  # ProxyHandler по умолчанию — системные настройки прокси


def urlopen(req, cfg=None, timeout=30, retries=1):
    """Как urllib.request.urlopen, но с прокси из настроек трекера и одним повтором при сбое соединения.
    Ответ сервера с ошибкой (HTTPError) не повторяем — это не сбой сети."""
    op = opener(cfg)
    for attempt in range(retries + 1):
        try:
            return op.open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError):
            if attempt >= retries:
                raise
            time.sleep(RETRY_DELAY)


HINT = "Проверьте интернет и VPN, если сервис доступен только через него. Если в браузере открывается, " \
       "а здесь нет — укажите прокси в «Настройки → Сеть»."
