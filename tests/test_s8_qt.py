"""S8, потоки Qt: находки ревью (до исправления падали); фейки из tests/test_app.py, настоящий Core."""

import os
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import test_app as ta  # noqa: E402
from jarvis import app as app_mod  # noqa: E402

# настоящие модули — до того, как фикстура fakes подменит их в sys.modules
from jarvis import core as real_core  # noqa: E402
from jarvis import execute as real_execute  # noqa: E402
from jarvis import hands as real_hands  # noqa: E402
from jarvis.events import Done, TextChunk  # noqa: E402
from test_app import CALLS, FakeBrain, FakeHands, dispose, fakes, make_app, qapp, rec, wait_for  # noqa: E402,F401


def _executed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """execute.run без действий ПК: только записать, что команда исполнена."""
    done: list[tuple[str, dict[str, Any]]] = []

    def run(tool: str, args: dict[str, Any], text: str, ctx: Any, source: str, dry: bool = False) -> Any:
        done.append((tool, dict(args)))
        return real_execute.Outcome("done", True, f"выполнено: {tool}", autohide=True, action=tool)

    monkeypatch.setattr(real_execute, "run", run)
    return done


def _use_real_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "jarvis.core", real_core)
    monkeypatch.setattr(ta.FakeJournal, "write", lambda self, record: None, raising=False)


def _last_done(a: Any) -> Done | None:
    dones = [e for e in a.window.events if isinstance(e, Done)]
    return dones[-1] if dones else None


# --- 1. Esc до готовности Core теряется ------------------------------------------------------------


