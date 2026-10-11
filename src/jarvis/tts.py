"""Озвучка ответов: sherpa-onnx OfflineTts (piper/VITS, русский голос) и что из ответа говорить вслух.

Голоса — в <data>\\models\\tts-<имя> (piper ru_RU medium; скачивает scripts\\get_voice_models.ps1).
По умолчанию мужской denis (датасет CC0): по замеру распознаётся обратно 16/20 фраз, синтез фразы ≈140 мс
на 2 потоках; ruslan — 17/20, но датасет CC BY-NC-SA 4.0; dmitri (CC0) — 13/20, глотает начало фразы.

- Speaker — движок и два рабочих потока: синтез (следующее предложение готовится, пока звучит текущее)
  и воспроизведение (sounddevice через jarvis.mic, блоки по ~50 мс). stop() глушит сразу: поколение `gen`
  растёт, всё старое выбрасывается, поток вывода — abort(). Модель грузится в фоне (load()).
- Narrator — что говорить по событиям запроса (jarvis.events): [voice] tts = "voice" — только запросы,
  сказанные голосом; "always" — и набранные; "off" — молчать. Мозг — весь ответ по предложениям по мере
  прихода текста (длинное, код, таблицы — «Остальное — в окне»); «Секунду…», если до первых слов мозга
  дольше SECOND_DELAY_S; грамматика и руки — короткий итог Done.text; вопрос подтверждения — вслух (ответ —
  только клавишами окна). В журнал — запись kind="tts": tts_ms (от первой фразы до звука), reply_ms (от начала
  запроса до звука), spoken, interrupted.
- speakable(text) — текст для голоса: без markdown, ссылок, путей и расширений файлов; латиница —
  русскими буквами (piper с espeak-ng ru латиницу читает плохо: «Telegram» не распознаётся обратно).
"""

import array
import logging
import queue
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pc import settings

log = logging.getLogger("jarvis")

DEFAULT_VOICE = "ruslan"
VOICE_ORDER = ("ruslan", "denis", "dmitri", "irina")  # нужного голоса нет — первый скачанный отсюда
TTS_MODES = ("voice", "always", "off")
SPEAK_LIMIT = 600  # больше знаков ответа мозга вслух не читаем
SHORT_LIMIT = 200  # итог грамматики/рук длиннее — только первое предложение
SECOND_DELAY_S = 0.8  # «Секунду…», если мозг молчит дольше
SECOND = "Секунду…"
REST = "Остальное — в окне."
CHUNK_FRAMES = 1024  # блок вывода (~46 мс при 22 кГц): stop() глушит не позже чем через блок
WARMUP_TEXT = "Готов."


def voices_dir() -> Path:
    return settings.data_dir() / "models"


def voice_dir(name: str) -> Path:
    return voices_dir() / f"tts-{name}"


def available_voices() -> list[str]:
    """Скачанные голоса: папки tts-<имя> с моделью .onnx."""
    try:
        return sorted(
            p.name.removeprefix("tts-")
            for p in voices_dir().glob("tts-*")
            if p.is_dir() and any(p.glob("*.onnx"))
        )
    except OSError:
        return []


def pick_voice(wanted: str) -> str:
    """Голос для озвучки: wanted, если скачан; иначе первый скачанный (VOICE_ORDER, потом любой); нет — ""."""
    have = available_voices()
    if wanted in have:
        return wanted
    return next((name for name in (*VOICE_ORDER, *have) if name in have), "")


class TtsError(RuntimeError):
    """Голос не найден или не загрузился."""


