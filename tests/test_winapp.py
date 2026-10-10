"""jarvis.winapp: хоткей и запасные сочетания, автозапуск (мок реестра), Job Object (фейк win32job), mutex."""

import subprocess
import sys
import time
import types
from pathlib import PureWindowsPath
from typing import Any

import pytest

from jarvis import winapp
from pc import settings


class FakeApi:
    """Фейковый слой ОС: записывает вызовы, ответы задаёт тест."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.hotkey_errors: dict[tuple[int, int], int] = {}  # (mods, vk) → GetLastError
        self.mutex_exists = False
        self.mutex_error: OSError | None = None
        self.windows: list[int] = []  # что вернёт find_window по очереди
        self.registry: dict[str, str] = {}

    def register_hotkey(self, hwnd: int, hotkey_id: int, mods: int, vk: int) -> tuple[bool, int]:
        self.calls.append(("register", hwnd, hotkey_id, mods, vk))
        err = self.hotkey_errors.get((mods, vk), 0)
        return err == 0, err

    def unregister_hotkey(self, hwnd: int, hotkey_id: int) -> bool:
        self.calls.append(("unregister", hwnd, hotkey_id))
        return True

    def create_mutex(self, name: str) -> tuple[int, bool]:
        self.calls.append(("mutex", name))
        if self.mutex_error is not None:
            raise self.mutex_error
        return 77, self.mutex_exists

    def close_handle(self, handle: int) -> None:
        self.calls.append(("close", handle))

    def register_window_message(self, name: str) -> int:
        return 0xC123

    def find_window(self, title: str) -> int:
        self.calls.append(("find", title))
        return self.windows.pop(0) if self.windows else 0

    def window_pid(self, hwnd: int) -> int:
        return 4321

    def allow_set_foreground(self, pid: int) -> bool:
        self.calls.append(("allow", pid))
        return True

    def post_message(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        self.calls.append(("post", hwnd, msg))
        return True

    def set_foreground(self, hwnd: int) -> bool:
        self.calls.append(("foreground", hwnd))
        return True

    def reg_get(self, name: str) -> str | None:
        return self.registry.get(name)

    def reg_set(self, name: str, value: str) -> None:
        self.registry[name] = value

    def reg_delete(self, name: str) -> bool:
        return self.registry.pop(name, None) is not None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi()
    monkeypatch.setattr(winapp, "_api", api)
    winapp.show_message_id.cache_clear()
    yield api
    winapp.show_message_id.cache_clear()


# --- разбор хоткея ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "mods", "vk"),
    [
        ("ctrl+alt+space", winapp.MOD_CONTROL | winapp.MOD_ALT, 0x20),
        ("Ctrl + Alt + Space", winapp.MOD_CONTROL | winapp.MOD_ALT, 0x20),
        ("ctrl+alt+shift+space", winapp.MOD_CONTROL | winapp.MOD_ALT | winapp.MOD_SHIFT, 0x20),
        ("ctrl+alt+j", winapp.MOD_CONTROL | winapp.MOD_ALT, ord("J")),
        ("alt+shift+F5", winapp.MOD_ALT | winapp.MOD_SHIFT, 0x74),
        ("win+f24", winapp.MOD_WIN, 0x87),
        ("control+7", winapp.MOD_CONTROL, 0x37),
        ("ctrl+`", winapp.MOD_CONTROL, 0xC0),
        ("ctrl+alt+numpad5", winapp.MOD_CONTROL | winapp.MOD_ALT, 0x65),
        ("f13", 0, 0x7C),  # F13–F24 можно без модификаторов
    ],
)
def test_parse_hotkey(text: str, mods: int, vk: int) -> None:
    assert winapp.parse_hotkey(text) == (mods | winapp.MOD_NOREPEAT, vk)


def test_parse_hotkey_always_norepeat() -> None:
    for text in ["ctrl+alt+space", *winapp.FALLBACK_HOTKEYS]:
        mods, _vk = winapp.parse_hotkey(text)
        assert mods & winapp.MOD_NOREPEAT == 0x4000


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "пустое"),
        ("   ", "пустое"),
        ("ctrl+", "лишний"),
        ("ctrl++a", "лишний"),
        ("ctrl+alt", "нет обычной клавиши"),
        ("space", "модификатор"),
        ("f5", "модификатор"),
        ("ctrl+a+b", "две обычные"),
        ("ctrl+ctrl+a", "повторяется"),
        ("ctrl+alt+ж", "неизвестная клавиша"),
        ("ctrl+alt+f25", "неизвестная клавиша"),
        ("hyper+space", "неизвестная клавиша"),
    ],
)
def test_parse_hotkey_errors(text: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        winapp.parse_hotkey(text)


def test_format_hotkey() -> None:
    assert winapp.format_hotkey("ctrl+alt+space") == "Ctrl+Alt+Space"
    assert winapp.format_hotkey("shift+alt+ctrl+j") == "Ctrl+Alt+Shift+J"
    assert winapp.format_hotkey("win+f12") == "Win+F12"
    assert winapp.format_hotkey("не сочетание") == "не сочетание"


# --- регистрация и запасные сочетания ----------------------------------------------------------------


def _key(text: str) -> tuple[int, int]:
    return winapp.parse_hotkey(text)


def test_register_primary_ok(fake: FakeApi) -> None:
    result = winapp.register_with_fallback(100, 1, "ctrl+alt+space")
    assert result == winapp.HotkeyResult("ctrl+alt+space", "")
    assert fake.calls == [("register", 100, 1, *_key("ctrl+alt+space"))]


def test_register_busy_uses_fallback(fake: FakeApi) -> None:
    fake.hotkey_errors[_key("ctrl+alt+space")] = winapp.ERROR_HOTKEY_ALREADY_REGISTERED
    result = winapp.register_with_fallback(100, 1, "ctrl+alt+space")
    assert result.combo == "ctrl+alt+shift+space"
    assert "Ctrl+Alt+Space занято" in result.message
    assert "Ctrl+Alt+Shift+Space" in result.message
    assert [c[3:] for c in fake.calls] == [_key("ctrl+alt+space"), _key("ctrl+alt+shift+space")]


def test_register_second_fallback(fake: FakeApi) -> None:
    for combo in ("ctrl+alt+space", "ctrl+alt+shift+space"):
        fake.hotkey_errors[_key(combo)] = winapp.ERROR_HOTKEY_ALREADY_REGISTERED
    result = winapp.register_with_fallback(100, 1, "ctrl+alt+space")
    assert result.combo == "ctrl+alt+j"
    assert len(fake.calls) == 3


def test_register_all_busy(fake: FakeApi) -> None:
    for combo in ("ctrl+alt+space", *winapp.FALLBACK_HOTKEYS):
        fake.hotkey_errors[_key(combo)] = winapp.ERROR_HOTKEY_ALREADY_REGISTERED
    result = winapp.register_with_fallback(100, 1, "ctrl+alt+space")
    assert result.combo is None
    assert "из трея" in result.message


def test_register_other_error_stops(fake: FakeApi) -> None:
    fake.hotkey_errors[_key("ctrl+alt+space")] = 1400  # ERROR_INVALID_WINDOW_HANDLE
    result = winapp.register_with_fallback(100, 1, "ctrl+alt+space")
    assert result.combo is None
    assert "1400" in result.message
    assert len(fake.calls) == 1  # запасные при чужой ошибке не пробуем


def test_register_bad_config_hotkey_uses_fallback(fake: FakeApi) -> None:
    result = winapp.register_with_fallback(100, 1, "ctrl+alt+пробел")
    assert result.combo == "ctrl+alt+shift+space"
    assert "не разобрано" in result.message


def test_register_preferred_equal_to_fallback_not_repeated(fake: FakeApi) -> None:
    fake.hotkey_errors[_key("ctrl+alt+j")] = winapp.ERROR_HOTKEY_ALREADY_REGISTERED
    result = winapp.register_with_fallback(100, 1, "Ctrl+Alt+J")
    assert result.combo == "ctrl+alt+shift+space"
    assert [c[3:] for c in fake.calls] == [_key("ctrl+alt+j"), _key("ctrl+alt+shift+space")]


def test_unregister(fake: FakeApi) -> None:
    assert winapp.unregister_hotkey(100, 1)
    assert fake.calls == [("unregister", 100, 1)]


def test_foreground_helpers(fake: FakeApi) -> None:
    assert winapp.allow_set_foreground_any()
    assert winapp.set_foreground(555)
    assert fake.calls == [("allow", 0xFFFFFFFF), ("foreground", 555)]


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows слой ОС настоящий")
def test_not_windows_without_fake() -> None:
    assert winapp._api is None
    assert not winapp.available()
    assert not winapp.autostart_enabled()
    assert "только в Windows" in winapp.set_autostart(True)
    assert winapp.register_hotkey(1, 1, 0, 0x20) == (False, 0)
    assert not winapp.set_foreground(1)


def test_no_qt_import() -> None:
    code = "import sys, jarvis.winapp; sys.exit(any(m.startswith('PySide6') for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", code], check=False, timeout=60).returncode == 0


# --- один экземпляр ---------------------------------------------------------------------------------


def test_mutex_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERNAME", "me")
    assert winapp.instance_mutex_name() == r"Local\Jarvis-me-run"
    monkeypatch.setenv("USERNAME", r"evil\..\name")
    assert "\\" not in winapp.instance_mutex_name().removeprefix("Local\\")


def test_acquire_first_instance(fake: FakeApi) -> None:
    assert winapp.acquire_instance() == 77
    assert fake.calls == [("mutex", winapp.instance_mutex_name())]
    winapp.release_instance(77)
    assert fake.calls[-1] == ("close", 77)


def test_acquire_second_instance(fake: FakeApi) -> None:
    fake.mutex_exists = True
    assert winapp.acquire_instance() is None
    assert ("close", 77) in fake.calls  # свою копию дескриптора закрываем


def test_acquire_mutex_error_still_runs(fake: FakeApi) -> None:
    fake.mutex_error = OSError(5, "доступ запрещён")
    assert winapp.acquire_instance() == 0
    winapp.release_instance(0)
    assert ("close", 0) not in fake.calls


def test_signal_first_instance_waits_for_window(fake: FakeApi) -> None:
    fake.windows = [0, 0, 9001]
    assert winapp.signal_first_instance(timeout_s=2.0)
    assert ("allow", 4321) in fake.calls
    assert fake.calls[-1] == ("post", 9001, 0xC123)
    assert ("find", winapp.RECEIVER_TITLE) in fake.calls


def test_signal_first_instance_timeout(fake: FakeApi) -> None:
    t0 = time.monotonic()
    assert not winapp.signal_first_instance(timeout_s=0.15)
    assert time.monotonic() - t0 < 2
    assert not any(c[0] == "post" for c in fake.calls)


# --- автозапуск ------------------------------------------------------------------------------------


def test_autostart_command_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "is_frozen", lambda: False)
    monkeypatch.setattr(sys, "executable", r"C:\Jarvis\.venv\Scripts\python.exe")
    assert winapp.autostart_command() == r'"C:\Jarvis\.venv\Scripts\pythonw.exe" -m jarvis run'


def test_autostart_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    install = PureWindowsPath(r"C:\Users\me\AppData\Local\Programs\Jarvis")
    monkeypatch.setattr(settings, "install_dir", lambda: install)
    assert winapp.autostart_command() == r'"C:\Users\me\AppData\Local\Programs\Jarvis\Jarvis.exe"'


class FakeKey:
    def __init__(self, reg: "FakeWinreg", path: str) -> None:
        self.reg, self.path = reg, path

    def __enter__(self) -> "FakeKey":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeWinreg(types.ModuleType):
    """Мок winreg: только то, что нужно HKCU\\…\\Run."""

    HKEY_CURRENT_USER = 0x80000001
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        super().__init__("winreg")
        self.values: dict[tuple[str, str], tuple[Any, int]] = {}
        self.keys: set[str] = set()

    def OpenKey(self, root: int, path: str, reserved: int = 0, access: int = 0) -> FakeKey:
        assert root == self.HKEY_CURRENT_USER
        if path not in self.keys:
            raise FileNotFoundError(path)
        return FakeKey(self, path)

    def CreateKeyEx(self, root: int, path: str, reserved: int = 0, access: int = 0) -> FakeKey:
        assert root == self.HKEY_CURRENT_USER and access == self.KEY_SET_VALUE
        self.keys.add(path)
        return FakeKey(self, path)

    def QueryValueEx(self, key: FakeKey, name: str) -> tuple[Any, int]:
        if (key.path, name) not in self.values:
            raise FileNotFoundError(name)
        return self.values[(key.path, name)]

    def SetValueEx(self, key: FakeKey, name: str, reserved: int, kind: int, value: Any) -> None:
        self.values[(key.path, name)] = (value, kind)

    def DeleteValue(self, key: FakeKey, name: str) -> None:
        if (key.path, name) not in self.values:
            raise FileNotFoundError(name)
        del self.values[(key.path, name)]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeWinreg:
    """Настоящий _WinApi с подменённым модулем winreg — реестр не трогается ни на какой ОС."""
    reg = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", reg)
    monkeypatch.setattr(winapp, "_api", winapp._WinApi())
    monkeypatch.setattr(settings, "is_frozen", lambda: False)
    monkeypatch.setattr(sys, "executable", r"C:\Jarvis\.venv\Scripts\python.exe")
    return reg


def test_autostart_on_off(registry: FakeWinreg) -> None:
    assert not winapp.autostart_enabled()  # ключа Run ещё нет
    text = winapp.set_autostart(True)
    assert text.startswith("Автозапуск включён")
    value, kind = registry.values[(winapp.RUN_KEY, "Jarvis")]
    assert value == r'"C:\Jarvis\.venv\Scripts\pythonw.exe" -m jarvis run'
    assert kind == FakeWinreg.REG_SZ
    assert winapp.autostart_enabled()
    assert winapp.set_autostart(False) == "Автозапуск выключен"
    assert not winapp.autostart_enabled()
    assert winapp.set_autostart(False) == "Автозапуск и так выключен"


def test_autostart_registry_error(registry: FakeWinreg, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object) -> None:
        raise PermissionError(5, "Отказано в доступе")

    monkeypatch.setattr(registry, "CreateKeyEx", denied)
    assert winapp.set_autostart(True).startswith("Не удалось изменить автозапуск")


def test_autostart_with_fake_api(fake: FakeApi) -> None:
    winapp.set_autostart(True)
    assert fake.registry["Jarvis"] == winapp.autostart_command()
    assert winapp.autostart_enabled()


# --- Job Object ------------------------------------------------------------------------------------


class FakeHandle:
    def __init__(self, pid: int) -> None:
        self.pid = pid


@pytest.fixture
def pywin(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Фейковые win32job, win32api, win32con под настоящим _WinApi."""
    state: dict[str, Any] = {"assigned": [], "opened": [], "closed": [], "in_job": set(), "set_info": None}
    job = types.ModuleType("win32job")
    job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    job.JobObjectExtendedLimitInformation = 9

    def create(attrs: object, name: str) -> str:
        assert attrs is None
        state["created"] = name
        return "JOB"

    def query(handle: str, cls: int) -> dict[str, Any]:
        assert (handle, cls) == ("JOB", 9)
        return {"BasicLimitInformation": {"LimitFlags": 0x4}, "IoInfo": {}}

    def set_info(handle: str, cls: int, info: dict[str, Any]) -> None:
        state["set_info"] = (handle, cls, info)

    def is_in_job(h: FakeHandle, handle: str) -> bool:
        return h.pid in state["in_job"]

    def assign(handle: str, h: FakeHandle) -> None:
        state["assigned"].append((handle, h.pid))

    job.CreateJobObject = create
    job.QueryInformationJobObject = query
    job.SetInformationJobObject = set_info
    job.IsProcessInJob = is_in_job
    job.AssignProcessToJobObject = assign
    api = types.ModuleType("win32api")

    def open_process(access: int, inherit: bool, pid: int) -> FakeHandle:
        if pid == 666:
            raise OSError(87, "Неверный параметр")
        state["opened"].append((access, inherit, pid))
        return FakeHandle(pid)

    api.OpenProcess = open_process
    api.CloseHandle = lambda h: state["closed"].append(h if isinstance(h, str) else h.pid)
    con = types.ModuleType("win32con")
    con.PROCESS_SET_QUOTA = 0x100
    con.PROCESS_TERMINATE = 0x1
    con.PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    for mod in (job, api, con):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
    monkeypatch.setattr(winapp, "_api", winapp._WinApi())
    monkeypatch.setattr(winapp, "_child_pids", lambda pid: [])
    return state