def test_esc_before_core_ready_is_not_lost(
    qapp: Any, make_app: Any, fakes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_real_core(monkeypatch)
    executed = _executed(monkeypatch)
    gate = threading.Event()
    init = real_core.Core.__init__

    def slow_init(self: Any, *args: Any, **kw: Any) -> None:
        gate.wait(5)  # компоненты ещё создаются (первые секунды после автозапуска)
        init(self, *args, **kw)

    monkeypatch.setattr(real_core.Core, "__init__", slow_init)
    a = make_app()
    a.window.submitted.emit("громкость 30")
    qapp.processEvents()
    a.window.cancel_requested.emit()  # Esc: окно уже показало «Отменяю…»
    qapp.processEvents()
    gate.set()
    assert wait_for(qapp, lambda: _last_done(a) is not None)
    assert executed == [], "команда исполнена, хотя человек нажал Esc"
    assert _last_done(a).cancelled


# --- 2. Esc, пока новый запрос ждёт конца старого, теряется ---------------------------------------


class SlowCancelBrain(FakeBrain):
    """Ход мозга, который codex прерывает не мгновенно (interrupt идёт по сети ~0,5 с)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.cancelled = threading.Event()

    def ask(self, text: str, ctx: Any, deep: bool = False) -> Iterator[Any]:
        self.cancelled.clear()
        yield TextChunk("Vulkan — это…")
        self.cancelled.wait(5)
        time.sleep(0.6)
        yield Done(ok=False, text="Отменено", cancelled=True)

    def cancel(self) -> None:
        rec("brain.cancel")
        self.cancelled.set()


def test_esc_while_previous_request_finishes_is_not_lost(
    qapp: Any, make_app: Any, fakes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_real_core(monkeypatch)
    monkeypatch.setitem(sys.modules, "jarvis.brain", types.SimpleNamespace(Brain=SlowCancelBrain))
    executed = _executed(monkeypatch)
    a = make_app()
    assert wait_for(qapp, lambda: isinstance(a.core, real_core.Core))
    a.window.submitted.emit("gpt: расскажи про Vulkan")
    assert wait_for(qapp, lambda: any(isinstance(e, TextChunk) for e in a.window.events))
    a.window.submitted.emit("громкость 30")  # новый запрос: рабочий поток отменяет и ждёт старый
    qapp.processEvents()
    a.window.cancel_requested.emit()  # и сразу Esc — отменить «громкость 30»
    assert wait_for(qapp, lambda: _last_done(a) is not None and _last_done(a).level == "grammar")
    assert executed == [], "Esc потерян: громкость изменена"
    assert _last_done(a).cancelled


def test_superseded_request_that_never_started_is_not_executed(
    qapp: Any, make_app: Any, fakes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_real_core(monkeypatch)
    monkeypatch.setitem(sys.modules, "jarvis.brain", types.SimpleNamespace(Brain=SlowCancelBrain))
    executed = _executed(monkeypatch)
    a = make_app()
    assert wait_for(qapp, lambda: isinstance(a.core, real_core.Core))
    a.window.submitted.emit("gpt: расскажи про Vulkan")
    assert wait_for(qapp, lambda: any(isinstance(e, TextChunk) for e in a.window.events))
    a.window.submitted.emit("громкость 3")  # опечатка…
    qapp.processEvents()
    a.window.submitted.emit("громкость 30")  # …тут же исправлена: «громкость 3» заменена
    assert wait_for(qapp, lambda: ("vol", {"set": 30}) in executed)
    time.sleep(0.2)
    assert executed == [("vol", {"set": 30})], f"заменённая команда исполнена: {executed}"


# --- 3. Отменённая (заменённая) команда рук исполняется после новой ---------------------------------


class LoadingHands(FakeHands):
    """Как Hands.decide при незагруженной модели: ensure_server() ждёт загрузку (до 60 с), потом решение."""

    gate = threading.Event()

    def decide(self, text: str, ctx: Any = None) -> Any:
        LoadingHands.gate.wait(10)
        return real_hands.HandsDecision("tool", tool="close", args={"target": "chrome"})


def test_superseded_hands_command_is_not_executed(
    qapp: Any, make_app: Any, fakes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_real_core(monkeypatch)
    monkeypatch.setitem(sys.modules, "jarvis.hands", types.SimpleNamespace(Hands=LoadingHands))
    monkeypatch.setattr(app_mod, "PREV_REQUEST_JOIN_S", 0.3)  # в коде 5 с; загрузка модели — 10–30 с
    LoadingHands.gate = threading.Event()
    executed = _executed(monkeypatch)
    a = make_app()
    try:
        assert wait_for(qapp, lambda: isinstance(a.core, real_core.Core))
        a.window.submitted.emit("закрой хром")  # руки ещё грузят модель — «Запускаю руки…»
        qapp.processEvents()
        time.sleep(0.1)
        a.window.submitted.emit("громкость 30")  # человек передумал: новая команда отменяет старую
        assert wait_for(qapp, lambda: ("vol", {"set": 30}) in executed)
        LoadingHands.gate.set()  # модель загрузилась
        time.sleep(0.5)
        qapp.processEvents()
        assert ("close", {"target": "chrome"}) not in executed, f"отменённая команда исполнена: {executed}"
    finally:
        LoadingHands.gate.set()


# --- 4. Выход во время загрузки рук: остальное дочернее не останавливается -------------------------


class LockedHands(FakeHands):
    """Замок как в Hands: ensure_server держит его всё ожидание загрузки (_wait_ready), stop() ждёт его."""

    gate = threading.Event()
    loading = threading.Event()

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self._lock = threading.Lock()

    def ensure_server(self) -> None:
        with self._lock:
            rec("hands.ensure_server")
            LockedHands.loading.set()
            LockedHands.gate.wait(10)
            self.status["server"] = "ready"

    def stop(self) -> None:
        with self._lock:
            super().stop()


def test_quit_while_hands_loading_still_closes_brain_and_journal(
    qapp: Any, make_app: Any, fakes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "jarvis.hands", types.SimpleNamespace(Hands=LockedHands))
    monkeypatch.setattr(app_mod, "STOP_TIMEOUT_S", 1.0)  # в коде 8 с
    LockedHands.gate, LockedHands.loading = threading.Event(), threading.Event()
    a = make_app()
    try:
        assert wait_for(qapp, LockedHands.loading.is_set)
        t0 = time.monotonic()
        a.tray.trigger("Выход")
        assert wait_for(qapp, a._stopped.is_set, timeout=5)
        took = time.monotonic() - t0
        missing = [n for n in ("brain.close", "confirm_server.close", "journal.close") if n not in CALLS]
        assert missing == [], f"не остановлено: {missing}; выход занял {took:.1f} с"
    finally:
        LockedHands.gate.set()


# --- 5. Хоткей во время идущей команды подменяет «@cur» уже решённой команды ------------------------


class CurHands(FakeHands):
    """Руки грузят модель (ensure_server внутри decide), потом решают «можешь закрыть его» → close @cur."""

    gate = threading.Event()
    seen: ClassVar[list[Any]] = []

    def decide(self, text: str, ctx: Any = None) -> Any:
        CurHands.seen.append(ctx.active_window.hwnd if ctx and ctx.active_window else None)
        CurHands.gate.wait(10)
        return real_hands.HandsDecision("tool", tool="close", args={"target": "@cur"})


def test_hotkey_during_request_does_not_retarget_cur(
    qapp: Any, make_app: Any, fakes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pc import windows as pc_windows
    from pc.result import Result
    from pc.windows import WindowInfo

    _use_real_core(monkeypatch)
    monkeypatch.setitem(sys.modules, "jarvis.hands", types.SimpleNamespace(Hands=CurHands))
    CurHands.gate, CurHands.seen = threading.Event(), []
    word = WindowInfo(hwnd=0x111, title="отчёт.docx — Word", pid=10, exe="WINWORD.EXE")
    chrome = WindowInfo(hwnd=0x222, title="Почта — Google Chrome", pid=20, exe="chrome.exe")
    fg = [word]
    monkeypatch.setattr(pc_windows, "foreground", lambda: fg[0])
    monkeypatch.setattr(pc_windows, "find_window", lambda target: None)
    closed: list[Any] = []
    monkeypatch.setattr(
        pc_windows, "close_target", lambda target, caller: closed.append(target) or Result(True, "Закрываю")
    )
    a = make_app()
    try:
        assert wait_for(qapp, lambda: isinstance(a.core, real_core.Core))
        a.on_hotkey()  # человек в Word нажал хоткей
        a.window.submitted.emit("можешь закрыть его")  # не грамматика — руки
        assert wait_for(qapp, lambda: CurHands.seen == [0x111])  # руки видят Word
        fg[0] = chrome  # пока руки думают, человек ушёл в Chrome и снова нажал хоткей — посмотреть на Jarvis
        a.on_hotkey()
        CurHands.gate.set()
        assert wait_for(qapp, lambda: bool(closed))
        assert closed == [0x111], f"закрыто не то окно: {closed} (команда давалась про Word 0x111)"
    finally:
        CurHands.gate.set()


# --- 6. Локальный режим включён, а вопрос «просит GPT» остаётся и подтверждается ---------------------

from jarvis.ui import logic as real_logic  # noqa: E402
from jarvis.ui import window as real_window  # noqa: E402


def test_local_mode_denies_pending_gpt_confirmation(
    qapp: Any, make_app: Any, fakes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "jarvis.ui.window", real_window)
    monkeypatch.setitem(sys.modules, "jarvis.ui.logic", real_logic)
    from pc import windows as pc_windows

    monkeypatch.setattr(pc_windows, "foreground", lambda: None)
    a = make_app()
    assert wait_for(qapp, lambda: a.health.brain == "ready")
    result: dict[str, bool] = {}
    t = threading.Thread(
        target=lambda: result.setdefault(
            "ok", ta.FakeServer.last.callback("Завершить chrome.exe?", "", "brain")
        ),
        daemon=True,
    )
    t.start()  # поток ConfirmServer: MCP-процесс мозга ждёт ответа человека
    assert wait_for(qapp, lambda: a.window._confirms.current is not None)
    req = a.window._confirms.current
    a.tray.trigger("Локальный режим")  # человек выключает облако: Brain.close(), ход прерван
    assert wait_for(qapp, lambda: "brain.close" in CALLS)
    qapp.processEvents()
    # вопрос мозга, у которого больше нет хода, должен сняться «нет»; иначе Ctrl+Enter исполнит действие
    assert wait_for(qapp, lambda: req.done, timeout=1.0), "карточка «просит GPT» осталась после Brain.close()"
    assert req.approved is False
