"""«Чтение вслух»: статьи и книги голосом ассистента — с подсветкой текущей фразы и своей скоростью.

Откуда текст: args.text → args.url → выделенный текст (Ctrl+C в активном окне, прежний буфер обмена потом
возвращается) → буфер обмена. Если текст — ссылка, страница скачивается и из неё берётся статья
(<article>, <main> или самый «текстовый» блок — без меню, скриптов и рекламы).
Текст режется на предложения; следующее синтезируется (ctx.tts.synthesize), пока звучит текущее
(ctx.tts.play), и ускоряется без изменения высоты голоса до config.speed. Ход — plugin.reader.progress
{state: playing|paused|stopped|done, index, total, sentence, title, sentences (только в первом событии)}.
"""

import asyncio
import contextlib
import logging
import re
import time
from typing import Any

import httpx
import numpy as np

from jarvis.bus import Event
from jarvis.plugins.audio import time_stretch
from jarvis.plugins.base import ConfigField, Plugin, PluginAction, error_text
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

MAX_CHARS = 300_000
MAX_SENTENCE = 320
FETCH_LIMIT = 5 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
SPEED_STEP = 0.25
SYNTH_FAILURES_LIMIT = 3

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_BOUNDARY = re.compile(r"([.!?…]+[»\"”’)\]]*)(\s+)(?=[«\"“„(\[]?[A-ZА-ЯЁ0-9—–-])")
# сокращения, после которых точка не заканчивает предложение
_ABBREVIATIONS = frozenset(
    (
        *("т.е", "т.д", "т.п", "т.к", "т.н", "др", "пр", "см", "стр", "рис", "гг", "ул", "им", "тыс", "млн"),
        *("млрд", "руб", "коп", "мин", "сек", "проф", "акад", "доц", "напр", "англ", "лат", "св", "вв", "ст"),
        *("пп", "гл", "ср", "тел", "обл", "р-н", "пос", "кв", "корп", "e.g", "i.e", "mr", "mrs", "ms", "dr"),
        *("vs", "etc", "no", "jr", "sr", "st", "fig", "inc", "ltd"),
    )
)
_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "svg",
    "button",
    "template",
    "menu",
    "dialog",
)
_JUNK_CLASS = re.compile(
    r"(^|[\s_-])(nav|menu|footer|header|sidebar|comment|share|social|advert|promo|related|subscribe|cookie|"
    r"banner|breadcrumb|popup|modal)", re.IGNORECASE
)  # fmt: skip
_BLOCKS = ("h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre")


# --- текст ---------------------------------------------------------------------------------------------