def test_create_job_kill_on_close(pywin: dict[str, Any]) -> None:
    assert winapp.create_job() == "JOB"
    handle, cls, info = pywin["set_info"]
    assert (handle, cls) == ("JOB", 9)
    assert info["BasicLimitInformation"]["LimitFlags"] == 0x4 | 0x2000  # прежние флаги сохранены


def test_create_job_failure_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def create_job(self) -> None:
            raise ImportError("нет pywin32")

    monkeypatch.setattr(winapp, "_api", Broken())
    assert winapp.create_job() is None
    assert winapp.job_hook(None) is None


def test_add_to_job_popen_and_children(pywin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winapp, "_child_pids", lambda pid: [11, 12] if pid == 10 else [])
    pywin["in_job"].add(12)  # унаследовал членство — повторно не добавляем
    proc = types.SimpleNamespace(pid=10)
    assert winapp.add_to_job("JOB", proc)
    assert pywin["assigned"] == [("JOB", 10), ("JOB", 11)]
    access = pywin["opened"][0][0]
    assert access & 0x100 and access & 0x1  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    assert pywin["opened"][0][1] is False
    assert sorted(pywin["closed"]) == [10, 11, 12]  # дескрипторы процессов закрыты


def test_add_to_job_error_is_logged_not_raised(pywin: dict[str, Any]) -> None:
    assert not winapp.add_to_job("JOB", 666)
    assert not winapp.add_to_job(None, 10)
    assert not winapp.add_to_job("JOB", None)
    assert pywin["assigned"] == []


