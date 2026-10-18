"""«Конспект видео и созвонов»: запись того, что звучит, и микрофона → расшифровка → конспект в заметки.

Запись (системный звук + микрофон, смесь в моно 16 кГц) идёт кусками в <data>/summaries/<время>.wav. После
«стоп» запись распознаётся кусками до 25 с, разрезанными по паузам (ctx.voice.transcribe), а ИИ пишет
конспект: заголовок, «О чём говорили», «Решения», «Задачи». Конспект сохраняется заметкой, задачи — в список
задач. Без ИИ заметкой сохраняется расшифровка. Ход — plugin.summary.state {state, seconds, …}:
recording → transcribing → summarizing → done | error.
"""

import asyncio
import contextlib
import json
import logging
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np

from jarvis import paths
from jarvis.plugins.audio import AudioSource, Recorder, SoundcardSource, read_wav_chunks
from jarvis.plugins.base import ConfigField, Option, Plugin, PluginAction, error_text
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

MIN_SECONDS = 2.0
CHUNK_S = 25.0
SILENT_RMS = 1e-3  # кусок тише — не распознаём
MAX_PROMPT_CHARS = 60_000  # длиннее — конспект по частям, потом сводный
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "короткий заголовок, до 8 слов"},
        "about": {"type": "array", "items": {"type": "string"}, "description": "о чём говорили, 3–7 пунктов"},
        "decisions": {"type": "array", "items": {"type": "string"}, "description": "принятые решения"},
        "tasks": {"type": "array", "items": {"type": "string"}, "description": "задачи: что, кто, срок"},
    },
    "required": ["title", "about", "decisions", "tasks"],
    "additionalProperties": False,
}
SYSTEM_PROMPT = (
    "Ты — внимательный секретарь. По расшифровке созвона, встречи или видео составь конспект на русском "
    "языке. Пиши кратко и по делу. Расшифровка сделана автоматически и может содержать ошибки "
    "распознавания — "
    "исправляй очевидные, но не выдумывай того, чего нет в записи. Если решений или задач не было — оставь "
    "список пустым. Задачи формулируй как поручения: что сделать и, если прозвучало, кто и к какому сроку."
)
MERGE_PROMPT = (
    "Ниже — конспекты последовательных частей одной записи в JSON. Объедини их в один конспект: общий "
    "заголовок, без повторов, сохрани все решения и задачи."
)
STATES = ("idle", "recording", "transcribing", "summarizing", "done", "error")


