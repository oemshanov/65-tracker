"""Иконки приложений для таймлайна: берутся из самих приложений на этом Маке, без интернета.

Figma, Claude, Chrome, Telegram, Телемост — из их .icns через `sips`; Jira — favicon вашего сервера Jira.
Результат кешируется в ~/Library/Application Support/TimeTracker/icons.
"""
import os
import plistlib
import re
import subprocess
import urllib.request

from . import config

DIR = os.path.join(config.SUPPORT_DIR, "icons")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,120}$")


def _none(path):
    open(path + ".none", "w").close()  # не искать заново при каждом открытии таймлайна
    return None


def app_icon(bundle):
    """Путь к PNG 64×64 иконки приложения или None."""
    if not bundle or not BUNDLE_RE.match(bundle):
        return None
    os.makedirs(DIR, exist_ok=True)
    out = os.path.join(DIR, bundle + ".png")
    if os.path.exists(out):
        return out
    if os.path.exists(out + ".none"):
        return None
    try:
        found = subprocess.run(["mdfind", "kMDItemCFBundleIdentifier == '%s'" % bundle],
                               capture_output=True, text=True, timeout=5).stdout.split("\n")
        app = next((p for p in found if p.endswith(".app")), None)
        if not app:
            return _none(out)
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        name = info.get("CFBundleIconFile") or info.get("CFBundleIconName") or ""
        icns = os.path.join(app, "Contents", "Resources", name if name.endswith(".icns") else name + ".icns")
        if not name or not os.path.exists(icns):
            return _none(out)
        r = subprocess.run(["sips", "-s", "format", "png", "-Z", "64", icns, "--out", out],
                           capture_output=True, timeout=10)
        return out if r.returncode == 0 and os.path.exists(out) else _none(out)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return _none(out)


def jira_icon(base_url):
    if not base_url:
        return None
    os.makedirs(DIR, exist_ok=True)
    out = os.path.join(DIR, "jira.ico")
    if os.path.exists(out):
        return out
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/favicon.ico", timeout=10) as r:
            data = r.read()
        if data:
            with open(out, "wb") as f:
                f.write(data)
            return out
    except OSError:
        pass
    return None
