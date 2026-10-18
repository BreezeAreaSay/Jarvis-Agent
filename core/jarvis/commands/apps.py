"""Программы: известные приложения с русскими названиями («телега», «спотик», «вс код»), список из меню «Пуск»
и поиск программы по сказанному названию (точно, нечётко и по транслиту: «фотошоп» ≈ «Photoshop»).
"""

import ntpath
import threading
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz, process

from jarvis.commands.textutil import normalize, phonetic
from jarvis.platform.base import AppInfo


@dataclass(frozen=True)
class KnownApp:
    key: str
    name: str  # как показываем: «Google Chrome»
    aliases: tuple[str, ...]  # как говорят; во фразы встроенных команд идут все, кроме no_phrases
    exe: str = ""  # процесс — закрыть, переключиться, свернуть
    launch: str = ""  # запуск, если программы нет в «Пуске»: exe из App Paths или ссылка приложения
    icon: str = ""
    match: tuple[str, ...] = ()  # как программа называется в «Пуске»: часть названия, «=имя» — целиком
    kind: str = "app"  # browser | messenger | media | game | creative | dev | office | system | tool
    no_phrases: tuple[str, ...] = ()


def _k(
    key: str,
    name: str,
    aliases: str,
    *,
    exe: str = "",
    launch: str = "",
    icon: str = "",
    match: str = "",
    kind: str = "app",
    no_phrases: str = "",
) -> KnownApp:
    return KnownApp(
        key,
        name,
        tuple(a.strip() for a in aliases.split("|") if a.strip()),
        exe,
        launch,
        icon or key,
        tuple(m.strip() for m in match.split("|") if m.strip()),
        kind,
        tuple(a.strip() for a in no_phrases.split("|") if a.strip()),
    )


