"""jarvis.tts: текст для голоса, деление потока на предложения, Speaker и Narrator на фейках."""

import threading
import time
import types
from typing import Any

import pytest

from jarvis import mic, tts
from jarvis.events import Done, Level, Status, TextChunk

# --- текст ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "spoken"),
    [
        ("Telegram", "телеграм"),
        ("Chrome", "хром"),
        ("GPT", "джипити"),
        ("USB", "ю эс би"),
        ("OBS", "о би эс"),
        ("HDMI", "эйч ди эм ай"),
        ("store", "стор"),
        ("Shazam", "шазам"),
        ("cinema", "синема"),
        ("Echo", "эчо"),
    ],
)
def test_transliterate(word: str, spoken: str) -> None:
    assert tts.transliterate(word) == spoken


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("Открыл Telegram", "Открыл телеграм"),
        ("Громкость 30 %", "Громкость 30 процентов"),
        ("Завершить chrome.exe?", "Завершить хром?"),
        ("Открыл отчёт.docx", "Открыл отчёт"),
        ("Нашёл: C:\\Users\\me\\Documents\\план.txt", "Нашёл"),
        ("Подробнее на https://example.com/page, там всё", "Подробнее на там всё"),
        ("**Важно:** `jarvis doctor` проверит", "Важно: джарвис доктор проверит"),
        ("## Итог\n- первое\n- второе", "Итог первое второе"),
        ("Готово ✓ ⚙", "Готово"),
        ("Температура 2.5 градуса", "Температура 2,5 градуса"),
        ("✓", ""),
        ("https://example.com", ""),
    ],
)
def test_speakable(text: str, spoken: str) -> None:
    assert tts.speakable(text) == spoken


def test_speakable_long_inline_code_dropped() -> None:
    assert tts.speakable("Выполни `Get-ChildItem -Recurse | Where-Object Length` и всё") == "Выполни и всё"


def test_splitter_streams_sentences() -> None:
    sp = tts.Splitter()
    assert sp.feed("Привет! Сейчас посмо") == ["Привет!"]
    assert sp.feed("трю окна.") == []  # конец предложения подтверждается пробелом или концом ответа
    assert sp.feed(" У вас три") == ["Сейчас посмотрю окна."]
    assert sp.flush() == ["У вас три"]


def test_splitter_newline_is_boundary() -> None:
    sp = tts.Splitter()
    assert sp.feed("Первое\nВторое\n") == ["Первое", "Второе"]


def test_splitter_code_block_stops_with_rest() -> None:
    sp = tts.Splitter()
    assert sp.feed("Вот команда.\n```\nrm -rf /\n```\nИ ещё.") == ["Вот команда."]
    assert sp.flush() == [tts.REST]


def test_splitter_table_stops() -> None:
    sp = tts.Splitter()
    assert sp.feed("Сравнение:\n| A | B |\n|---|---|\n") == ["Сравнение"]
    assert sp.flush() == [tts.REST]


def test_splitter_limit() -> None:
    sp = tts.Splitter(limit=40)
    out = sp.feed("Первое предложение тут. Второе предложение тоже. Третье. ")
    assert out == ["Первое предложение тут."]
    assert sp.flush() == [tts.REST]


def test_splitter_nothing_cut_no_rest() -> None:
    sp = tts.Splitter()
    assert sp.feed("Коротко.") == []
    assert sp.flush() == ["Коротко."]
    assert sp.flush() == []


def test_short_text() -> None:
    assert tts.short_text("Громкость 30 %") == "Громкость 30 процентов"
    long = "Первое предложение. " + "очень длинное " * 30
    assert tts.short_text(long) == "Первое предложение."
    assert len(tts.short_text("слово " * 100)) <= tts.SHORT_LIMIT


def test_voice_files_and_available(_isolated_data: Any) -> None:
    with pytest.raises(tts.TtsError, match=r"get_voice_models\.ps1 -Voice denis"):
        tts.voice_files("denis")
    folder = tts.voice_dir("denis")
    (folder / "espeak-ng-data").mkdir(parents=True)
    (folder / "ru_RU-denis-medium.onnx").write_bytes(b"x")
    (folder / "ru_RU-denis-medium.onnx.json").write_text("{}")
    (folder / "tokens.txt").write_text("a 0")
    files = tts.voice_files("denis")
    assert files["model"].name == "ru_RU-denis-medium.onnx"
    tts.voice_dir("empty").mkdir()
    assert tts.available_voices() == ["denis"]