def voice_files(name: str) -> dict[str, Path]:
    """model (.onnx), tokens.txt, espeak-ng-data. Нет — TtsError с подсказкой."""
    folder = voice_dir(name)
    hint = f"скачай: pwsh -File scripts\\get_voice_models.ps1 -Voice {name}"
    if not folder.is_dir():
        raise TtsError(f"нет голоса {name}: папки {folder} нет — {hint}")
    models = sorted(p for p in folder.glob("*.onnx") if p.is_file())
    if not models:
        raise TtsError(f"голос {name}: нет .onnx в {folder} — {hint}")
    tokens, espeak = folder / "tokens.txt", folder / "espeak-ng-data"
    if not tokens.is_file() or not espeak.is_dir():
        raise TtsError(f"голос {name}: нет tokens.txt или espeak-ng-data в {folder} — {hint}")
    return {"model": models[0], "tokens": tokens, "espeak": espeak}


# --- текст для голоса ---------------------------------------------------------------------------

_LATIN_WORDS = {
    "telegram": "телеграм", "chrome": "хром", "google": "гугл", "youtube": "ютуб", "discord": "дискорд",
    "steam": "стим", "spotify": "спотифай", "word": "ворд", "excel": "эксель", "powerpoint": "пауэрпоинт",
    "outlook": "аутлук", "windows": "виндоус", "explorer": "эксплорер", "firefox": "файрфокс",
    "edge": "эдж", "opera": "опера", "yandex": "яндекс", "whatsapp": "вотсап", "viber": "вайбер",
    "skype": "скайп", "zoom": "зум", "notepad": "ноутпад", "paint": "пэйнт", "studio": "студио",
    "visual": "вижуал", "code": "код", "github": "гитхаб", "git": "гит", "python": "пайтон",
    "jarvis": "джарвис", "chatgpt": "чат джипити", "ok": "окей", "wifi": "вайфай", "bluetooth": "блютус",
    "email": "имейл", "online": "онлайн", "offline": "офлайн", "photoshop": "фотошоп",
    "microsoft": "майкрософт", "office": "офис", "teams": "тимс", "slack": "слак", "notion": "ноушн",
    "obsidian": "обсидиан", "media": "медиа", "player": "плеер", "calculator": "калькулятор",
    "settings": "сеттингс", "store": "стор", "terminal": "терминал", "brave": "брейв", "twitch": "твич",
    "netflix": "нетфликс", "minecraft": "майнкрафт", "epic": "эпик", "games": "геймс",
    "launcher": "лаунчер", "nvidia": "энвидиа", "radeon": "радеон", "llama": "лама", "qwen": "квен",
    "codex": "кодекс", "everything": "эвритинг", "mail": "мейл", "drive": "драйв", "docs": "докс",
    "music": "мьюзик", "app": "эпп", "pro": "про", "max": "макс", "plus": "плюс", "chat": "чат",
    "bot": "бот", "desktop": "десктоп", "cloud": "клауд", "server": "сервер", "file": "файл",
    "files": "файлс", "home": "хоум", "browser": "браузер", "mozilla": "мозилла", "acrobat": "акробат",
    "reader": "ридер", "adobe": "адоби", "vlc": "ви эл си", "obs": "о би эс", "pdf": "пэ дэ эф",
    "usb": "ю эс би", "gpt": "джипити", "ai": "эй ай",
}  # fmt: skip
_LETTER_NAMES = {
    "a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи", "h": "эйч", "i": "ай",
    "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн", "o": "оу", "p": "пи", "q": "кью", "r": "ар",
    "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл ю", "x": "экс", "y": "уай", "z": "зед",
}  # fmt: skip
_DIGRAPHS = (
    ("sch", "ш"), ("tch", "ч"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("th", "т"), ("ph", "ф"),
    ("kh", "х"), ("ck", "к"), ("qu", "кв"), ("oo", "у"), ("ee", "и"), ("ea", "и"), ("ou", "ау"),
    ("ow", "оу"), ("ai", "эй"), ("ay", "эй"), ("ey", "эй"), ("oi", "ой"), ("oy", "ой"), ("au", "о"),
    ("aw", "о"), ("wh", "в"), ("ya", "я"), ("yu", "ю"), ("yo", "йо"), ("ts", "ц"),
)  # fmt: skip
_LETTERS = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж",
    "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т",
    "u": "у", "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з",
}  # fmt: skip
_VOWELS = set("aeiouy")