def test_job_hook_and_close(pywin: dict[str, Any]) -> None:
    hook = winapp.job_hook("JOB")
    assert hook is not None
    hook(types.SimpleNamespace(pid=20))
    hook(21)
    assert pywin["assigned"] == [("JOB", 20), ("JOB", 21)]
    winapp.close_job("JOB")
    assert pywin["closed"][-1] == "JOB"
    winapp.close_job(None)


def test_own_descendants_finds_child() -> None:
    import psutil

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        name = psutil.Process(child.pid).name()
        assert child.pid in winapp.own_descendants(name.upper())
        assert winapp.own_descendants("llama-server.exe") == []
    finally:
        child.kill()
        child.wait(10)


# --- настоящий Windows (CI windows-latest) ------------------------------------------------------------


win_only = pytest.mark.skipif(sys.platform != "win32", reason="нужен настоящий Windows")


@win_only
def test_real_ctypes_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    api = winapp._WinApi()
    assert api.tick_count() > 0
    assert api.register_window_message("Jarvis.Test") >= 0xC000
    assert api.find_window("Jarvis.NoSuchWindow.7f3a") == 0
    assert api.set_foreground(0) is False


@win_only
def test_real_mutex() -> None:
    api = winapp._WinApi()
    name = rf"Local\Jarvis-test-{time.time_ns()}"
    first, existed = api.create_mutex(name)
    try:
        assert first and not existed
        second, existed2 = api.create_mutex(name)
        api.close_handle(second)
        assert existed2
    finally:
        api.close_handle(first)


@win_only
def test_real_job_kills_child_on_close(monkeypatch: pytest.MonkeyPatch) -> None:
    from pc import subproc

    monkeypatch.setattr(winapp, "_api", winapp._WinApi())
    job = winapp.create_job()
    assert job is not None
    child = subproc.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert winapp.add_to_job(job, child)
        assert winapp.add_to_job(job, child)  # повторно — без ошибки (уже в job)
        winapp.close_job(job)
        child.wait(10)  # KILL_ON_JOB_CLOSE: Windows завершила процесс
    finally:
        if child.poll() is None:
            child.kill()
