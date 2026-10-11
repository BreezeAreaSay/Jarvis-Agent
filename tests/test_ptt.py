"""jarvis.ptt: имя клавиши → VK, колбэк хука, поток хука с переустановкой (фейковый слой ОС)."""

import queue
import sys
import threading
from collections.abc import Callable
from typing import Any

import pytest

from jarvis import ptt

# --- имя клавиши ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "vk"),
    [
        ("right ctrl", 0xA3),
        ("RightCtrl", 0xA3),
        ("rctrl", 0xA3),
        ("ctrl right", 0xA3),
        ("Right Control", 0xA3),
        ("правый ctrl", 0xA3),
        ("ПРАВЫЙ КОНТРОЛ", 0xA3),
        ("left ctrl", 0xA2),
        ("левый шифт", 0xA0),
        ("right_alt", 0xA5),
        ("altgr", 0xA5),
        ("right win", 0x5C),
        ("menu", 0x5D),
        ("caps lock", 0x14),
        ("Scroll-Lock", 0x91),
        ("F9", 0x78),
        ("f24", 0x87),
        ("0xA3", 0xA3),
        ("0x7b", 0x7B),
    ],
)
def test_parse_key(name: str, vk: int) -> None:
    assert ptt.parse_key(name) == vk


@pytest.mark.parametrize(
    "name", ["", "ctrl", "control", "shift", "alt", "win", "xyz", "f25", "0x00", "0x1FF"]
)
def test_parse_key_errors(name: str) -> None:
    with pytest.raises(ValueError):
        ptt.parse_key(name)


def test_parse_key_ambiguous_message() -> None:
    with pytest.raises(ValueError, match="right ctrl или left ctrl"):
        ptt.parse_key("ctrl")


def test_default_key_parses() -> None:
    assert ptt.parse_key(ptt.DEFAULT_KEY) == ptt.VK_RCONTROL


def test_key_label() -> None:
    assert ptt.key_label(0xA3) == "правый Ctrl"
    assert ptt.key_label(0x78) == "F9"
    assert ptt.key_label(0x41) == "клавиша 0x41"


# --- колбэк -------------------------------------------------------------------------------------


def drain(q: "queue.SimpleQueue[Any]") -> list[str]:
    out = []
    while True:
        try:
            out.append(q.get_nowait()[0])
        except queue.Empty:
            return out


def test_on_key_down_up_without_autorepeat() -> None:
    q: queue.SimpleQueue[ptt.PttEvent] = queue.SimpleQueue()
    hook = ptt.Hook(0xA3, q)
    for _ in range(5):  # автоповтор удержания — одно down
        hook.on_key(0xA3, up=False)
    hook.on_key(0xA3, up=True)
    hook.on_key(0xA3, up=True)  # дубль отпускания (два хука при переустановке) — отсеян
    assert drain(q) == ["down", "up"]
    assert hook.down is False


def test_on_key_other_only_while_held() -> None:
    q: queue.SimpleQueue[ptt.PttEvent] = queue.SimpleQueue()
    hook = ptt.Hook(0xA3, q)
    hook.on_key(0x43, up=False)  # «C» без PTT — не наше дело
    hook.on_key(0xA3, up=False)
    hook.on_key(0x43, up=False)  # правый Ctrl+C — сочетание
    hook.on_key(0x43, up=True)  # отпускание другой клавиши — не событие
    hook.on_key(0xA3, up=True)
    assert drain(q) == ["down", "other", "up"]


def test_escape_anywhere_is_event() -> None:
    q: queue.SimpleQueue[ptt.PttEvent] = queue.SimpleQueue()
    hook = ptt.Hook(0xA3, q)
    hook.on_key(ptt.VK_ESCAPE, up=False)  # Esc в любом окне — заглушить речь
    hook.on_key(ptt.VK_ESCAPE, up=True)
    hook.on_key(0xA3, up=False)
    hook.on_key(ptt.VK_ESCAPE, up=False)  # Esc при удержании PTT — сочетание, отмена записи
    assert drain(q) == ["esc", "down", "other"]


