"""Голос в приложении (S9, п.7): события jarvis.voice → окно и запрос, озвучка по событиям, настоящее окно.

Фейки и фикстуры app — из tests/test_app.py (окно, Core, руки, мозг, слой ОС); Qt — офскрин.
Фикстуры берутся через request.getfixturevalue: параметр с именем фикстуры затенял бы импорт (F811).
"""

import os
import sys
import threading
import time
import types
from collections.abc import Iterator
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from jarvis import app as app_mod
from jarvis import asr, tts, voice
from jarvis.config import UiConfig
from jarvis.events import Done, Items, Level, TextChunk
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
        self.lingers: list[float] = []

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

    def linger(self, seconds: float) -> None:
        self.lingers.append(seconds)

    def dismiss_inactive(self) -> None:
        if self.isVisible() and not self.isActiveWindow():
            self.hide()


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


class QuietNarrator(FakeNarrator):
    """Озвучка готова (режим voice): голосовой команде строка ввода не нужна. Код в ответе — on_unsaid."""

    def __init__(self) -> None:
        super().__init__()
        self.mode = "voice"
        self.speaker = types.SimpleNamespace(ready=True)
        self.on_unsaid: Any = None

    def on_event(self, rid: int, ev: Any) -> None:
        super().on_event(rid, ev)
        if isinstance(ev, TextChunk) and "```" in ev.text and self.on_unsaid is not None:
            self.on_unsaid()


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


def quiet(app: Any) -> QuietNarrator:
    n = QuietNarrator()
    n.on_unsaid = app._on_unsaid
    app.narrator = n
    return n