KNOWN_APPS: tuple[KnownApp, ...] = (
    # браузеры
    _k(
        "chrome",
        "Google Chrome",
        "хром|гугл хром|google chrome|chrome|хроме",
        exe="chrome.exe",
        launch="chrome.exe",
        match="google chrome|=chrome",
        kind="browser",
    ),
    _k(
        "yandex_browser",
        "Яндекс Браузер",
        "яндекс браузер|браузер яндекс|яндекс браузера|yandex browser|яндекс",
        exe="browser.exe",
        launch="browser.exe",
        icon="yandex",
        match="яндекс браузер|yandex browser|=yandex|=яндекс",
        kind="browser",
        no_phrases="яндекс",
    ),
    _k(
        "opera",
        "Opera",
        "опера|оперу|opera|браузер опера",
        exe="opera.exe",
        launch="opera.exe",
        match="opera browser|=opera",
        kind="browser",
    ),
    _k(
        "opera_gx",
        "Opera GX",
        "опера gx|опера гх|опера джи икс|opera gx",
        exe="opera.exe",
        icon="opera",
        match="opera gx",
        kind="browser",
    ),
    _k(
        "firefox",
        "Mozilla Firefox",
        "фаерфокс|файрфокс|фаер фокс|firefox|мозилла|мозила|мазила",
        exe="firefox.exe",
        launch="firefox.exe",
        match="firefox",
        kind="browser",
    ),
    _k(
        "edge",
        "Microsoft Edge",
        "эдж|едж|edge|майкрософт эдж|microsoft edge",
        exe="msedge.exe",
        launch="msedge.exe",
        match="microsoft edge|=edge",
        kind="browser",
    ),
    _k(
        "brave",
        "Brave",
        "брейв|brave|браузер брейв",
        exe="brave.exe",
        launch="brave.exe",
        match="brave",
        kind="browser",
    ),
    # общение
    _k(
        "telegram",
        "Telegram",
        "телеграм|телеграмм|телега|телегу|тележку|тг|telegram",
        exe="Telegram.exe",
        launch="tg://",
        match="telegram",
        kind="messenger",
    ),
    _k(
        "discord",
        "Discord",
        "дискорд|дискорда|дс|discord",
        exe="Discord.exe",
        launch="discord://",
        match="discord",
        kind="messenger",
    ),
    _k(
        "whatsapp",
        "WhatsApp",
        "ватсап|вотсап|вацап|воцап|whatsapp",
        exe="WhatsApp.exe",
        launch="whatsapp://",
        match="whatsapp",
        kind="messenger",
    ),
    _k("viber", "Viber", "вайбер|viber", exe="Viber.exe", launch="viber://", match="viber", kind="messenger"),
    _k("skype", "Skype", "скайп|skype", exe="Skype.exe", launch="skype:", match="skype", kind="messenger"),
    _k("zoom", "Zoom", "зум|zoom", exe="Zoom.exe", launch="zoommtg://", match="zoom", kind="messenger"),
    _k(
        "teams",
        "Microsoft Teams",
        "тимс|майкрософт тимс|teams|microsoft teams",
        exe="ms-teams.exe",
        launch="msteams:",
        match="teams",
        kind="messenger",
    ),
    _k(
        "slack",
        "Slack",
        "слак|слэк|slack",
        exe="slack.exe",
        launch="slack://",
        match="slack",
        kind="messenger",
    ),
    _k(
        "vk_messenger",
        "VK Мессенджер",
        "вк мессенджер|мессенджер вк|вк месседжер|vk messenger",
        icon="vk",
        match="vk messenger|vk мессенджер|мессенджер vk",
        kind="messenger",
    ),
    _k(
        "teamspeak",
        "TeamSpeak",
        "тим спик|тимспик|тимспик 3|teamspeak",
        exe="ts3client_win64.exe",
        launch="ts3server://",
        match="teamspeak",
        kind="messenger",
    ),
    # музыка и видео
    _k(
        "spotify",
        "Spotify",
        "спотифай|спотик|спотифи|спотифая|спотифае|spotify",
        exe="Spotify.exe",
        launch="spotify:",
        match="spotify",
        kind="media",
    ),
    _k(
        "yandex_music",
        "Яндекс Музыка",
        "яндекс музыка|яндекс музыку|яндекс музыки|yandex music",
        exe="Яндекс Музыка.exe",
        launch="yandexmusic://",
        icon="yandex",
        match="яндекс музыка|yandex music",
        kind="media",
    ),
    _k(
        "vlc",
        "VLC",
        "влс|влц|ви эл си|vlc|vlc плеер|влс плеер",
        exe="vlc.exe",
        launch="vlc.exe",
        match="vlc",
        kind="media",
    ),
    _k("aimp", "AIMP", "аимп|аимп плеер|aimp", exe="AIMP.exe", match="aimp", kind="media"),
    _k("foobar", "foobar2000", "фубар|foobar|foobar2000", exe="foobar2000.exe", match="foobar", kind="media"),
    # игры и лаунчеры
    _k(
        "steam",
        "Steam",
        "стим|стима|steam",
        exe="steam.exe",
        launch="steam://open/main",
        match="=steam",
        kind="game",
    ),
    _k(
        "epic",
        "Epic Games Launcher",
        "эпик геймс|эпик|епик геймс|epic games|эпик гейм",
        exe="EpicGamesLauncher.exe",
        launch="com.epicgames.launcher://",
        match="epic games",
        kind="game",
    ),
    _k(
        "battlenet",
        "Battle.net",
        "батл нет|батлнет|баттл нет|battle net|battle.net",
        exe="Battle.net.exe",
        launch="battlenet://",
        match="battle.net|battle net",
        kind="game",
    ),
    _k(
        "ea_app",
        "EA app",
        "еа апп|иа апп|ea app|ориджин|origin",
        exe="EADesktop.exe",
        match="ea app|=ea|origin",
        kind="game",
    ),
    _k(
        "ubisoft",
        "Ubisoft Connect",
        "юбисофт|убисофт|ubisoft connect|юплей|uplay",
        exe="upc.exe",
        launch="uplay://",
        match="ubisoft connect|uplay",
        kind="game",
    ),
    _k(
        "gog",
        "GOG GALAXY",
        "гог|гог галакси|gog galaxy|gog",
        exe="GalaxyClient.exe",
        launch="goggalaxy://",
        match="gog galaxy",
        kind="game",
    ),
    _k(
        "minecraft",
        "Minecraft Launcher",
        "майнкрафт|майн|minecraft|лаунчер майнкрафта",
        exe="MinecraftLauncher.exe",
        match="minecraft",
        kind="game",
    ),
    _k(
        "roblox",
        "Roblox",
        "роблокс|roblox",
        exe="RobloxPlayerBeta.exe",
        launch="roblox://",
        match="roblox",
        kind="game",
    ),
    _k(
        "xbox",
        "Xbox",
        "иксбокс|икс бокс|xbox|приложение xbox",
        exe="XboxPcApp.exe",
        launch="xbox:",
        match="=xbox",
        kind="game",
    ),
    # творчество
    _k(
        "obs",
        "OBS Studio",
        "обс|обс студио|obs|obs studio|обску",
        exe="obs64.exe",
        match="obs studio|=obs",
        kind="creative",
    ),
    _k(
        "photoshop",
        "Adobe Photoshop",
        "фотошоп|фотошопе|photoshop|адоб фотошоп",
        exe="Photoshop.exe",
        match="photoshop",
        kind="creative",
    ),
    _k(
        "premiere",
        "Adobe Premiere Pro",
        "премьер про|премьер|премьеру|premiere pro|premiere|адоб премьер",
        exe="Adobe Premiere Pro.exe",
        match="premiere pro|premiere",
        kind="creative",
    ),
    _k(
        "after_effects",
        "Adobe After Effects",
        "афтер эффектс|афтер эффекс|афтер|after effects|афтерэффектс",
        exe="AfterFX.exe",
        match="after effects",
        kind="creative",
    ),
    _k(
        "illustrator",
        "Adobe Illustrator",
        "иллюстратор|иллюстратора|illustrator",
        exe="Illustrator.exe",
        match="illustrator",
        kind="creative",
    ),
    _k(
        "lightroom",
        "Adobe Lightroom",
        "лайтрум|лайт рум|lightroom",
        exe="Lightroom.exe",
        match="lightroom",
        kind="creative",
    ),
    _k(
        "davinci",
        "DaVinci Resolve",
        "давинчи|давинчи резолв|да винчи|davinci resolve|резолв|davinci",
        exe="Resolve.exe",
        match="davinci resolve|=resolve",
        kind="creative",
    ),
    _k("blender", "Blender", "блендер|blender", exe="blender.exe", match="blender", kind="creative"),
    _k(
        "figma",
        "Figma",
        "фигма|фигму|figma",
        exe="Figma.exe",
        launch="figma://",
        match="figma",
        kind="creative",
    ),
    _k(
        "audacity",
        "Audacity",
        "аудасити|одасити|audacity",
        exe="Audacity.exe",
        match="audacity",
        kind="creative",
    ),
    _k(
        "paint",
        "Paint",
        "паинт|пейнт|paint",
        exe="mspaint.exe",
        launch="mspaint.exe",
        match="=paint",
        kind="creative",
    ),
    # работа и код
    _k(
        "notion",
        "Notion",
        "ноушен|ноушн|notion",
        exe="Notion.exe",
        launch="notion://",
        match="notion",
        kind="office",
    ),
    _k(
        "obsidian",
        "Obsidian",
        "обсидиан|obsidian",
        exe="Obsidian.exe",
        launch="obsidian://",
        match="obsidian",
        kind="office",
    ),
    _k(
        "vscode",
        "Visual Studio Code",
        "вс код|вскод|вэ эс код|визуал студио код|вижуал студио код|vs code|vscode|visual studio code",
        exe="Code.exe",
        launch="code",
        match="visual studio code|vs code",
        kind="dev",
    ),
    _k(
        "visual_studio",
        "Visual Studio",
        "визуал студио|вижуал студио|visual studio",
        exe="devenv.exe",
        launch="devenv.exe",
        match="=visual studio|visual studio 20",
        kind="dev",
    ),
    _k("pycharm", "PyCharm", "пайчарм|пичарм|pycharm", exe="pycharm64.exe", match="pycharm", kind="dev"),
    _k(
        "intellij",
        "IntelliJ IDEA",
        "интеллидж|интеллиджей|идея|intellij idea|intellij",
        exe="idea64.exe",
        match="intellij",
        kind="dev",
    ),
    _k(
        "sublime",
        "Sublime Text",
        "саблайм|сублайм|саблайм текст|sublime text|sublime",
        exe="sublime_text.exe",
        match="sublime",
        kind="dev",
    ),
    _k(
        "notepadpp",
        "Notepad++",
        "нотпад плюс плюс|нотепад плюс плюс|ноутпад плюс плюс|notepad++",
        exe="notepad++.exe",
        launch="notepad++.exe",
        match="notepad++",
        kind="dev",
    ),
    _k(
        "word",
        "Word",
        "ворд|ворде|word|майкрософт ворд|microsoft word",
        exe="WINWORD.EXE",
        launch="winword.exe",
        match="=word|microsoft word",
        kind="office",
    ),
    _k(
        "excel",
        "Excel",
        "эксель|ексель|excel|майкрософт эксель|microsoft excel",
        exe="EXCEL.EXE",
        launch="excel.exe",
        match="=excel|microsoft excel",
        kind="office",
    ),
    _k(
        "powerpoint",
        "PowerPoint",
        "пауэрпоинт|пауэр поинт|павер поинт|повер поинт|поверпоинт|powerpoint",
        exe="POWERPNT.EXE",
        launch="powerpnt.exe",
        match="=powerpoint|microsoft powerpoint",
        kind="office",
    ),
    _k(
        "outlook",
        "Outlook",
        "аутлук|outlook",
        exe="OUTLOOK.EXE",
        launch="outlook.exe",
        match="outlook",
        kind="office",
    ),
    _k(
        "onenote",
        "OneNote",
        "ван ноут|уан ноут|onenote",
        exe="ONENOTE.EXE",
        launch="onenote.exe",
        match="onenote",
        kind="office",
    ),
    # система и утилиты
    _k(
        "notepad",
        "Блокнот",
        "блокнот|блокнотик|notepad",
        exe="notepad.exe",
        launch="notepad.exe",
        icon="note",
        match="=блокнот|=notepad",
        kind="system",
    ),
    _k(
        "calculator",
        "Калькулятор",
        "калькулятор|калькулятора|calculator|кальк",
        exe="CalculatorApp.exe",
        launch="calculator:",
        icon="system",
        match="=калькулятор|=calculator",
        kind="system",
    ),
    _k(
        "explorer",
        "Проводник",
        "проводник|эксплорер|explorer|файловый менеджер",
        exe="explorer.exe",
        launch="explorer.exe",
        match="=проводник|=file explorer|=explorer",
        kind="system",
    ),
    _k(
        "task_manager",
        "Диспетчер задач",
        "диспетчер задач|диспетчер|таск менеджер|task manager",
        exe="Taskmgr.exe",
        launch="taskmgr.exe",
        icon="system",
        match="=диспетчер задач|=task manager",
        kind="system",
    ),
    _k(
        "cmd",
        "Командная строка",
        "командная строка|командную строку|командной строки|cmd|смд",
        exe="cmd.exe",
        launch="cmd.exe",
        icon="keyboard",
        match="=командная строка|=command prompt",
        kind="system",
    ),
    _k(
        "powershell",
        "PowerShell",
        "пауэршелл|повершелл|павершелл|power shell|powershell",
        exe="powershell.exe",
        launch="powershell.exe",
        icon="keyboard",
        match="=windows powershell|=powershell",
        kind="system",
    ),
    _k(
        "terminal",
        "Терминал",
        "терминал|terminal|виндовс терминал|windows terminal",
        exe="WindowsTerminal.exe",
        launch="wt.exe",
        icon="keyboard",
        match="=терминал|=terminal|windows terminal",
        kind="system",
    ),
    _k(
        "control_panel",
        "Панель управления",
        "панель управления|панельку управления|control panel",
        launch="control",
        icon="system",
        match="=панель управления|=control panel",
        kind="system",
    ),
    _k(
        "snipping",
        "Ножницы",
        "ножницы|snipping tool",
        exe="SnippingTool.exe",
        launch="ms-screenclip:",
        icon="screenshot",
        match="=ножницы|snipping tool",
        kind="system",
    ),
    _k(
        "camera",
        "Камера",
        "камера|камеру|camera",
        exe="WindowsCamera.exe",
        launch="microsoft.windows.camera:",
        icon="system",
        match="=камера|=camera",
        kind="system",
    ),
    _k(
        "photos",
        "Фотографии",
        "фотографии|фото|галерея|photos",
        exe="Photos.exe",
        launch="ms-photos:",
        icon="system",
        match="=фотографии|=photos",
        kind="system",
    ),
    _k(
        "store",
        "Microsoft Store",
        "майкрософт стор|microsoft store|магазин приложений|стор",
        exe="WinStore.App.exe",
        launch="ms-windows-store:",
        icon="system",
        match="microsoft store",
        kind="system",
    ),
    _k(
        "qbittorrent",
        "qBittorrent",
        "кубиторрент|кьюбиторрент|qbittorrent|торрент",
        exe="qbittorrent.exe",
        match="qbittorrent",
        kind="tool",
    ),
    _k(
        "utorrent",
        "µTorrent",
        "ю торрент|юторрент|utorrent",
        exe="uTorrent.exe",
        match="utorrent|µtorrent",
        kind="tool",
    ),
    _k("winrar", "WinRAR", "винрар|winrar", exe="WinRAR.exe", match="winrar", kind="tool"),
    _k("sevenzip", "7-Zip", "севен зип|7 зип|7zip|7-zip", exe="7zFM.exe", match="7-zip", kind="tool"),
    _k(
        "anydesk",
        "AnyDesk",
        "энидеск|анидеск|anydesk|эни деск",
        exe="AnyDesk.exe",
        match="anydesk",
        kind="tool",
    ),
    _k(
        "teamviewer",
        "TeamViewer",
        "тимвьюер|тим вьюер|teamviewer",
        exe="TeamViewer.exe",
        match="teamviewer",
        kind="tool",
    ),
    _k(
        "afterburner",
        "MSI Afterburner",
        "афтербернер|msi afterburner|afterburner",
        exe="MSIAfterburner.exe",
        match="afterburner",
        kind="tool",
    ),
    _k(
        "nvidia",
        "NVIDIA App",
        "нвидиа|энвидиа|нвидия|джифорс экспириенс|nvidia",
        exe="NVIDIA app.exe",
        match="nvidia app|geforce experience",
        kind="tool",
    ),
    _k(
        "wallpaper_engine",
        "Wallpaper Engine",
        "валпейпер энджин|wallpaper engine|обои wallpaper engine",
        exe="wallpaper64.exe",
        match="wallpaper engine",
        kind="tool",
    ),
)

