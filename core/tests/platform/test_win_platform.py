"""WindowsPlatform целиком: соответствие протоколу, поведение без Windows и логика поверх фейков ОС.

Тесты в конце файла (skipif не Windows) идут на настоящем Windows: они ничего не ломают — громкость и буфер
обмена возвращаются как были, процессы не завершаются.
"""

import inspect
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.platform import create_platform
from jarvis.platform.base import AppInfo, MediaInfo, Platform, PlatformError, WindowInfo
from jarvis.platform.windows import WindowsPlatform
from jarvis.platform.windows import apps as appmod
from jarvis.platform.windows import keys as k
from jarvis.platform.windows import winplatform as wp
from jarvis.platform.windows.audio import NoAudioDevice

ON_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="нужен настоящий Windows")
not_windows = pytest.mark.skipif(ON_WINDOWS, reason="проверка поведения без Windows")


# --- протокол -------------------------------------------------------------------------------------------


def _protocol_methods() -> list[str]:
    return [n for n, v in vars(Platform).items() if callable(v) and not n.startswith("_")]


def test_matches_platform_protocol() -> None:
    platform = WindowsPlatform()
    assert isinstance(platform, Platform)
    assert platform.name == "windows"
    for name in _protocol_methods():
        expected = inspect.signature(getattr(Platform, name))
        actual = inspect.signature(getattr(WindowsPlatform, name))
        assert list(expected.parameters) == list(actual.parameters), name
        for param in expected.parameters.values():
            assert actual.parameters[param.name].default == param.default, (name, param.name)


def test_create_platform_windows() -> None:
    assert isinstance(create_platform("windows"), WindowsPlatform)


@not_windows
def test_without_windows_everything_is_platform_error() -> None:
    platform = WindowsPlatform()
    calls = [
        lambda: platform.press_keys("ctrl+c"),
        lambda: platform.volume_get(),
        lambda: platform.volume_set(10),
        lambda: platform.list_windows(),
        lambda: platform.clipboard_get(),
        lambda: platform.mouse_move(1, 1),
        lambda: platform.recycle_bin_count(),
        lambda: platform.idle_seconds(),
        lambda: platform.window_action("minimize"),
    ]
    for call in calls:
        with pytest.raises(PlatformError):
            call()
    assert platform.now_playing() is None
    assert platform.is_fullscreen_app() is False
    assert platform.known_folder("downloads") is None
    platform._media._worker.stop()


def test_bad_input_is_reported_before_touching_windows() -> None:
    platform = WindowsPlatform()
    with pytest.raises(PlatformError, match="«foo»"):
        platform.press_keys("ctrl+foo")
    with pytest.raises(PlatformError, match="Не указано"):
        platform.launch("  ")
    with pytest.raises(PlatformError, match="Неизвестная медиаклавиша"):
        platform.media_key("rewind")  # type: ignore[arg-type]
    with pytest.raises(PlatformError):
        platform.power("explode")  # type: ignore[arg-type]


# --- фейки ----------------------------------------------------------------------------------------------


class FakeSender:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []

    def key(self, vk: int, up: bool = False) -> tuple[int, bool]:
        return (vk, up)

    def send(self, events: list[Any]) -> int:
        self.log.append(("send", list(events)))
        return len(events)

    def send_all(self, events: list[Any], what: str = "") -> None:
        self.log.append(("send_all", list(events)))

    def release_modifiers(self, wait_s: float = 0.0, keep: Any = ()) -> None:
        self.log.append(("release", wait_s))

    def combo(self, vks: list[int]) -> None:
        self.log.append(("combo", list(vks)))

    def type_text(self, text: str) -> None:
        self.log.append(("type", text))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.log]


class FakeClipboard:
    def __init__(self, content: list[tuple[int, bytes]]) -> None:
        self.content = list(content)
        self.seq = 1
        self.sets: list[tuple[str, bool]] = []
        self.restores: list[list[tuple[int, bytes]]] = []

    def snapshot(self) -> list[tuple[int, bytes]]:
        return list(self.content)

    def set_text(self, text: str, transient: bool = False) -> int:
        self.sets.append((text, transient))
        self.content = [(13, text.encode("utf-16-le") + b"\x00\x00")]
        self.seq += 1
        return self.seq

    def sequence(self) -> int:
        return self.seq

    def restore(self, snapshot: list[tuple[int, bytes]]) -> None:
        self.restores.append(list(snapshot))
        self.content = list(snapshot)
        self.seq += 1