def test_quiet_voice_uses_orb_not_window(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Озвучка готова и сфера на столе есть: строка ввода не появляется — слушает и работает сфера,
    ответ звучит; активное окно снимается, как по хоткею."""
    before = WindowInfo(77, "Без имени — Блокнот", 5, "notepad.exe")
    monkeypatch.setattr(pc_windows, "foreground", lambda: before)
    n = quiet(app)
    w, model = app.window, app.orb_model
    app.voice_event.emit(voice.Listening())
    assert wait_for(q(), lambda: model.listening)
    assert app.ctx.active_window is before and not w.isVisible() and w.voice == []
    assert not model.resting  # режим auto: сфера видна, пока слушает
    app.voice_event.emit(voice.Loudness(0.6))
    app.voice_event.emit(voice.Recognizing())
    app.voice_event.emit(voice.Heard("громкость 30", 200.0, 1.5, "gigaam-v3"))
    assert wait_for(q(), lambda: any(isinstance(ev, Done) for _, ev in n.events))
    assert not model.listening and not w.isVisible() and w.lingers == []
    assert FakeCore.last is not None and FakeCore.last.sources == ["voice"] and n.begun[-1][1] is True
    assert ("state", "listen") not in w.voice and ("state", "recognize") not in w.voice


def test_quiet_voice_failure_spoken_and_cancel(app: Any) -> None:
    n = quiet(app)
    w, model = app.window, app.orb_model
    app.voice_event.emit(voice.Listening())
    app.voice_event.emit(voice.Failed("Не расслышал — повтори"))
    assert wait_for(q(), lambda: n.said == ["Не расслышал — повтори"])
    assert not w.isVisible() and w.toasts == [] and model.state == "error"  # малиновая вспышка сферы
    app.voice_event.emit(voice.Listening())
    assert wait_for(q(), lambda: model.listening)
    app.voice_event.emit(voice.ListenCancelled("keys"))
    assert wait_for(q(), lambda: not model.listening)
    assert not w.isVisible()


def test_quiet_voice_reveals_window_for_list_or_code(app: Any) -> None:
    """Список результатов или то, что не прозвучит (код): окно показывается само, без фокуса, а после
    ответа уходит через REVEAL_LINGER_S; Esc в другой программе — прячет сразу и глушит речь."""
    n = quiet(app)
    w = app.window

    def found(core: Any, text: str) -> Any:
        yield Level("hands", "find")
        yield Items(["C:\\Users\\me\\отчёт.docx", "C:\\Users\\me\\отчёт-2.docx"])
        yield Done(ok=True, text="Нашёл 2 файла")

    FakeCore.script = found
    app.voice_event.emit(voice.Listening())
    app.voice_event.emit(voice.Heard("найди отчёт", 200.0, 1.5))
    assert wait_for(q(), lambda: w.lingers == [app_mod.REVEAL_LINGER_S])
    assert w.isVisible() and w.inactive[-1] is True
    t = threading.Thread(target=app._escape_anywhere)  # хук голоса — свой поток
    t.start()
    t.join(2)
    assert wait_for(q(), lambda: not w.isVisible()) and n.cancels == 1

    def code(core: Any, text: str) -> Any:
        yield Level("brain", "gpt")
        yield TextChunk("Вот так:\n```python\nprint(1)\n```")
        yield Done(ok=True, text="")

    FakeCore.script = code
    app.voice_event.emit(voice.Listening())
    app.voice_event.emit(voice.Heard("gpt: пример кода", 200.0, 1.5))
    assert wait_for(q(), lambda: len(w.lingers) == 2)
    assert w.isVisible()

    def plain(core: Any, text: str) -> Any:
        yield Level("brain", "gpt")
        yield TextChunk("Небо голубое из-за рассеяния.")
        yield Done(ok=True, text="")

    w.hide()
    FakeCore.script = plain
    app.voice_event.emit(voice.Listening())
    app.voice_event.emit(voice.Heard("почему небо голубое", 200.0, 1.5))
    assert wait_for(q(), lambda: sum(isinstance(ev, Done) for _, ev in n.events) == 3)
    assert not w.isVisible() and len(w.lingers) == 2  # прозвучало целиком — окно не нужно


def test_window_visible_voice_stays_in_window(app: Any) -> None:
    """Окно уже открыто (человек печатал) — голос идёт через строку ввода, как раньше."""
    quiet(app)
    w = app.window
    w.show_launcher()
    app.voice_event.emit(voice.Listening())
    assert wait_for(q(), lambda: ("state", "listen") in w.voice)
    assert not app.orb_model.listening


def test_speech_level_moves_orb(app: Any) -> None:
    model = app.orb_model
    app.speech_level.emit(0.7)
    assert wait_for(q(), lambda: model.talking)
    assert not model.resting  # режим auto: сфера видна, пока Jarvis говорит
    app.speech_level.emit(0.0)
    assert wait_for(q(), lambda: not model.talking)


def test_start_voice_with_models(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Модели скачаны: Narrator и Voice создаются при старте (на Linux хук не встаёт — статус error)."""
    folder = asr.model_dir("gigaam-v3")
    folder.mkdir(parents=True)
    for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (folder / name).write_bytes(b"x")
    voice_folder = tts.voice_dir("ruslan")
    (voice_folder / "espeak-ng-data").mkdir(parents=True)
    (voice_folder / "ru_RU-ruslan-medium.onnx").write_bytes(b"x")
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
    assert a.voice.on_press == a.narrator.cancel and a.voice.on_escape == a._escape_anywhere
    assert a.narrator.on_unsaid == a._on_unsaid and a.narrator.speaker.on_level is not None
    assert wait_for(q(), lambda: a.voice.status == "error", 10)  # модель-пустышка не грузится — честно error
    a.shutdown()
    assert getattr(hooks[0], "stopped", False)


def test_start_voice_falls_back_to_downloaded_voice(request: pytest.FixtureRequest) -> None:
    """[voice] tts_voice по умолчанию — ruslan, а скачан только прежний denis: Jarvis говорит голосом denis
    (с обращением «сэр» — persona по умолчанию jarvis); модели распознавания нет — голосового ввода нет."""
    voice_folder = tts.voice_dir("denis")
    (voice_folder / "espeak-ng-data").mkdir(parents=True)
    (voice_folder / "ru_RU-denis-medium.onnx").write_bytes(b"x")
    (voice_folder / "tokens.txt").write_text("a 0", encoding="utf-8")
    request.getfixturevalue("fakes")
    a = request.getfixturevalue("make_app")()
    assert wait_for(q(), lambda: a.narrator is not None)
    assert a.narrator.speaker.voice == "denis" and a.narrator.address == "сэр"
    assert a.voice is None


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