KNOWN_BY_KEY: dict[str, KnownApp] = {a.key: a for a in KNOWN_APPS}

_PREFIX_WORDS = frozenset(
    {"программу", "программа", "приложение", "прогу", "игру", "игра", "мой", "мою", "мое"}
)
_JUNK_WORDS = (
    "uninstall",
    "удалить",
    "удаление",
    "деинсталл",
    "readme",
    "справка",
    "help",
    "manual",
    "documentation",
    "release notes",
    "website",
    "license",
    "лицензия",
)
_VENDOR_WORDS = frozenset(
    {
        "adobe",
        "microsoft",
        "the",
        "x64",
        "x86",
        "64-bit",
        "32-bit",
        "64bit",
        "(64bit)",
        "(x64)",
        "app",
        "desktop",
        "for",
        "windows",
    }
)


@dataclass
class ResolvedApp:
    name: str  # название для людей: «Spotify», «Adobe Photoshop 2026»
    app_id: str  # чем запускать: AppID, путь, exe или ссылка приложения
    exe: str  # процесс (может быть пустым)
    icon: str
    score: float
    installed: bool
    key: str = ""  # ключ известной программы

    @property
    def target(self) -> str:
        """Чем назвать программу платформе при закрытии / переключении: процесс или название."""
        return self.exe or self.name

    def slots(self) -> dict[str, Any]:
        return {"app": self.name, "app_id": self.app_id, "exe": self.exe}


