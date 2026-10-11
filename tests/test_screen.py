"""pc.screen: снимок окна или экрана для GPT.

Отказы (мозг, Jarvis, менеджеры паролей, deny_apps, private_paths, свёрнутое и зависшее окно), чёрный снимок,
слой ОС на фейковых функциях user32/gdi32: GDI освобождается всегда, размер ограничен, DPI-контекст потока
восстанавливается."""

import ctypes
import sys
from typing import Any

import pytest

from pc import policy, procs, screen, windows
from pc.screen import Shot
from pc.windows import WindowInfo

OWN_PID = 4242
PIXEL = bytes([10, 20, 30, 0])  # B, G, R, A — альфа GDI обычно 0
MONITOR = (0, 0, 1920, 1080)
SECOND = (1920, 0, 3840, 1080)


def shot(width: int = 4, height: int = 3, pixel: bytes = PIXEL) -> Shot:
    return Shot(width, height, pixel * (width * height))


class FakeApi:
    """Слой ОС pc.screen: окна — словарь hwnd → сведения; снимки — заданный Shot."""

    def __init__(self) -> None:
        self.windows: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.shot: Shot | None = shot()
        self.monitors: dict[int | None, tuple[int, int, int, int] | None] = {None: MONITOR}

    def add(self, hwnd: int, exe: str, title: str = "Окно", pid: int = 100, **kw: Any) -> WindowInfo:
        self.windows[hwnd] = {"exe": exe, "title": title, "pid": pid, "rect": MONITOR, **kw}
        return WindowInfo(hwnd, title, pid, exe)

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self.windows[hwnd].get("iconic"))

    def is_hung(self, hwnd: int) -> bool:
        return bool(self.windows[hwnd].get("hung"))

    def pid_of(self, hwnd: int) -> int:
        return self.windows[hwnd]["pid"]

    def exe_of(self, pid: int) -> str:
        return next((w["exe"] for w in self.windows.values() if w["pid"] == pid), "")

    def title_of(self, hwnd: int) -> str:
        return self.windows[hwnd]["title"]

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return self.windows[hwnd]["rect"]

    def monitor_rect(self, hwnd: int | None) -> tuple[int, int, int, int] | None:
        self.calls.append(("monitor", hwnd))
        return self.monitors.get(hwnd, MONITOR)

    def grab_window(self, hwnd: int) -> Shot | None:
        self.calls.append(("grab_window", hwnd))
        return self.shot

    def grab_rect(self, rect: tuple[int, int, int, int]) -> Shot | None:
        self.calls.append(("grab_rect", rect))
        return self.shot

    def grabbed(self) -> bool:
        return any(c[0].startswith("grab") for c in self.calls)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi()
    monkeypatch.setattr(screen, "_api", api)
    monkeypatch.setattr(procs, "own_pids", lambda: {OWN_PID})

    def list_windows() -> list[WindowInfo]:
        return [
            WindowInfo(h, w["title"], w["pid"], w["exe"])
            for h, w in api.windows.items()
            if w["pid"] != OWN_PID
        ]

    monkeypatch.setattr(windows, "list_windows", list_windows)
    return api


# --- окно -----------------------------------------------------------------------------------------------


def test_window_shot(fake: FakeApi) -> None:
    fake.add(11, "chrome.exe", "Новости - Google Chrome")
    out = screen.capture_window(11, "user")
    assert out == fake.shot and fake.calls == [("grab_window", 11)]


@pytest.mark.parametrize("call", ["window", "screen"])
def test_brain_is_denied(fake: FakeApi, call: str) -> None:
    """У мозга нет снимков: policy «нет», слой ОС не трогается."""
    fake.add(11, "chrome.exe")
    out = screen.capture_window(11, "brain") if call == "window" else screen.capture_screen(11, "brain")
    assert not isinstance(out, Shot) and not out.ok and out.text == policy.DENY_TEXT
    assert fake.calls == []


def test_window_gone(fake: FakeApi) -> None:
    out = screen.capture_window(99, "user")
    assert not out.ok and out.text == screen.GONE_TEXT  # type: ignore[union-attr]
    assert not screen.capture_window(0, "user").ok  # type: ignore[union-attr]
    assert not fake.grabbed()