def clean_text(text: str) -> str:
    """Подготовить текст к чтению: без разметки Markdown, ссылок и лишних пробелов."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")[:MAX_CHARS]
    text = _URL_RE.sub("ссылка", text)
    text = re.sub(r"(?m)^[ \t]*(#{1,6}|>+|[-*•][ \t])[ \t]*", "", text)
    text = re.sub(r"(\*\*|__|`+)", "", text)
    text = re.sub(r"[ \t ]+", " ", text)
    return text.strip()


def _is_abbreviation(word: str, punct: str) -> bool:
    if not punct.startswith(".") or punct.startswith(".."):
        return False
    w = word.lower().strip("«(\"'[")
    if len(w) == 1 and w.isalpha():  # инициал «А.», «г.»
        return True
    return w in _ABBREVIATIONS or ("." in w and len(w) <= 6)


def _split_paragraph(paragraph: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(paragraph):
        before = paragraph[start : match.start(1)]
        word = before.split()[-1] if before.split() else ""
        if _is_abbreviation(word, match.group(1)):
            continue
        parts.append(paragraph[start : match.end(1)].strip())
        start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _split_long(sentence: str, limit: int = MAX_SENTENCE) -> list[str]:
    out: list[str] = []
    rest = sentence
    while len(rest) > limit:
        cut = -1
        for sep in ("; ", ": ", " — ", " – ", ", "):
            index = rest.rfind(sep, int(limit * 0.3), limit)
            if index > 0:
                cut = index + len(sep)
                break
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


def split_sentences(text: str) -> list[str]:
    """Текст → предложения для чтения (длинные режутся по знакам препинания)."""
    pieces: list[str] = []
    for line in text.split("\n"):
        paragraph = " ".join(line.split())
        if paragraph:
            pieces.extend(_split_paragraph(paragraph))
    out: list[str] = []
    carry = ""
    for piece in pieces:
        if not any(ch.isalnum() for ch in piece):
            continue
        if sum(ch.isalpha() for ch in piece) < 2:  # «1.», «IV.» — присоединить к следующему
            carry = f"{carry} {piece}".strip()
            continue
        if carry:
            piece = f"{carry} {piece}"
            carry = ""
        out.extend(_split_long(piece))
    if carry:
        out.append(carry)
    return out


def extract_article(html: bytes | str, encoding: str | None = None) -> tuple[str, str]:
    """Страница → (заголовок, текст статьи) без меню, скриптов и рекламы."""
    from bs4 import BeautifulSoup, Tag

    soup = (
        BeautifulSoup(html, "html.parser", from_encoding=encoding)
        if isinstance(html, bytes)
        else BeautifulSoup(html, "html.parser")
    )
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if isinstance(og, Tag) and og.get("content"):
        title = str(og.get("content"))
    elif soup.title and soup.title.string:
        title = str(soup.title.string)
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag) or tag.decomposed or tag.name in ("html", "body", "article", "main"):
            continue
        marker = " ".join([*(tag.get("class") or []), str(tag.get("id") or ""), str(tag.get("role") or "")])
        if marker.strip() and _JUNK_CLASS.search(marker):
            tag.decompose()

    def blocks(root: Tag) -> list[str]:
        out: list[str] = []
        for el in root.find_all(_BLOCKS):
            if any(parent.name in _BLOCKS for parent in el.parents if parent is not root):
                continue
            text = " ".join(el.get_text(" ", strip=True).split())
            if len(text) >= 2:
                out.append(text)
        return out

    candidates: list[Tag] = [t for t in (soup.find("article"), soup.find("main")) if isinstance(t, Tag)]
    role_main = soup.find(attrs={"role": "main"})
    if isinstance(role_main, Tag):
        candidates.append(role_main)
    scores: dict[int, tuple[int, Tag]] = {}
    for p in soup.find_all("p"):
        parent = p.parent
        if isinstance(parent, Tag):
            length = len(p.get_text(" ", strip=True))
            score, _ = scores.get(id(parent), (0, parent))
            scores[id(parent)] = (score + length, parent)
    if scores:
        candidates.append(max(scores.values(), key=lambda item: item[0])[1])
    body = soup.body if isinstance(soup.body, Tag) else None
    if body is not None:
        candidates.append(body)
    text_blocks: list[str] = []
    for candidate in candidates:
        found = blocks(candidate)
        if sum(len(b) for b in found) >= 200 or (found and candidate is body):
            text_blocks = found
            break
        if len(found) > len(text_blocks):
            text_blocks = found
    if not text_blocks and body is not None:
        text_blocks = [line for line in body.get_text("\n", strip=True).splitlines() if line.strip()]
    title = " ".join(title.split())
    if text_blocks and title and text_blocks[0].casefold() in title.casefold():
        title = text_blocks.pop(0)
    return title, "\n".join(text_blocks)


def grab_selection(platform: Any) -> str:
    """Скопировать выделенный текст (Ctrl+C) и вернуть его; прежний буфер обмена возвращается на место.

    Выполняется в потоке платформы (ctx.run_platform)."""
    old = platform.clipboard_get()
    try:
        platform.clipboard_set("")
        cleared = True
    except Exception:
        cleared = False
    text = ""
    try:
        platform.press_keys("ctrl+c")
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            time.sleep(0.05)
            text = platform.clipboard_get() or ""
            if text and (cleared or text != old):
                break
        if not cleared and text == old:
            text = ""
    finally:
        with contextlib.suppress(Exception):
            platform.clipboard_set(old)
    return text


# --- плагин --------------------------------------------------------------------------------------------


class Reader(Plugin):
    id = "reader"
    name = "Чтение вслух"
    description = "Читает статьи, книги и выделенный текст голосом ассистента и подсвечивает текущую фразу."
    icon = "reader"
    color = ("#F472B6", "#DB2777")
    fields = (ConfigField("speed", "number", "Скорость чтения", default=1.0, min=0.5, max=2.0, step=0.05),)
    actions = (
        PluginAction("read", "Читать выделенное"),
        PluginAction("pause", "Пауза"),
        PluginAction("resume", "Продолжить"),
        PluginAction("stop", "Стоп"),
    )
    examples = ("прочитай вслух", "хватит читать")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        self._task: asyncio.Task[Any] | None = None
        self._state = "idle"
        self._sentences: list[str] = []
        self._index = 0
        self._title = ""
        self._resume: asyncio.Event | None = None
        self._stop_requested = False
        self._unsubscribe: Any = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        # «стоп» / «хватит» диспетчер обрабатывает сам (глушит речь) — тогда и чтение заканчивается
        self._unsubscribe = self.ctx.bus.on("dispatch.done", self._on_dispatch)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.end()

    def _on_dispatch(self, event: Event) -> None:
        if event.data.get("kind") == "stop" and self.active:
            self._stop_requested = True
            if self._resume is not None:
                self._resume.set()

    def status(self) -> dict[str, Any]:
        total = len(self._sentences)
        return {
            "state": self._state,
            "index": self._index,
            "total": total,
            "title": self._title,
            "sentence": self._sentences[self._index] if 0 <= self._index < total else "",
            "speed": self.config.get("speed", 1.0),
        }

    # --- откуда текст ---------------------------------------------------------------------------------
    async def obtain(self, args: dict[str, Any]) -> tuple[str, str]:
        """(заголовок, текст): args.text → args.url → выделение → буфер обмена; ссылка — скачать статью."""
        text = str(args.get("text") or "").strip()
        url = str(args.get("url") or "").strip()
        if not text and not url:
            if args.get("selection", True):
                try:
                    text = (await self.ctx.run_platform(grab_selection, self.ctx.platform)).strip()
                except Exception as e:
                    log.info("чтение: выделение не скопировалось (%s)", error_text(e))
            if not text:
                text = str(await self.ctx.run_platform(self.ctx.platform.clipboard_get) or "").strip()
        if not url and _URL_RE.fullmatch(text):
            url, text = text, ""
        if url:
            return await self.fetch_article(url)
        if not text:
            raise UserFacingError("Нечего читать: выделите текст или скопируйте его (можно ссылку на статью)")
        return "", text

    async def fetch_article(self, url: str) -> tuple[str, str]:
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
        try:
            async with self.http(timeout=15.0, follow_redirects=True, headers=headers) as client:
                response = await client.get(url)
        except httpx.HTTPError as e:
            raise UserFacingError(f"Страница не открылась: {error_text(e)}") from e
        if response.status_code >= 400:
            raise UserFacingError(f"Страница не открылась (ошибка {response.status_code})")
        content_type = response.headers.get("content-type", "").lower()
        if content_type.startswith("text/plain"):
            return "", response.text[:MAX_CHARS]
        if content_type and "html" not in content_type and "xml" not in content_type:
            raise UserFacingError("По ссылке не статья, а файл — его прочитать не получится")
        title, text = await self.ctx.run_io(
            extract_article, response.content[:FETCH_LIMIT], response.charset_encoding
        )
        if not text.strip():
            raise UserFacingError("Не нашёл на странице текста статьи")
        return title, text

    # --- чтение ---------------------------------------------------------------------------------------
    async def read(self, args: dict[str, Any], *, wait_tts: bool = False) -> dict[str, Any]:
        title, text = await self.obtain(args)
        sentences = split_sentences(clean_text(text))
        if title and (not sentences or sentences[0].casefold() != title.casefold()):
            sentences.insert(0, title)
        if not sentences:
            raise UserFacingError("В тексте нечего читать")
        await self.end()
        start = int(args.get("index") or 0)
        self._sentences = sentences
        self._title = title
        self._launch(max(0, min(start, len(sentences) - 1)), wait_tts)
        return {"total": len(sentences), "title": title}

    def _launch(self, index: int, wait_tts: bool) -> None:
        self._index = index
        self._stop_requested = False
        self._resume = asyncio.Event()
        self._resume.set()
        self._state = "playing"
        self.error = ""
        self._progress("playing", first=True)
        self.changed()
        self._task = self.spawn(self._run(wait_tts), "read")

    async def end(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        self._stop_requested = True
        if self._resume is not None:
            self._resume.set()
        with contextlib.suppress(Exception):
            self.ctx.tts.silence()
        if task is not asyncio.current_task():
            await asyncio.wait([task], timeout=3.0)
            if not task.done():
                task.cancel()
                await asyncio.wait([task], timeout=1.0)
        return True

    def _progress(self, state: str, *, first: bool = False) -> None:
        total = len(self._sentences)
        index = min(self._index, total - 1) if total else 0
        data: dict[str, Any] = {
            "state": state,
            "index": index,
            "total": total,
            "sentence": self._sentences[index] if total else "",
            "title": self._title,
        }
        if first:
            data["sentences"] = list(self._sentences)
        self.publish("progress", data)

    async def _prepare(self, sentence: str, speed: float) -> tuple[Any, int, np.ndarray, float]:
        """Синтез и ускорение: (исходный звук, частота, звук на скорости speed, speed)."""
        raw, rate = await self.ctx.tts.synthesize(sentence)
        samples = np.asarray(raw, dtype=np.float32).reshape(-1)
        if abs(speed - 1.0) >= 0.01 and len(samples):
            return raw, rate, await self.ctx.run_io(time_stretch, samples, rate, speed), speed
        return raw, rate, samples, speed

    def _speed(self) -> float:
        return float(self.config.get("speed") or 1.0)

    async def _run(self, wait_tts: bool) -> None:
        sentences = self._sentences
        total = len(sentences)
        cache: dict[int, asyncio.Task[tuple[Any, int, np.ndarray, float]]] = {}
        resume = self._resume
        assert resume is not None
        final = "done"
        failures = 0
        try:
            if wait_tts:
                await self.wait_tts_idle()
            while self._index < total:
                if self._stop_requested:
                    final = "stopped"
                    break
                if not resume.is_set():
                    await resume.wait()
                    continue
                i = self._index
                task = cache.pop(i, None) or asyncio.ensure_future(self._prepare(sentences[i], self._speed()))
                for key in [k for k in cache if k != i + 1]:
                    cache.pop(key).cancel()
                if i + 1 < total and i + 1 not in cache:
                    cache[i + 1] = asyncio.ensure_future(self._prepare(sentences[i + 1], self._speed()))
                try:
                    raw, rate, samples, speed = await task
                    failures = 0
                except Exception as e:
                    failures += 1
                    log.warning("чтение: фраза %d не синтезировалась (%s)", i, error_text(e))
                    if failures >= SYNTH_FAILURES_LIMIT:
                        raise
                    if self._index == i:
                        self._index = i + 1
                    continue
                if self._stop_requested:
                    final = "stopped"
                    break
                if self._index != i or not resume.is_set():
                    cache[i] = task  # перемотали или пауза, пока готовили фразу, — готовый звук пригодится
                    continue
                if abs(speed - self._speed()) >= 0.01:
                    raw_samples = np.asarray(raw, dtype=np.float32).reshape(-1)
                    samples = await self.ctx.run_io(time_stretch, raw_samples, rate, self._speed())
                self._state = "playing"
                self._progress("playing")
                started = time.monotonic()
                await self.ctx.tts.play(samples, rate, wait=True)
                if self._stop_requested:
                    final = "stopped"
                    break
                if self._index != i or not resume.is_set():
                    cache[i] = task  # пауза или перемотка — фразу прочитать заново, без нового синтеза
                    continue
                duration = len(samples) / rate if rate else 0.0
                if duration > 1.0 and time.monotonic() - started < duration * 0.5:
                    final = "stopped"  # речь оборвали снаружи: «стоп» в оверлее, другая фраза Jarvis
                    break
                self._index = i + 1
        except asyncio.CancelledError:
            final = "stopped"
            raise
        except Exception as e:
            final = "stopped"
            log.warning("чтение остановлено: %s", error_text(e))
            self.toast("Чтение вслух", f"Не получилось прочитать: {error_text(e)}", status="error")
        finally:
            for task in cache.values():
                task.cancel()
            self._state = final
            if final == "done":
                self._index = total
            self._progress(final)
            self.changed()

    # --- действия -------------------------------------------------------------------------------------
    async def act_read(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self.read(args)

    async def act_pause(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.active or self._resume is None:
            raise UserFacingError("Сейчас ничего не читается")
        if self._resume.is_set():
            self._resume.clear()
            self._state = "paused"
            with contextlib.suppress(Exception):
                self.ctx.tts.silence()
            self._progress("paused")
            self.changed()
        return {"state": self._state}

    async def act_resume(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.active and self._resume is not None:
            if not self._resume.is_set():
                self._state = "playing"
                self._resume.set()
                self._progress("playing")
                self.changed()
            return {"state": self._state}
        if self._sentences and self._index < len(self._sentences):
            self._launch(self._index, wait_tts=False)  # после «стоп» — с того же места
            return {"state": self._state}
        raise UserFacingError("Нечего продолжать — начните чтение заново")

    async def act_toggle(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.active and self._resume is not None and self._resume.is_set():
            return await self.act_pause(args)
        return await self.act_resume(args)

    async def act_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        await self.end()
        return {"state": self._state}

    async def act_seek(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._sentences:
            raise UserFacingError("Сейчас ничего не читается")
        try:
            index = int(args.get("index", self._index))
        except (TypeError, ValueError):
            raise UserFacingError("Неверный номер фразы") from None
        index = max(0, min(index, len(self._sentences) - 1))
        if not self.active:
            self._launch(index, wait_tts=False)
            return {"index": index}
        self._index = index
        if self._resume is not None and self._resume.is_set():
            with contextlib.suppress(Exception):
                self.ctx.tts.silence()
        else:
            self._progress("paused")
        return {"index": index}

    async def act_next(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self.act_seek({"index": self._index + 1})

    async def act_prev(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self.act_seek({"index": self._index - 1})

    async def act_speed(self, args: dict[str, Any]) -> dict[str, Any]:
        value = args.get("value", args.get("speed"))
        try:
            speed = float(value)
        except (TypeError, ValueError):
            raise UserFacingError("Скорость — число от 0.5 до 2") from None
        await self.manager.update(self.id, config={"speed": speed})
        return {"speed": self._speed()}

    # --- голосовые команды ----------------------------------------------------------------------------
    def commands(self) -> list[DynamicCommand]:
        return [
            self.command(
                "read",
                "Прочитать вслух",
                [
                    "прочитай вслух",
                    "прочитай это",
                    "читай вслух",
                    "прочитай выделенное",
                    "прочитай статью",
                    "прочитай текст",
                    "озвучь текст",
                ],
                self._cmd_read,
            ),
            self.command(
                "stop",
                "Перестать читать",
                ["хватит читать", "стоп чтение", "перестань читать", "закончи чтение", "прекрати читать"],
                self._cmd_stop,
            ),
            self.command(
                "pause",
                "Пауза чтения",
                ["пауза чтения", "поставь чтение на паузу", "приостанови чтение"],
                self._cmd_pause,
            ),
            self.command(
                "resume",
                "Продолжить чтение",
                ["продолжи чтение", "продолжай читать", "читай дальше"],
                self._cmd_resume,
            ),
            self.command("faster", "Читать быстрее", ["читай быстрее", "быстрее читай"], self._cmd_faster),
            self.command(
                "slower", "Читать медленнее", ["читай медленнее", "медленнее читай"], self._cmd_slower
            ),
        ]

    async def _cmd_read(self, slots: dict[str, Any]) -> ExecResult:
        info = await self.read({}, wait_tts=True)
        message = info["title"] or f"Фраз: {info['total']}"
        return ExecResult(True, "Чтение вслух", message, icon=self.icon, say="Читаю.")

    async def _cmd_stop(self, slots: dict[str, Any]) -> ExecResult:
        stopped = await self.end()
        return ExecResult(
            True, "Чтение вслух", "Остановлено" if stopped else "Ничего не читалось", icon=self.icon
        )

    async def _cmd_pause(self, slots: dict[str, Any]) -> ExecResult:
        await self.act_pause({})
        return ExecResult(True, "Чтение вслух", "Пауза", icon=self.icon, say="Пауза.")

    async def _cmd_resume(self, slots: dict[str, Any]) -> ExecResult:
        await self.act_resume({})
        return ExecResult(True, "Чтение вслух", "Продолжаю", icon=self.icon, say="Продолжаю.")

    async def _cmd_faster(self, slots: dict[str, Any]) -> ExecResult:
        result = await self.act_speed({"value": self._speed() + SPEED_STEP})
        return ExecResult(True, "Скорость чтения", f"{result['speed']:g}×", icon=self.icon)

    async def _cmd_slower(self, slots: dict[str, Any]) -> ExecResult:
        result = await self.act_speed({"value": self._speed() - SPEED_STEP})
        return ExecResult(True, "Скорость чтения", f"{result['speed']:g}×", icon=self.icon)