def _core_name(name: str) -> str:
    """«Adobe Photoshop 2026» → «photoshop»: без производителя, версий и разрядности."""
    words = [w for w in normalize(name).split() if w not in _VENDOR_WORDS and not w.isdigit()]
    return " ".join(words)


class AppResolver:
    """Поиск программы по сказанному названию. Список «Пуска» обновляет сервис команд (set_apps)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._apps: list[AppInfo] = []
        self.loaded_at = 0.0
        self._installed_cache: dict[str, AppInfo | None] = {}
        alias_map: dict[str, KnownApp] = {}
        for app in KNOWN_APPS:
            names = [*app.aliases, app.name, app.key.replace("_", " ")]
            if app.exe:
                names += [app.exe, app.exe.rsplit(".", 1)[0]]
            for alias in names:
                alias_map.setdefault(normalize(alias), app)
        self._alias_map = alias_map
        self._alias_list = list(alias_map)
        self._alias_phon = [phonetic(a) for a in self._alias_list]
        self._set_installed_index([])

    # --- список «Пуска» ---------------------------------------------------------------------------
    @property
    def apps(self) -> list[AppInfo]:
        return list(self._apps)

    def set_apps(self, apps: list[AppInfo], at: float = 0.0) -> None:
        clean = [a for a in apps if a.name and not any(j in a.name.casefold() for j in _JUNK_WORDS)]
        with self._lock:
            self._apps = clean
            self.loaded_at = at
            self._installed_cache = {}
            self._set_installed_index(clean)

    def _set_installed_index(self, apps: list[AppInfo]) -> None:
        exact: dict[str, int] = {}
        choices: list[str] = []
        owners: list[int] = []
        for i, app in enumerate(apps):
            for variant in {normalize(app.name), _core_name(app.name)}:
                if variant:
                    exact.setdefault(variant, i)
                    choices.append(variant)
                    owners.append(i)
        self._inst_exact = exact
        self._inst_choices = choices
        self._inst_phon = [phonetic(c) for c in choices]
        self._inst_owner = owners

    def find_installed(self, known: KnownApp) -> AppInfo | None:
        """Известная программа → её пункт в «Пуске» (по процессу, затем по названию)."""
        if known.key in self._installed_cache:
            return self._installed_cache[known.key]
        found: AppInfo | None = None
        exe = known.exe.casefold()
        if exe:
            for app in self._apps:
                if ntpath.basename(app.path).casefold() == exe or app.app_id.casefold() == exe:
                    found = app
                    break
        if found is None:
            candidates = []
            for app in self._apps:
                name = normalize(app.name)
                for pattern in known.match:
                    if (pattern[1:] == name) if pattern.startswith("=") else (pattern in name):
                        candidates.append(app)
                        break
            if candidates:
                found = min(candidates, key=lambda a: len(a.name))
        self._installed_cache[known.key] = found
        return found

    # --- поиск ------------------------------------------------------------------------------------
    def resolve(self, text: str) -> ResolvedApp | None:
        """Сказанное название → программа или None. score 100 — точно, ниже — похоже."""
        words = normalize(text).split()
        while words and words[0] in _PREFIX_WORDS:
            words = words[1:]
        if not words:
            return None
        queries = [(" ".join(words), 0.0)]
        last = words[-1]
        if len(last) > 3 and last[-1] in "ую":  # «закрой криту» → «крита», «открой студию» → «студия»
            nominative = last[:-1] + ("а" if last[-1] == "у" else "я")
            queries.append((" ".join([*words[:-1], nominative]), 1.0))
        best: tuple[float, KnownApp | AppInfo] | None = None
        for query, penalty in queries:
            found = self._resolve_query(query)
            if found is not None and (best is None or found[0] - penalty > best[0]):
                best = (found[0] - penalty, found[1])
        if best is None:
            return None
        score, item = best
        if isinstance(item, KnownApp):
            return self._from_known(item, score)
        return self._from_installed(item, score)

    def _resolve_query(self, query: str) -> tuple[float, "KnownApp | AppInfo"] | None:
        known = self._alias_map.get(query)
        if known is not None:
            return 100.0, known
        idx = self._inst_exact.get(query)
        if idx is not None:
            return 100.0, self._apps[idx]
        if len(query) <= 2:
            return None
        need = 88.0 if len(query) <= 4 else 80.0
        best: tuple[float, KnownApp | AppInfo] | None = None
        phon = phonetic(query)
        for choice_list, owner, q, penalty in (
            (self._alias_list, None, query, 0.0),
            (self._alias_phon, None, phon, 2.0),
            (self._inst_choices, self._inst_owner, query, 0.0),
            (self._inst_phon, self._inst_owner, phon, 2.0),
        ):
            if not choice_list or not q:
                continue
            hit = process.extractOne(q, choice_list, scorer=fuzz.ratio, score_cutoff=need)
            if hit is None:
                continue
            score = float(hit[1]) - penalty
            item: KnownApp | AppInfo = (
                self._alias_map[self._alias_list[hit[2]]] if owner is None else self._apps[owner[hit[2]]]
            )
            if score >= need and (best is None or score > best[0]):
                best = (score, item)
        return best

    def _from_known(self, known: KnownApp, score: float) -> ResolvedApp:
        installed = self.find_installed(known)
        if installed is not None:
            return ResolvedApp(
                installed.name, installed.app_id, known.exe, known.icon, score, True, known.key
            )
        return ResolvedApp(
            known.name,
            known.launch or known.exe or known.name,
            known.exe,
            known.icon,
            score,
            False,
            known.key,
        )

    def _from_installed(self, app: AppInfo, score: float) -> ResolvedApp:
        query = normalize(app.name)
        for known in KNOWN_APPS:
            if self.find_installed(known) is app:
                return ResolvedApp(app.name, app.app_id, known.exe, known.icon, score, True, known.key)
        exe = ntpath.basename(app.path) if app.path.casefold().endswith(".exe") else ""
        if not exe and app.app_id.casefold().endswith(".exe") and "\\" not in app.app_id:
            exe = app.app_id
        return ResolvedApp(app.name, app.app_id, exe, "", score, True, query)