@pytest.fixture
def platform(tmp_path: Path) -> Iterator[WindowsPlatform]:
    p = WindowsPlatform()
    p._sender = FakeSender()  # type: ignore[assignment]
    p._apps = appmod.AppInventory(lambda: tmp_path / "apps.json", lambda: list(APPS))
    yield p
    p._media._worker.stop()


APPS = [
    AppInfo("Google Chrome", "Chrome", ""),
    AppInfo("Калькулятор", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "", "uwp"),
    AppInfo("Telegram", r"C:\SM\Telegram.lnk", r"C:\Users\me\AppData\Roaming\Telegram Desktop\Telegram.exe"),
]


def sender(p: WindowsPlatform) -> FakeSender:
    return p._sender  # type: ignore[return-value]


def wait_until(condition: Any, timeout: float = 3.0) -> bool:
    """Таймеры под нагрузкой срабатывают с опозданием: ждём условие, а не фиксированное время."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


# --- ввод -----------------------------------------------------------------------------------------------


def test_press_keys_releases_stuck_modifiers_first(platform: WindowsPlatform) -> None:
    platform.press_keys("ctrl + shift + s")
    assert sender(platform).kinds() == ["release", "combo"]
    assert sender(platform).log[-1] == ("combo", [k.VK_CONTROL, k.VK_SHIFT, ord("S")])


def test_media_keys(platform: WindowsPlatform) -> None:
    platform.media_key("next")
    platform.media_key("play_pause")
    assert sender(platform).log == [
        ("combo", [k.VK_MEDIA_NEXT_TRACK]),
        ("combo", [k.VK_MEDIA_PLAY_PAUSE]),
    ]


def test_key_down_up_and_typing(platform: WindowsPlatform) -> None:
    platform.key_down("ctrl+alt")
    platform.key_up("ctrl+alt")
    platform.type_text("")
    platform.type_text("Привет")
    log = sender(platform).log
    assert log[0] == ("send_all", [(k.VK_CONTROL, False), (k.VK_MENU, False)])
    assert log[1] == ("send_all", [(k.VK_MENU, True), (k.VK_CONTROL, True)])
    assert log[2:] == [("release", wp.inputs.MODIFIERS_WAIT_S), ("type", "Привет")]


def test_paste_text_restores_clipboard(platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wp, "PASTE_RESTORE_S", 0.05)
    cb = FakeClipboard([(13, b"o\x00l\x00d\x00\x00\x00")])
    platform._clipboard = cb  # type: ignore[assignment]
    platform.paste_text("новый текст")
    assert cb.sets == [("новый текст", True)]  # временный текст — мимо журнала буфера
    assert sender(platform).log[-1] == ("combo", [k.VK_CONTROL, ord("V")])
    assert wait_until(lambda: cb.restores)
    assert cb.restores == [[(13, b"o\x00l\x00d\x00\x00\x00")]]


def test_paste_twice_restores_original_once(
    platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wp, "PASTE_RESTORE_S", 0.2)
    original = [(13, b"x\x00\x00\x00")]
    cb = FakeClipboard(original)
    platform._clipboard = cb  # type: ignore[assignment]
    platform.paste_text("один")
    platform.paste_text("два")
    assert wait_until(lambda: cb.restores)
    time.sleep(0.3)  # второго восстановления быть не должно
    assert cb.restores == [original]  # вернулся исходный буфер человека, а не «один»


def test_paste_does_not_clobber_new_clipboard(
    platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wp, "PASTE_RESTORE_S", 0.05)
    cb = FakeClipboard([(13, b"x\x00\x00\x00")])
    platform._clipboard = cb  # type: ignore[assignment]
    platform.paste_text("текст")
    cb.seq += 1  # человек успел скопировать что-то своё
    time.sleep(0.3)
    assert cb.restores == []


# --- звук -----------------------------------------------------------------------------------------------


class FakeAudio:
    def __init__(self, error: Exception | None = None, sessions: list[tuple[int, Any]] | None = None) -> None:
        self.error = error
        self.level = 40
        self.muted = False
        self._sessions = sessions or []
        self.session_levels: list[tuple[Any, int]] = []

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    def get_level(self) -> int:
        self._check()
        return self.level

    def set_level(self, level: int, unmute: bool) -> int:
        self._check()
        self.level = level
        if unmute:
            self.muted = False
        return level

    def change_level(self, delta: int) -> int:
        self._check()
        self.level = max(0, min(100, self.level + delta))
        return self.level

    def is_muted(self) -> bool:
        self._check()
        return self.muted

    def set_muted(self, muted: bool | None) -> bool:
        self._check()
        self.muted = (not self.muted) if muted is None else muted
        return self.muted

    def sessions(self) -> list[tuple[int, Any]]:
        return self._sessions

    def set_session_level(self, control: Any, level: int) -> None:
        self.session_levels.append((control, level))


def test_volume_with_com(platform: WindowsPlatform) -> None:
    platform._audio = FakeAudio()  # type: ignore[assignment]
    assert platform.volume_get() == 40
    assert platform.volume_set(130) == 100
    assert platform.volume_change(-25) == 75
    assert platform.mute_set(None) is True
    assert platform.is_muted() is True
    assert platform.mute_set(False) is False


def test_volume_falls_back_to_keys(platform: WindowsPlatform) -> None:
    platform._audio = FakeAudio(error=RuntimeError("COM сломался"))  # type: ignore[assignment]
    assert platform.volume_set(30) == 30
    _kind, events = sender(platform).log[-1]
    downs = [e for e in events if e == (k.VK_VOLUME_DOWN, False)]
    ups = [e for e in events if e == (k.VK_VOLUME_UP, False)]
    assert len(downs) == 50 and len(ups) == 15  # до нуля, потом 15 шагов по 2 %
    assert platform.volume_change(-10) == 20
    _kind, events = sender(platform).log[-1]
    assert events.count((k.VK_VOLUME_DOWN, False)) == 5
    assert platform.mute_set(True) is True
    assert sender(platform).log[-1] == ("send_all", [(k.VK_VOLUME_MUTE, False), (k.VK_VOLUME_MUTE, True)])
    with pytest.raises(PlatformError, match="громкость"):
        platform.volume_get()


def test_no_audio_device(platform: WindowsPlatform) -> None:
    platform._audio = FakeAudio(error=NoAudioDevice())  # type: ignore[assignment]
    for call in (platform.volume_get, lambda: platform.volume_set(5), platform.is_muted):
        with pytest.raises(PlatformError, match="Нет устройства вывода звука"):
            call()


def test_app_volume(platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    names = {1111: "Spotify.exe", 2222: "chrome.exe"}

    class Proc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def name(self) -> str:
            return names[self.pid]

    monkeypatch.setattr(wp.psutil, "Process", Proc)
    audio = FakeAudio(sessions=[(1111, "spotify-session"), (2222, "chrome-session"), (0, "system-session")])
    platform._audio = audio  # type: ignore[assignment]
    assert platform.app_volume_set("spotify", 40) is True
    assert platform.app_volume_set("Google Chrome", 150) is True
    assert platform.app_volume_set("системные звуки", 10) is True
    assert platform.app_volume_set("telegram", 50) is False
    assert audio.session_levels == [("spotify-session", 40), ("chrome-session", 100), ("system-session", 10)]
    with pytest.raises(PlatformError):
        platform.app_volume_set(" ", 10)


# --- запуск ---------------------------------------------------------------------------------------------


class Launches:
    def __init__(self, platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch, missing: set[str]) -> None:
        self.calls: list[tuple[str, ...]] = []

        def shell_open(file: str, args: str = "", cwd: str | None = None, label: str = "") -> None:
            if file in missing:
                raise PlatformError(f"Не найдено: «{file}»")
            self.calls.append(("open", file, args))

        monkeypatch.setattr(platform, "_shell_open", shell_open)
        monkeypatch.setattr(platform, "_explorer", lambda arg: self.calls.append(("explorer", arg)))


def test_launch_dispatch(platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "Telegram.exe"
    exe.write_bytes(b"")
    platform._apps = appmod.AppInventory(
        lambda: tmp_path / "apps.json",
        lambda: [*APPS[:2], AppInfo("Telegram", r"C:\SM\Telegram.lnk", str(exe))],
    )
    calls = Launches(platform, monkeypatch, missing={"zzzz.exe", "Spotify.exe"}).calls

    platform.launch("https://youtube.com")
    platform.launch("shell:Downloads")
    platform.launch(str(exe))
    platform.launch("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
    platform.launch("chrome")
    platform.launch("калькулятор")
    platform.launch("telegram", "-startintray")  # аргументы — напрямую exe
    platform.launch("notepad.exe")
    platform.launch("calc")
    assert calls == [
        ("open", "https://youtube.com", ""),
        ("explorer", "shell:Downloads"),
        ("open", str(exe), ""),
        ("explorer", r"shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
        ("explorer", r"shell:AppsFolder\Chrome"),
        ("explorer", r"shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
        ("open", str(exe), "-startintray"),
        ("open", "notepad.exe", ""),
        ("open", "calc.exe", ""),
    ]
    with pytest.raises(PlatformError, match="Программа «zzzz» не найдена"):
        platform.launch("zzzz")
    with pytest.raises(PlatformError, match="не найдена"):
        platform.launch("совсем другая программа")
    with pytest.raises(PlatformError, match="Не найдено"):
        platform.launch(str(tmp_path / "missing.exe"))
    with pytest.raises(PlatformError, match="Не найдено"):
        platform.launch("Spotify.exe")


def test_open_url_and_path(
    platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = Launches(platform, monkeypatch, missing=set()).calls
    platform.open_url("youtube.com/watch?v=1")
    platform.open_url("ms-settings:display")
    platform.open_url("https://example.com/a b")
    for bad in ("javascript:alert(1)", "file:///C:/x.exe", "search-ms:query=x", "просто текст", ""):
        with pytest.raises(PlatformError):
            platform.open_url(bad)
    file = tmp_path / "отчёт.txt"
    file.write_text("x", encoding="utf-8")
    platform.open_path(str(file))
    with pytest.raises(PlatformError, match="Не найдено"):
        platform.open_path(str(tmp_path / "нет.txt"))
    assert calls == [
        ("open", "https://youtube.com/watch?v=1", ""),
        ("open", "ms-settings:display", ""),
        ("open", "https://example.com/a%20b", ""),
        ("open", str(file), ""),
    ]


# --- процессы и окна ------------------------------------------------------------------------------------


class Killable:
    killed: list[int] = []
    denied: set[int] = set()
    names: dict[int, str] = {}

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def name(self) -> str:
        return self.names[self.pid]

    def kill(self) -> None:
        import psutil

        if self.pid in self.denied:
            raise psutil.AccessDenied(self.pid)
        self.killed.append(self.pid)


def test_kill_process_protects_jarvis_and_system(
    platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    me = os.getpid()
    table = [
        (me, "python.exe"),
        (4, "System"),
        (700, "svchost.exe"),
        (900, "chrome.exe"),
        (901, "chrome.exe"),
    ]
    table += [(950, "admin.exe")]
    Killable.names = dict(table)
    Killable.killed = []
    Killable.denied = {950}
    monkeypatch.setattr(wp, "process_table", lambda: table)
    monkeypatch.setattr(wp.psutil, "Process", Killable)
    monkeypatch.setattr(wp.psutil, "wait_procs", lambda procs, timeout: (list(procs), []))
    monkeypatch.setattr(platform._own, "pids", lambda: frozenset({me}))

    with pytest.raises(PlatformError, match="сам Jarvis"):
        platform.kill_process("python")
    with pytest.raises(PlatformError, match="системный"):
        platform.kill_process("svchost")
    with pytest.raises(PlatformError, match="системный"):
        platform.kill_process("4")
    with pytest.raises(PlatformError, match="Нет прав"):
        platform.kill_process("admin")
    assert platform.kill_process("telegram") == 0
    assert platform.kill_process("Google Chrome") == 2
    assert Killable.killed == [900, 901]
    assert platform.is_running("chrome") is True
    assert platform.is_running("python") is False  # сам Jarvis не в счёт
    assert platform.is_running("telegram") is False


class FakeWindows:
    def __init__(self, wins: list[WindowInfo]) -> None:
        self.wins = wins
        self.actions: list[tuple[str, int, Any]] = []
        self.w = self

    def enum(self) -> tuple[list[WindowInfo], list[WindowInfo]]:
        return self.wins, []

    def find(self, target: Any) -> WindowInfo:
        if target is None:
            return self.wins[0]
        found = next((x for x in self.wins if target in (x.hwnd, x.exe)), None)
        if found is None:
            raise PlatformError("Окно не найдено")
        return found

    def close(self, hwnd: int) -> bool:
        self.actions.append(("close", hwnd, None))
        return True

    def show(self, hwnd: int, cmd: int) -> None:
        self.actions.append(("show", hwnd, cmd))

    def ShowWindow(self, hwnd: int, cmd: int) -> bool:  # имя — как в user32
        self.actions.append(("ShowWindow", hwnd, cmd))
        return True

    def focus(self, hwnd: int) -> None:
        self.actions.append(("focus", hwnd, None))

    def is_hung(self, hwnd: int) -> bool:
        return False


def test_close_app_and_window_actions(platform: WindowsPlatform) -> None:
    wins = [
        WindowInfo(1, "Документ1 - Word", "WINWORD.EXE", 10),
        WindowInfo(2, "YouTube — Google Chrome", "chrome.exe", 20),
        WindowInfo(3, "Почта — Google Chrome", "chrome.exe", 20),
        WindowInfo(4, "Калькулятор", "CalculatorApp.exe", 30),
    ]
    fake = FakeWindows(wins)
    platform._windows = fake  # type: ignore[assignment]
    assert platform.close_app("chrome") == 2
    assert platform.close_app("Калькулятор") == 1
    assert platform.close_app("telegram") == 0
    fake.actions.clear()
    platform.window_action("minimize")
    platform.window_action("maximize", "chrome.exe")
    platform.window_action("focus", 4)
    platform.window_action("close", 1)
    assert fake.actions == [
        ("show", 1, wp.wa.SW_MINIMIZE),
        ("ShowWindow", 2, wp.wa.SW_MAXIMIZE),
        ("focus", 2, None),
        ("focus", 4, None),
        ("close", 1, None),
    ]
    with pytest.raises(PlatformError, match="Неизвестное действие"):
        platform.window_action("explode")  # type: ignore[arg-type]
    with pytest.raises(PlatformError, match="не найдено"):
        platform.window_action("focus", "telegram.exe")


# --- система --------------------------------------------------------------------------------------------


def test_power(platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        wp.system, "run_shutdown", lambda action, delay=0: calls.append(("shutdown.exe", action))
    )
    monkeypatch.setattr(wp.system, "lock_workstation", lambda: calls.append(("lock", None)))
    monkeypatch.setattr(wp.system, "check_suspend", lambda hibernate: calls.append(("check", hibernate)))
    monkeypatch.setattr(wp.system, "suspend", lambda hibernate: calls.append(("suspend", hibernate)))

    platform.power("shutdown", 60)
    platform.power("lock")
    platform.power("lock", 30)  # позже — по таймеру
    platform.power("sleep")  # сон — тоже по таймеру (0,3 с): SetSuspendState не держит поток платформы
    assert calls == [("shutdown.exe", "shutdown"), ("lock", None), ("check", False)]
    platform.power("cancel")  # отменяет и shutdown.exe, и таймеры Jarvis
    time.sleep(0.4)  # отменённый сон сработал бы через 0,3 с
    assert calls == [
        ("shutdown.exe", "shutdown"),
        ("lock", None),
        ("check", False),
        ("shutdown.exe", "cancel"),
    ]
    platform.power("hibernate")
    assert wait_until(lambda: ("suspend", True) in calls)
    assert calls[-2:] == [("check", True), ("suspend", True)]


def test_trash_and_recent(platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    trashed: list[str] = []
    monkeypatch.setattr("send2trash.send2trash", trashed.append)
    file = tmp_path / "старый.txt"
    file.write_text("x", encoding="utf-8")
    platform.trash(str(file))
    assert trashed == [str(file)]
    with pytest.raises(PlatformError, match="Не найдено"):
        platform.trash(str(tmp_path / "нет"))

    class Reader:
        def read(self, lnk: str) -> tuple[str, str]:
            return (str(file) if lnk == "a.lnk" else "", "")

    platform._local.shortcuts = Reader()
    monkeypatch.setattr(wp.system, "recent_links", lambda: [("a.lnk", 5.0), ("b.lnk", 4.0)])
    files = platform.recent_files(5)
    assert [(f.name, f.kind) for f in files] == [("старый.txt", "txt")]


def test_media_passthrough(platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    info = MediaInfo(title="Song", app="Spotify", status="playing")
    monkeypatch.setattr(platform._media, "now_playing", lambda: info)
    assert platform.now_playing() is info


# --- настоящий Windows ----------------------------------------------------------------------------------


@pytest.fixture
def live() -> Iterator[WindowsPlatform]:
    import pythoncom  # type: ignore[import-not-found]

    pythoncom.CoInitialize()
    yield WindowsPlatform()


@windows_only
def test_live_volume_roundtrip(live: WindowsPlatform) -> None:
    before, muted = live.volume_get(), live.is_muted()
    try:
        target = 30 if before != 30 else 31
        assert live.volume_set(target) == target
        assert live.volume_get() == target
        assert live.volume_change(1) == target + 1
    finally:
        live.volume_set(before)
        live.mute_set(muted)
    assert live.volume_get() == before


@windows_only
def test_live_windows_and_screen(live: WindowsPlatform) -> None:
    wins = live.list_windows()
    assert all(isinstance(x, WindowInfo) and x.title for x in wins)
    own = live._own.pids()
    assert all(x.pid not in own for x in wins)  # окон самого Jarvis в списке нет
    fg = live.foreground_window()
    assert fg is None or isinstance(fg, WindowInfo)
    png = live.screenshot()
    assert png.startswith(b"\x89PNG")
    if fg is not None and not fg.minimized:
        assert live.screenshot(fg.hwnd).startswith(b"\x89PNG")
    assert isinstance(live.is_fullscreen_app(), bool)
    w, h = live.screen_size()
    assert w > 0 and h > 0
    x, y = live.cursor_pos()
    left, top, width, height = live.virtual_screen()
    assert left <= x < left + width and top <= y < top + height


@windows_only
def test_live_clipboard_roundtrip(live: WindowsPlatform) -> None:
    before = live.clipboard_get()
    try:
        live.clipboard_set("Jarvis тест 😀\nвторая строка")
        assert live.clipboard_get() == "Jarvis тест 😀\nвторая строка"
    finally:
        live.clipboard_set(before)


@windows_only
def test_live_system(live: WindowsPlatform) -> None:
    downloads = live.known_folder("downloads")
    assert downloads and os.path.isdir(downloads)
    stats = live.system_stats()
    assert 0 <= stats.cpu <= 100 and stats.ram_total_gb > 0
    assert live.recycle_bin_count() >= 0
    assert live.idle_seconds() >= 0
    assert isinstance(live.recent_files(5), list)
    brightness = live.brightness_get()
    assert brightness is None or 0 <= brightness <= 100
    playing = live.now_playing()
    assert playing is None or isinstance(playing, MediaInfo)


@windows_only
def test_live_apps_and_processes(live: WindowsPlatform) -> None:
    start_apps = appmod.run_get_start_apps()
    assert start_apps and all(name and app_id for name, app_id in start_apps)
    apps = live.list_apps()
    assert apps and all(isinstance(x, AppInfo) for x in apps)
    assert appmod.resolve_app("блокнот", apps) or appmod.resolve_app("notepad", apps)
    assert live.is_running("explorer") is True
    assert live.kill_process("jarvis-test-no-such-process-xyz") == 0
    assert live.close_app("jarvis-test-no-such-process-xyz") == 0


@windows_only
def test_live_keys_are_harmless(live: WindowsPlatform) -> None:
    live.press_keys("shift")  # Shift ничего не делает
    live.key_down("shift")
    live.key_up("shift")


def test_lazy_parts_are_created_once(platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ленивые части создаются под замком: параллельный первый вызов не создаёт два объекта."""
    platform._sender = None
    created: list[int] = []

    class Counting(FakeSender):
        def __init__(self) -> None:
            super().__init__()
            created.append(1)
            time.sleep(0.01)

    monkeypatch.setattr(wp.inputs, "Sender", Counting)
    threads = [threading.Thread(target=platform._input) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)
    assert created == [1]