def test_release_resets_down_flag() -> None:
    q: queue.SimpleQueue[ptt.PttEvent] = queue.SimpleQueue()
    hook = ptt.Hook(0xA3, q)
    hook.on_key(0xA3, up=False)
    hook.release()  # voice: отпускание потеряно, клавиша физически отпущена
    hook.on_key(0xA3, up=False)
    assert drain(q) == ["down", "down"]


class FakeApi:
    """Слой ОС для потока хука: сообщения потока — по сценарию, затем ждём WM_QUIT."""

    def __init__(self, script: list[Callable[[], int | None]] | None = None, fail_install: int = 0) -> None:
        self.script = list(script or [])
        self.installed: list[int] = []
        self.uninstalled: list[int] = []
        self.handle = 100
        self.fail_install = fail_install  # столько первых install() падают
        self.quit = threading.Event()
        self.script_done = threading.Event()
        self.timer_killed: int | None = None
        self.keys: dict[int, bool] = {}
        self.proc: Any = None

    def thread_id(self) -> int:
        return 77

    def make_proc(self, fn: Any) -> Any:
        self.proc = ("proc", fn)
        return self.proc

    def install(self, proc: Any) -> int:
        assert proc is self.proc
        if self.fail_install:
            self.fail_install -= 1
            raise OSError("отказано")
        self.handle += 1
        self.installed.append(self.handle)
        return self.handle

    def uninstall(self, handle: int) -> None:
        self.uninstalled.append(handle)

    def call_next(self, code: int, wparam: int, lparam: int) -> int:
        return 42

    def vk_of(self, lparam: int) -> int:
        return lparam

    def get_message(self) -> int | None:
        if self.script:
            return self.script.pop(0)()
        self.script_done.set()
        self.quit.wait(5)
        return None

    def post_quit(self, thread_id: int) -> None:
        assert thread_id == 77
        self.quit.set()

    def set_timer(self, ms: int) -> int:
        return 9

    def kill_timer(self, timer: int) -> None:
        self.timer_killed = timer

    def key_down(self, vk: int) -> bool:
        return self.keys.get(vk, False)


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(ptt, "_api", fake)
    monkeypatch.setattr(ptt, "_proc", None)
    monkeypatch.setattr(ptt, "_active", None)
    return fake


