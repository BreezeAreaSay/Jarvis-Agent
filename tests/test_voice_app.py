"""Голос в приложении (S9, п.7): события jarvis.voice → окно и запрос, озвучка по событиям, настоящее окно.

Фейки и фикстуры app — из tests/test_app.py (окно, Core, руки, мозг, слой ОС); Qt — офскрин.
Фикстуры берутся через request.getfixturevalue: параметр с именем фикстуры затенял бы импорт (F811).
"""

import os
import sys
import time
from collections.abc import Iterator
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from jarvis import asr, tts, voice
from jarvis.config import UiConfig
from jarvis.events import Done, Level, TextChunk
from jarvis.reminders import Reminder
from pc import windows as pc_windows
from pc.windows import WindowInfo
from test_app import FakeConfirmRequest, FakeCore, FakeWindow, fakes, make_app, qapp, wait_for  # noqa: F401


class VoiceWindow(FakeWindow):
    """Фейковое окно с голосовыми методами настоящего LauncherWindow."""

    def __init__(self, ui_cfg: Any, orb: Any = None) -> None:
        super().__init__(ui_cfg, orb)
        self.voice: list[tuple[str, Any]] = []
        self.toasts: list[str] = []
        self.inactive: list[bool] = []

    def show_launcher(self) -> None:
        self.inactive.append(self.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        super().show_launcher()

    def voice_state(self, phase: str) -> None:
        self.voice.append(("state", phase))

    def voice_level(self, value: float) -> None:
        self.voice.append(("level", value))

    def voice_dismiss(self, delay_ms: int = 2500) -> None:
        self.voice.append(("dismiss", delay_ms))

    def show_toast(self, text: str, kind: str = "error") -> None:
        self.toasts.append(text)

    def deny_confirms(self, caller: str) -> None:
        self.voice.append(("deny", caller))


class VoiceCore(FakeCore):
    """Core, который принимает source (как настоящий)."""

    def handle(
        self, text: str, ctx: Any, dry: bool = False, hotkey_ms: float | None = None, source: str = "typed"
    ) -> Iterator[Any]:
        self.sources = [*getattr(self, "sources", []), source]
        yield from super().handle(text, ctx, dry, hotkey_ms)


class FakeNarrator:
    def __init__(self) -> None:
        self.begun: list[tuple[int, bool]] = []
        self.events: list[tuple[int, Any]] = []
        self.confirms: list[str] = []
        self.said: list[str] = []
        self.cancels = 0

    def begin(self, rid: int, spoken: bool) -> None:
        self.begun.append((rid, spoken))

    def on_event(self, rid: int, ev: Any) -> None:
        self.events.append((rid, ev))

    def confirm(self, summary: str) -> None:
        self.confirms.append(summary)

    def say(self, text: str) -> None:
        self.said.append(text)

    def cancel(self) -> None:
        self.cancels += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def app(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """JarvisApp на фейках test_app с голосовым окном и Core, принимающим source."""
    request.getfixturevalue("fakes")
    monkeypatch.setattr(sys.modules["jarvis.ui.window"], "LauncherWindow", VoiceWindow)
    monkeypatch.setattr(sys.modules["jarvis.core"], "Core", VoiceCore)
    a = request.getfixturevalue("make_app")()
    assert wait_for(request.getfixturevalue("qapp"), lambda: a.core is not None)
    return a


def q() -> QApplication:
    inst = QApplication.instance()
    assert isinstance(inst, QApplication)
    return inst


def test_voice_events_drive_window_and_request(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    before = WindowInfo(77, "Без имени — Блокнот", 5, "notepad.exe")
    monkeypatch.setattr(pc_windows, "foreground", lambda: before)
    w = app.window
    app.voice_event.emit(voice.Listening())
    assert wait_for(q(), lambda: ("state", "listen") in w.voice)
    assert app.ctx.active_window is before  # снято ДО показа окна, как по хоткею
    assert w.isVisible() and w.inactive == [True]  # фокус у программы человека (вдруг это правый Ctrl+C)
    app.voice_event.emit(voice.Loudness(0.4))
    app.voice_event.emit(voice.Recognizing())
    assert wait_for(q(), lambda: ("state", "recognize") in w.voice)
    assert ("level", 0.4) in w.voice
    app.voice_event.emit(voice.Heard("открой телегу", 210.0, 2.1, "gigaam-v3"))
    assert wait_for(q(), lambda: any(isinstance(e, Done) for e in w.events))
    assert w.begun == ["открой телегу"]
    assert FakeCore.last is not None and FakeCore.last.sources == ["voice"]
    assert w.voice[-3:] == [("state", ""), ("deny", "user"), ("deny", "brain")]  # голосом «да» не подтвердить


def test_voice_failure_and_cancel(app: Any) -> None:
    w = app.window
    app.voice_event.emit(voice.Failed("Не расслышал — повтори"))
    assert wait_for(q(), lambda: w.toasts == ["Не расслышал — повтори"])
    assert w.isVisible() and w.inactive == [True]
    assert w.voice[-1] == ("dismiss", 2500)
    app.voice_event.emit(voice.ListenCancelled("keys"))
    assert wait_for(q(), lambda: w.voice[-1] == ("dismiss", 0))


def test_typed_request_has_no_source(app: Any) -> None:
    app.window.submitted.emit("громкость 30")
    assert wait_for(q(), lambda: any(isinstance(e, Done) for e in app.window.events))
    assert FakeCore.last.sources == ["typed"]


def test_narrator_wiring(app: Any) -> None:
    n = FakeNarrator()
    app.narrator = n
    app.window.submitted.emit("громкость 30")
    assert wait_for(q(), lambda: any(isinstance(ev, Done) for _, ev in n.events))
    assert n.begun == [(1, False)]
    assert [type(ev) for rid, ev in n.events if rid == 1] == [Level, TextChunk, Done]
    app.voice_event.emit(voice.Heard("что ты умеешь", 180.0, 1.4))
    assert wait_for(q(), lambda: (2, True) in n.begun)
    app._on_cancel()  # Esc в окне — речь замолкает
    assert n.cancels == 1
    app._on_confirm(FakeConfirmRequest("Завершить chrome.exe?"))
    assert n.confirms == ["Завершить chrome.exe?"]
    app._on_reminders([Reminder("r1", "проверить почту", time.time(), time.time())])
    assert n.said == ["Напоминание: проверить почту"]


def test_start_voice_with_models(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Модели скачаны: Narrator и Voice создаются при старте (на Linux хук не встаёт — статус error)."""
    folder = asr.model_dir("gigaam-v3")
    folder.mkdir(parents=True)
    for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (folder / name).write_bytes(b"x")
    voice_folder = tts.voice_dir("denis")
    (voice_folder / "espeak-ng-data").mkdir(parents=True)
    (voice_folder / "ru_RU-denis-medium.onnx").write_bytes(b"x")
    (voice_folder / "tokens.txt").write_text("a 0", encoding="utf-8")
    request.getfixturevalue("fakes")
    hooks: list[Any] = []

    class Hook:
        def __init__(self, vk: int, events: Any) -> None:
            self.vk, self.down = vk, False
            hooks.append(self)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            self.stopped = True

        def key_down(self) -> bool:
            return False

        def release(self) -> None:
            pass

    monkeypatch.setattr(voice.ptt, "Hook", Hook)
    a = request.getfixturevalue("make_app")()
    assert wait_for(q(), lambda: a.voice is not None and a.narrator is not None)
    assert hooks and hooks[0].vk == 0xA3
    assert a.voice.on_press == a.narrator.cancel and a.voice.on_escape == a.narrator.cancel
    assert wait_for(q(), lambda: a.voice.status == "error", 10)  # модель-пустышка не грузится — честно error
    a.shutdown()
    assert getattr(hooks[0], "stopped", False)


def test_no_models_no_voice(app: Any) -> None:
    assert app.voice is None and app.narrator is None


# --- настоящее окно -------------------------------------------------------------------------------


@pytest.fixture
def window(request: pytest.FixtureRequest) -> Iterator[Any]:
    request.getfixturevalue("qapp")
    from jarvis.ui.window import LauncherWindow

    w = LauncherWindow(UiConfig(animations=False))
    yield w
    w.close()
    w.deleteLater()
    q().processEvents()


def test_real_window_voice_states(window: Any) -> None:
    from jarvis.ui import window as win

    window._input.setText("старый текст")
    window.voice_state("listen")
    assert window._input.text() == "" and window._input.placeholderText() == win.LISTEN_TEXT
    window.voice_state("recognize")
    assert window._input.placeholderText() == win.RECOGNIZE_TEXT
    before = window.orb_model.pulse
    window.voice_level(0.9)
    assert window.orb_model.pulse >= before
    window.voice_state("")
    assert window._input.placeholderText() == win.PLACEHOLDER


def test_real_window_dismiss_only_untouched(window: Any) -> None:
    window.isActiveWindow = lambda: False  # офскрин активирует окно при показе; на Windows — нет
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    window.show_launcher()
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
    assert window.isVisible()
    window._input.setText("поправлю руками")  # человек начал править — не прятать
    window.voice_dismiss(0)
    assert window.isVisible()
    window._input.clear()
    window.voice_dismiss(10)
    assert wait_for(q(), lambda: not window.isVisible(), 2)
    window.isActiveWindow = lambda: True  # окно, с которым человек работает, не прячется
    window.show_launcher()
    window.voice_dismiss(0)
    assert window.isVisible()
