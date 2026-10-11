"""Зрение: «посмотри на экран», «что в этом окне», «экран: что тут не так?» — снимок уходит GPT
вместе с вопросом.

`match(text)` — строгое распознавание без модели (ложное срабатывание хуже промаха: «сверни окно», «заблокируй
экран», «разверни на весь экран» должны дойти до грамматики и рук как раньше). Дешёвая предпроверка подстрок
отсекает почти все команды до регулярных выражений. Вопрос — без служебных слов («посмотри на экран и …»,
«экран:», «сделай скриншот и …»); остальные формы («что у меня на экране?») уходят GPT как сказаны;
пусто — DEFAULT_QUESTION.

`encode_jpeg(shot)` — снимок pc.screen.Shot в JPEG (QImage из QtGui; импорт — внутри функции, QImage работает
и вне GUI-потока, и без QApplication). `data_url(jpeg)` — вход хода мозга. Картинка живёт только в памяти:
ни файла на диске, ни записи в журнал.
"""

import base64
import os
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pc.screen import Shot

Target = Literal["window", "screen"]

DEFAULT_QUESTION = "Что на снимке? Коротко опиши главное."
NO_WINDOW_TEXT = "Не знаю, какое окно: вызовите Jarvis хоткеем поверх нужного окна"
DISABLED_TEXT = "Зрение выключено в настройках"
TITLE_MAX = 50  # заголовок окна в строке «⚙ снимок окна «…» → GPT»