def test_hook_proc_feeds_active_hook_and_calls_next(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    q: queue.SimpleQueue[ptt.PttEvent] = queue.SimpleQueue()
    hook = ptt.Hook(0xA3, q)
    monkeypatch.setattr(ptt, "_active", hook)
    assert ptt._hook_proc(ptt.HC_ACTION, ptt.WM_KEYDOWN, 0xA3) == 42
    assert ptt._hook_proc(ptt.HC_ACTION, ptt.WM_SYSKEYUP, 0xA3) == 42
    assert ptt._hook_proc(-1, ptt.WM_KEYDOWN, 0xA3) == 42  # nCode < 0 — только дальше по цепочке
    assert drain(q) == ["down", "up"]


def test_hook_proc_swallows_errors(fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(lparam: int) -> int:
        raise RuntimeError("плохой адрес")

    monkeypatch.setattr(fake_api, "vk_of", boom)
    monkeypatch.setattr(ptt, "_active", ptt.Hook(0xA3, queue.SimpleQueue()))
    assert ptt._hook_proc(ptt.HC_ACTION, ptt.WM_KEYDOWN, 1) == 42


def test_hook_proc_without_active_hook(fake_api: FakeApi) -> None:
    assert ptt._hook_proc(ptt.HC_ACTION, ptt.WM_KEYDOWN, 0xA3) == 42


# --- поток хука ---------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def tick(clock: Clock, advance: float) -> Callable[[], int]:
    def step() -> int:
        clock.now += advance
        return ptt.WM_TIMER

    return step


def run_hook(fake: FakeApi, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> tuple[ptt.Hook, list[str]]:
    monkeypatch.setattr(ptt.time, "monotonic", clock)
    q: queue.SimpleQueue[ptt.PttEvent] = queue.SimpleQueue()
    hook = ptt.Hook(0xA3, q)
    hook.start()
    assert fake.script_done.wait(5)
    hook.stop()
    return hook, drain(q)


def test_hook_installs_and_uninstalls(fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    hook, events = run_hook(fake_api, monkeypatch, Clock())
    assert fake_api.installed == [101]
    assert fake_api.uninstalled == [101]
    assert fake_api.timer_killed == 9
    assert events == []
    assert not hook.alive
    assert ptt._active is None
    assert ptt._proc is fake_api.proc  # объект колбэка живёт на уровне модуля


def test_hook_rehooks_periodically_new_before_old(fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    fake_api.script = [tick(clock, 1.0) for _ in range(int(ptt.REHOOK_S) + 1)]
    hook, events = run_hook(fake_api, monkeypatch, clock)
    assert hook.rehooks == 1
    assert fake_api.installed == [101, 102]
    assert fake_api.uninstalled == [101, 102]  # старый снят после установки нового; новый — при выходе
    assert events == []


def test_hook_resume_after_sleep(fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    fake_api.script = [tick(clock, 1.0), tick(clock, 600.0), tick(clock, 1.0)]  # ПК спал 10 минут
    hook, events = run_hook(fake_api, monkeypatch, clock)
    assert events == ["resume"]
    assert hook.rehooks == 1


def test_hook_rehook_failure_keeps_old(fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    fake_api.script = [tick(clock, 600.0)]

    def install_then_fail(proc: Any) -> int:
        if fake_api.installed:
            raise OSError("нет")
        fake_api.installed.append(101)
        return 101

    monkeypatch.setattr(fake_api, "install", install_then_fail)
    hook, _ = run_hook(fake_api, monkeypatch, clock)
    assert hook.rehooks == 0
    assert fake_api.uninstalled == [101]


def test_hook_start_failure_raises(fake_api: FakeApi) -> None:
    fake_api.fail_install = 1
    hook = ptt.Hook(0xA3, queue.SimpleQueue())
    with pytest.raises(ptt.PttError, match="хук клавиатуры не установлен"):
        hook.start()
    assert ptt._active is None


def test_second_hook_stops_first(fake_api: FakeApi) -> None:
    first = ptt.Hook(0xA3, queue.SimpleQueue())
    first.start()
    assert fake_api.script_done.wait(5)
    thread = first._thread
    second = ptt.Hook(0xA2, queue.SimpleQueue())
    second.start()  # сначала останавливает первый (WM_QUIT его потоку) и ждёт его
    assert thread is not None and not thread.is_alive()
    assert ptt._active is second
    second.stop()
    assert ptt._active is None


def test_key_down_reads_async_state(fake_api: FakeApi) -> None:
    hook = ptt.Hook(0xA3, queue.SimpleQueue())
    assert hook.key_down() is False
    fake_api.keys[0xA3] = True
    assert hook.key_down() is True


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows user32 есть")
def test_start_without_windows_is_ptt_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ptt, "_api", None)
    monkeypatch.setattr(ptt, "_active", None)
    with pytest.raises(ptt.PttError, match="недоступен"):
        ptt.Hook(0xA3, queue.SimpleQueue()).start()


@pytest.mark.skipif(sys.platform != "win32", reason="нужен user32")
def test_win_api_binds_functions() -> None:
    """Сигнатуры ctypes собираются (argtypes/restype), GetAsyncKeyState отвечает; хук не ставится."""
    api = ptt._WinApi()
    proc = api.make_proc(lambda code, wparam, lparam: 0)
    assert proc is not None
    assert isinstance(api.key_down(0xA3), bool)
    assert api.thread_id() > 0
