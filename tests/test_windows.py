"""pc.windows: фильтры списка, поиск цели, лестница фокуса, защита своих и elevated-окон (фейковый _api)."""

import os
import sys
from dataclasses import dataclass, field

import pytest

from pc import _input, apps, confirm_client, privacy, procs, windows
from pc.windows import WindowInfo

ME = os.getpid()
JARVIS_CHILD = 700  # процесс из дерева Jarvis (например, окно консоли codex)


@dataclass
class W:
    title: str
    pid: int
    visible: bool = True
    ex: int = 0
    owner: int = 0
    cloaked: bool = False
    cls: str = "Chrome_WidgetWin_1"
    iconic: bool = False


@dataclass
class FakeApi:
    wins: dict[int, W]
    exes: dict[int, str]
    elevated: set[int] = field(default_factory=set)
    fg: int = 1
    accept_at: str = "never"  # шаг, на котором SetForegroundWindow сработает: plain | alt | attach | never
    calls: list[tuple] = field(default_factory=list)
    attached: bool = False
    exe_lookups: list[int] = field(default_factory=list)
    alt_sent: bool = False

    # список
    def enum_windows(self) -> list[int]:
        return list(self.wins)

    def get_title(self, hwnd: int) -> str:
        return self.wins[hwnd].title if hwnd in self.wins else ""

    def class_name(self, hwnd: int) -> str:
        return self.wins[hwnd].cls

    def is_visible(self, hwnd: int) -> bool:
        return self.wins[hwnd].visible

    def is_cloaked(self, hwnd: int) -> bool:
        return self.wins[hwnd].cloaked

    def ex_style(self, hwnd: int) -> int:
        return self.wins[hwnd].ex

    def owner(self, hwnd: int) -> int:
        return self.wins[hwnd].owner

    def pid_of(self, hwnd: int) -> int:
        return self.wins[hwnd].pid if hwnd in self.wins else 0

    def thread_of(self, hwnd: int) -> int:
        return 5000 + hwnd

    def current_thread_id(self) -> int:
        return 42

    def exe_of(self, pid: int) -> str:
        self.exe_lookups.append(pid)
        return self.exes.get(pid, "")

    def is_elevated(self, pid: int) -> bool:
        return pid in self.elevated

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.wins

    def is_iconic(self, hwnd: int) -> bool:
        return self.wins[hwnd].iconic

    # действия
    def foreground(self) -> int:
        return self.fg

    def set_foreground(self, hwnd: int) -> bool:
        self.calls.append(("set_foreground", hwnd))
        step = {"plain": True, "alt": self.alt_sent, "attach": self.attached}.get(self.accept_at, False)
        if step:
            self.fg = hwnd
        return step

    def show(self, hwnd: int, cmd: int) -> None:
        self.calls.append(("show", hwnd, cmd))
        if cmd == windows.SW_RESTORE:
            self.wins[hwnd].iconic = False
        if cmd == windows.SW_MINIMIZE:
            self.wins[hwnd].iconic = True

    def attach_thread_input(self, thread: int, to: int, attach: bool) -> bool:
        self.calls.append(("attach", thread, to, attach))
        self.attached = attach
        return True

    def bring_to_top(self, hwnd: int) -> bool:
        self.calls.append(("bring_to_top", hwnd))
        return True

    def flash(self, hwnd: int) -> None:
        self.calls.append(("flash", hwnd))

    def post_close(self, hwnd: int) -> bool:
        self.calls.append(("post_close", hwnd))
        return True

    def minimize_all(self) -> None:
        self.calls.append(("minimize_all",))


