"""jarvis.app на фейках: порядок старта, локальный режим, трей, запросы, хоткей, подтверждения из потоков.

Соседние модули (окно, руки, мозг, Core, журнал, ConfirmServer, слой ОС winapp) — фейки, Qt — офскрин.
"""

import ctypes
import gc
import os
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from ctypes import wintypes
from pathlib import Path
from typing import Any, ClassVar

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget

from jarvis import app as app_mod
from jarvis import config, winapp
from jarvis.events import Done, Level, TextChunk
from jarvis.ui.tray import Tray
from pc import apps, confirm_client, files, settings
from pc import windows as pc_windows
from pc.result import Result
from pc.windows import WindowInfo

try:  # настоящее окно — для проверки контракта app ↔ ui (тест ниже пропускается, если окна ещё нет)
    from jarvis.ui import logic as real_logic
    from jarvis.ui import window as real_window
except Exception:  # pragma: no cover
    real_logic = real_window = None

CALLS: list[str] = []


def rec(name: str) -> None:
    CALLS.append(name)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


def wait_for(qapp: QApplication, cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return cond()


# --- фейки --------------------------------------------------------------------------------------------


class FakeWindow(QWidget):
    submitted = Signal(str)
    cancel_requested = Signal()
    open_item = Signal(int)
    hidden = Signal()

    last: "FakeWindow | None" = None

    def __init__(self, ui_cfg: Any) -> None:
        super().__init__()
        rec("window")
        self.ui_cfg = ui_cfg
        self.events: list[Any] = []
        self.begun: list[str] = []
        self.confirms: list[Any] = []
        self.confirm_threads: list[threading.Thread] = []
        self.local: bool | None = None
        self.busy = False
        self.active = False
        FakeWindow.last = self

    def show_launcher(self) -> None:
        rec("show")
        self.show()

    def hide_launcher(self) -> None:
        rec("hide")
        self.hide()

    def isActiveWindow(self) -> bool:  # офскрин сам окна не активирует
        return self.active and self.isVisible()

    def begin_request(self, text: str) -> None:
        self.begun.append(text)

    def on_event(self, ev: Any) -> None:
        self.events.append(ev)

    def ask_confirm(self, req: Any) -> None:
        self.confirm_threads.append(threading.current_thread())
        self.confirms.append(req)

    def set_local_mode(self, on: bool) -> None:
        self.local = on

    def show_toast(self, text: str, kind: str = "error") -> None:
        pass

    def is_busy(self) -> bool:
        return self.busy


class FakeConfirmRequest:
    def __init__(self, summary: str, details: str = "", caller: str = "user") -> None:
        self.summary, self.details, self.caller = summary, details, caller
        self.approved: bool | None = None
        self._event = threading.Event()

    def resolve(self, approved: bool) -> bool:
        if self._event.is_set():
            return False
        self.approved = approved
        self._event.set()
        return True

    def wait(self, timeout: float = 60) -> bool:
        if not self._event.wait(timeout):
            self.resolve(False)
        return self.approved is True


class FakeHands:
    fail: str = ""

    def __init__(self, cfg: Any, job_hook: Any = None) -> None:
        rec("hands")
        self.job_hook = job_hook
        self.status: dict[str, Any] = {"server": "unknown", "prefix_cache": "unknown", "vram": "unknown"}

    def ensure_server(self) -> None:
        rec("hands.ensure_server")
        if FakeHands.fail:
            self.status.update(server="error", error=FakeHands.fail)
            raise RuntimeError(FakeHands.fail)
        self.status["server"] = "ready"

    def warmup(self) -> None:
        rec("hands.warmup")
        self.status.update(prefix_cache="ok", vram="ok")

    def stop(self) -> None:
        rec("hands.stop")
        self.status["server"] = "stopped"

    def close(self) -> None:
        rec("hands.close")


class FakeBrain:
    def __init__(self, cfg: Any, confirm_address: str, process_hook: Any = None) -> None:
        rec("brain")
        self.confirm_address = confirm_address
        self.process_hook = process_hook
        self.ready = False
        self.status: dict[str, Any] = {"state": "closed", "error": ""}

    def start(self) -> None:
        rec("brain.start")
        self.ready = True
        self.status["state"] = "ready"

    close_threads: ClassVar[list[threading.Thread]] = []
    close_delay = 0.0

    def close(self) -> None:
        rec("brain.close")
        FakeBrain.close_threads.append(threading.current_thread())
        time.sleep(FakeBrain.close_delay)  # настоящий close() ждёт codex до ~2,5 с
        self.ready = False
        self.status["state"] = "closed"

    def new_conversation(self) -> None:
        rec("brain.new_conversation")

    def cancel(self) -> None:
        rec("brain.cancel")


class FakeCore:
    broken = False
    script: Callable[["FakeCore", str], Iterator[Any]] | None = None
    last: "FakeCore | None" = None

    def __init__(self, cfg: Any, hands: Any, brain: Any, journal: Any = None) -> None:
        if FakeCore.broken:
            raise RuntimeError("сломан")
        rec("core")
        self.cfg, self.hands, self.brain, self.journal = cfg, hands, brain, journal
        self.handled: list[tuple[str, float | None, Any]] = []
        self.cancelled = threading.Event()
        FakeCore.last = self

    def handle(self, text: str, ctx: Any, dry: bool = False, hotkey_ms: float | None = None) -> Iterator[Any]:
        self.handled.append((text, hotkey_ms, ctx))
        self.cancelled.clear()
        if FakeCore.script is not None:
            yield from FakeCore.script(self, text)
            return
        yield Level("grammar", "grammar:test")
        yield TextChunk(f"ответ: {text}")
        yield Done(ok=True, text=f"ответ: {text}")

    def cancel(self) -> None:
        rec("core.cancel")
        self.cancelled.set()


class FakeJournal:
    def __init__(self) -> None:
        rec("journal")

    def close(self) -> None:
        rec("journal.close")


class FakeServer:
    last: "FakeServer | None" = None

    def __init__(self, callback: Callable[[str, str, str], bool]) -> None:
        rec("confirm_server")
        self.callback = callback
        self.address = r"\\.\pipe\jarvis-confirm-me-1"
        FakeServer.last = self

    def start(self) -> None:
        rec("confirm_server.start")

    def close(self) -> None:
        rec("confirm_server.close")


class FakeWinApi:
    """Слой ОС winapp: хоткей, передний план, job, реестр — без Windows."""

    def __init__(self) -> None:
        self.busy_hotkeys: set[tuple[int, int]] = set()
        self.registry: dict[str, str] = {}
        self.assigned: list[int] = []
        self.foreground: list[int] = []
        self.tick = 1_000_000
        self.mutex_exists = False
        self.posted: list[tuple[int, int]] = []

    def register_hotkey(self, hwnd: int, hotkey_id: int, mods: int, vk: int) -> tuple[bool, int]:
        rec(f"hotkey:{mods:#x}:{vk:#x}")
        if (mods, vk) in self.busy_hotkeys:
            return False, winapp.ERROR_HOTKEY_ALREADY_REGISTERED
        return True, 0

    def unregister_hotkey(self, hwnd: int, hotkey_id: int) -> bool:
        rec("hotkey.unregister")
        return True

    def register_window_message(self, name: str) -> int:
        return 0xC0DE

    def tick_count(self) -> int:
        return self.tick

    def set_foreground(self, hwnd: int) -> bool:
        rec("set_foreground")
        self.foreground.append(hwnd)
        return True

    def allow_set_foreground(self, pid: int) -> bool:
        return True

    def create_job(self) -> str:
        rec("job")
        return "JOB"

    def assign_to_job(self, job: Any, pid: int) -> bool:
        self.assigned.append(pid)
        return True

    def close_job(self, job: Any) -> None:
        rec("job.close")

    def reg_get(self, name: str) -> str | None:
        return self.registry.get(name)

    def reg_set(self, name: str, value: str) -> None:
        rec("reg_set")
        self.registry[name] = value

    def reg_delete(self, name: str) -> bool:
        return self.registry.pop(name, None) is not None

    def create_mutex(self, name: str) -> tuple[int, bool]:
        return 5, self.mutex_exists

    def close_handle(self, handle: int) -> None:
        rec(f"close_handle:{handle}")

    def find_window(self, title: str) -> int:
        return 0xBEEF

    def window_pid(self, hwnd: int) -> int:
        return 1234

    def post_message(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        self.posted.append((hwnd, msg))
        return True


class RecTray(Tray):
    notes: ClassVar[list[tuple[str, str]]] = []

    def show(self) -> None:
        rec("tray.show")
        super().show()

    def notify(self, text: str, kind: str = "info") -> None:
        RecTray.notes.append((text, kind))


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> FakeWinApi:
    CALLS.clear()
    RecTray.notes = []
    FakeHands.fail = ""
    FakeCore.broken = False
    FakeCore.script = None
    FakeCore.last = None
    FakeBrain.close_threads = []
    FakeBrain.close_delay = 0.0
    FakeServer.last = None
    FakeWindow.last = None
    for name, attrs in {
        "jarvis.ui.window": {"LauncherWindow": FakeWindow},
        "jarvis.ui.logic": {"ConfirmRequest": FakeConfirmRequest},
        "jarvis.hands": {"Hands": FakeHands},
        "jarvis.brain": {"Brain": FakeBrain},
        "jarvis.core": {"Core": FakeCore},
        "jarvis.journal": {"Journal": FakeJournal},
    }.items():
        monkeypatch.setitem(sys.modules, name, _module(name, **attrs))
    monkeypatch.setattr(confirm_client, "ConfirmServer", FakeServer, raising=False)
    real_load = config.load

    def load() -> Any:
        rec("config")
        return real_load()

    monkeypatch.setattr(config, "load", load)
    monkeypatch.setattr(apps, "inventory", lambda: rec("inventory") or [])
    monkeypatch.setattr(apps, "enable_auto_refresh", lambda on: rec(f"auto_refresh:{on}"))
    monkeypatch.setattr(apps, "refresh", lambda: rec("apps.refresh") or [])
    monkeypatch.setattr(app_mod, "APPS_FIRST_REFRESH_S", 3600.0)
    monkeypatch.setattr(app_mod, "Tray", RecTray)
    monkeypatch.setattr(winapp, "own_descendants", lambda name: [])
    api = FakeWinApi()
    monkeypatch.setattr(winapp, "_api", api)
    winapp.show_message_id.cache_clear()
    yield api
    winapp.show_message_id.cache_clear()


@pytest.fixture
def make_app(qapp: QApplication, fakes: FakeWinApi) -> Iterator[Callable[[], app_mod.JarvisApp]]:
    created: list[app_mod.JarvisApp] = []

    def make() -> app_mod.JarvisApp:
        a = app_mod.JarvisApp(qapp)
        created.append(a)
        a.start()
        return a

    yield make
    for a in created:
        dispose(qapp, a)


def dispose(qapp: QApplication, a: app_mod.JarvisApp) -> None:
    """Выход, дождаться рабочих потоков, удалить Qt-объекты в UI-потоке.

    Иначе сборщик мусора может удалить QWidget прошлого теста из рабочего потока — Qt этого не любит.
    """
    a.shutdown()
    for t in threading.enumerate():
        if t.name.startswith("jarvis-") and t is not threading.current_thread():
            t.join(5)
    for obj in (a.window, a.receiver, a.tray):
        if obj is not None:
            if isinstance(obj, QWidget):
                obj.hide()
            obj.deleteLater()
    a.window = a.receiver = a.tray = None
    qapp.processEvents()
    gc.collect()


def ready(a: app_mod.JarvisApp) -> Callable[[], bool]:
    def check() -> bool:
        h = a.health
        return h.hands in ("ready", "error") and h.brain in ("ready", "off", "error") and a.core is not None

    return check


def before(*names: str) -> None:
    idx = [CALLS.index(n) for n in names]
    assert idx == sorted(idx), f"порядок {names}: {CALLS}"


# --- старт ----------------------------------------------------------------------------------------


def test_start_order(qapp: QApplication, make_app: Any, fakes: FakeWinApi) -> None:
    a = make_app()
    assert wait_for(qapp, ready(a))
    hotkey = next(c for c in CALLS if c.startswith("hotkey:"))
    # UI-поток: конфиг → инвентарь → канал подтверждений → окно → трей → хоткей; дальше всё в фоне
    before("config", "inventory", "confirm_server.start", "window", "tray.show", hotkey, "job", "hands")
    assert "auto_refresh:True" in CALLS
    before("hands", "brain", "core", "hands.ensure_server", "hands.warmup")
    before("core", "brain.start")
    assert confirm_client._handler == a._confirm
    assert FakeServer.last is not None and FakeServer.last.callback == a._confirm
    core = FakeCore.last
    assert core is not None and core.brain.confirm_address == FakeServer.last.address
    assert core.hands.job_hook is not None and core.brain.process_hook is not None
    assert isinstance(core.journal, FakeJournal)
    assert a.window.local is False and not a.window.isVisible()  # окно создано скрытым
    assert wait_for(qapp, lambda: a.tray.state == "ready")
    assert "Jarvis — готов" in a.tray.tooltip and "Ctrl+Alt+Space" in a.tray.tooltip
    assert len(a.tray.tooltip) <= 127


def test_job_hook_puts_processes_in_job(qapp: QApplication, make_app: Any, fakes: FakeWinApi) -> None:
    a = make_app()
    assert wait_for(qapp, ready(a))
    a.hands.job_hook(types.SimpleNamespace(pid=321))
    a.brain.process_hook(types.SimpleNamespace(pid=654))
    assert 321 in fakes.assigned and 654 in fakes.assigned


def test_hands_server_started_before_hook_goes_to_job(
    qapp: QApplication, make_app: Any, fakes: FakeWinApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(winapp, "own_descendants", lambda name: seen.append(name) or [4242])
    a = make_app()
    assert wait_for(qapp, ready(a))
    assert seen == ["llama-server.exe"]
    assert 4242 in fakes.assigned


def test_local_mode_at_start_does_not_start_brain(
    qapp: QApplication, make_app: Any, config_file: Callable[[str], Path]
) -> None:
    config_file('mode = "local"\n')
    a = make_app()
    assert wait_for(qapp, ready(a))
    time.sleep(0.05)
    qapp.processEvents()
    assert "brain.start" not in CALLS
    assert a.health.brain == "off"
    assert a.window.local is True
    assert a.tray.is_local_checked()
    assert wait_for(qapp, lambda: a.tray.state == "local")
    assert "локальный" in a.tray.tooltip


def test_toggle_local_mode(qapp: QApplication, make_app: Any, config_file: Callable[[str], Path]) -> None:
    path = config_file('# мой конфиг\nmode = "normal"  # режим\n\n[aliases]\n"телега" = "Telegram"\n')
    a = make_app()
    assert wait_for(qapp, ready(a))
    a.tray.trigger("Локальный режим")
    assert a.window.local is True and a.tray.state == "local"  # окно и трей — сразу, в UI-потоке
    assert wait_for(qapp, lambda: "brain.close" in CALLS)
    text = path.read_text(encoding="utf-8")
    assert 'mode = "local"  # режим' in text and '"телега" = "Telegram"' in text and "# мой конфиг" in text
    assert wait_for(qapp, lambda: a.core.cfg.mode == "local")  # Core читает cfg на каждый запрос
    starts = CALLS.count("brain.start")
    a.tray.trigger("Локальный режим")
    assert wait_for(qapp, lambda: CALLS.count("brain.start") == starts + 1)
    assert 'mode = "normal"' in path.read_text(encoding="utf-8")
    assert wait_for(qapp, lambda: a.core.cfg.mode == "normal" and a.health.brain == "ready")
    assert a.window.local is False


def test_toggle_local_mode_save_error_still_switches(
    qapp: QApplication, make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(mode: str) -> None:
        raise PermissionError("нет доступа")

    monkeypatch.setattr(config, "save_mode", broken)
    a = make_app()
    assert wait_for(qapp, ready(a))
    a.tray.trigger("Локальный режим")
    assert wait_for(qapp, lambda: "brain.close" in CALLS and a.core.cfg.mode == "local")
    assert wait_for(qapp, lambda: any("не сохранён" in t for t, _ in RecTray.notes))


def test_hands_error_shows_warning(qapp: QApplication, make_app: Any) -> None:
    FakeHands.fail = "Порт 8081 занят чужим процессом (ollama.exe)"
    a = make_app()
    assert wait_for(qapp, ready(a))
    assert wait_for(qapp, lambda: a.tray.state == "warn")
    assert "руки" in a.tray.tooltip


def test_init_failure_answers_requests(qapp: QApplication, make_app: Any) -> None:
    FakeCore.broken = True
    a = make_app()
    assert wait_for(qapp, lambda: a.health.init_error != "")
    assert wait_for(qapp, lambda: a.tray.state == "warn")
    a.window.submitted.emit("громкость 30")
    assert wait_for(qapp, lambda: any(isinstance(e, Done) for e in a.window.events))
    done = a.window.events[-1]
    assert not done.ok and "не готов" in done.text


def test_hotkey_busy_uses_fallback_and_notifies(qapp: QApplication, make_app: Any, fakes: FakeWinApi) -> None:
    fakes.busy_hotkeys.add(winapp.parse_hotkey("ctrl+alt+space"))
    a = make_app()
    assert a.health.hotkey == "Ctrl+Alt+Shift+Space"
    assert RecTray.notes and "занято" in RecTray.notes[0][0]
    assert wait_for(qapp, ready(a))
    assert wait_for(qapp, lambda: a.tray.state == "ready")  # запасное сочетание работает — не проблема


def test_no_hotkey_at_all_is_warning(qapp: QApplication, make_app: Any, fakes: FakeWinApi) -> None:
    for combo in ("ctrl+alt+space", *winapp.FALLBACK_HOTKEYS):
        fakes.busy_hotkeys.add(winapp.parse_hotkey(combo))
    a = make_app()
    assert a.health.hotkey is None
    assert wait_for(qapp, ready(a))
    assert wait_for(qapp, lambda: a.tray.state == "warn")
    assert "Хоткей: нет" in a.tray.tooltip


# --- сводка трея (без Qt) -----------------------------------------------------------------------------


def test_summarize() -> None:
    h = app_mod.Health(hands="ready", brain="ready", hotkey="Ctrl+Alt+Space")
    assert app_mod.summarize(h)[0] == "ready"
    h.hands = "starting"
    assert app_mod.summarize(h)[0] == "busy"
    h.hands, h.mode, h.brain = "ready", "local", "off"
    state, text = app_mod.summarize(h)
    assert state == "local" and "выключен" in text
    h.hands_warnings = app_mod.hands_warnings({"prefix_cache": "broken", "vram": "slow"})
    state, text = app_mod.summarize(h)
    assert state == "warn" and len(text) <= 127
    h = app_mod.Health(hands="stopped", brain="ready", hotkey="Ctrl+Alt+J")
    state, text = app_mod.summarize(h)
    assert state == "ready" and "выгружены" in text
    h = app_mod.Health(hands="ready", brain="error", brain_error="x" * 300, hotkey="Ctrl+Alt+J")
    state, text = app_mod.summarize(h)
    assert state == "warn" and len(text) <= 127 and text.endswith("…")


def test_brain_error_text() -> None:
    assert app_mod.brain_error({"state": "error", "error": "GPT недоступен"}) == "GPT недоступен"
    assert app_mod.brain_error({"state": "ready", "error": ""}) == ""
    assert app_mod.brain_error(None) == ""


# --- запросы --------------------------------------------------------------------------------------


def test_request_events_reach_window(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    a.window.submitted.emit("  громкость 30  ")
    assert a.window.begun == ["громкость 30"]
    assert wait_for(qapp, lambda: any(isinstance(e, Done) for e in a.window.events))
    assert [type(e) for e in a.window.events] == [Level, TextChunk, Done]
    text, hotkey_ms, ctx = FakeCore.last.handled[0]
    assert text == "громкость 30" and hotkey_ms is None and ctx is a.ctx
    a.window.submitted.emit("   ")  # пустое не отправляется
    assert a.window.begun == ["громкость 30"]


def test_open_item_is_open_command(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    a.window.open_item.emit(1)
    assert wait_for(qapp, lambda: FakeCore.last is not None and FakeCore.last.handled)
    assert FakeCore.last.handled[0][0] == "открой 2"


def test_request_error_becomes_done(qapp: QApplication, make_app: Any) -> None:
    def boom(core: FakeCore, text: str) -> Iterator[Any]:
        yield Level("hands", "test")
        raise RuntimeError("упало")

    FakeCore.script = boom
    a = make_app()
    a.window.submitted.emit("открой телегу")
    assert wait_for(qapp, lambda: any(isinstance(e, Done) for e in a.window.events))
    done = a.window.events[-1]
    assert not done.ok and "упало" in done.text


def test_new_request_cancels_old(qapp: QApplication, make_app: Any) -> None:
    def slow(core: FakeCore, text: str) -> Iterator[Any]:
        yield Level("brain", "test")
        if text == "первый":
            core.cancelled.wait(5)
            yield Done(ok=False, text="Отменено", cancelled=True)
            return
        yield Done(ok=True, text="второй готов")

    FakeCore.script = slow
    a = make_app()
    assert wait_for(qapp, lambda: a.core is not None)
    a.window.submitted.emit("первый")
    assert wait_for(qapp, lambda: len(a.window.events) == 1)
    a.window.submitted.emit("второй")
    assert wait_for(qapp, lambda: any(isinstance(e, Done) and e.ok for e in a.window.events))
    assert "core.cancel" in CALLS
    assert [h[0] for h in FakeCore.last.handled] == ["первый", "второй"]
    time.sleep(0.05)
    qapp.processEvents()
    dones = [e for e in a.window.events if isinstance(e, Done)]
    assert [d.text for d in dones] == ["второй готов"]  # Done отменённого старого запроса не показан


def test_escape_cancels_current(qapp: QApplication, make_app: Any) -> None:
    def slow(core: FakeCore, text: str) -> Iterator[Any]:
        yield Level("brain", "test")
        core.cancelled.wait(5)
        yield Done(ok=False, text="Отменено", cancelled=True)

    FakeCore.script = slow
    a = make_app()
    a.window.submitted.emit("расскажи про Vulkan")
    assert wait_for(qapp, lambda: len(a.window.events) == 1)
    a.window.cancel_requested.emit()
    assert wait_for(qapp, lambda: any(isinstance(e, Done) for e in a.window.events))
    assert a.window.events[-1].cancelled


# --- хоткей и показ окна ------------------------------------------------------------------------------


def _msg(message: int, wparam: int = 0, tick: int = 0) -> wintypes.MSG:
    msg = wintypes.MSG()
    msg.message = message
    msg.wParam = wparam
    msg.time = tick
    return msg


def test_hotkey_captures_foreground_before_show(
    qapp: QApplication, make_app: Any, fakes: FakeWinApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = WindowInfo(hwnd=0x1234, title="отчёт.docx — Word", pid=42, exe="WINWORD.EXE")
    monkeypatch.setattr(pc_windows, "foreground", lambda: rec("foreground") or info)
    a = make_app()
    msg = _msg(winapp.WM_HOTKEY, app_mod.HOTKEY_ID, fakes.tick - 7)
    assert a.handle_native(ctypes.addressof(msg)) is True
    before("foreground", "show", "set_foreground")
    assert a.ctx.active_window == info
    assert fakes.foreground[-1] == int(a.window.winId())
    assert a.window.isVisible()
    assert a._hotkey_ms is not None and a._hotkey_ms >= 7  # задержка в очереди учтена
    a.window.submitted.emit("закрой его")
    assert wait_for(qapp, lambda: FakeCore.last is not None and FakeCore.last.handled)
    assert FakeCore.last.handled[0][1] is not None and FakeCore.last.handled[0][1] >= 7
    a.window.submitted.emit("ещё")
    assert wait_for(qapp, lambda: len(FakeCore.last.handled) == 2)
    assert FakeCore.last.handled[1][1] is None  # время хоткея — только первому запросу после него


def test_hotkey_toggles_when_active(
    qapp: QApplication, make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pc_windows, "foreground", lambda: None)
    a = make_app()
    a.on_hotkey()
    a.window.active = True
    a.window.busy = True
    a.on_hotkey()
    assert a.window.isVisible()  # идёт запрос — не прячем
    a.window.busy = False
    a.on_hotkey()
    assert not a.window.isVisible()


def test_other_messages_pass_through(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    assert a.handle_native(ctypes.addressof(_msg(0x0200))) is False  # WM_MOUSEMOVE
    assert a.handle_native(ctypes.addressof(_msg(winapp.WM_HOTKEY, 99))) is False  # чужой id
    assert not a.window.isVisible()
    flt = app_mod.NativeFilter(a.handle_native)
    msg = _msg(winapp.WM_HOTKEY, app_mod.HOTKEY_ID)
    assert flt.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(msg)) == (False, 0)
    assert flt.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg)) == (True, 0)
    assert a.window.isVisible()


def test_second_instance_message_shows_window(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    a.ctx.active_window = WindowInfo(hwnd=1, title="старое", pid=1, exe="x.exe")
    assert a.handle_native(ctypes.addressof(_msg(0xC0DE))) is True
    assert a.window.isVisible()
    assert a.ctx.active_window is None  # «до Jarvis» здесь был проводник, а не цель команды


def test_tray_open_shows_window(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    a.tray.trigger("Открыть")
    assert a.window.isVisible()
    assert a.ctx.active_window is None


# --- подтверждения --------------------------------------------------------------------------------


def _ask_in_thread(fn: Callable[[], bool]) -> tuple[threading.Thread, dict[str, bool]]:
    result: dict[str, bool] = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", fn()), daemon=True)
    t.start()
    return t, result


@pytest.mark.parametrize("approve", [True, False])
def test_confirm_from_worker_thread(qapp: QApplication, make_app: Any, approve: bool) -> None:
    a = make_app()
    t, result = _ask_in_thread(
        lambda: confirm_client.confirm("Завершить chrome.exe?", "12 процессов", "user")
    )
    assert wait_for(qapp, lambda: len(a.window.confirms) == 1)
    req = a.window.confirms[0]
    assert (req.summary, req.details, req.caller) == ("Завершить chrome.exe?", "12 процессов", "user")
    assert a.window.confirm_threads[0] is threading.main_thread()  # в окно — только в UI-потоке
    assert a.window.isVisible()  # окно было скрыто — показано
    req.resolve(approve)
    t.join(5)
    assert result == {"ok": approve}


def test_confirm_from_pipe_server_same_queue(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    server = FakeServer.last
    t1, r1 = _ask_in_thread(lambda: server.callback("Ввести текст в окно?", "привет", "brain"))
    t2, r2 = _ask_in_thread(lambda: confirm_client.confirm("Переместить в корзину?", "отчёт.docx", "user"))
    assert wait_for(qapp, lambda: len(a.window.confirms) == 2)
    callers = sorted(r.caller for r in a.window.confirms)
    assert callers == ["brain", "user"]
    for req in a.window.confirms:
        req.resolve(req.caller == "brain")
    t1.join(5)
    t2.join(5)
    assert r1 == {"ok": True} and r2 == {"ok": False}


def test_confirm_timeout_is_no(qapp: QApplication, make_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "CONFIRM_TIMEOUT_S", 0.2)
    a = make_app()
    t, result = _ask_in_thread(lambda: a._confirm("Выключить компьютер?", "", "user"))
    assert wait_for(qapp, lambda: len(a.window.confirms) == 1)
    t.join(5)
    assert result == {"ok": False}


def test_confirm_from_ui_thread_refused(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    assert a._confirm("Завершить?", "", "user") is False
    qapp.processEvents()
    assert a.window.confirms == []


# --- меню трея ------------------------------------------------------------------------------------


def test_unload_and_restart_hands(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    assert wait_for(qapp, ready(a))
    a.tray.trigger("Выгрузить руки")
    assert wait_for(qapp, lambda: a.health.hands == "stopped" and "выгружены" in a.tray.tooltip)
    # следующая команда подняла сервер сам (Hands.decide) — подсказка обновляется после запроса
    a.hands.status["server"] = "ready"
    a.window.submitted.emit("открой телегу")
    assert wait_for(qapp, lambda: a.health.hands == "ready")
    n = CALLS.count("hands.ensure_server")
    a.tray.trigger("Перезапустить руки")
    assert wait_for(qapp, lambda: CALLS.count("hands.ensure_server") == n + 1 and a.health.hands == "ready")
    assert CALLS.count("hands.stop") == 2


def test_new_conversation(qapp: QApplication, make_app: Any) -> None:
    a = make_app()
    assert wait_for(qapp, ready(a))
    a.tray.trigger("Новый разговор")
    assert wait_for(qapp, lambda: "brain.new_conversation" in CALLS)


def test_open_logs(qapp: QApplication, make_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[str, str | None, str]] = []
    monkeypatch.setattr(
        files, "open_target", lambda t, k, c: opened.append((t, k, c)) or Result(True, "Открыл")
    )
    a = make_app()
    a.tray.trigger("Журнал")
    assert wait_for(qapp, lambda: bool(opened))
    assert opened == [(str(settings.data_dir() / "logs"), "folder", "user")]


def test_autostart_toggle(qapp: QApplication, make_app: Any, fakes: FakeWinApi) -> None:
    a = make_app()
    assert not a.tray.is_autostart_checked()
    a.tray.trigger("Автозапуск")
    assert wait_for(qapp, lambda: "reg_set" in CALLS)
    assert fakes.registry["Jarvis"] == winapp.autostart_command()
    assert wait_for(qapp, lambda: a.tray.is_autostart_checked())
    a.tray.trigger("Автозапуск")
    assert wait_for(qapp, lambda: "Jarvis" not in fakes.registry and not a.tray.is_autostart_checked())


def test_quit_stops_everything(qapp: QApplication, make_app: Any, fakes: FakeWinApi) -> None:
    a = make_app()
    assert wait_for(qapp, ready(a))
    a.tray.trigger("Выход")
    assert not a.tray._tray.isVisible()  # трей убран сразу, остальное — в рабочем потоке
    assert wait_for(qapp, a._stopped.is_set)
    for name in (
        "hotkey.unregister",
        "core.cancel",
        "hands.stop",
        "brain.close",
        "confirm_server.close",
        "journal.close",
        "job.close",
    ):
        assert name in CALLS, name
    assert confirm_client._handler is None
    assert a._confirm("ещё?", "", "user") is False
    a.shutdown()  # повторный выход — ничего
    assert CALLS.count("hands.stop") == 1


def test_brain_close_never_blocks_ui(qapp: QApplication, make_app: Any) -> None:
    FakeBrain.close_delay = 0.5
    a = make_app()
    assert wait_for(qapp, ready(a))
    t0 = time.monotonic()
    a.tray.trigger("Локальный режим")
    a.tray.trigger("Выход")
    assert time.monotonic() - t0 < 0.3  # UI-поток не ждал codex
    assert wait_for(qapp, a._stopped.is_set)
    assert FakeBrain.close_threads
    assert all(t is not threading.main_thread() for t in FakeBrain.close_threads)


# --- main ------------------------------------------------------------------------------------------


def test_main_second_instance_shows_first(fakes: FakeWinApi) -> None:
    fakes.mutex_exists = True
    assert app_mod.main([]) == 0
    assert fakes.posted == [(0xBEEF, 0xC0DE)]
    assert "close_handle:5" in CALLS
    assert "window" not in CALLS  # второй экземпляр ничего не создаёт


# --- контракт с настоящим окном ------------------------------------------------------------------------


@pytest.mark.skipif(real_window is None, reason="jarvis.ui.window ещё нет")
def test_real_window_contract(qapp: QApplication, fakes: FakeWinApi, monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящие LauncherWindow и ConfirmRequest: хоткей → запрос → подтверждение из потока → Done."""
    monkeypatch.setitem(sys.modules, "jarvis.ui.window", real_window)
    monkeypatch.setitem(sys.modules, "jarvis.ui.logic", real_logic)
    monkeypatch.setattr(pc_windows, "foreground", lambda: None)
    asked: list[Any] = []
    original = real_window.LauncherWindow.ask_confirm

    def ask(self: Any, req: Any) -> None:
        asked.append(req)
        original(self, req)

    monkeypatch.setattr(real_window.LauncherWindow, "ask_confirm", ask)

    def script(core: FakeCore, text: str) -> Iterator[Any]:
        yield Level("hands", "test")
        ok = confirm_client.confirm("Завершить chrome.exe?", "", "user")
        yield Done(ok=ok, text="Завершил" if ok else "Не стал")

    FakeCore.script = script
    a = app_mod.JarvisApp(qapp)
    try:
        a.start()
        assert isinstance(a.window, real_window.LauncherWindow)
        assert wait_for(qapp, lambda: a.core is not None)
        a.on_hotkey()
        assert a.window.isVisible()
        a.window.submitted.emit("закрой процесс хром")
        assert wait_for(qapp, lambda: bool(asked))
        assert isinstance(asked[0], real_logic.ConfirmRequest)
        asked[0].resolve(True)
        assert wait_for(qapp, lambda: not a.window.is_busy())
        assert FakeCore.last.handled[0][0] == "закрой процесс хром"
    finally:
        dispose(qapp, a)
