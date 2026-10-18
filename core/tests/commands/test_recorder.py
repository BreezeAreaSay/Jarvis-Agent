"""«Запись действий»: из событий ввода — шаги команды."""

import sys
from types import SimpleNamespace

import pytest

from jarvis.commands.recorder import ActionRecorder, Recorder, pynput_key
from jarvis.context import AppContext
from jarvis.services import UserFacingError
from tests.conftest import EventLog


def test_typing_combos_waits() -> None:
    counts: list[int] = []
    rec = ActionRecorder(counts.append)
    for i, ch in enumerate("Прив"):
        rec.key_down("char", ch, t=0.1 * i)
    rec.key_down("backspace", None, t=0.5)  # исправление опечатки
    rec.key_down("char", "в", t=0.6)
    rec.key_down("char", "е", t=0.7)
    rec.key_down("char", "т", t=0.8)
    rec.key_down("enter", None, t=0.9)
    rec.key_down("ctrl", None, t=3.0)
    rec.key_down("c", "\x03", t=3.1)
    rec.key_up("ctrl", t=3.2)
    for i in range(3):
        rec.key_down("down", None, t=3.3 + 0.1 * i)
    assert rec.finish(t=10) == [
        {"type": "Keys.Type", "value": "Привет"},
        {"type": "Keys.Press", "value": "enter"},
        {"type": "Wait", "value": "2.2"},
        {"type": "Keys.Press", "value": "ctrl+c"},
        {"type": "Keys.Press", "value": "down*3"},
    ]
    assert counts[-1] == 5


def test_clicks_double_click_scroll_and_last_click_dropped() -> None:
    rec = ActionRecorder()
    rec.click(100, 200, "left", t=0.0)
    rec.click(101, 201, "left", t=0.2)  # двойной
    rec.scroll(-1, t=1.5)
    rec.scroll(-2, t=1.6)
    rec.click(500, 500, "right", t=1.8)
    rec.key_down("shift", None, t=1.9)
    rec.key_down("char", "A", t=2.0)
    rec.key_up("shift", t=2.05)
    rec.click(1800, 20, "left", t=5.0)  # «Остановить запись» в окне Jarvis
    actions = rec.finish(t=5.5)
    assert actions == [
        {"type": "Mouse.DoubleClick", "value": "100 200"},
        {"type": "Wait", "value": "1.3"},
        {"type": "Mouse.Scroll", "value": "-3"},
        {"type": "Mouse.ClickRight", "value": "500 500"},
        {"type": "Keys.Type", "value": "A"},
    ]


def test_old_last_click_is_kept() -> None:
    rec = ActionRecorder()
    rec.click(10, 20, "left", t=0.0)
    assert rec.finish(t=9.0) == [{"type": "Mouse.ClickLeft", "value": "10 20"}]


def test_pynput_key_mapping() -> None:
    assert pynput_key(SimpleNamespace(name="ctrl_l")) == ("ctrl", None)
    assert pynput_key(SimpleNamespace(name="cmd")) == ("win", None)
    assert pynput_key(SimpleNamespace(name="space")) == ("space", " ")
    assert pynput_key(SimpleNamespace(name="f5")) == ("f5", None)
    assert pynput_key(SimpleNamespace(name="media_play_pause")) == ("play_pause", None)
    assert pynput_key(SimpleNamespace(char="\x03", vk=67)) == ("c", None)  # ctrl+c
    assert pynput_key(SimpleNamespace(char="с", vk=67)) == ("c", "с")  # русская раскладка
    assert pynput_key(SimpleNamespace(char=",", vk=188)) == ("comma", ",")
    assert pynput_key(SimpleNamespace(char="ж", vk=None)) == (None, "ж")


def test_recorder_unavailable_without_pynput(
    ctx: AppContext, events: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pynput", None)
    recorder = Recorder(ctx)
    with pytest.raises(UserFacingError, match="Запись действий недоступна"):
        recorder.start()
    assert recorder.recording is False
    assert recorder.stop() == []
    assert events.of("record.state")[-1] == {"recording": False, "count": 0}
