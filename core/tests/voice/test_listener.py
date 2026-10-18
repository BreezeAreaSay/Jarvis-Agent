"""Машина состояний голосового ввода: активатор, команды, окна ожидания, эхо, просьбы, клавиша PTT."""

import numpy as np
import pytest

from jarvis.voice import listener as lst
from jarvis.voice.listener import Listener, find_wake_anywhere, match_wake
from tests.voice.helpers import BLOCK_S, Clock, FakeHost, FakeVad, Script, feed

Rig = tuple[Listener, FakeHost, Script, Clock]


@pytest.fixture
def rig() -> Rig:
    host, script, clock = FakeHost(), Script(), Clock()
    listener = Listener(host, vad=FakeVad(), stream_factory=script.session, final=script.final, clock=clock)
    return listener, host, script, clock


def say(rig: Rig, text: str, seconds: float = 1.0, pause: float = 0.6) -> None:
    listener, _, script, clock = rig
    feed(listener, script.silence(0.3) + script.utter(text, seconds) + script.silence(pause), clock)


def test_wake_word_and_command_dispatched(rig: Rig) -> None:
    listener, host, _, _ = rig
    say(rig, "джарвис включи музыку")
    assert host.commands == ["включи музыку"]
    final = {"text": "джарвис включи музыку", "wake": True, "command": "включи музыку"}
    assert host.of("voice.final") == [final]
    assert host.of("voice.wake") == [{}]  # раньше конца фразы — по потоковому тексту
    partials = host.of("voice.partial")
    assert partials and all(p["wake"] for p in partials)
    assert "listening" in host.activities and "processing" in host.activities
    assert listener.activity == "idle"


def test_without_wake_word_ignored_and_hidden(rig: Rig) -> None:
    _, host, _, _ = rig
    say(rig, "какая сегодня погода")
    assert host.commands == []
    assert host.of("voice.final") == [] and host.of("voice.partial") == []  # чужой разговор не показываем
    assert "listening" not in host.activities


def test_wake_only_then_command_without_wake(rig: Rig) -> None:
    listener, host, _, _clock = rig
    say(rig, "джарвис", seconds=0.5)
    assert host.earcons == ["wake"]
    assert host.of("voice.final")[-1] == {"text": "джарвис", "wake": True, "command": ""}
    assert listener.activity == "listening"
    say(rig, "открой калькулятор")
    assert host.commands == ["открой калькулятор"]
    # окно закрылось: следующая фраза без активатора — снова чужая
    say(rig, "закрой калькулятор")
    assert host.commands == ["открой калькулятор"]


def test_wake_window_expires_with_done_earcon(rig: Rig) -> None:
    listener, host, script, clock = rig
    say(rig, "джарвис", seconds=0.5)
    feed(listener, script.silence(lst.WAKE_WINDOW_S + 0.5), clock)
    assert host.earcons == ["wake", "done"]
    assert listener.activity == "idle"
    say(rig, "открой калькулятор")
    assert host.commands == []


def test_prefix_mode_off_every_phrase_is_command(rig: Rig) -> None:
    _, host, _, _ = rig
    host.voice = host.voice.model_copy(update={"prefix_mode": False})
    say(rig, "включи свет")
    say(rig, "джарвис выключи свет")
    assert host.commands == ["включи свет", "выключи свет"]


def test_follow_up_after_answer(rig: Rig) -> None:
    listener, host, script, clock = rig
    host.speaking = True  # Jarvis отвечает…
    listener.follow_up(4.0)
    feed(listener, script.silence(1.0), clock)
    host.speaking = False  # …договорил — окно открылось
    feed(listener, script.silence(0.4), clock)
    assert listener.activity == "listening"
    say(rig, "а завтра")
    assert host.commands == ["а завтра"]
    say(rig, "и послезавтра")  # окно одноразовое
    assert host.commands == ["а завтра"]


def test_follow_up_chain_is_limited(rig: Rig) -> None:
    """Фоновая речь (видео, чужой разговор) не держит Jarvis в разговоре без конца: без «Джарвис» подряд —
    не больше MAX_FOLLOW_CHAIN команд, потом окно продолжения не открывается."""
    listener, host, script, clock = rig
    for i in range(lst.MAX_FOLLOW_CHAIN + 1):
        listener.follow_up(4.0)  # Jarvis ответил — окно продолжения
        feed(listener, script.silence(0.4), clock)
        say(rig, f"фраза номер {i}")
    assert host.commands == [f"фраза номер {i}" for i in range(lst.MAX_FOLLOW_CHAIN)]
    assert listener.activity == "idle"
    # к нему обратились — снова можно продолжать без активатора
    say(rig, "джарвис какая погода")
    listener.follow_up(4.0)
    feed(listener, script.silence(0.4), clock)
    say(rig, "а завтра")
    assert host.commands[-2:] == ["какая погода", "а завтра"]


def test_follow_up_window_expires(rig: Rig) -> None:
    listener, host, script, clock = rig
    listener.follow_up(2.0)
    feed(listener, script.silence(2.5), clock)
    say(rig, "а завтра")
    assert host.commands == []