def test_own_window_refused_via_policy(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.add(12, "Jarvis.exe", "Jarvis", pid=OWN_PID)
    asked: list[str] = []
    real = policy.require

    def spy(action: str, caller: str, summary: str, details: str = "") -> Any:
        asked.append(action)
        return real(action, caller, summary, details)  # type: ignore[arg-type]

    monkeypatch.setattr(policy, "require", spy)
    out = screen.capture_window(12, "user")
    assert not out.ok and out.text == screen.SELF_TEXT  # type: ignore[union-attr]
    assert asked == ["screenshot", "jarvis_self"] and not fake.grabbed()


@pytest.mark.parametrize(
    "exe",
    ["KeePass.exe", "KeePassXC.exe", "1Password.exe", "Bitwarden.exe", "Enpass.exe", "Dashlane.exe",
     "NordPass.exe", "RoboForm.exe", "keepass.EXE", r"C:\Program Files\KeePass\KeePass.exe"],
)  # fmt: skip
def test_password_manager_window_is_never_sent(fake: FakeApi, exe: str) -> None:
    fake.add(13, exe, "База.kdbx - KeePass")
    out = screen.capture_window(13, "user")
    assert not out.ok and out.text == screen.CLOSED_TEXT  # type: ignore[union-attr]
    assert not fake.grabbed()


@pytest.mark.parametrize("title", ["Менеджер паролей Google - Google Chrome", "Password Manager - Edge"])
def test_password_page_by_title(fake: FakeApi, title: str) -> None:
    fake.add(14, "chrome.exe", title)
    assert screen.capture_window(14, "user").text == screen.CLOSED_TEXT  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "deny", [["Telegram.exe"], ["telegram"], [r"C:\Apps\Telegram.exe"], ["  TELEGRAM  "]]
)
def test_deny_apps(fake: FakeApi, deny: list[str]) -> None:
    fake.add(15, "Telegram.exe", "Telegram")
    assert screen.capture_window(15, "user", deny).text == screen.CLOSED_TEXT  # type: ignore[union-attr]
    assert isinstance(screen.capture_window(15, "user", ["notepad.exe"]), Shot)


def test_private_name_in_title(fake: FakeApi, config_file: Any) -> None:
    config_file("[pc]\nprivate_paths = ['C:\\\\Users\\\\me\\\\Тайное']\n")
    fake.add(16, "explorer.exe", "Тайное — Проводник")
    assert screen.capture_window(16, "user").text == screen.CLOSED_TEXT  # type: ignore[union-attr]
    assert not fake.grabbed()


def test_broken_config_refuses(fake: FakeApi, config_file: Any) -> None:
    """Конфиг не читается — закрытые окна неизвестны: снимок не отправляется."""
    config_file("[pc\nprivate_paths = ")
    fake.add(17, "notepad.exe", "Безымянный - Блокнот")
    assert screen.capture_window(17, "user").text == screen.CONFIG_TEXT  # type: ignore[union-attr]
    assert screen.capture_screen(17, "user").text == screen.CONFIG_TEXT  # type: ignore[union-attr]
    assert not fake.grabbed()


def test_unknown_program_refused(fake: FakeApi) -> None:
    fake.add(18, "", "Окно без программы")
    assert screen.capture_window(18, "user").text == screen.UNKNOWN_TEXT  # type: ignore[union-attr]


def test_minimized_and_hung(fake: FakeApi) -> None:
    fake.add(19, "notepad.exe", iconic=True)
    assert screen.capture_window(19, "user").text == screen.MINIMIZED_TEXT  # type: ignore[union-attr]
    fake.add(20, "notepad.exe", pid=101, hung=True)
    assert screen.capture_window(20, "user").text == screen.HUNG_TEXT  # type: ignore[union-attr]
    assert not fake.grabbed()


def test_window_capture_failed_or_black(fake: FakeApi) -> None:
    fake.add(21, "game.exe")
    fake.shot = None
    assert screen.capture_window(21, "user").text == screen.WINDOW_FAILED_TEXT  # type: ignore[union-attr]
    fake.shot = shot(pixel=bytes([0, 0, 0, 255]))  # DWM отдал чёрное с непрозрачной альфой
    assert screen.capture_window(21, "user").text == screen.BLANK_WINDOW_TEXT  # type: ignore[union-attr]