def format_duration(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {secs:02d} с"
    return f"{secs} с"


def _clean_list(value: Any, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = " ".join(str(item or "").split()).strip(" -•*")
        if text:
            out.append(text[:500])
    return out[:limit]


def normalize_summary(raw: Any) -> dict[str, Any]:
    """Ответ ИИ → {title, about, decisions, tasks}; строку пробуем разобрать как JSON."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            raw = json.loads(text)
        except ValueError:
            return {"title": "", "about": [raw.strip()] if raw.strip() else [], "decisions": [], "tasks": []}
    if not isinstance(raw, dict):
        return {"title": "", "about": [], "decisions": [], "tasks": []}
    return {
        "title": " ".join(str(raw.get("title") or "").split())[:80],
        "about": _clean_list(raw.get("about")),
        "decisions": _clean_list(raw.get("decisions")),
        "tasks": _clean_list(raw.get("tasks")),
    }


def note_body(summary: dict[str, Any], footer: str, transcript: str = "") -> str:
    lines = ["## О чём говорили"]
    lines += [f"- {x}" for x in summary["about"]] or ["- —"]
    lines += ["", "## Решения"]
    lines += [f"- {x}" for x in summary["decisions"]] or ["- Не принимали"]
    lines += ["", "## Задачи"]
    lines += [f"- {x}" for x in summary["tasks"]] or ["- Нет"]
    lines += ["", footer]
    if transcript:
        lines += ["", "## Расшифровка", transcript]
    return "\n".join(lines)


def _split_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) > limit and current:
            parts.append("\n".join(current))
            current, size = [], 0
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


class Summary(Plugin):
    id = "summary"
    name = "Конспект видео и созвонов"
    description = "Слушает созвон или видео и пишет конспект: о чём говорили, какие решения и задачи."
    icon = "summary"
    color = ("#A78BFA", "#7C3AED")
    fields = (
        ConfigField(
            "language",
            "select",
            "Язык разговора",
            default="ru",
            options=(
                Option("ru", "Русский"),
                Option("auto", "Определять автоматически"),
                Option("en", "Английский"),
            ),
        ),
        ConfigField("microphone", "bool", "Записывать и мой микрофон", default=True),
        ConfigField("mic_device", "device", "Микрофон", default="", device="input"),
        ConfigField("speaker", "device", "Чей звук записывать", default="", device="output"),
        ConfigField("keep_transcript", "bool", "Добавлять расшифровку в заметку", default=True),
        ConfigField("keep_audio", "bool", "Хранить запись после конспекта", default=False),
        ConfigField(
            "max_minutes", "number", "Остановить запись через, мин", default=180, min=5, max=600, integer=True
        ),
    )
    actions = (PluginAction("start", "Начать запись"), PluginAction("stop", "Закончить и сделать конспект"))
    examples = ("начни конспект", "закончи конспект")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        # тесты подставляют свои источники звука
        self.source_factory: Callable[[dict[str, Any]], list[AudioSource]] | None = None
        self._recorder: Recorder | None = None
        self._ticker: asyncio.Task[Any] | None = None
        self._job: asyncio.Task[Any] | None = None
        self._state = "idle"
        self._seconds = 0.0
        self._file: Path | None = None
        self._started_at = 0.0
        self._progress = 0.0
        self._last: dict[str, Any] = {}
        self._failure = ""

    async def stop(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            # запись остаётся в файле — после включения её можно обработать действием retry
            await self.ctx.run_io(recorder.stop)
            self._seconds = recorder.seconds
        self._state = "idle"

    def status(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "state": self._state,
            "seconds": round(self._recorder.seconds if self._recorder else self._seconds, 1),
            "file": str(self._file) if self._file else "",
            "last": self._last,
        }
        if self._state == "transcribing":
            data["progress"] = round(self._progress, 2)
        if self._state == "error" and self._failure:
            data["failure"] = self._failure
        return data

    def _set_state(self, state: str, **extra: Any) -> None:
        changed = state != self._state
        self._state = state
        seconds = self._recorder.seconds if self._recorder else self._seconds
        self.publish("state", {"state": state, "seconds": round(seconds, 1), **extra})
        if changed:
            self.changed()

    # --- запись ---------------------------------------------------------------------------------------
    def _sources(self, config: dict[str, Any]) -> list[AudioSource]:
        if self.source_factory is not None:
            return self.source_factory(config)
        sources: list[AudioSource] = [SoundcardSource("loopback", str(config.get("speaker") or ""))]
        if config.get("microphone"):
            sources.append(SoundcardSource("mic", str(config.get("mic_device") or "")))
        return sources

    async def begin(self) -> Path:
        if self._recorder is not None:
            raise UserFacingError("Запись уже идёт")
        if self._job is not None and not self._job.done():
            raise UserFacingError("Ещё готовлю прошлый конспект — подождите минуту")
        config = self.config
        folder = paths.sub_dir("summaries")
        path = folder / f"{time.strftime('%Y-%m-%d_%H-%M-%S')}.wav"
        loop = asyncio.get_running_loop()
        recorder = Recorder(
            path,
            self._sources(config),
            on_error=lambda message: loop.call_soon_threadsafe(self._source_failed, message),
        )
        await self.ctx.run_io(recorder.start)
        self._recorder = recorder
        self._file = path
        self._seconds = 0.0
        self._failure = ""
        self.error = ""
        self._started_at = time.time()
        self._set_state("recording")
        self._ticker = self.spawn(
            self._tick(recorder, float(config.get("max_minutes") or 180) * 60), "ticker"
        )
        return path

    def _source_failed(self, message: str) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        if len(recorder.errors) >= recorder.source_count:
            self.toast("Конспект", f"Запись остановилась: {message}", status="error")
        else:
            self.toast("Конспект", f"Пишу без источника — {message}", status="info")

    async def _tick(self, recorder: Recorder, limit_s: float) -> None:
        while self._recorder is recorder:
            await asyncio.sleep(1.0)
            if self._recorder is not recorder:
                return
            self.publish(
                "state",
                {"state": "recording", "seconds": round(recorder.seconds, 1), "level": recorder.level},
            )
            if recorder.seconds >= limit_s:
                self.toast("Конспект", "Запись остановлена: достигнута максимальная длина", status="info")
                self.ctx.spawn(self.finish(), "summary-finish")
                return
            if not recorder.alive:
                self._recorder = None
                self._seconds = recorder.seconds
                problem = "; ".join(recorder.errors) or "запись звука остановилась"
                self._fail(f"Не удалось записать звук: {problem}")
                return

    async def finish(self) -> Path:
        """Остановить запись и запустить расшифровку и конспект в фоне."""
        recorder = self._recorder
        if recorder is None:
            raise UserFacingError("Запись не идёт")
        self._recorder = None
        ticker, self._ticker = self._ticker, None
        if ticker is not None and ticker is not asyncio.current_task():
            ticker.cancel()
        seconds = await self.ctx.run_io(recorder.stop)
        self._seconds = seconds
        path = recorder.path
        if seconds < MIN_SECONDS:
            with contextlib.suppress(OSError):
                path.unlink()
            self._file = None
            self._fail("Запись слишком короткая")
            return path
        self._job = self.spawn(self.process(path, seconds), "process")
        return path

    def _fail(self, message: str) -> None:
        self._failure = message
        self._set_state("error", error=message)
        self.toast("Конспект", message, status="error")

    # --- обработка ------------------------------------------------------------------------------------
    async def process(self, path: Path, seconds: float | None = None) -> dict[str, Any] | None:
        """Расшифровать запись и сделать конспект. Вернёт {note_id, title, tasks} или None при ошибке."""
        try:
            if seconds is None:
                seconds = await self.ctx.run_io(_wav_seconds, path)
            self._seconds = seconds
            self._progress = 0.0
            self._set_state("transcribing", progress=0.0)
            transcript = await self.transcribe(path, seconds)
            if not transcript.strip():
                self._fail("В записи не распознано речи")
                return None
            config = self.config
            started = time.localtime(path.stat().st_mtime - seconds) if path.exists() else time.localtime()
            when = time.strftime("%d.%m.%Y %H:%M", started)
            footer = f"Запись от {when} · {format_duration(seconds)}"
            if self._ai_ready():
                self._set_state("summarizing")
                summary = await self.summarize(transcript)
                title = f"Конспект: {summary['title']}" if summary["title"] else f"Конспект от {when}"
                body = note_body(summary, footer, transcript if config.get("keep_transcript") else "")
                tasks = summary["tasks"]
            else:
                title = f"Расшифровка от {when}"
                body = f"{transcript}\n\n{footer}\n\nИИ не настроен — конспект не составлен."
                tasks = []
            note = self.ctx.notes.note_add(title, body) or {}
            for task in tasks:
                try:
                    self.ctx.notes.task_add(task)
                except Exception:
                    log.exception("конспект: задача не добавилась")
            if not config.get("keep_audio"):
                with contextlib.suppress(OSError):
                    path.unlink()
                self._file = None
            self._last = {
                "note_id": note.get("id", ""),
                "title": title,
                "tasks": len(tasks),
                "seconds": round(seconds, 1),
                "at": time.time(),
            }
            self._set_state("done", **self._last)
            self.toast("Конспект готов", title, status="ok", icon="note")
            await self._announce(title, len(tasks))
            return self._last
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not isinstance(e, UserFacingError):
                log.exception("конспект: обработка упала")
            self._fail(f"Конспект не получился: {error_text(e)}")
            return None

    def _ai_ready(self) -> bool:
        try:
            return bool(self.ctx.ai.configured)
        except Exception:
            return False

    async def transcribe(self, path: Path, seconds: float) -> str:
        language = str(self.config.get("language") or "ru")
        chunks: Iterator[tuple[np.ndarray, int]] = read_wav_chunks(path, CHUNK_S)
        texts: list[str] = []
        done_s = 0.0
        while True:
            item = await self.ctx.run_io(next, chunks, None)
            if item is None:
                break
            samples, rate = item
            done_s += len(samples) / rate
            if len(samples) and float(np.sqrt(np.mean(np.square(samples)))) >= SILENT_RMS:
                text = await self.ctx.voice.transcribe(samples, rate, language=language)
                if text and text.strip():
                    texts.append(" ".join(text.split()))
            self._progress = min(1.0, done_s / seconds) if seconds else 1.0
            self.publish(
                "state", {"state": "transcribing", "seconds": round(seconds, 1), "progress": self._progress}
            )
        return "\n".join(texts)

    async def summarize(self, transcript: str) -> dict[str, Any]:
        if len(transcript) <= MAX_PROMPT_CHARS:
            return await self._ask(transcript)
        partials = [await self._ask(part) for part in _split_text(transcript, MAX_PROMPT_CHARS)]
        merged = json.dumps(partials, ensure_ascii=False)
        return await self._ask(merged, merge=True)

    async def _ask(self, text: str, *, merge: bool = False) -> dict[str, Any]:
        user = f"{MERGE_PROMPT}\n\n{text}" if merge else f"Расшифровка:\n\n{text}"
        raw = await self.ctx.ai.complete(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            json_schema=SUMMARY_SCHEMA,
            temperature=0.2,
            max_tokens=2000,
        )
        return normalize_summary(raw)

    async def _announce(self, title: str, tasks: int) -> None:
        if self.ctx.settings.value.voice.silent_mode:
            return
        tail = f", задач: {tasks}" if tasks else ""
        try:
            await self.ctx.tts.say(f"Конспект готов и сохранён в заметки{tail}.")
        except Exception:
            log.info("конспект: не удалось сказать о готовности")

    # --- действия и команды ---------------------------------------------------------------------------
    async def act_start(self, args: dict[str, Any]) -> dict[str, Any]:
        path = await self.begin()
        return {"file": str(path)}

    async def act_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        path = await self.finish()
        return {"file": str(path), "seconds": round(self._seconds, 1)}

    async def act_toggle(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._recorder is not None:
            return await self.act_stop(args)
        return await self.act_start(args)

    async def act_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        """Остановить запись без конспекта и удалить файл."""
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            raise UserFacingError("Запись не идёт")
        await self.ctx.run_io(recorder.stop)
        with contextlib.suppress(OSError):
            recorder.path.unlink()
        self._file = None
        self._seconds = 0.0
        self._set_state("idle")
        return {}

    async def act_retry(self, args: dict[str, Any]) -> dict[str, Any]:
        """Ещё раз обработать последнюю запись (после ошибки распознавания или ИИ)."""
        path = self._file
        if path is None or not path.exists():
            raise UserFacingError("Нет записи для повторной обработки")
        if self._recorder is not None or (self._job is not None and not self._job.done()):
            raise UserFacingError("Сейчас идёт запись или обработка")
        self._job = self.spawn(self.process(path), "process")
        return {"file": str(path)}

    def commands(self) -> list[DynamicCommand]:
        return [
            self.command(
                "start",
                "Начать конспект",
                [
                    "начни конспект",
                    "запиши созвон",
                    "начни запись созвона",
                    "конспектируй",
                    "сделай конспект созвона",
                ],
                self._cmd_start,
            ),
            self.command(
                "stop",
                "Закончить конспект",
                [
                    "закончи конспект",
                    "останови конспект",
                    "стоп конспект",
                    "заверши конспект",
                    "закончи запись созвона",
                ],
                self._cmd_stop,
            ),
        ]

    async def _cmd_start(self, slots: dict[str, Any]) -> ExecResult:
        await self.begin()
        return ExecResult(True, "Конспект", "Записываю звук и микрофон", icon=self.icon, say="Записываю.")

    async def _cmd_stop(self, slots: dict[str, Any]) -> ExecResult:
        await self.finish()
        return ExecResult(
            True, "Конспект", "Готовлю конспект — сохраню в заметки", icon=self.icon, say="Готовлю конспект."
        )


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / (wav.getframerate() or 1)