def make_api() -> FakeApi:
    wins = {
        1: W("Новая вкладка - Google Chrome", 100),
        2: W("отчёт.docx - Word", 140),
        3: W("Telegram", 110),
        4: W("Невидимое", 100, visible=False),
        5: W("", 100),
        6: W("Призрак", 100, cloaked=True),
        7: W("Плавающая панель", 100, ex=windows.WS_EX_TOOLWINDOW),
        8: W("Диалог сохранения", 140, owner=2),
        9: W("Окно с APPWINDOW", 140, owner=2, ex=windows.WS_EX_APPWINDOW),
        10: W("Program Manager", 200, cls="Progman"),
        11: W("Jarvis", ME, cls="Qt6QWindowIcon"),
        12: W("codex", JARVIS_CHILD, cls="ConsoleWindowClass"),
        13: W("Подсказка", 100, ex=windows.WS_EX_NOACTIVATE),
        14: W("Без имени — Блокнот", 300, iconic=True),
        15: W("Администратор: Командная строка", 400),
        16: W("Steam", 120),
    }
    exes = {
        100: "chrome.exe",
        140: "WINWORD.EXE",
        110: "Telegram.exe",
        200: "explorer.exe",
        ME: "Jarvis.exe",
        JARVIS_CHILD: "codex.exe",
        300: "Notepad.exe",
        400: "WindowsTerminal.exe",
        120: "steam.exe",
    }
    return FakeApi(wins, exes, elevated={400})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, apps_fixture) -> FakeApi:
    api = make_api()
    monkeypatch.setattr(windows, "_api", api)
    monkeypatch.setattr(windows, "FG_POLL_S", 0)
    monkeypatch.setattr(procs, "own_pids", lambda: {ME, JARVIS_CHILD})
    monkeypatch.setattr(apps, "_inv", None)
    apps.set_inventory(apps_fixture)

    def send_key(vk: int, up: bool = False) -> bool:
        api.calls.append(("send_key", vk, up))
        api.alt_sent = True
        return True

    monkeypatch.setattr(_input, "send_key", send_key)
    return api


def hwnds(wins: list[WindowInfo]) -> list[int]:
    return [w.hwnd for w in wins]


# --- список и поиск -------------------------------------------------------------------------------


def test_list_windows_filters(fake: FakeApi) -> None:
    wins = windows.list_windows()
    assert hwnds(wins) == [1, 2, 3, 9, 14, 15, 16]
    assert wins[0] == WindowInfo(1, "Новая вкладка - Google Chrome", 100, "chrome.exe")
    assert sorted(fake.exe_lookups) == sorted(set(fake.exe_lookups))  # exe — один раз на pid


def test_foreground_includes_jarvis(fake: FakeApi) -> None:
    fake.fg = 11
    assert windows.foreground() == WindowInfo(11, "Jarvis", ME, "Jarvis.exe")
    fake.fg = 0
    assert windows.foreground() is None


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (2, 2),
        ("3", 3),
        (11, None),  # окно Jarvis не в списке
        (4, None),  # невидимое
        ("chrome", 1),
        ("winword.exe", 2),
        ("telegram", 3),
        ("Telegram", 3),
        ("телега", 3),
        ("телегу", 3),
        ("ворд", 2),
        ("хром", 1),
        ("стим", 16),
        ("блокнот", 14),
        ("отчёт", 2),
        ("отчет docx", 2),
        ("новая вкладка", 1),
        ("фотошоп", None),
        ("@cur", None),
        ("", None),
    ],
)
def test_find_window(fake: FakeApi, target: str | int, expected: int | None) -> None:
    win = windows.find_window(target)
    assert (win.hwnd if win else None) == expected


def test_find_window_known_app_without_window(fake: FakeApi) -> None:
    # Telegram не запущен: вкладка браузера с «Telegram» в заголовке — не его окно
    fake.wins = {20: W("Telegram Web - Google Chrome", 100), 21: W("Калькулятор", 500)}
    fake.exes[500] = "ApplicationFrameHost.exe"
    assert windows.find_window("телега") is None
    assert windows.find_window("калькулятор").hwnd == 21  # UWP: процесс-хост, узнаём по заголовку
    assert windows.find_window("telegram web").hwnd == 20


def test_find_window_prefers_top_of_z_order(fake: FakeApi) -> None:
    fake.wins = {20: W("Вторая вкладка - Google Chrome", 100), **fake.wins}
    assert windows.find_window("chrome").hwnd == 20