def transliterate(word: str) -> str:
    """Латинское слово → русскими буквами: словарь частых имён, аббревиатуры по буквам, иначе правила."""
    low = word.casefold()
    if low in _LATIN_WORDS:
        return _LATIN_WORDS[low]
    if word.isupper() and 2 <= len(word) <= 5:  # GPT, USB — по буквам
        return " ".join(_LETTER_NAMES.get(c, c) for c in low)
    if len(low) > 3 and low.endswith("e") and low[-2] not in _VOWELS:
        low = low[:-1]  # немая «e» на конце: «store» → «стор»
    out: list[str] = []
    i = 0
    while i < len(low):
        for src, dst in _DIGRAPHS:
            if low.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            c = low[i]
            if c == "c" and i + 1 < len(low) and low[i + 1] in "eiy":
                out.append("с")
            elif c == "e" and i == 0:
                out.append("э")
            else:
                out.append(_LETTERS.get(c, c))
            i += 1
    return "".join(out)


_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_WIN_PATH = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s,;«»\"']*")
_FILE_EXT = re.compile(r"(?<=\w)\.(?:[A-Za-z][A-Za-z0-9]{1,4})\b")
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_MARKUP = re.compile(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)|~~")
_LINE_MARK = re.compile(r"(?m)^\s*(?:#{1,6}\s+|>\s+|[-*•]\s+|\d{1,2}[.)]\s+)")
_LATIN = re.compile(r"[A-Za-z][A-Za-z']*")
_SPACES = re.compile(r"\s+")
_DECIMAL = re.compile(r"(?<=\d)\.(?=\d)")


def speakable(text: str) -> str:
    """Текст для синтеза: без markdown, ссылок, путей, расширений файлов и значков; латиница — по-русски."""
    t = _INLINE_CODE.sub(lambda m: m.group(1) if len(m.group(1)) <= 24 else "", str(text))
    t = _URL.sub("", t)
    t = _WIN_PATH.sub("", t)
    t = _FILE_EXT.sub("", t)
    t = _LINE_MARK.sub("", t)
    t = _MARKUP.sub("", t)
    t = "".join(
        ch for ch in t if not unicodedata.category(ch).startswith(("So", "Sk", "Cs", "Co", "Cn")) or ch in "°"
    )
    t = _DECIMAL.sub(",", t)  # 2.5 → 2,5: иначе «два точка пять»
    t = t.replace("%", " процентов").replace("№", "номер ").replace("&", " и ")
    t = _LATIN.sub(lambda m: transliterate(m.group(0)), t)
    t = _SPACES.sub(" ", t).strip(" ,;:—–-")
    return t if any(ch.isalnum() for ch in t) else ""


_SENTENCE_END = re.compile(r"[.!?…]+[»\"')\]]*(?=\s)|\n")
_UNSPOKEN = re.compile(r"^\s*(?:```|~~~|\|)")


@dataclass
class Splitter:
    """Поток текста мозга → законченные предложения для голоса, не больше limit знаков всего.

    Код (```), таблица (|) или превышение limit — дальше не читаем: одна фраза REST и тишина.
    """

    limit: int = SPEAK_LIMIT
    buffer: str = ""
    spoken: int = 0
    stopped: bool = False
    cut: bool = False  # что-то осталось только в окне

    def feed(self, chunk: str) -> list[str]:
        if self.stopped:
            self.cut = self.cut or bool(chunk.strip())
            return []
        self.buffer += chunk
        out: list[str] = []
        while not self.stopped:
            m = _SENTENCE_END.search(self.buffer)
            if m is None:
                break
            sentence, self.buffer = self.buffer[: m.end()], self.buffer[m.end() :]
            out += self._take(sentence)
        if not self.stopped and _UNSPOKEN.match(self.buffer):
            out += self._stop()
        return out

    def flush(self) -> list[str]:
        """Конец ответа: остаток буфера — последнее предложение."""
        if self.stopped:
            return self._rest()
        rest, self.buffer = self.buffer, ""
        out = self._take(rest)
        return out + (self._rest() if self.stopped else [])

    def _take(self, sentence: str) -> list[str]:
        if _UNSPOKEN.match(sentence):
            return self._stop()
        text = speakable(sentence)
        if not text:
            return []
        if self.spoken + len(text) > self.limit and self.spoken > 0:
            return self._stop()
        self.spoken += len(text)
        return [text]

    def _stop(self) -> list[str]:
        self.stopped, self.cut = True, True
        self.buffer = ""
        return []

    def _rest(self) -> list[str]:
        if self.cut and self.spoken:
            self.cut = False
            return [REST]
        return []