_HINTS = ("экран", "окн", "скрин", "монитор")
# невидимые символы направления текста: ими подделывают вид заголовка («exe.txt» ↔ «txt.exe»)
_BIDI = str.maketrans(
    dict.fromkeys("\u200e\u200f\u061c\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
)


@dataclass(frozen=True)
class VisionAsk:
    target: Target
    question: str


# --- распознавание ----------------------------------------------------------------------------------------

_LEAD = r"(?:(?:джарвис|jarvis|слушай|эй|ну|а|так|окей|ок|давай|пожалуйста|плиз)\s*[,!]?\s+)*"
_PLEASE = r"(?:-ка|,?\s+пожалуйста,?|,?\s+плиз,?)?"
_CAN = r"(?:(?:ты\s+)?(?:можешь|можете|сможешь|сможете)\s+)?"
_FILL = r"(?:\s+(?:у меня|сейчас|тут|там|здесь|пожалуйста|плиз))*"
# слово, с которого после «посмотри на экран» без запятой начинается вопрос
_QW = (
    r"(?:что|чего|как|почему|зачем|где|когда|какой|какая|какое|какие|какую|каким|кто|сколько|чем|можно|нужно"
    r"|надо|есть|скажи|подскажи|объясни|переведи|прочитай|прочти|опиши|найди|помоги|исправь|сравни|посчитай"
    r"|расскажи|напиши|ответь|реши|выпиши|перечисли|правильно|верно|нормально)"
)
# конец фразы: ничего, знаки препинания, «, вопрос», «и вопрос» или вопрос с вопросительного слова
_TAIL = (
    rf"{_FILL}(?:\s*[?!.…]*"
    rf"|(?:\s*[,;:—–?!.-]+\s*|\s+и\s+|\s+(?={_QW}(?:[\s?!.,]|$)))(?P<q>\S.*))"
)
_SCR = r"(?:экран|монитор)"
_SCR_P = r"(?:экране|мониторе)"
_WIN_ADJ = r"(?:это|текущее|активное|открытое|мое|верхнее)"
_WIN_ADJ_P = r"(?:этом|текущем|активном|открытом|моем|верхнем)"
# «на экране», «в этом окне» — где смотреть
_WHERE = rf"(?P<scr>на\s+(?:моем\s+|этом\s+)?{_SCR_P})|(?P<win>в\s+(?:{_WIN_ADJ_P}\s+)?окне)"
_LOOK_VERB = (
    r"(?:посмотри|посмотрите|посмотреть|смотри|глянь|гляньте|глянуть|взгляни|взгляните|взглянуть"
    r"|погляди|поглядите|поглядеть)"
)
_WHAT_FILL = (
    r"(?:(?:у меня|у тебя|тут|там|здесь|сейчас|ты|вообще|такое|это"
    r"|за\s+(?:ошибка|ошибку|сообщение|окно|надпись|текст|табличка|уведомление|программа|приложение)"
    r"|не так|написано|показано|показывает|происходит|творится|открыто|видишь|видно|случилось"
    r"|изображено)\s+)*"
)
_LANG = (
    r"(?:на\s+(?:русский|английский|немецкий|французский|испанский|итальянский|китайский|японский|украинский)"
    r"|по-русски|по-английски)"
)
_OBJ = (
    r"(?:(?:этот|эту|это|весь|всю|все|мне)\s+)*"
    r"(?:текст|ошибку|ошибки|сообщение|сообщения|уведомление|надпись|надписи|код|таблицу|график|диаграмму"
    r"|письмо|страницу|документ|окно ошибки|что\s+(?:тут\s+|там\s+)?(?:написано|происходит|видно|показано"
    r"|изображено)|что)"
)
_READ_VERB = (
    r"(?:прочитай|прочитайте|прочти|прочтите|прочитать|прочесть|зачитай|переведи|переведите|перевести|опиши"
    r"|опишите|описать|объясни|объясните|объяснить|расшифруй|расшифровать|перескажи|пересказать|проанализируй"
    r"|проанализировать)"
)
_READ_WHERE = (
    rf"(?P<scr>на\s+(?:моем\s+|этом\s+)?{_SCR_P}|с\s+экрана)"
    rf"|(?P<win>в\s+(?:{_WIN_ADJ_P}\s+)?окне|из\s+(?:этого\s+|текущего\s+|активного\s+)?окна)"
)
_BARE = rf"(?P<scr2>(?:этот\s+|мой\s+)?{_SCR})|(?P<win2>(?:{_WIN_ADJ}\s+)?окно)"

# (регулярное выражение, откуда вопрос): "q" — группа q (пусто — DEFAULT_QUESTION), "text" — вся фраза
_RULES: list[tuple[str, str]] = [
    # «экран: что тут не так?», «окно: переведи»
    (r"(?P<pfx>экран|скрин|скриншот|окно)\s*:\s*(?P<q>.*)", "q"),
    # «посмотри на экран (и скажи …)», «глянь это окно», «можешь посмотреть на экран?»
    (
        rf"{_LEAD}{_CAN}{_LOOK_VERB}{_PLEASE}\s+"
        rf"(?:(?P<scr>(?:на\s+)?(?:мой\s+|этот\s+|весь\s+)?{_SCR})"
        rf"|(?P<win>(?:на\s+)?(?:{_WIN_ADJ}\s+)?окно|в\s+{_WIN_ADJ}\s+окно)){_TAIL}",
        "q",
    ),
    # «посмотри, что у меня на экране», «глянь что в окне не так»
    (rf"{_LEAD}{_CAN}{_LOOK_VERB}{_PLEASE},?\s+(?:что|чего)\s+{_WHAT_FILL}(?:{_WHERE}){_TAIL}", "text"),
    # «что у меня на экране?», «что в этом окне», «что за ошибка на экране», «что ты видишь на экране»
    (rf"{_LEAD}(?:что|чего)\s+{_WHAT_FILL}(?:{_WHERE}){_TAIL}", "text"),
    # «какая ошибка на экране», «какой текст в этом окне»
    (
        rf"{_LEAD}(?:какая|какое|какой|какие)\s+(?:(?:у меня|тут|там|здесь|сейчас)\s+)*"
        r"(?:ошибка|ошибки|сообщение|текст|надпись|цифра|число|сумма|программа|приложение|страница|сайт)"
        rf"(?:\s+(?:у меня|тут|там|здесь|сейчас))*\s+(?:{_WHERE}){_TAIL}",
        "text",
    ),
    # «прочитай ошибку в окне», «переведи текст на экране на английский», «опиши экран»
    (
        rf"{_LEAD}{_CAN}{_READ_VERB}{_PLEASE}(?:\s+мне)?(?:\s+{_LANG})?"
        rf"(?:(?:,?\s+{_OBJ})?,?\s+(?:{_READ_WHERE})|,?\s+(?:{_BARE}))(?:\s+{_LANG})?{_TAIL}",
        "text",
    ),
    # «сделай скриншот и скажи, что не так», «сделай скрин окна и переведи»
    (
        rf"{_LEAD}(?:сделай|сделайте|сними|снимите|возьми)\s+(?:скриншот|скрин|скриншотик|снимок)"
        r"(?:\s+(?:(?P<scr>экрана|монитора)|(?P<win>(?:этого\s+|текущего\s+|активного\s+)?окна)))?"
        r"(?:\s*,\s*|\s+)и\s+(?P<q>\S.*)",
        "q",
    ),
    # «на экране ошибка, что делать?», «у меня в окне какое-то сообщение»
    (
        rf"{_LEAD}(?:у меня\s+)?(?:{_WHERE})\s+"
        r"(?:(?:какая-то|какое-то|какой-то|какие-то|непонятная|непонятное|странная|странное|красная)\s+)?"
        r"(?:ошибка|ошибки|сообщение|надпись|табличка|уведомление|предупреждение)(?:[\s,.!?:—-].*)?",
        "text",
    ),
    # «ты видишь мой экран?», «видишь ошибку на экране?»
    (
        rf"{_LEAD}(?:ты\s+)?(?:видишь|видно)\s+(?:ли\s+)?(?:(?P<scr2>(?:мой\s+|этот\s+)?{_SCR})"
        rf"|(?:(?:что|ошибку|текст|сообщение|надпись)\s+)?(?:{_WHERE})){_TAIL}",
        "text",
    ),
    # «помоги с ошибкой на экране»
    (
        rf"{_LEAD}помоги(?:те)?(?:\s+мне)?(?:\s+(?:разобраться|понять|исправить))?\s+"
        r"(?:с\s+(?:этой\s+)?(?:ошибкой|ошибками|сообщением)|(?:с\s+)?(?:тем,?\s+)?что\s+(?:тут\s+)?"
        rf"(?:написано|происходит))\s+(?:{_WHERE}){_TAIL}",
        "text",
    ),
]
_COMPILED = [(re.compile(rx), src) for rx, src in _RULES]


def _normalize(text: str) -> tuple[str, str]:
    """(строка для сопоставления: нижний регистр, ё → е, одиночные пробелы; исходная с теми же позициями)."""
    orig = " ".join(text.split())
    low = orig.lower().replace("ё", "е")
    if len(low) != len(orig):  # редкие символы меняют длину при lower() — без исходного регистра
        orig = low
    return low, orig


def _target(m: re.Match[str]) -> Target:
    groups = m.groupdict()
    if groups.get("pfx") is not None:
        return "window" if groups["pfx"] == "окно" else "screen"
    return "window" if groups.get("win") is not None or groups.get("win2") is not None else "screen"


def _clean_question(q: str) -> str:
    """Вопрос без хвостовых «пожалуйста» и одиночных знаков; без букв и цифр — пусто."""
    q = q.strip(" ,;:—–-")
    q = re.sub(r"(?i)^(?:пожалуйста|плиз)[\s,!.]*|[\s,]+(?:пожалуйста|плиз)[\s!.]*$", "", q).strip(" ,;:—–-")
    return q if any(ch.isalnum() for ch in q) else ""


def match(text: str) -> VisionAsk | None:
    """«Посмотри на экран …» → VisionAsk(target, question); не зрение — None. Без модели, микросекунды."""
    if not text:
        return None
    low_raw = text.lower()
    if not any(h in low_raw for h in _HINTS):  # горячий путь грамматики: обычно сразу выход
        return None
    low, orig = _normalize(text)
    for rx, source in _COMPILED:
        m = rx.fullmatch(low)
        if m is None:
            continue
        if source == "q":
            start, end = m.span("q")
            question = _clean_question(orig[start:end]) if start >= 0 else ""
        else:
            question = orig.strip()
        return VisionAsk(_target(m), question or DEFAULT_QUESTION)
    return None


# --- строка состояния --------------------------------------------------------------------------------------


def status_label(target: Target, window: Any = None) -> str:
    """«⚙ снимок окна «Заголовок» → GPT» / «⚙ снимок экрана → GPT». Заголовок — недоверенный текст: одной
    строкой, без управляющих символов и знаков направления текста, не длиннее TITLE_MAX; окно показывает его
    только как обычный текст."""
    if target != "window":
        return "⚙ снимок экрана → GPT"
    from pc.windows import clean_text

    raw = str(getattr(window, "title", "") or "") or str(getattr(window, "exe", "") or "")
    text = clean_text(raw).translate(_BIDI)
    visible = "".join(" " if ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 else c for c in text)
    title = " ".join(visible.split())
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"
    return f"⚙ снимок окна «{title}» → GPT" if title else "⚙ снимок окна → GPT"


# --- JPEG ------------------------------------------------------------------------------------------------


def _qt_plugins() -> None:
    """В собранном jarvis-cli.exe нет QT_PLUGIN_PATH (его ставят только run и selftest): плагин qjpeg ищем
    в папке бандла. В Jarvis.exe и в разработке Qt находит плагины сам."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not meipass:
        return
    from PySide6.QtCore import QCoreApplication

    if QCoreApplication.instance() is not None:
        return
    sub = ("PySide6", "plugins") if sys.platform == "win32" else ("PySide6", "Qt", "plugins")
    path = os.path.join(meipass, *sub)
    if path not in QCoreApplication.libraryPaths():
        QCoreApplication.addLibraryPath(path)


def encode_jpeg(shot: "Shot", max_side: int = 1600, quality: int = 80) -> bytes:
    """Снимок → JPEG: длинная сторона не больше max_side (SmoothTransformation; меньший снимок не
    увеличивается), качество quality. Ошибка — ValueError (битый снимок) или RuntimeError (Qt не сохранил)."""
    width, height = int(shot.width), int(shot.height)
    if width <= 0 or height <= 0 or len(shot.bgra) < width * height * 4:
        raise ValueError(f"снимок битый: {width}x{height}, {len(shot.bgra)} байт")
    _qt_plugins()
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage

    data = shot.bgra
    # Format_RGB32 = 0xffRRGGBB, в памяти — B, G, R, X: как у DIB-секции. Альфа GDI (обычно 0) не учитывается;
    # с Format_ARGB32 сглаживание считало бы точки прозрачными и снимок вышел бы чёрным
    image = QImage(data, width, height, width * 4, QImage.Format.Format_RGB32)
    if max(width, height) > max_side:
        image = image.scaled(
            max_side,
            max_side,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    out = QByteArray()
    buf = QBuffer(out)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        saved = image.save(buf, "JPEG", int(quality))
    finally:
        buf.close()
    del image  # QImage держит указатель на data — до этой строки data жива
    if not saved or out.size() == 0:
        raise RuntimeError("Qt не сохранил JPEG (нет плагина qjpeg?)")
    return bytes(out.data())


def data_url(jpeg: bytes) -> str:
    """data:image/jpeg;base64,… — вход хода мозга (openai_codex.ImageInput)."""
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