def fake_voice(name: str) -> None:
    folder = tts.voice_dir(name)
    (folder / "espeak-ng-data").mkdir(parents=True)
    (folder / f"ru_RU-{name}-medium.onnx").write_bytes(b"x")
    (folder / "tokens.txt").write_text("a 0")


def test_pick_voice_falls_back_to_downloaded(_isolated_data: Any) -> None:
    """Голос по умолчанию сменился на ruslan, а скачан прежний denis — говорим denis, а не молчим."""
    assert tts.pick_voice("ruslan") == ""
    fake_voice("zed")
    assert tts.pick_voice("ruslan") == "zed"  # не из VOICE_ORDER — тоже годится
    fake_voice("denis")
    assert tts.pick_voice("ruslan") == "denis" and tts.pick_voice("zed") == "zed"
    fake_voice("ruslan")
    assert tts.pick_voice("ruslan") == "ruslan" and tts.pick_voice("irina") == "ruslan"


# --- Speaker -------------------------------------------------------------------------------------


class FakeEngine:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def generate(self, text: str, sid: int = 0, speed: float = 1.0) -> Any:
        self.texts.append(text)
        return types.SimpleNamespace(samples=[0.25] * (len(text) * 400), sample_rate=22050)


class FakeOut:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.writes = 0
        self.bytes = 0
        self.aborted = self.stopped = self.closed = False
        self.first_write = threading.Event()

    def write(self, data: bytes) -> None:
        self.first_write.set()
        if self.gate is not None:
            self.gate.wait(5)
        self.writes += 1
        self.bytes += len(data)

    def abort(self) -> None:
        self.aborted = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakeSd:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.outputs: list[tuple[Any, int, FakeOut]] = []

    def open_output(self, device: Any, rate: int) -> FakeOut:
        out = FakeOut(self.gate)
        self.outputs.append((device, rate, out))
        return out


SPEAKERS: list[tts.Speaker] = []


@pytest.fixture(autouse=True)
def _close_speakers() -> Any:
    """Потоки Speaker (jarvis-tts*) не переживают тест: иначе их ждёт чужая уборка (dispose в test_app)."""
    yield
    while SPEAKERS:
        sp = SPEAKERS.pop()
        sp.close()
        for t in sp._threads:
            t.join(2)


def ready_speaker(
    monkeypatch: pytest.MonkeyPatch, gate: threading.Event | None = None
) -> tuple[tts.Speaker, FakeSd]:
    sd = FakeSd(gate)
    monkeypatch.setattr(mic, "_api", sd)
    sp = tts.Speaker("denis")
    sp._engine = FakeEngine()
    sp.ready = True
    SPEAKERS.append(sp)
    return sp, sd


