"""Конфиг: значения по умолчанию + ~/Library/Application Support/TimeTracker/config.json."""
import copy
import json
import os

SUPPORT_DIR = os.environ.get("TT_HOME") or os.path.expanduser("~/Library/Application Support/TimeTracker")
CONFIG_PATH = os.path.join(SUPPORT_DIR, "config.json")
DB_PATH = os.path.join(SUPPORT_DIR, "tt.db")
LOG_DIR = os.path.join(SUPPORT_DIR, "logs")
APP_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

DEFAULTS = {
    "collector": {
        "interval_sec": 10,
        "idle_threshold_sec": 300,
        "browser_urls": True,
        "calendar": True,
        "ignore_bundles": [],
        "redact_title_patterns": [],
        "mic_ignore_bundles": [],
    },
    "day": {
        # День считается с 04:00 до 04:00, чтобы ночная работа не делилась пополам.
        "start_hour": 4,
        # ISO-номера дней недели: 1 = пн … 7 = вс.
        "workdays": [1, 2, 3, 4, 5],
    },
    "segment": {
        "slot_min": 5,
        "min_active_sec": 60,
        "calendar_fills_idle": True,
        # Мелочь без задачи (активность за день короче, без подсказки: Telegram на пару минут и т. п.)
        # сама делится между задачами дня. 0 — выключить.
        "auto_split_min": 15,
    },
    "worklog": {
        "round_min": 15,
        # Конкретная задача логируется отдельно (минимум одна единица округления), даже если коротка.
        # Короче min_min — это случайные мелькания: их время делится между задачами дня.
        "min_min": 5,
    },
    "jira": {
        "base_url": "",  # адрес вашей Jira вводится при подключении
        "username": "",
        # pat — Personal Access Token (Bearer), basic — логин+пароль.
        "auth": "pat",
        # jira — нативный /rest/api/2/issue/{key}/worklog (Tempo DC его видит),
        # tempo — /rest/tempo-timesheets/4/worklogs (если обязательны атрибуты Tempo).
        "mode": "jira",
        "tempo_attributes": {},
        "issue_jql": (
            "(assignee = currentUser() AND resolution = Unresolved) "
            "OR (worklogAuthor = currentUser() AND worklogDate >= -45d) "
            "ORDER BY updated DESC"
        ),
        "project_keys": [],
    },
    # Правила: первое совпадение побеждает. Поля if: app, bundle, title, url, label, any — regex.
    # Результат: "issue": "KEY-1" или "category": "personal" (не логировать).
    "rules": [],
    "personal_bundles": [
        "com.spotify.client", "com.valvesoftware.steam", "org.qbittorrent.qBittorrent",
        "org.videolan.vlc", "ru.yandex.desktop.music", "com.apple.Music", "com.apple.TV",
    ],
    "reminders": [
        {"days": ["mon", "tue", "wed", "thu", "fri"], "time": "10:00", "review": "previous_workday"},
    ],
    # dialog — окно с кнопками, notification — тихое уведомление в углу.
    "reminder_style": "dialog",
    "snooze_min": 60,
    "llm": {
        "enabled": True,
        "command": "~/.local/bin/claude",
        "model": "sonnet",
        # Фоновый вызов Claude при утреннем напоминании (тратит токены). По умолчанию — только по кнопке.
        "pre_classify_on_remind": False,
        # Подсказки Claude с уверенностью не ниже порога сразу попадают в черновик
        # (до отправки вы всё равно их видите).
        "auto_accept": 0.8,
        "timeout_sec": 180,
        # Кнопка «Подобрать локально»: Ollama + embedding-модель на этом Маке, без Claude и токенов.
        # Ollama запускается только на время подбора. min_score — порог похожести, margin — отрыв от второй задачи.
        "local": {"url": "http://127.0.0.1:11434", "model": "bge-m3", "min_score": 0.55, "margin": 0.02},
    },
    "claude": {
        # Время в Claude Code раскладывается по сессиям (проект/ветка/название сессии).
        "sessions": True,
        # Пока Claude работает, а вы ждёте без клавиатуры, время считается работой (не дольше cap).
        "count_waiting": True,
        "waiting_cap_min": 30,
    },
    "server": {"port": 8765, "idle_exit_min": 30},
    # Где лежат новые версии: релизы этого репозитория GitHub. auto — раз в день спрашивать о новой версии
    # (выключается в настройках); иначе только по кнопке «Проверить обновления».
    "updates": {"repo": "oemshanov/65-tracker", "auto": True},
    "retention_days": 365,
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    return cfg


def save_user(patch):
    """Точечно обновляет config.json (только переданные ключи)."""
    data = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    _merge(data, patch)
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
