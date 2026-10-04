"""Имена приложений: ID из имени и разговорные алиасы известных программ (ADR 0030).

Алиасы — данные адаптера, а не ядра: ядро знает только понятие «браузер по умолчанию», а то, что
«хром» — это Google Chrome, знает инвентарь этого компьютера.
"""

import re

from jarvis.domain.inventory import name_key as norm

# Нормализованное имя из меню «Пуск» → разговорные имена. Совпадение по имени целиком.
KNOWN_ALIASES: dict[str, tuple[str, ...]] = {
    "google chrome": ("chrome", "хром", "гугл хром"),  # «гугл» — скорее сайт, чем браузер
    "microsoft edge": ("edge", "эдж", "едж"),
    "firefox": ("файрфокс", "фаерфокс", "мозилла", "mozilla firefox"),
    "mozilla firefox": ("firefox", "файрфокс", "фаерфокс", "мозилла"),
    "yandex": ("яндекс", "яндекс браузер", "yandex browser"),
    "yandex browser": ("яндекс", "яндекс браузер"),
    "opera": ("опера",),
    "visual studio code": ("vs code", "vscode", "вс код", "вскод", "вижуал студио код"),
    "telegram": ("телеграм", "телеграмм", "telegram desktop"),
    "telegram desktop": ("telegram", "телеграм", "телеграмм"),
    "discord": ("дискорд",),
    "steam": ("стим",),
    "spotify": ("спотифай",),
    "obs studio": ("obs", "обс"),
    "terminal": ("терминал", "windows terminal"),
    "windows terminal": ("терминал", "terminal"),
    "windows powershell": ("powershell", "повершелл", "пауэршелл"),
    "powershell": ("повершелл", "пауэршелл"),
    "command prompt": ("cmd", "командная строка", "командную строку"),
    "командная строка": ("cmd", "командную строку", "command prompt"),
    "notepad": ("блокнот",),
    "блокнот": ("notepad",),
    "notepad++": ("notepad plus plus", "нотпад"),
    "pycharm": ("пайчарм",),
    "word": ("ворд", "microsoft word"),
    "excel": ("эксель", "excel", "microsoft excel"),
    "slack": ("слак",),
    "zoom": ("зум",),
    "zoom workplace": ("zoom", "зум"),
    "obsidian": ("обсидиан",),
    "vlc media player": ("vlc", "влк"),
    "7 zip file manager": ("7zip", "7 zip", "7-zip"),
}

_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)  # fmt: skip


def aliases_for(name: str) -> list[str]:
    return list(KNOWN_ALIASES.get(norm(name), ()))


def slug(name: str) -> str:
    """ID приложения из имени: латиница, цифры, «.», «-» и «_»."""
    text = norm(name).replace("+", " plus ").translate(_TRANSLIT)  # «Notepad++» ≠ «Notepad»
    text = re.sub(r"[^a-z0-9._]+", "-", text).strip("-._")
    return text or "app"


def unique(base: str, taken: set[str]) -> str:
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    taken.add(candidate)
    return candidate
