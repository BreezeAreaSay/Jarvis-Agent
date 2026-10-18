"""Инвентарь программ: разбор Get-StartApps, правила ярлыков, поиск по имени, кэш (чистый код — на Linux)."""

import json
import threading
import time
from pathlib import Path

import pytest

from jarvis.platform.base import AppInfo, PlatformError
from jarvis.platform.windows import apps as a

PF = r"C:\Program Files"
LOCAL = r"C:\Users\me\AppData\Local"
ROAMING = r"C:\Users\me\AppData\Roaming"

INVENTORY = [
    AppInfo("Google Chrome", "Chrome", rf"{PF}\Google\Chrome\Application\chrome.exe"),
    AppInfo("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "", "uwp"),
    AppInfo("Telegram", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Telegram Desktop\Telegram.exe", ""),
    AppInfo(
        "Visual Studio Code", "Microsoft.VisualStudioCode", rf"{LOCAL}\Programs\Microsoft VS Code\Code.exe"
    ),
    AppInfo("Word", "Microsoft.Office.WINWORD.EXE.15", ""),
    AppInfo("WordPad", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Windows NT\Accessories\wordpad.exe", ""),
    AppInfo("Калькулятор", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "", "uwp"),
    AppInfo(
        "Блокнот", r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\notepad.exe", r"C:\Windows\System32\notepad.exe"
    ),
    AppInfo("OBS Studio", r"{6D809377-6AF0-444B-8957-A3773F02200E}\obs-studio\bin\64bit\obs64.exe", ""),
    AppInfo("Discord", "com.squirrel.Discord.Discord", rf"{LOCAL}\Discord\Update.exe"),
    AppInfo("Paint", "Microsoft.Paint_8wekyb3d8bbwe!App", "", "uwp"),
    AppInfo("Paint 3D", "Microsoft.MSPaint_8wekyb3d8bbwe!Microsoft.MSPaint", "", "uwp"),
    AppInfo("Яндекс Музыка", "ru.yandex.desktop.music", rf"{LOCAL}\Programs\YandexMusic\Яндекс Музыка.exe"),
    AppInfo("Steam", r"{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\Steam\steam.exe", ""),
    AppInfo("Notepad++", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Notepad++\notepad++.exe", ""),
]


def _name(query: str) -> str | None:
    app = a.resolve_app(query, INVENTORY)
    return app.name if app else None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Google Chrome.exe", "google chrome"),
        ("  «Telegram»  ", "telegram"),
        ("Notepad++", "notepad plus plus"),
        ("Ёлка", "елка"),
        ("Adobe® Photoshop™ 2026", "adobe photoshop 2026"),
        ("VLC media player", "vlc media player"),
        ("7-Zip File Manager", "7 zip file manager"),
        ("ярлык.lnk", "ярлык"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert a.normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("chrome", "Google Chrome"),
        ("Chrome", "Google Chrome"),
        ("google chrome", "Google Chrome"),
        ("google chrome.exe", "Google Chrome"),
        ("chrome.exe", "Google Chrome"),
        ("spotify", "Spotify"),
        ("SPOTIFY", "Spotify"),
        ("spotfy", "Spotify"),  # опечатка
        ("telegram", "Telegram"),
        ("vs code", "Visual Studio Code"),
        ("code", "Visual Studio Code"),
        ("visual studio code", "Visual Studio Code"),
        ("word", "Word"),
        ("wordpad", "WordPad"),
        ("obs", "OBS Studio"),
        ("obs64", "OBS Studio"),
        ("калькулятор", "Калькулятор"),
        ("calculator", "Калькулятор"),
        ("блокнот", "Блокнот"),
        ("notepad", "Блокнот"),
        ("notepad++", "Notepad++"),
        ("paint", "Paint"),
        ("paint 3d", "Paint 3D"),
        ("discord", "Discord"),
        ("яндекс музыка", "Яндекс Музыка"),
        ("steam", "Steam"),
    ],
)
def test_resolve_app(query: str, expected: str) -> None:
    assert _name(query) == expected


@pytest.mark.parametrize("query", ["", "   ", "совсем другая программа", "zzzz", "photoshop"])
def test_resolve_app_misses(query: str) -> None:
    assert _name(query) is None


def test_resolve_prefers_start_menu_and_short_names() -> None:
    apps = [
        AppInfo("Chrome Helper", r"C:\x\chromehelper.lnk", "", "custom"),
        AppInfo("Google Chrome", "Chrome", r"C:\c\chrome.exe"),
    ]
    assert a.resolve_app("chrome", apps) is apps[1]


@pytest.mark.parametrize(
    ("name", "exe"),
    [
        ("Google Chrome", "chrome.exe"),
        ("Word", "winword.exe"),
        ("Discord", "discord.exe"),  # Update.exe Squirrel — не имя программы
        ("OBS Studio", "obs64.exe"),
        ("Spotify", "spotify.exe"),  # пакет Store: имя процесса — из таблицы известных
        ("Telegram", "telegram.exe"),
        ("Калькулятор", "calculatorapp.exe"),
    ],
)
def test_app_exe(name: str, exe: str) -> None:
    app = next(x for x in INVENTORY if x.name == name)
    assert a.app_exe(app) == exe


def test_exe_candidates() -> None:
    assert "spotify.exe" in a.exe_candidates("spotify")
    assert "spotify.exe" in a.exe_candidates("Spotify.exe")
    assert "chrome.exe" in a.exe_candidates("Google Chrome")
    assert "chrome.exe" in a.exe_candidates("chrome")
    assert "obs64.exe" in a.exe_candidates(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe")
    assert "code.exe" in a.exe_candidates("vs code")
    assert "winword.exe" in a.exe_candidates("word", INVENTORY)
    assert "telegram.exe" in a.exe_candidates("Telegram", INVENTORY)
    assert a.exe_candidates("") == set()


# --- Get-StartApps -------------------------------------------------------------------------------------


def test_parse_start_apps_array() -> None:
    raw = json.dumps(
        [
            {"Name": "Google Chrome", "AppID": "Chrome"},
            {"Name": "Калькулятор", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
            {"Name": "Uninstall Spotify", "AppID": r"C:\Users\me\AppData\Roaming\Spotify\uninstall.exe"},
            {"Name": "Spotify Website", "AppID": "https://www.spotify.com/"},
            {"Name": "Readme", "AppID": r"C:\x\readme.txt"},
            {"Name": "Google Chrome", "AppID": "Chrome"},
            {"Name": "", "AppID": "Empty"},
            {"Name": "No id"},
            "not a dict",
        ],
        ensure_ascii=False,
    ).encode("utf-8")
    assert a.parse_start_apps(raw) == [
        ("Google Chrome", "Chrome"),
        ("Калькулятор", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    ]


def test_parse_start_apps_single_object_bom_and_empty() -> None:
    raw = "\ufeff" + json.dumps({"Name": "Telegram", "AppID": "TelegramDesktop"}) + "\r\n"
    assert a.parse_start_apps(raw.encode("utf-8")) == [("Telegram", "TelegramDesktop")]
    assert a.parse_start_apps(raw) == [("Telegram", "TelegramDesktop")]
    assert a.parse_start_apps(b"") == []
    assert a.parse_start_apps(b"  \r\n") == []
    assert a.parse_start_apps(b"[]") == []
    with pytest.raises(ValueError):
        a.parse_start_apps(b"{oops")


# --- ярлыки --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "target", "ok"),
    [
        ("Google Chrome", rf"{PF}\Google\Chrome\Application\chrome.exe", True),
        ("Подключение к удаленному рабочему столу", r"C:\Windows\system32\mstsc.exe", True),
        ("Uninstall OBS", rf"{PF}\obs-studio\uninstall.exe", False),
        ("OBS", rf"{PF}\obs-studio\unins000.exe", False),
        ("Удалить Telegram", rf"{ROAMING}\Telegram Desktop\unins000.exe", False),
        ("Справка", r"C:\x\help.chm", False),
        ("7-Zip Help", r"C:\x\7-zip.chm", False),
        ("Сайт программы", r"C:\x\site.url", False),
        ("Лицензия", r"C:\x\license.rtf", False),
        ("Release Notes", r"C:\x\notes.exe", False),
        ("Документация", r"C:\x\docs.pdf", False),
        ("Установщик", r"C:\Windows\System32\msiexec.exe", False),
        ("Папка программы", rf"{PF}\App", False),
        ("Пустой ярлык", "", False),
    ],
)
def test_shortcut_rules(name: str, target: str, ok: bool) -> None:
    assert a.shortcut_ok(a.Shortcut(name, rf"C:\Start Menu\{name}.lnk", target)) is ok


def test_split_guid_path() -> None:
    assert a.split_guid_path(r"{6D809377-6AF0-444B-8957-A3773F02200E}\Mozilla Firefox\firefox.exe") == (
        "6D809377-6AF0-444B-8957-A3773F02200E",
        r"Mozilla Firefox\firefox.exe",
    )
    assert a.split_guid_path("Chrome") is None
    assert a.split_guid_path(r"C:\x\y.exe") is None


def test_merge_inventory() -> None:
    start = [
        ("Google Chrome", "Chrome"),
        ("Firefox", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Mozilla Firefox\firefox.exe"),
        ("Калькулятор", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
        ("Uninstall Firefox", r"{6D809377-6AF0-444B-8957-A3773F02200E}\Mozilla Firefox\uninstall\helper.exe"),
    ]
    shortcuts = [
        a.Shortcut(
            "Google Chrome", r"C:\SM\Google Chrome.lnk", rf"{PF}\Google\Chrome\Application\chrome.exe"
        ),
        a.Shortcut("Telegram", r"C:\SM\Telegram.lnk", rf"{ROAMING}\Telegram Desktop\Telegram.exe"),
        a.Shortcut("Uninstall Telegram", r"C:\SM\Uninstall Telegram.lnk", rf"{ROAMING}\TD\unins000.exe"),
        a.Shortcut("Readme", r"C:\SM\Readme.lnk", r"C:\x\readme.txt"),
        a.Shortcut("Telegram", r"C:\SM2\Telegram.lnk", rf"{ROAMING}\Telegram Desktop\Telegram.exe"),
    ]

    def resolve(app_id: str) -> str:
        return rf"{PF}\Mozilla Firefox\firefox.exe" if "firefox.exe" in app_id else ""

    apps = a.merge_inventory(start, shortcuts, resolve)
    assert [(x.name, x.source) for x in apps] == [
        ("Google Chrome", "start"),
        ("Firefox", "start"),
        ("Калькулятор", "uwp"),
        ("Telegram", "start"),
    ]
    chrome, firefox, calc, telegram = apps
    assert chrome.app_id == "Chrome" and chrome.path.endswith("chrome.exe")  # путь — из ярлыка
    assert firefox.path == rf"{PF}\Mozilla Firefox\firefox.exe"  # путь — из AppID
    assert calc.path == ""
    assert telegram.app_id == r"C:\SM\Telegram.lnk"  # только ярлык: запускается сам ярлык


def test_squirrel_exe(tmp_path: Path) -> None:
    root = tmp_path / "Discord"
    for version in ("app-1.0.9", "app-1.0.10", "packages"):
        (root / version).mkdir(parents=True)
    (root / "app-1.0.9" / "Discord.exe").write_bytes(b"")
    (root / "app-1.0.10" / "Discord.exe").write_bytes(b"")

    def exists(path: str) -> bool:
        return Path(path.replace("\\", "/")).is_file()

    target = str(root / "Update.exe")
    found = a.squirrel_exe(target, "--processStart Discord.exe", exists)
    assert found.replace("\\", "/").endswith("app-1.0.10/Discord.exe")
    assert a.squirrel_exe(target, '--processStart "Discord.exe" --flag', exists).endswith("Discord.exe")
    assert a.squirrel_exe(target, "", exists) == target
    assert a.squirrel_exe(r"C:\x\other.exe", "--processStart Discord.exe", exists) == r"C:\x\other.exe"


@pytest.mark.parametrize(
    ("target", "kind"),
    [
        ("https://youtube.com", "url"),
        ("ms-settings:display", "url"),
        ("steam://rungameid/570", "url"),
        ("mailto:me@example.com", "url"),
        (r"shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "shell"),
        ("shell:Downloads", "shell"),
        (r"C:\Program Files\obs-studio\bin\64bit\obs64.exe", "path"),
        (r'"C:\Program Files\App\app.exe"', "path"),
        ("C:/Users/me/file.txt", "path"),
        (r"\\server\share\x.exe", "path"),
        (r"%APPDATA%\Telegram Desktop\Telegram.exe", "path"),
        (r"~\Desktop", "path"),
        ("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "appid"),
        (r"{6D809377-6AF0-444B-8957-A3773F02200E}\Mozilla Firefox\firefox.exe", "appid"),
        ("notepad.exe", "exe"),
        ("Code.exe", "exe"),
        ("Spotify", "name"),
        ("Google Chrome", "name"),
        ("chrome", "name"),
        ("калькулятор", "name"),
    ],
)
def test_classify_target(target: str, kind: str) -> None:
    assert a.classify_target(target) == kind


# --- кэш и инвентарь -----------------------------------------------------------------------------------


class Scanner:
    def __init__(self, apps: list[AppInfo] | None = None, error: Exception | None = None) -> None:
        self.apps = list(apps if apps is not None else INVENTORY)
        self.error = error
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self) -> list[AppInfo]:
        with self.lock:
            self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.apps)


def test_cache_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "apps.json"
    a.write_cache(path, INVENTORY, 123.0)
    cached = a.read_cache(path)
    assert cached is not None
    apps, updated = cached
    assert apps == INVENTORY and updated == 123.0
    assert not list(path.parent.glob("*.tmp"))
    path.write_text(json.dumps({"version": 999, "apps": []}), encoding="utf-8")
    assert a.read_cache(path) is None
    path.write_text("{oops", encoding="utf-8")
    assert a.read_cache(path) is None
    assert a.read_cache(tmp_path / "missing.json") is None


def test_inventory_builds_once_then_reads_cache(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    scanner = Scanner()
    inv = a.AppInventory(lambda: path, scanner)
    assert inv.peek() == []  # ничего не известно и ничего не строится
    assert inv.get() == INVENTORY
    assert scanner.calls == 1 and path.exists()
    assert inv.get() == INVENTORY and scanner.calls == 1  # из памяти

    other_scanner = Scanner([AppInfo("X", "x.exe")])
    again = a.AppInventory(lambda: path, other_scanner)
    assert again.get() == INVENTORY and other_scanner.calls == 0  # из кэша, мгновенно
    assert again.get(refresh=True) == [AppInfo("X", "x.exe")]
    assert other_scanner.calls == 1
    assert a.read_cache(path) is not None and a.read_cache(path)[0] == [AppInfo("X", "x.exe")]  # type: ignore[index]


def test_inventory_failure_keeps_old_cache(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    a.write_cache(path, INVENTORY, time.time())
    inv = a.AppInventory(lambda: path, Scanner(error=RuntimeError("boom")))
    with pytest.raises(PlatformError):
        inv.get(refresh=True)
    assert inv.get() == INVENTORY
    empty = a.AppInventory(lambda: tmp_path / "none.json", Scanner([]))
    with pytest.raises(PlatformError):
        empty.get()


def test_stale_cache_refreshes_in_background(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    a.write_cache(path, INVENTORY, time.time() - a.STALE_S - 10)
    scanner = Scanner([AppInfo("Fresh", "fresh.exe")])
    finished: list[bool] = []

    def init() -> object:
        return lambda: finished.append(True)

    inv = a.AppInventory(lambda: path, scanner, init)  # type: ignore[arg-type]
    assert inv.get() == INVENTORY  # сразу — старый кэш
    assert inv._bg is not None
    inv._bg.join(5)
    assert scanner.calls == 1 and finished == [True]
    assert inv.get() == [AppInfo("Fresh", "fresh.exe")]


def test_resolve_refreshes_on_miss_but_not_too_often(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    a.write_cache(path, INVENTORY, time.time())
    scanner = Scanner([*INVENTORY, AppInfo("Obsidian", "md.obsidian", "", "start")])
    inv = a.AppInventory(lambda: path, scanner)
    assert inv.resolve("chrome") is not None and scanner.calls == 0
    found = inv.resolve("obsidian")  # промах по кэшу → обновление → нашлась
    assert found is not None and found.name == "Obsidian" and scanner.calls == 1
    assert inv.resolve("совсем неизвестная") is None
    assert scanner.calls == 1  # только что обновляли — не чаще раза в MISS_REFRESH_S


def test_resolve_without_any_inventory_returns_none(tmp_path: Path) -> None:
    inv = a.AppInventory(lambda: tmp_path / "apps.json", Scanner(error=RuntimeError("no powershell")))
    assert inv.resolve("chrome") is None


def test_concurrent_refresh_builds_once(tmp_path: Path) -> None:
    gate = threading.Event()

    class Slow(Scanner):
        def __call__(self) -> list[AppInfo]:
            gate.wait(5)
            return super().__call__()

    scanner = Slow()
    inv = a.AppInventory(lambda: tmp_path / "apps.json", scanner)
    results: list[list[AppInfo]] = []
    threads = [threading.Thread(target=lambda: results.append(inv.refresh())) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(results) == 3 and all(r == INVENTORY for r in results)
    assert scanner.calls == 1