# --- лестница фокуса ------------------------------------------------------------------------------


def steps(api: FakeApi) -> list[str]:
    return [c[0] for c in api.calls]


def test_focus_step1_restores_minimized(fake: FakeApi) -> None:
    fake.accept_at = "plain"
    r = windows.focus(14)
    assert r.ok and "Блокнот" in r.text
    assert fake.calls[:2] == [("show", 14, windows.SW_RESTORE), ("set_foreground", 14)]
    assert "send_key" not in steps(fake)


def test_focus_already_foreground(fake: FakeApi) -> None:
    fake.fg = 3
    assert windows.focus(3).ok
    assert fake.calls == []


def test_focus_step2_releases_alt(fake: FakeApi) -> None:
    fake.accept_at = "alt"
    r = windows.focus(3)
    assert r.ok
    assert steps(fake) == ["set_foreground", "send_key", "set_foreground"]
    assert ("send_key", windows.VK_MENU, True) in fake.calls


def test_focus_step3_attach_thread_input(fake: FakeApi) -> None:
    fake.accept_at = "attach"
    fake.fg = 1
    r = windows.focus(3)
    assert r.ok
    assert steps(fake) == [
        "set_foreground",
        "send_key",
        "set_foreground",
        "attach",
        "bring_to_top",
        "set_foreground",
        "attach",
    ]
    assert fake.calls[3] == ("attach", 42, 5001, True)
    assert fake.calls[-1] == ("attach", 42, 5001, False)


def test_focus_step3_detaches_on_error(fake: FakeApi) -> None:
    def broken(hwnd: int) -> bool:
        raise OSError("сбой")

    fake.bring_to_top = broken  # type: ignore[method-assign]
    with pytest.raises(OSError):
        windows.focus(3)
    assert fake.calls[-1] == ("attach", 42, 5001, False)


def test_focus_step4_flash_and_honest_fail(fake: FakeApi) -> None:
    r = windows.focus(3)
    assert not r.ok and r.text.startswith("Не удалось переключиться на «Telegram»")
    assert fake.calls[-1] == ("flash", 3)


