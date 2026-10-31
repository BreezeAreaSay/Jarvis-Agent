"""Окна, которые ИИ не видит: менеджеры паролей, окна входа Windows и программы из списка человека
(settings.ai.private_apps — имя программы «keepass» / «keepass.exe» или слово из заголовка окна).

Агент такие окна не смотрит и в них не работает (ни элементов, ни снимка); снимок всего экрана, на котором
видно такое окно, не делается — только окно задачи; «что на экране?» с таким окном впереди — без снимка.
"""

import re
from collections.abc import Iterable
from typing import Any

from jarvis.platform.base import WindowInfo

PASSWORD_APPS = frozenset(
    {
        "keepass.exe",
        "keepassxc.exe",
        "1password.exe",
        "bitwarden.exe",
        "lastpass.exe",
        "dashlane.exe",
        "enpass.exe",
        "roboform.exe",
        "keeper.exe",
        "keeperpasswordmanager.exe",
        "nordpass.exe",
        "protonpass.exe",
        "passwordsafe.exe",
        "pwsafe.exe",
        "kaspersky password manager.exe",
        "credentialuibroker.exe",  # окно «Безопасность Windows» с вводом пароля
        "consent.exe",  # «Разрешить этому приложению вносить изменения?»
    }
)
PASSWORD_TITLES = re.compile(
    r"\b(keepass(?:xc)?|1password|bitwarden|lastpass|dashlane|enpass|roboform|nordpass|proton pass|"
    r"keeper security|password safe|менеджер паролей|password manager|kaspersky password)\b",
    re.I,
)
PASSWORD_REASON = "менеджер паролей или окно ввода пароля"
SCREEN_NOTE = "(Весь экран не снят: на нём видно окно, скрытое от ИИ, — показано только окно задачи.)"


def _names(items: Iterable[str]) -> list[str]:
    return [s for s in (str(i).strip().casefold() for i in items or []) if s]


def private_reason(window: WindowInfo | None, extra: Iterable[str] = ()) -> str:
    """Почему окно скрыто от ИИ («менеджер паролей…», «в списке «Не показывать ИИ»: …»); не скрыто — ""."""
    if window is None:
        return ""
    exe = (window.exe or "").casefold()
    title = (window.title or "").casefold()
    if exe in PASSWORD_APPS or PASSWORD_TITLES.search(title):
        return PASSWORD_REASON
    for name in _names(extra):
        program = name if name.endswith(".exe") else f"{name}.exe"
        if exe == program or (len(name) >= 3 and name in title):
            return f"в списке «Не показывать ИИ»: {name}"
    return ""


def _overlaps(rect: tuple[int, int, int, int], area: tuple[int, int, int, int] | None) -> bool:
    if area is None:
        return True
    return rect[0] < area[2] and area[0] < rect[2] and rect[1] < area[3] and area[1] < rect[3]


def visible_private(
    windows: Iterable[WindowInfo], extra: Iterable[str] = (), area: tuple[int, int, int, int] | None = None
) -> list[tuple[WindowInfo, str]]:
    """Скрытые от ИИ окна, которые видно на экране (не свёрнуты, есть размер; area — только на этой части)."""
    found: list[tuple[WindowInfo, str]] = []
    names = _names(extra)
    for window in windows:
        rect = window.rect
        if window.minimized or rect[2] - rect[0] < 20 or rect[3] - rect[1] < 20 or not _overlaps(rect, area):
            continue
        reason = private_reason(window, names)
        if reason:
            found.append((window, reason))
    return found


def settings_list(ctx: Any) -> list[str]:
    return list(getattr(ctx.settings.value.ai, "private_apps", []) or [])


def hidden_text(window: WindowInfo, reason: str) -> str:
    """Что сказать модели вместо снимка скрытого окна."""
    name = window.exe or "программы"
    return (
        f"Окно {name} скрыто от ИИ ({reason}): его содержимое не показывается, и работать в нём нельзя. "
        "Нужно что-то в этом окне — попроси человека (ask_user) или переключись на другое окно "
        "(focus_window)."
    )


def shown_title(window: WindowInfo, extra: Iterable[str] = ()) -> str:
    """Заголовок окна для модели: у скрытого от ИИ — пометка вместо заголовка (в нём бывает имя записи
    менеджера паролей или сайта)."""
    return "(окно скрыто от ИИ)" if private_reason(window, extra) else (window.title or "")