def short_text(text: str, limit: int = SHORT_LIMIT) -> str:
    """Итог грамматики/рук для голоса: speakable, длинное — до конца первого предложения."""
    t = speakable(text)
    if len(t) <= limit:
        return t
    m = _SENTENCE_END.search(t + " ")
    return t[: m.end()].strip() if m and m.end() <= limit else t[:limit].rsplit(" ", 1)[0]


# --- движок и воспроизведение -------------------------------------------------------------------


def addressed(text: str, ok: bool, address: str, lead: bool = False) -> str:
    """Короткая фраза в манере Джарвиса: «Открыл Telegram, сэр.», «Какой файл открыть, сэр?»; ошибка
    и вопрос подтверждения — «Сэр, нет устройства вывода звука».

    address "" — без изменений. Только для озвучки: в окне текст остаётся как есть.
    """
    text = text.strip()
    if not address or not text:
        return text
    if lead or not ok:
        head = text[:1].lower() + text[1:] if text[1:2].islower() else text  # «GPT …» не трогаем
        return f"{address[:1].upper()}{address[1:]}, {head}"
    body = text.rstrip(".!?… ")
    return f"{body}, {address}{text[len(body) :].strip() or '.'}"


class Speaker:
    """Синтез и воспроизведение. say() не блокирует; stop() глушит сразу (из любого потока).

    Поколение `gen` растёт на каждый stop(): фразы старого поколения выбрасываются. on_idle(gen) — все фразы
    поколения отзвучали (зовёт поток вывода). first_sound — time.monotonic() начала звука в текущем поколении.
    """

    def __init__(
        self,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        threads: int = 2,
        pitch: float = 1.0,
        effect: float = 0.0,
    ) -> None:
        self.voice = voice
        self.speed = speed
        self.pitch = min(1.2, max(0.8, float(pitch)))  # < 1 — ниже: синтез быстрее, вывод на меньшей частоте
        self.effect = min(1.0, max(0.0, float(effect)))  # «электроника» (jarvis.voicefx)
        self.threads = max(1, int(threads))
        self.ready = False
        self.error = ""
        self.load_ms = 0.0
        self.first_sound = 0.0
        self.on_idle: Callable[[int], None] | None = None
        self._engine: Any = None
        self._gen = 0
        self._lock = threading.Lock()
        self._say_q: queue.SimpleQueue[tuple[int, str] | None] = queue.SimpleQueue()
        self._play_q: queue.SimpleQueue[tuple[int, bytes, int] | None] = queue.SimpleQueue()
        self._pending = 0  # фраз текущего поколения в синтезе и в очереди вывода
        self._playing_until = 0.0
        self._cache: dict[str, tuple[bytes, int]] = {}
        self._threads: list[threading.Thread] = []
        self._closed = False

    # --- загрузка

    def load(self) -> None:
        """Модель голоса и прогрев (секунда-две) — в фоне при старте. Ошибка — TtsError."""
        t = time.perf_counter()
        try:
            files = voice_files(self.voice)
            import sherpa_onnx as so

            vits = so.OfflineTtsVitsModelConfig(
                model=str(files["model"]), tokens=str(files["tokens"]), data_dir=str(files["espeak"])
            )
            cfg = so.OfflineTtsConfig(
                model=so.OfflineTtsModelConfig(vits=vits, num_threads=self.threads), max_num_sentences=1
            )
            self._engine = so.OfflineTts(cfg)
            self._synth(WARMUP_TEXT)
            for phrase in (SECOND, REST):
                self._cache[phrase] = self._synth(phrase)
            from jarvis import mic

            mic.warmup()  # PortAudio — заранее, а не на первой фразе
        except TtsError as e:
            self.error = str(e)
            raise
        except Exception as e:
            self.error = f"голос {self.voice} не загрузился: {type(e).__name__}: {e}"
            raise TtsError(self.error) from e
        self.load_ms = round((time.perf_counter() - t) * 1000, 1)
        self.ready, self.error = True, ""
        log.info("голос: озвучка %s загружена за %.0f мс", self.voice, self.load_ms)

    def _synth(self, text: str) -> tuple[bytes, int]:
        """Текст → PCM float32 (байты) и частота вывода.

        Тон ниже (pitch < 1): синтез быстрее в 1/pitch раза, а вывод на частоте ×pitch — длительность та же,
        голос ниже. «Электроника» — jarvis.voicefx.electronic, десятки миллисекунд на фразу.
        """
        audio = self._engine.generate(text, sid=0, speed=self.speed / self.pitch)
        rate = int(audio.sample_rate)
        samples = audio.samples
        if self.effect > 0:
            from jarvis.voicefx import electronic

            samples = electronic(samples, rate, self.effect)
        return array.array("f", samples).tobytes(), max(8000, round(rate * self.pitch))

    # --- управление

    @property
    def gen(self) -> int:
        return self._gen

    @property
    def speaking(self) -> bool:
        """Звучит или вот-вот зазвучит (защита от эха: микрофон в это время не пишет)."""
        return self._pending > 0 or time.monotonic() < self._playing_until

    def say(self, text: str) -> int:
        """Сказать фразу после уже поставленных; вернуть поколение. Пустое или не загружен — ничего."""
        text = text.strip()
        if not text or self._closed or not self.ready:
            return self._gen
        self._ensure_threads()
        with self._lock:
            self._pending += 1
            gen = self._gen
        self._say_q.put((gen, text))
        return gen

    def stop(self) -> bool:
        """Заглушить сейчас: очередь — в мусор, звук прерывается. True — что-то звучало или ждало."""
        with self._lock:
            busy = self._pending > 0 or time.monotonic() < self._playing_until
            self._gen += 1
            self._pending = 0
            self._playing_until = 0.0
            self.first_sound = 0.0
        return busy

    def close(self) -> None:
        self.stop()
        self._closed = True
        self._say_q.put(None)
        self._play_q.put(None)

    # --- потоки

    def _ensure_threads(self) -> None:
        with self._lock:
            if self._threads and all(t.is_alive() for t in self._threads):
                return
            self._threads = [
                threading.Thread(target=self._synth_loop, name="jarvis-tts", daemon=True),
                threading.Thread(target=self._play_loop, name="jarvis-tts-play", daemon=True),
            ]
            for t in self._threads:
                t.start()

    def _synth_loop(self) -> None:
        while True:
            item = self._say_q.get()
            if item is None:
                return
            gen, text = item
            if gen != self._gen:
                continue
            try:
                pcm, rate = self._cache.get(text) or self._synth(text)
            except Exception as e:
                log.warning("голос: синтез не удался: %s", e)
                self._done_one(gen)
                continue
            self._play_q.put((gen, pcm, rate))

    def _play_loop(self) -> None:
        from jarvis import mic

        stream: Any = None
        stream_rate = 0
        while True:
            item = self._play_q.get()
            if item is None:
                break
            gen, pcm, rate = item
            if gen != self._gen:
                continue
            try:
                if stream is None or stream_rate != rate:
                    stream = _close_stream(stream, abort=False)
                    stream = mic.api().open_output(None, rate)
                    stream_rate = rate
                interrupted = self._write(stream, gen, pcm, rate)
            except Exception as e:
                log.warning("голос: вывод звука не удался: %s", e)
                stream = _close_stream(stream, abort=True)
                self._done_one(gen)
                continue
            if interrupted:
                stream = _close_stream(stream, abort=True)
                continue
            if self._done_one(gen):  # больше нечего говорить — дождаться хвоста и закрыть устройство
                stream = _close_stream(stream, abort=False)
        _close_stream(stream, abort=True)

    def _write(self, stream: Any, gen: int, pcm: bytes, rate: int) -> bool:
        """Блоками по CHUNK_FRAMES; True — прервано stop()."""
        step = CHUNK_FRAMES * 4
        with self._lock:
            if gen != self._gen:
                return True
            if not self.first_sound:
                self.first_sound = time.monotonic()
        for off in range(0, len(pcm), step):
            if gen != self._gen:
                return True
            block = pcm[off : off + step]
            stream.write(block)
            with self._lock:
                if gen != self._gen:
                    return True
                self._playing_until = time.monotonic() + len(block) / 4 / rate + 0.1
        return gen != self._gen

    def _done_one(self, gen: int) -> bool:
        """Фраза поколения gen закончилась; True — последняя из поставленных (тогда зовётся on_idle)."""
        with self._lock:
            if gen != self._gen:
                return False
            self._pending = max(0, self._pending - 1)
            if self._pending:
                return False
        if self.on_idle is not None:
            try:
                self.on_idle(gen)
            except Exception:
                log.exception("голос: on_idle")
        return True