def test_blank_detection() -> None:
    assert screen.blank(shot(100, 100, bytes([0, 0, 0, 0])))
    assert screen.blank(shot(100, 100, bytes([0, 0, 0, 255])))
    assert not screen.blank(shot(100, 100))
    one = bytearray(bytes(4) * 10_000)
    one[4 * 7777 + 1] = 1  # одна точка не чёрная — мимо выборки, но полная проверка её находит
    assert not screen.blank(Shot(100, 100, bytes(one)))


# --- экран ----------------------------------------------------------------------------------------------


def test_screen_of_primary_without_window(fake: FakeApi) -> None:
    out = screen.capture_screen(None, "user")
    assert out == fake.shot
    assert fake.calls == [("monitor", None), ("grab_rect", MONITOR)]


def test_screen_of_active_window_monitor(fake: FakeApi) -> None:
    fake.add(30, "chrome.exe", rect=SECOND)
    fake.monitors[30] = SECOND
    assert screen.capture_screen(30, "user") == fake.shot
    assert fake.calls == [("monitor", 30), ("grab_rect", SECOND)]
    fake.calls.clear()
    assert isinstance(screen.capture_screen(31, "user"), Shot)  # окна уже нет — основной монитор
    assert fake.calls[0] == ("monitor", None)


def test_screen_refused_when_active_window_is_closed(fake: FakeApi) -> None:
    fake.add(32, "KeePassXC.exe", "Пароли - KeePassXC")
    assert screen.capture_screen(32, "user").text == screen.CLOSED_TEXT  # type: ignore[union-attr]
    fake.add(33, "Telegram.exe", "Telegram", pid=102)
    assert screen.capture_screen(33, "user", ["Telegram.exe"]).text == screen.CLOSED_TEXT  # type: ignore[union-attr]
    assert not fake.grabbed()


def test_screen_refused_when_password_manager_visible(fake: FakeApi) -> None:
    fake.add(34, "chrome.exe", "Новости - Google Chrome")
    fake.add(35, "Bitwarden.exe", "Bitwarden", pid=103, rect=(100, 100, 900, 700))
    out = screen.capture_screen(34, "user")
    assert out.text == screen.BLOCKER_TEXT.format(exe="Bitwarden.exe")  # type: ignore[union-attr]
    assert not fake.grabbed()


@pytest.mark.parametrize("where", ["minimized", "other_monitor"])
def test_screen_ok_when_password_manager_not_visible(fake: FakeApi, where: str) -> None:
    fake.add(36, "chrome.exe", "Новости - Google Chrome")
    if where == "minimized":
        fake.add(37, "KeePass.exe", "KeePass", pid=104, iconic=True)
    else:
        fake.add(37, "KeePass.exe", "KeePass", pid=104, rect=SECOND)
    assert isinstance(screen.capture_screen(36, "user"), Shot)


def test_screen_from_jarvis_window_uses_its_monitor(fake: FakeApi) -> None:
    fake.add(38, "Jarvis.exe", "Jarvis", pid=OWN_PID)
    fake.monitors[38] = SECOND
    assert screen.capture_screen(38, "user") == fake.shot
    assert ("grab_rect", SECOND) in fake.calls