def wait_for(cond: Any, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("не дождался")
        time.sleep(0.005)


def test_speaker_plays_in_chunks_and_goes_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    sp, sd = ready_speaker(monkeypatch)
    idle: list[int] = []
    sp.on_idle = idle.append
    gen = sp.say("Открыл телеграм")
    sp.say("Готово")
    wait_for(lambda: idle)
    assert idle == [gen]
    assert sp._engine.texts == ["Открыл телеграм", "Готово"]
    _, rate, out = sd.outputs[0]
    assert rate == 22050
    total = (len("Открыл телеграм") + len("Готово")) * 400 * 4
    assert out.bytes == total
    assert out.writes > 2  # блоками, а не одним write
    assert out.stopped and out.closed and not out.aborted  # хвост доигран, устройство закрыто
    assert sp.first_sound > 0
    wait_for(lambda: not sp.speaking, 2)


def test_speaker_reports_level_for_orb(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_level: громкость каждого блока (сфера говорит в такт), 0 — речь кончилась."""
    sp, _ = ready_speaker(monkeypatch)
    levels: list[float] = []
    sp.on_level = levels.append
    idle: list[int] = []
    sp.on_idle = idle.append
    sp.say("Открыл телеграм")
    wait_for(lambda: idle)
    assert len(levels) > 2 and levels[-1] == 0.0
    assert all(v == pytest.approx(1.0) for v in levels[:-1])  # 0,25 × 4 = 1 (предел)
    assert tts.block_level(b"") == 0.0
    quiet = tts.array.array("f", [0.05, -0.05] * 64).tobytes()
    assert tts.block_level(quiet) == pytest.approx(0.2, abs=1e-6)


def test_speaker_stop_interrupts_now(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    sp, sd = ready_speaker(monkeypatch, gate)
    idle: list[int] = []
    sp.on_idle = idle.append
    sp.say("Длинная фраза, которую перебьют")
    sp.say("И ещё одна")
    wait_for(lambda: sd.outputs and sd.outputs[0][2].first_write.is_set())
    assert sp.speaking
    assert sp.stop() is True
    gate.set()
    out = sd.outputs[0][2]
    wait_for(lambda: out.aborted)
    assert out.writes == 1  # дописался только блок, который уже шёл
    time.sleep(0.05)
    assert idle == []  # прерванное поколение не «закончилось», а прервано
    assert not sp.speaking
    assert sp.stop() is False


def test_speaker_stale_phrases_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    sp, _ = ready_speaker(monkeypatch)
    sp.stop()
    old_gen = sp.gen
    sp._say_q.put((old_gen - 1, "старое"))  # фраза прошлого поколения
    idle: list[int] = []
    sp.on_idle = idle.append
    sp.say("новое")
    wait_for(lambda: idle)
    assert sp._engine.texts == ["новое"]


def test_speaker_not_ready_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    sp, sd = ready_speaker(monkeypatch)
    sp.ready = False
    sp.say("ничего")
    sp.say("   ")
    time.sleep(0.02)
    assert sd.outputs == [] and not sp.speaking


def test_speaker_output_error_does_not_kill_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    sp, sd = ready_speaker(monkeypatch)
    calls = {"n": 0}
    real_open = sd.open_output

    def flaky(device: Any, rate: int) -> FakeOut:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("устройство занято")
        return real_open(device, rate)

    monkeypatch.setattr(sd, "open_output", flaky)
    idle: list[int] = []
    sp.on_idle = idle.append
    sp.say("раз")
    wait_for(lambda: idle)
    sp.say("два")
    wait_for(lambda: len(idle) == 2)
    assert sd.outputs[-1][2].bytes == len("два") * 400 * 4


def test_speaker_load_missing_voice() -> None:
    sp = tts.Speaker("denis")
    with pytest.raises(tts.TtsError, match="нет голоса denis"):
        sp.load()
    assert not sp.ready and sp.error


# --- Narrator ------------------------------------------------------------------------------------


class FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.stops = 0
        self.ready = True
        self.voice = "denis"
        self.gen = 0
        self.first_sound = 0.0
        self.busy = False
        self.on_idle: Any = None

    @property
    def speaking(self) -> bool:
        return self.busy

    def say(self, text: str) -> int:
        self.said.append(text)
        self.busy = True
        if not self.first_sound:
            self.first_sound = time.monotonic()
        return self.gen

    def stop(self) -> bool:
        was = self.busy
        self.stops += 1
        self.gen += 1
        self.busy = False
        self.first_sound = 0.0
        return was

    def finish(self) -> None:
        """Все фразы отзвучали."""
        self.busy = False
        if self.on_idle is not None:
            self.on_idle(self.gen)


class FakeJournal:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


def narrator(mode: str = "voice") -> tuple[tts.Narrator, FakeSpeaker, FakeJournal]:
    sp, journal = FakeSpeaker(), FakeJournal()
    return tts.Narrator(sp, mode, journal), sp, journal  # type: ignore[arg-type]


def test_narrator_grammar_done_spoken_for_voice_request() -> None:
    n, sp, journal = narrator("voice")
    n.begin(1, spoken=True)
    n.on_event(1, Level("grammar", "grammar:open"))
    n.on_event(1, Status("⚙ открываю", kind="tool"))
    n.on_event(1, Done(ok=True, text="Открыл Telegram", level="grammar"))
    assert sp.said == ["Открыл телеграм"]
    sp.finish()
    [rec] = journal.records
    assert rec["kind"] == "tts" and rec["level"] == "grammar"
    assert rec["spoken"] is True and rec["interrupted"] is False
    assert rec["tts_ms"] >= 0 and rec["reply_ms"] >= 0 and rec["phrases"] == 1


def test_narrator_typed_request_silent_in_voice_mode() -> None:
    n, sp, journal = narrator("voice")
    n.begin(1, spoken=False)
    n.on_event(1, Level("hands", "hands"))
    n.on_event(1, Done(ok=True, text="Готово", level="hands"))
    assert sp.said == [] and journal.records == []


def test_narrator_always_and_off() -> None:
    n, sp, _ = narrator("always")
    n.begin(1, spoken=False)
    n.on_event(1, Level("hands", "hands"))
    n.on_event(1, Done(ok=False, text="Не нашёл окно", level="hands"))
    assert sp.said == ["Не нашёл окно"]
    n, sp, _ = narrator("off")
    n.begin(1, spoken=True)
    n.on_event(1, Level("grammar", "g"))
    n.on_event(1, Done(ok=True, text="Готово"))
    n.say("Напоминание: чай")
    assert sp.said == []


def test_narrator_brain_streams_sentences() -> None:
    n, sp, journal = narrator("voice")
    n.begin(7, spoken=True)
    n.on_event(7, Level("brain", "heur:question", model="gpt"))
    n.on_event(7, TextChunk("Сейчас открыто три окна. Брау"))
    assert sp.said == ["Сейчас открыто три окна."]
    n.on_event(7, TextChunk("зер и проводник"))
    n.on_event(7, Done(ok=True, text="", level="brain"))
    assert sp.said == ["Сейчас открыто три окна.", "Браузер и проводник"]
    sp.finish()
    assert journal.records[0]["phrases"] == 2


def test_narrator_brain_error_without_text_says_done() -> None:
    n, sp, _ = narrator("voice")
    n.begin(1, spoken=True)
    n.on_event(1, Level("brain", "x"))
    n.on_event(1, Done(ok=False, text="Нет связи с GPT", level="brain"))
    assert sp.said == ["Нет связи с джипити"]


def test_narrator_silent_while_brain_thinks() -> None:
    """Никаких «Секунду…»: пока мозг думает, тишина (о работе говорит сфера); первые слова — сразу."""
    assert not hasattr(tts, "SECOND")
    n, sp, _ = narrator("voice")
    n.begin(1, spoken=True)
    n.on_event(1, Level("brain", "x"))
    time.sleep(0.05)
    assert sp.said == []
    n.on_event(1, TextChunk("Готово. "))
    assert sp.said == ["Готово."]


def test_narrator_unsaid_brain_code_and_long_result() -> None:
    """on_unsaid — один раз, когда часть ответа не прозвучит: код у мозга, длинный итог грамматики."""
    n, sp, _ = narrator("voice")
    calls: list[int] = []
    n.on_unsaid = lambda: calls.append(1)
    n.begin(1, spoken=True)
    n.on_event(1, Level("brain", "x"))
    n.on_event(1, TextChunk("Вот пример. "))
    assert calls == []
    n.on_event(1, TextChunk("```python\nprint(1)\n```\nЕщё текст. "))
    assert calls == [1]
    n.on_event(1, Done(ok=True, text=""))
    assert calls == [1] and sp.said == ["Вот пример.", tts.REST]

    n.begin(2, spoken=True)
    n.on_event(2, Level("grammar", "x"))
    n.on_event(2, Done(ok=True, text="Открыл Telegram"))
    assert calls == [1]
    long = "Умею так. " + "Открывать программы и папки, менять громкость и окна. " * 6
    n.begin(3, spoken=True)
    n.on_event(3, Level("grammar", "x"))
    n.on_event(3, Done(ok=True, text=long))
    assert calls == [1, 1] and sp.said[-1] == "Умею так."


def test_narrator_unsaid_not_called_when_silent_or_cancelled() -> None:
    n, _, _ = narrator("voice")
    calls: list[int] = []
    n.on_unsaid = lambda: calls.append(1)
    n.begin(1, spoken=False)  # набранный запрос в режиме voice — не озвучивается
    n.on_event(1, Level("brain", "x"))
    n.on_event(1, TextChunk("```\ncode\n```"))
    n.begin(2, spoken=True)
    n.on_event(2, Level("brain", "x"))
    n.on_event(2, Done(ok=False, text="Отменено", cancelled=True))
    assert calls == []


def test_narrator_cancelled_done_stops_and_logs_interrupted() -> None:
    n, sp, journal = narrator("voice")
    n.begin(1, spoken=True)
    n.on_event(1, Level("brain", "x"))
    n.on_event(1, TextChunk("Первое предложение. "))
    n.on_event(1, Done(ok=False, text="Отменено", cancelled=True, level="brain"))
    assert sp.said == ["Первое предложение."]
    assert sp.stops == 2  # begin + отмена
    [rec] = journal.records
    assert rec["interrupted"] is True


def test_narrator_new_request_interrupts_old() -> None:
    n, sp, journal = narrator("voice")
    n.begin(1, spoken=True)
    n.on_event(1, Level("brain", "x"))
    n.on_event(1, TextChunk("Длинный ответ. "))
    n.begin(2, spoken=True)  # PTT посреди речи — новый запрос
    assert journal.records[0]["interrupted"] is True
    n.on_event(1, TextChunk("Хвост старого. "))  # событие старого запроса — мимо
    assert sp.said == ["Длинный ответ."]


def test_narrator_cancel_esc() -> None:
    n, sp, _ = narrator("voice")
    n.begin(1, spoken=True)
    n.on_event(1, Level("grammar", "g"))
    n.cancel()
    n.on_event(1, Done(ok=True, text="Готово"))
    assert sp.said == [] and not n.active


def test_narrator_confirm_spoken_only_when_active() -> None:
    n, sp, _ = narrator("voice")
    n.begin(1, spoken=False)
    n.confirm("Завершить chrome.exe?")
    assert sp.said == []
    n.begin(2, spoken=True)
    n.confirm("Завершить chrome.exe?")
    assert sp.said == ["Завершить хром?"]


def test_narrator_reminder_and_speaker_not_ready() -> None:
    n, sp, _ = narrator("voice")
    n.say("Напоминание: проверить почту")
    assert sp.said == ["Напоминание: проверить почту"]
    sp.ready = False
    n.begin(3, spoken=True)
    n.on_event(3, Level("grammar", "g"))
    n.on_event(3, Done(ok=True, text="Готово"))
    assert sp.said == ["Напоминание: проверить почту"]  # голос не загружен — запрос не озвучивается


def test_narrator_close_stops_speaker() -> None:
    n, sp, _ = narrator("voice")
    closed: list[bool] = []
    sp.close = lambda: closed.append(True)  # type: ignore[attr-defined]
    n.begin(1, spoken=True)
    n.close()
    assert closed == [True] and not n.active


# --- голос Джарвиса: тон, «электроника», обращение -------------------------------------------------


class RecordingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.speeds: list[float] = []

    def generate(self, text: str, sid: int = 0, speed: float = 1.0) -> Any:
        self.speeds.append(speed)
        return super().generate(text, sid, speed)


def test_pitch_lowers_tone_keeping_length() -> None:
    sp = tts.Speaker("ruslan", speed=1.0, pitch=0.88)
    engine = RecordingEngine()
    sp._engine = engine
    pcm, rate = sp._synth("привет")
    assert engine.speeds == [1.0 / 0.88]  # синтез быстрее…
    assert rate == round(22050 * 0.88)  # …а вывод на меньшей частоте: голос ниже, длительность та же
    assert len(pcm) == len("привет") * 400 * 4


def test_pitch_and_effect_bounds() -> None:
    sp = tts.Speaker("ruslan", pitch=0.1, effect=7.0)
    assert (sp.pitch, sp.effect) == (0.8, 1.0)


def test_effect_applied_to_samples() -> None:
    plain = tts.Speaker("ruslan")
    plain._engine = FakeEngine()
    fx = tts.Speaker("ruslan", effect=0.65)
    fx._engine = FakeEngine()
    assert plain._synth("тест")[0] != fx._synth("тест")[0]
    assert plain._synth("тест")[1] == fx._synth("тест")[1] == 22050


@pytest.mark.parametrize(
    ("text", "ok", "lead", "spoken"),
    [
        ("Открыл Telegram", True, False, "Открыл Telegram, сэр."),
        ("Напомню в 18:30.", True, False, "Напомню в 18:30, сэр."),
        ("Какой файл открыть?", True, False, "Какой файл открыть, сэр?"),
        ("Готово!", True, False, "Готово, сэр!"),
        ("Нет устройства вывода звука", False, False, "Сэр, нет устройства вывода звука"),
        ("GPT не ответил", False, False, "Сэр, GPT не ответил"),
        ("Завершить chrome.exe?", True, True, "Сэр, завершить chrome.exe?"),
    ],
)
def test_addressed(text: str, ok: bool, lead: bool, spoken: str) -> None:
    assert tts.addressed(text, ok, "сэр", lead=lead) == spoken
    assert tts.addressed(text, ok, "", lead=lead) == text


def test_narrator_speaks_with_address() -> None:
    sp, journal = FakeSpeaker(), FakeJournal()
    n = tts.Narrator(sp, "voice", journal, address="сэр")  # type: ignore[arg-type]
    n.begin(1, spoken=True)
    n.on_event(1, Level("grammar", "grammar:open"))
    n.on_event(1, Done(True, "Открыл Telegram", level="grammar"))
    assert sp.said == ["Открыл телеграм, сэр."]  # латиница — транслитом (speakable), «сэр» — в конце
    n.say("Напоминание: вынуть пиццу")
    assert sp.said[-1] == "Сэр, напоминание: вынуть пиццу"