def test_ignores_own_voice_while_speaking(rig: Rig) -> None:
    _listener, host, _script, _clock = rig
    host.speaking = True
    say(rig, "включи музыку")
    assert host.commands == [] and host.silenced == 0
    assert host.of("voice.final") == []


def test_barge_in_with_wake_word_silences_and_dispatches(rig: Rig) -> None:
    _listener, host, _script, _clock = rig
    host.speaking = True
    say(rig, "джарвис стоп", seconds=1.2)
    assert host.silenced >= 1  # замолчал, как только потоковая услышала «Джарвис»
    assert host.commands == ["стоп"]


def test_barge_in_keeps_only_stop_word(rig: Rig) -> None:
    _, host, _, _ = rig
    host.speaking = True
    say(rig, "погода сегодня джарвис стоп солнечная", seconds=1.5)
    assert host.commands == ["стоп"]


def test_echo_utterance_dropped_when_jarvis_stops(rig: Rig) -> None:
    listener, host, script, clock = rig
    vad = listener.vad
    host.speaking = True
    feed(listener, script.utter("его же слова", 0.6), clock)
    assert listener.utt is not None and listener.utt.echo
    host.speaking = False
    feed(listener, script.silence(0.1), clock)
    assert listener.utt is None and vad.resets >= 1


def test_listen_now_accepts_next_phrase(rig: Rig) -> None:
    listener, host, _, _ = rig
    results: list[str | None] = []
    listener.listen_now(8.0, on_done=results.append)
    say(rig, "громкость пятьдесят")
    assert host.earcons == ["wake"]
    assert host.commands == ["громкость пятьдесят"]
    assert results == ["громкость пятьдесят"]


def test_listen_now_times_out(rig: Rig) -> None:
    listener, host, script, clock = rig
    results: list[str | None] = []
    listener.listen_now(2.0, on_done=results.append)
    feed(listener, script.silence(2.5), clock)
    assert results == [None]
    assert host.earcons == ["wake", "done"]
    say(rig, "громкость пятьдесят")
    assert host.commands == []


def test_ask_yes_no_and_capture_text_do_not_dispatch(rig: Rig) -> None:
    listener, host, script, _ = rig
    answers: list[str | None] = []
    listener.request("yesno", 8.0, on_done=answers.append)
    say(rig, "да давай")
    listener.request("text", 8.0, punct=True, on_done=answers.append, source="capture")
    say(rig, "Купить хлеба и молока.")
    assert answers == ["да давай", "Купить хлеба и молока."]
    assert host.commands == []
    assert script.finals[-1] == ("Купить хлеба и молока.", True)  # диктовке — модель с пунктуацией
    assert [f["command"] for f in host.of("voice.final")] == ["", ""]


def test_capture_strips_wake_word(rig: Rig) -> None:
    listener, _, _, _ = rig
    answers: list[str | None] = []
    listener.request("yesno", 8.0, on_done=answers.append)
    say(rig, "джарвис нет")
    assert answers == ["нет"]


def test_new_request_cancels_previous(rig: Rig) -> None:
    listener, host, _script, _clock = rig
    first: list[str | None] = []
    second: list[str | None] = []
    listener.request("text", 15.0, on_done=first.append, source="capture")
    listener.listen_now(8.0, on_done=second.append)
    say(rig, "открой почту")
    assert first == [None]  # диктовку отменила кнопка микрофона
    assert second == ["открой почту"] and host.commands == ["открой почту"]


def test_stop_cancels_requests_and_window(rig: Rig) -> None:
    listener, host, script, clock = rig
    results: list[str | None] = []
    listener.request("text", 15.0, on_done=results.append)
    listener.follow_up(5.0)
    listener.stop()
    feed(listener, script.silence(0.1), clock)
    assert results == [None]
    say(rig, "что-то ещё")
    assert host.commands == [] and listener.activity == "idle"


def test_request_waits_for_phrase_in_progress(rig: Rig) -> None:
    listener, _host, script, clock = rig
    results: list[str | None] = []
    listener.request("text", 0.5, on_done=results.append)
    # фраза началась до конца срока и длится дольше него — просьба её дождётся
    feed(listener, script.utter("длинная диктовка", 1.5) + script.silence(0.3), clock)
    assert results == ["длинная диктовка"]


def test_push_to_talk(rig: Rig) -> None:
    listener, host, script, clock = rig
    host.speaking = True
    listener.ptt(True)
    feed(listener, script.utter("включи музыку", 1.0), clock)
    assert host.silenced == 1 and host.earcons == ["wake"]
    feed(listener, script.silence(1.0), clock)  # пауза при зажатой клавише фразу не кончает
    assert host.commands == []
    listener.ptt(False)
    feed(listener, script.silence(0.1), clock)
    assert host.commands == ["включи музыку"]