def test_screen_check_failure_refuses(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[WindowInfo]:
        raise OSError("EnumWindows")

    monkeypatch.setattr(windows, "list_windows", boom)
    assert screen.capture_screen(None, "user").text == screen.CHECK_FAILED_TEXT  # type: ignore[union-attr]
    assert not fake.grabbed()


def test_screen_failures(fake: FakeApi) -> None:
    fake.monitors[None] = None
    assert screen.capture_screen(None, "user").text == screen.SCREEN_FAILED_TEXT  # type: ignore[union-attr]
    fake.monitors[None] = MONITOR
    fake.shot = None
    assert screen.capture_screen(None, "user").text == screen.SCREEN_FAILED_TEXT  # type: ignore[union-attr]
    fake.shot = shot(pixel=bytes(4))
    assert screen.capture_screen(None, "user").text == screen.BLANK_SCREEN_TEXT  # type: ignore[union-attr]


def test_app_key_and_denied_apps() -> None:
    assert screen.app_key(r"C:\Program Files\KeePass Password Safe 2\KeePass.exe") == "keepass"
    assert screen.app_key("Proton Pass.exe") == "proton pass"
    assert screen.app_key("") == ""
    denied = screen.denied_apps(["Telegram.exe", "", "  ", 5])  # type: ignore[list-item]
    assert {"telegram", "keepass", "bitwarden", "1password"} <= denied and "" not in denied


# --- слой ОС на фейковых user32/gdi32 ----------------------------------------------------------------------

PMV2 = ctypes.c_void_p(-4).value
PMV1 = ctypes.c_void_p(-3).value
STOCK_BITMAP = 0xB0


def _value(arg: Any) -> Any:
    return arg.value if isinstance(arg, ctypes.c_void_p) else arg


class FakeFns:
    """user32/gdi32 без Windows: дескрипторы — числа, точки DIB-секции — настоящий буфер ctypes."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail: set[str] = set()
        self.boom: set[str] = set()
        self.rect = (10, 20, 74, 52)  # 64 x 32
        self.monitor = (-1920, 0, 0, 1080)
        self.buffers: dict[int, Any] = {}
        self.selected: dict[int, int] = {}
        self.headers: list[dict[str, int]] = []
        self.thread_ctx = 0x22
        self.window_ctx: int | None = 0x12
        self.reject_ctx: set[int] = set()
        self.ctx_during_rect: list[int] = []
        self._next = 0x100

    def _hit(self, name: str, *args: Any) -> bool:
        self.calls.append((name, *args))
        if name in self.boom:
            raise RuntimeError(f"{name} упал")
        return name not in self.fail

    def GetDC(self, hwnd: Any) -> int | None:
        return 0xD0 if self._hit("GetDC", hwnd) else None

    def ReleaseDC(self, hwnd: Any, dc: int) -> int:
        self._hit("ReleaseDC", hwnd, dc)
        return 1

    def CreateCompatibleDC(self, dc: int) -> int | None:
        return 0xD1 if self._hit("CreateCompatibleDC", dc) else None

    def DeleteDC(self, dc: int) -> int:
        self._hit("DeleteDC", dc)
        return 1

    def CreateDIBSection(
        self, dc: int, pinfo: Any, usage: int, ppbits: Any, section: Any, offset: int
    ) -> Any:
        h = pinfo.contents.bmiHeader
        self.headers.append(
            {
                "size": h.biSize,
                "width": h.biWidth,
                "height": h.biHeight,
                "planes": h.biPlanes,
                "bits": h.biBitCount,
                "compression": h.biCompression,
            }
        )
        if not self._hit("CreateDIBSection", dc, usage):
            return None
        buf = ctypes.create_string_buffer(h.biWidth * -h.biHeight * 4)
        self._next += 1
        self.buffers[self._next] = buf
        ppbits.contents.value = ctypes.addressof(buf)
        return self._next

    def SelectObject(self, dc: int, obj: int) -> int:
        self._hit("SelectObject", dc, obj)
        prev = self.selected.get(dc, STOCK_BITMAP)
        self.selected[dc] = obj
        return prev

    def DeleteObject(self, obj: int) -> int:
        self._hit("DeleteObject", obj)
        return 1

    def GdiFlush(self) -> int:
        return 1

    def _paint(self, dc: int) -> None:
        buf = self.buffers[self.selected[dc]]
        ctypes.memmove(buf, PIXEL * (len(buf) // 4), len(buf))

    def PrintWindow(self, hwnd: int, dc: int, flags: int) -> int:
        if not self._hit("PrintWindow", hwnd, dc, flags):
            return 0
        self._paint(dc)
        return 1

    def BitBlt(self, dst: int, x: int, y: int, w: int, h: int, src: int, sx: int, sy: int, rop: int) -> int:
        if not self._hit("BitBlt", dst, x, y, w, h, src, sx, sy, rop):
            return 0
        self._paint(dst)
        return 1

    def GetWindowRect(self, hwnd: int, prect: Any) -> int:
        self.ctx_during_rect.append(self.thread_ctx)
        if not self._hit("GetWindowRect", hwnd):
            return 0
        r = prect.contents
        r.left, r.top, r.right, r.bottom = self.rect
        return 1

    def MonitorFromWindow(self, hwnd: int, flags: int) -> int:
        self._hit("MonitorFromWindow", hwnd, flags)
        return 0xA1

    def MonitorFromPoint(self, pt: Any, flags: int) -> int:
        self._hit("MonitorFromPoint", pt.x, pt.y, flags)
        return 0xA2

    def GetMonitorInfoW(self, mon: int, pinfo: Any) -> int:
        info = pinfo.contents
        if not self._hit("GetMonitorInfoW", mon, info.cbSize):
            return 0
        r = info.rcMonitor
        r.left, r.top, r.right, r.bottom = self.monitor
        return 1

    def SetThreadDpiAwarenessContext(self, ctx: Any) -> int:
        value = _value(ctx)
        self._hit("SetThreadDpiAwarenessContext", value)
        if value in self.reject_ctx:
            return 0
        old, self.thread_ctx = self.thread_ctx, value
        return old

    def GetWindowDpiAwarenessContext(self, hwnd: int) -> int | None:
        return self.window_ctx


def names(fns: FakeFns) -> list[str]:
    return [c[0] for c in fns.calls]


def assert_released(fns: FakeFns, created: bool = True) -> None:
    """Всё, что создано, освобождено ровно один раз; битмап вынут из DC до удаления; DC экрана возвращён."""
    n = names(fns)
    assert n.count("ReleaseDC") == 1 and ("ReleaseDC", None, 0xD0) in fns.calls
    if created:
        assert n.count("DeleteDC") == 1 and ("DeleteDC", 0xD1) in fns.calls
    assert [c[1] for c in fns.calls if c[0] == "DeleteObject"] == list(fns.buffers)
    if "SelectObject" in n:
        restore = ("SelectObject", 0xD1, STOCK_BITMAP)
        assert fns.calls.count(restore) == 1 and fns.calls.index(restore) < n.index("DeleteObject")


def test_struct_sizes() -> None:
    assert ctypes.sizeof(screen.BITMAPINFOHEADER) == 40
    assert ctypes.sizeof(screen.MONITORINFO) == 40
    assert ctypes.sizeof(screen.RECT) == 16 and ctypes.sizeof(screen.POINT) == 8


def test_grab_rect_sequence_and_cleanup() -> None:
    fns = FakeFns()
    out = screen._Api(fns).grab_rect((-1920, 0, -1856, 16))
    assert isinstance(out, Shot) and (out.width, out.height) == (64, 16) and out.bgra == PIXEL * (64 * 16)
    assert fns.headers == [
        {"size": 40, "width": 64, "height": -16, "planes": 1, "bits": 32, "compression": 0}
    ]
    blt = next(c for c in fns.calls if c[0] == "BitBlt")
    assert blt[1:] == (0xD1, 0, 0, 64, 16, 0xD0, -1920, 0, screen.SRCCOPY | screen.CAPTUREBLT)
    assert_released(fns)
    assert fns.selected[0xD1] == STOCK_BITMAP  # битмап вынут из DC до удаления


def test_grab_window_print_window_full_content() -> None:
    fns = FakeFns()
    out = screen._Api(fns).grab_window(77)
    assert isinstance(out, Shot) and (out.width, out.height) == (64, 32)
    assert ("PrintWindow", 77, 0xD1, screen.PW_RENDERFULLCONTENT) in fns.calls
    assert_released(fns)


@pytest.mark.parametrize("step", ["BitBlt", "CreateDIBSection", "CreateCompatibleDC"])
def test_gdi_released_on_failure(step: str) -> None:
    fns = FakeFns()
    fns.fail.add(step)
    assert screen._Api(fns).grab_rect((0, 0, 8, 8)) is None
    assert_released(fns, created=step != "CreateCompatibleDC")


def test_gdi_released_on_exception() -> None:
    fns = FakeFns()
    fns.boom.add("PrintWindow")
    with pytest.raises(RuntimeError):
        screen._Api(fns).grab_window(77)
    assert_released(fns)
    assert fns.thread_ctx == 0x22  # контекст DPI потока восстановлен и после исключения


def test_no_screen_dc() -> None:
    fns = FakeFns()
    fns.fail.add("GetDC")
    assert screen._Api(fns).grab_rect((0, 0, 8, 8)) is None
    assert "CreateCompatibleDC" not in names(fns) and "ReleaseDC" not in names(fns)


@pytest.mark.parametrize(
    ("rect", "size"),
    [((0, 0, 10_000, 10), (8192, 10)), ((0, 0, 10, 9_000), (10, 8192)), ((5, 5, 5, 40), None)],
)
def test_size_is_limited(rect: tuple[int, int, int, int], size: tuple[int, int] | None) -> None:
    fns = FakeFns()
    fns.rect = rect
    out = screen._Api(fns).grab_window(77)
    if size is None:  # нулевая ширина — снимать нечего, GDI не трогается
        assert out is None and "GetDC" not in names(fns)
        return
    assert isinstance(out, Shot) and (out.width, out.height) == size
    assert (fns.headers[0]["width"], -fns.headers[0]["height"]) == size


def test_window_grab_uses_window_dpi_context_and_restores() -> None:
    fns = FakeFns()
    screen._Api(fns).grab_window(77)
    ctx = [c[1] for c in fns.calls if c[0] == "SetThreadDpiAwarenessContext"]
    assert ctx == [0x12, 0x22]  # контекст самого окна, потом — как было
    assert fns.ctx_during_rect == [0x12] and fns.thread_ctx == 0x22


def test_window_dpi_falls_back_to_per_monitor() -> None:
    fns = FakeFns()
    fns.reject_ctx.add(0x12)
    screen._Api(fns).grab_window(77)
    ctx = [c[1] for c in fns.calls if c[0] == "SetThreadDpiAwarenessContext"]
    assert ctx == [0x12, PMV2, 0x22] and fns.ctx_during_rect == [PMV2]
    fns = FakeFns()
    fns.reject_ctx |= {0x12, PMV2, PMV1}  # ничего не встало — снимаем как есть, восстанавливать нечего
    assert isinstance(screen._Api(fns).grab_window(77), Shot)
    assert [c[1] for c in fns.calls if c[0] == "SetThreadDpiAwarenessContext"] == [0x12, PMV2, PMV1]


def test_screen_grab_and_monitor_are_per_monitor_aware() -> None:
    fns = FakeFns()
    api = screen._Api(fns)
    assert api.monitor_rect(None) == (-1920, 0, 0, 1080)
    assert ("MonitorFromPoint", 0, 0, screen.MONITOR_DEFAULTTOPRIMARY) in fns.calls
    assert ("GetMonitorInfoW", 0xA2, 40) in fns.calls  # cbSize заполнен
    assert api.monitor_rect(55) == (-1920, 0, 0, 1080)
    assert ("MonitorFromWindow", 55, screen.MONITOR_DEFAULTTONEAREST) in fns.calls
    api.grab_rect((0, 0, 8, 8))
    ctx = [c[1] for c in fns.calls if c[0] == "SetThreadDpiAwarenessContext"]
    assert ctx == [PMV2, 0x22] * 3 and fns.thread_ctx == 0x22


def test_old_windows_without_dpi_functions() -> None:
    fns = FakeFns()
    fns.SetThreadDpiAwarenessContext = None  # type: ignore[assignment,method-assign]
    fns.GetWindowDpiAwarenessContext = None  # type: ignore[assignment,method-assign]
    api = screen._Api(fns)
    assert isinstance(api.grab_window(77), Shot) and api.window_rect(77) == (10, 20, 74, 52)


def test_window_rect_failure() -> None:
    fns = FakeFns()
    fns.fail.add("GetWindowRect")
    api = screen._Api(fns)
    assert api.window_rect(77) is None and api.grab_window(77) is None
    assert "GetDC" not in names(fns)


def test_capture_screen_end_to_end_with_fake_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий _Api (геометрия, GDI, DPI) под capture_screen: основной монитор → Shot с точками."""
    fns = FakeFns()
    fns.monitor = (0, 0, 32, 16)
    monkeypatch.setattr(screen, "_api", screen._Api(fns))
    monkeypatch.setattr(windows, "list_windows", lambda: [])
    out = screen.capture_screen(None, "user")
    assert isinstance(out, Shot) and (out.width, out.height) == (32, 16) and out.bgra == PIXEL * 512
    assert_released(fns)


@pytest.mark.skipif(sys.platform != "win32", reason="нужен настоящий Windows")
def test_real_functions_load() -> None:
    fns = screen._load_fns()
    for name in ("GetDC", "ReleaseDC", "CreateCompatibleDC", "DeleteDC", "CreateDIBSection", "SelectObject",
                 "DeleteObject", "BitBlt", "GdiFlush", "PrintWindow", "GetWindowRect", "MonitorFromWindow",
                 "MonitorFromPoint", "GetMonitorInfoW", "SetThreadDpiAwarenessContext",
                 "GetWindowDpiAwarenessContext"):  # fmt: skip
        assert getattr(fns, name) is not None, name