def _close_stream(stream: Any, abort: bool) -> None:
    if stream is None:
        return None
    try:
        if abort:
            stream.abort()
        else:
            stream.stop()
    except Exception as e:
        log.debug("голос: остановка вывода: %s", e)
    try:
        stream.close()
    except Exception as e:
        log.debug("голос: закрытие вывода: %s", e)
    return None


# --- что говорить -------------------------------------------------------------------------------


@dataclass
class _Request:
    rid: int
    active: bool
    t0: float
    gen: int = 0
    level: str = ""
    splitter: Splitter = field(default_factory=Splitter)
    got_text: bool = False
    done: bool = False  # Done обработан — после последней фразы пишем журнал
    timer: threading.Timer | None = None
    first_say: float = 0.0
    chars: int = 0
    phrases: int = 0
    logged: bool = False


class Narrator:
    """Решает, что сказать по событиям запроса; говорит через Speaker. Методы быстрые (UI-поток)."""

    def __init__(self, speaker: Speaker, mode: str = "voice", journal: Any = None, address: str = "") -> None:
        self.speaker = speaker
        self.mode = mode if mode in TTS_MODES else "voice"
        self.journal = journal
        # манера Джарвиса ([brain] persona): «Открыл Telegram, сэр.»; "" — без обращения
        self.address = address
        self._req = _Request(0, False, 0.0)
        self._lock = threading.Lock()
        speaker.on_idle = self._idle

    @property
    def active(self) -> bool:
        """Озвучивается ли текущий запрос."""
        return self._req.active

    def begin(self, rid: int, spoken: bool) -> None:
        """Новый запрос: старая речь замолкает; говорить ли — по режиму и тому, сказан ли запрос голосом."""
        self._interrupt()
        active = self.mode == "always" or (self.mode == "voice" and spoken)
        with self._lock:
            self._req = _Request(rid, active and self.speaker.ready, time.monotonic(), gen=self.speaker.gen)

    def cancel(self) -> bool:
        """Esc или PTT: замолчать сейчас. True — речь звучала (или ждала) и прервана."""
        with self._lock:
            self._req.active = False
        return self._interrupt()

    def close(self) -> None:
        """Выход Jarvis: замолчать и остановить потоки озвучки."""
        self.cancel()
        self.speaker.close()

    def say(self, text: str) -> None:
        """Сказать вне запроса (напоминание) — при tts != "off"."""
        if self.mode != "off":
            self.speaker.say(addressed(short_text(text), True, self.address, lead=True))

    def confirm(self, summary: str) -> None:
        """Вопрос подтверждения — вслух (ответ — только клавишами окна: голосом «да» нельзя)."""
        with self._lock:
            req = self._req
            if not req.active:
                return
            self._cancel_timer()
            phrases = [addressed(short_text(summary), True, self.address, lead=True)]
        self._say(req, phrases)

    def on_event(self, rid: int, ev: Any) -> None:
        """Событие запроса rid (jarvis.events) — может поставить фразы в очередь Speaker."""
        from jarvis.events import Done, Level, TextChunk

        with self._lock:
            req = self._req
            if rid != req.rid or not req.active:
                return
            if isinstance(ev, Level):
                req.level = ev.level
                if ev.level == "brain" and req.timer is None and not req.got_text:
                    req.timer = threading.Timer(SECOND_DELAY_S, self._second, args=(rid,))
                    req.timer.daemon = True
                    req.timer.start()
                return
            if isinstance(ev, TextChunk):
                if req.level != "brain":
                    return
                req.got_text = True
                self._cancel_timer()
                phrases = req.splitter.feed(ev.text)
            elif isinstance(ev, Done):
                self._cancel_timer()
                req.active, req.done = False, True
                if ev.cancelled:
                    phrases = []
                elif req.level == "brain" and req.got_text:
                    phrases = req.splitter.flush()
                else:
                    phrases = [addressed(short_text(ev.text), ev.ok, self.address)]
            else:
                return
        if isinstance(ev, Done) and ev.cancelled:
            self._interrupt()
            return
        self._say(req, phrases)
        if isinstance(ev, Done) and not self.speaker.speaking:
            self._log(req, interrupted=False)

    def _say(self, req: _Request, phrases: list[str]) -> None:
        for phrase in phrases:
            if not phrase:
                continue
            with self._lock:
                if not req.first_say:
                    req.first_say = time.monotonic()
                req.chars += len(phrase)
                req.phrases += 1
            self.speaker.say(phrase)

    def _second(self, rid: int) -> None:
        with self._lock:
            req = self._req
            if rid != req.rid or not req.active or req.got_text:
                return
            req.timer = None
        self._say(req, [SECOND])

    def _cancel_timer(self) -> None:
        timer, self._req.timer = self._req.timer, None
        if timer is not None:
            timer.cancel()

    def _interrupt(self) -> bool:
        with self._lock:
            req = self._req
            self._cancel_timer()
        first_sound = self.speaker.first_sound
        interrupted = self.speaker.stop()
        self._log(req, interrupted=interrupted, first_sound=first_sound)
        return interrupted

    def _idle(self, gen: int) -> None:
        """Speaker: все фразы поколения отзвучали; запрос закончен — запись в журнал."""
        req = self._req
        if gen == req.gen and req.done:
            self._log(req, interrupted=False)

    def _log(self, req: _Request, interrupted: bool, first_sound: float | None = None) -> None:
        """Одна запись kind="tts" на запрос: tts_ms — от первой фразы до звука, reply_ms — от начала
        запроса до звука."""
        with self._lock:
            if req.logged or not req.phrases:
                return
            req.logged = True
        sound = self.speaker.first_sound if first_sound is None else first_sound
        if self.journal is None:
            return
        record: dict[str, Any] = {
            "kind": "tts",
            "level": req.level,
            "voice": self.speaker.voice,
            "spoken": bool(sound),
            "interrupted": interrupted,
            "chars": req.chars,
            "phrases": req.phrases,
        }
        if sound and req.first_say:
            record["tts_ms"] = round(max(0.0, sound - req.first_say) * 1000, 1)
            record["reply_ms"] = round(max(0.0, sound - req.t0) * 1000, 1)
        try:
            self.journal.write(record)
        except Exception:
            log.exception("голос: запись журнала")