def test_push_to_talk_short_tap_and_combo_ignored(rig: Rig) -> None:
    listener, host, script, clock = rig
    listener.ptt(True)
    feed(listener, script.utter("а", 0.1), clock)
    listener.ptt(False)
    feed(listener, script.silence(0.3), clock)
    listener.ptt(True)
    feed(listener, script.utter("копировать", 1.0), clock)
    listener.ptt_cancel()  # правый Ctrl+C — не голос
    feed(listener, script.silence(0.5), clock)
    assert host.commands == []


def test_push_to_talk_empty_result_error_earcon(rig: Rig) -> None:
    listener, host, script, clock = rig
    listener.ptt(True)
    feed(listener, script.silence(1.0), clock)
    listener.ptt(False)
    feed(listener, script.silence(0.1), clock)
    assert host.earcons == ["wake", "error"]
    assert host.of("voice.final")[-1] == {"text": "", "wake": False, "command": ""}


def test_max_utterance_cut(rig: Rig) -> None:
    listener, host, script, clock = rig
    host.voice = host.voice.model_copy(update={"max_utterance_s": 3})
    feed(listener, script.utter("джарвис расскажи длинную историю", 4.0), clock)
    assert host.commands == ["расскажи длинную историю"]
    assert listener.vad.resets >= 1


def test_long_unaddressed_phrase_decodes_head_only(rig: Rig) -> None:
    listener, host, script, _clock = rig
    listener.stream_factory = None  # без потоковой модели: «Джарвис» узнаём только по итогу
    say(rig, "длинный разговор по телевизору о погоде", seconds=5.0)
    assert host.commands == []
    assert len(script.finals) == 1  # только начало фразы, полная — не нужна
    say(rig, "джарвис включи новости", seconds=4.0)
    assert host.commands == ["включи новости"]
    assert len(script.finals) == 3  # начало (есть «Джарвис») → вся фраза


def test_partials_throttled(rig: Rig) -> None:
    _listener, host, _script, _clock = rig
    say(rig, "джарвис раз два три четыре пять шесть семь восемь девять десять", seconds=4.0)
    partials = host.of("voice.partial")
    assert 3 <= len(partials) <= int(4.0 / lst.PARTIAL_EVERY_S) + 1
    assert partials[-1]["text"].startswith("джарвис раз")


def test_activity_listening_after_early_wake() -> None:
    host, script, clock = FakeHost(), Script(), Clock()
    listener = Listener(host, vad=FakeVad(), stream_factory=script.session, final=script.final, clock=clock)
    feed(listener, script.silence(0.2) + script.utter("джарвис открой браузер", 1.0), clock)
    assert listener.activity == "listening" and host.of("voice.wake") == [{}]


def test_tick_runs_timers_without_frames(rig: Rig) -> None:
    listener, _, _, clock = rig
    results: list[str | None] = []
    listener.listen_now(1.0, on_done=results.append)
    listener.tick()
    clock.t += 2.0
    listener.tick()
    assert results == [None]


@pytest.mark.parametrize(
    ("text", "early", "expected"),
    [
        ("джарвис включи свет", False, (True, "включи свет")),
        ("джарлиз сверни все окна", False, (True, "сверни все окна")),  # искажение GigaAM
        ("жарес", False, (True, "")),  # одно слово
        ("джарлес", True, (True, "")),
        ("джералис поставь громкость", True, (True, "поставь громкость")),
        ("жарим картошку", False, (False, "жарим картошку")),
        ("джинсы купи", False, (False, "джинсы купи")),
        ("давай музыку", True, (False, "давай музыку")),
        ("закрой окно", True, (False, "закрой окно")),
    ],
)
def test_match_wake(text: str, early: bool, expected: tuple[bool, str]) -> None:
    assert match_wake(text, ["джарвис"], early=early) == expected


def test_find_wake_anywhere() -> None:
    assert find_wake_anywhere("погода сегодня солнечная джарвис стоп", ["джарвис"]) == (True, "стоп")
    assert find_wake_anywhere("погода сегодня", ["джарвис"])[0] is False


def test_custom_wake_word() -> None:
    host, script, clock = FakeHost(wake_words=["пятница"]), Script(), Clock()
    listener = Listener(host, vad=FakeVad(), stream_factory=script.session, final=script.final, clock=clock)
    feed(listener, script.silence(0.3) + script.utter("пятница включи музыку") + script.silence(0.6), clock)
    feed(listener, script.utter("джарвис включи музыку") + script.silence(0.6), clock)
    assert host.commands == ["включи музыку"]


def test_preroll_is_part_of_utterance(rig: Rig) -> None:
    listener, _, script, _clock = rig
    captured: list[int] = []
    listener.final = lambda audio, punct: (captured.append(len(audio)), script.text_for(audio))[1]
    say(rig, "джарвис", seconds=0.5, pause=0.6)
    speech_and_tail = int((0.5 + 3 * BLOCK_S) / BLOCK_S) * 512
    assert captured and captured[0] > speech_and_tail  # плюс ≈0,3 с тишины до VAD — первые звуки целы
    assert np.isfinite(captured[0])