def test_focus_survives_missing_input(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    def not_ready(vk: int, up: bool = False) -> bool:
        raise NotImplementedError

    monkeypatch.setattr(_input, "send_key", not_ready)
    fake.accept_at = "attach"
    assert windows.focus(3).ok


def test_focus_elevated_untouched(fake: FakeApi) -> None:
    fake.accept_at = "plain"
    for r in (windows.focus(15), windows.focus_target("командная строка", "user")):
        assert not r.ok and "администратора" in r.text
    assert fake.calls == []


def test_focus_elevated_allowed_when_we_are_elevated(fake: FakeApi) -> None:
    fake.accept_at = "plain"
    fake.elevated.add(ME)
    assert windows.focus(15).ok


def test_focus_unknown_hwnd(fake: FakeApi) -> None:
    assert not windows.focus(999).ok


# --- высокоуровневые ------------------------------------------------------------------------------


def test_focus_target(fake: FakeApi) -> None:
    fake.accept_at = "plain"
    r = windows.focus_target("телега", "user")
    assert r.ok and r.text == "Переключился на «Telegram»"
    assert fake.fg == 3


def test_targets_refuse_cur_and_missing(fake: FakeApi) -> None:
    for fn in (windows.focus_target, windows.close_target):
        r = fn("@cur", "user")
        assert not r.ok and "@cur" in r.text
        r = fn("фотошоп", "user")
        assert not r.ok and "Не нашёл окно" in r.text
    assert not windows.window_action("minimize", None, "user").ok
    assert fake.calls == []


@pytest.mark.parametrize("target", [11, "11", "jarvis", "codex", 12])
def test_jarvis_windows_are_off_limits(fake: FakeApi, target: str | int) -> None:
    for r in (
        windows.close_target(target, "user"),
        windows.focus_target(target, "brain"),
        windows.window_action("minimize", target, "user"),
    ):
        assert not r.ok and r.text == windows.SELF_TEXT
    assert fake.calls == []


def test_close_target_user_and_brain(fake: FakeApi) -> None:
    r = windows.close_target("ворд", "user")
    assert r.ok and fake.calls == [("post_close", 2)]
    asked = []
    confirm_client.set_confirm_handler(lambda s, d, c: asked.append((s, d, c)) or False)
    r = windows.close_target("chrome", "brain")
    assert not r.ok and ("post_close", 1) not in fake.calls
    summary, details, caller = asked[0]
    assert caller == "brain" and "Новая вкладка - Google Chrome" in summary and "chrome.exe" in summary
    assert "chrome.exe" in details
    confirm_client.set_confirm_handler(lambda s, d, c: True)
    assert windows.close_target("chrome", "brain").ok
    assert fake.calls[-1] == ("post_close", 1)


def test_close_elevated_untouched(fake: FakeApi) -> None:
    r = windows.close_target(15, "user")
    assert not r.ok and "администратора" in r.text
    assert fake.calls == []


def test_window_actions(fake: FakeApi) -> None:
    r = windows.window_action("minimize", "телеграм", "user")
    assert r.ok and fake.calls == [("show", 3, windows.SW_MINIMIZE)]
    fake.calls.clear()
    fake.accept_at = "plain"
    r = windows.window_action("maximize", 3, "brain")
    assert r.ok and r.text.startswith("Развернул")
    assert fake.calls[0] == ("show", 3, windows.SW_MAXIMIZE) and fake.fg == 3
    r = windows.window_action("restore", "3", "user")
    assert r.ok and ("show", 3, windows.SW_RESTORE) in fake.calls
    r = windows.window_action("minimize_all", None, "user")
    assert r.ok and fake.calls[-1] == ("minimize_all",)
    assert not windows.window_action("explode", 3, "user").ok
    assert not windows.window_action("minimize", 15, "user").ok


def test_list_windows_result(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    r = windows.list_windows_result("user")
    assert r.ok and r.data[0] == {"hwnd": 1, "title": "Новая вкладка - Google Chrome", "exe": "chrome.exe"}
    monkeypatch.setattr(privacy, "redact_title", lambda t: "(скрыто)" if "отчёт" in t else t[:80])
    r = windows.list_windows_result("brain")
    assert r.ok and {"hwnd": 2, "title": "(скрыто)", "exe": "WINWORD.EXE"} in r.data
    assert all(d["hwnd"] not in (11, 12) for d in r.data)


def test_brain_text_uses_privacy(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privacy, "redact_title", lambda t: "(скрыто)")
    fake.accept_at = "plain"
    r = windows.focus_target("ворд", "brain")
    assert r.ok and r.text == "Переключился на «(скрыто)»"


# --- настоящий Windows --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="нужен настоящий Windows")
def test_real_windows_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows, "_api", None)
    wins = windows.list_windows()
    assert isinstance(wins, list)
    for w in wins:
        assert isinstance(w, WindowInfo)
        assert isinstance(w.hwnd, int) and w.hwnd > 0
        assert isinstance(w.title, str) and w.title.strip()
        assert isinstance(w.pid, int) and isinstance(w.exe, str)
        assert w.pid not in procs.own_pids()
    fg = windows.foreground()
    assert fg is None or (isinstance(fg, WindowInfo) and isinstance(fg.title, str))
    api = windows.api()
    assert isinstance(api.is_elevated(os.getpid()), bool)
    assert api.exe_of(os.getpid()).casefold().endswith(".exe")
    if wins:
        found = windows.find_window(wins[0].hwnd)
        assert found is None or found.hwnd == wins[0].hwnd  # окно могло закрыться между вызовами


def test_clean_text_replaces_lone_surrogates() -> None:
    """S8: GetWindowTextW отдаёт непарные суррогаты — дальше UTF-8 (руки, журнал, мозг) падал."""
    from pc.windows import clean_text

    assert clean_text("Чат \ud83d — Telegram") == "Чат � — Telegram"
    assert clean_text("эмодзи 😀 ок").encode("utf-8")
