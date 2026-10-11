"""Грамматика (уровень 0): ~30 частых команд разбираются правилами, без модели.

`match(text, ctx) -> GrammarHit | None`; action и args — из «Словаря инструментов» (docs/architecture.md):
open{target, kind}, close{target}, focus{target}, win{action, target?}, find{query, kind},
vol{set|delta|mute}, media{action}, open_found{index}, lock{}, clock{what}; только у грамматики (без модели,
не действия ПК, кроме undo): undo{}, remind{text, due}, reminders{}, cancel_reminder{which?},
remember{text}, forget{which}, memory{}, help{}.

Главный принцип: ложное срабатывание хуже промаха. Всё, в чём нет уверенности, — None, и команду разбирают
руки: приложение ниже порога apps.resolve, «его» без цели, номер вне результатов find, вопрос, несколько
шагов, лишние слова. Сопоставление — fullmatch по нормализованной строке (нижний регистр, ё → е, без
пунктуации и вежливых слов); свободный текст (запрос find) берётся из исходной строки с теми же позициями —
с регистром и «ё»; текст напоминания и заметки памяти — из исходной строки вместе со знаками препинания.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jarvis import reminders
from jarvis.context import CUR, Context
from pc import apps


@dataclass(frozen=True)
class GrammarHit:
    action: str
    args: dict[str, Any]
    confidence: float = 1.0


# --- нормализация --------------------------------------------------------------------------------------

# точка и двоеточие — только в конце слова (в «отчёт.docx» точка остаётся); запятая — кроме «1,5»; тире
# между словами
_PUNCT = re.compile(r"[.:](?=\s|$)|(?<!\d),|,(?!\d)|[!?;…\"«»“”„()\[\]]|\s[-–—]+(?=\s)")
_POLITE = re.compile(
    r"(?<!\S)(?:пожалуйста|пожалуста|плиз|пжлст|пж|джарвис|jarvis)(?!\S)|(?<=[а-я])-ка(?!\S)"
)
_LEAD = re.compile(r"^(?:(?:ну|давай|слушай|эй|так|ок|окей|а|теперь)\s+)+")


def _cut(low: str, orig: str, rx: re.Pattern[str]) -> tuple[str, str]:
    """Вырезать совпадения rx (найденные в low) из обеих строк — позиции символов остаются общими."""
    spans = [m.span() for m in rx.finditer(low)]
    if not spans:
        return low, orig
    parts_low, parts_orig, pos = [], [], 0
    for a, b in spans:
        parts_low.append(low[pos:a])
        parts_orig.append(orig[pos:a])
        pos = b
    parts_low.append(low[pos:])
    parts_orig.append(orig[pos:])
    low, orig = " ".join("".join(parts_low).split()), " ".join("".join(parts_orig).split())
    return low, orig


def _normalize(text: str) -> tuple[str, str]:
    """(строка для сопоставления, исходная строка с теми же позициями символов)."""
    orig = " ".join(_PUNCT.sub(" ", text).split())
    low = orig.lower().replace("ё", "е")
    if len(low) != len(orig):  # редкие символы меняют длину при lower() — тогда без исходного регистра
        orig = low
    low, orig = _cut(low, orig, _POLITE)
    low, orig = _cut(low, orig, _LEAD)
    return low, orig


# --- словари --------------------------------------------------------------------------------------------

_UNITS = {
    "ноль": 0, "один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
}  # fmt: skip
_TEENS = {
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
}  # fmt: skip
_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
}  # fmt: skip
_WORD_NUM = {**_UNITS, **_TEENS, **_TENS, "сто": 100, "половину": 50}


def _alt(words: Any) -> str:
    """Альтернатива для regex: длинные варианты раньше коротких."""
    return "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))


_NUM = (
    rf"(?:\d{{1,3}}|(?:{_alt(_TENS)})(?: (?:{_alt(k for k in _UNITS if k != 'ноль')}))?"
    rf"|{_alt(_TEENS)}|{_alt(_UNITS)}|сто|половину)"
)
_PCT = r"(?: ?%| процент(?:ов|а)?)?"


def _num(s: str) -> int:
    s = s.strip()
    return int(s) if s.isdigit() else sum(_WORD_NUM[w] for w in s.split())


_ORD_STEMS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6, "седьм": 7,
    "восьм": 8, "девят": 9, "десят": 10, "последн": -1,
}  # fmt: skip
_ORD = (
    rf"(?:(?P<stem>{_alt(_ORD_STEMS)})(?:ый|ой|ий|ую|юю|ое|ее|ье|ью|ая|яя)"
    r"|(?P<n>\d{1,2})(?:-?(?:й|ый|ой|ий|ая|ое|ую))?)"
)


def _words(s: str) -> frozenset[str]:
    return frozenset(s.split())


# известные папки: как сказано → имя для files.known_folder
_FOLDERS = {
    "загрузки": "загрузки", "загрузок": "загрузки", "закачки": "загрузки",
    "документы": "документы", "документов": "документы", "мои документы": "документы",
    "рабочий стол": "рабочий стол", "рабочего стола": "рабочий стол",
    "изображения": "изображения", "изображений": "изображения", "картинки": "изображения",
    "картинок": "изображения", "картинками": "изображения",
    "видео": "видео",
    "музыку": "музыка", "музыка": "музыка", "музыки": "музыка", "музыкой": "музыка",
}  # fmt: skip

# слова, с которыми «X» — не имя приложения: служебные, местоимения, «процесс» (это kill), несколько шагов
_STOP = _words(
    """и или а но потом затем после в во на с со из по для от до за под над про через без к ко у о об при
    не ни то что чтобы который которая которое которые которую это этот эта эту его ее их него нее них
    мой моя мою мое мои свой свою свое все весь всю вчера сегодня завтра сейчас недавно последний последнюю
    последнее новый новую новое процесс процессы процесса задачу вкладку вкладки вкладка сайт страницу ссылку
    файл файлы файлик папку папки документ игру как где когда зачем почему какой какую какое сколько кто чем
    там тут здесь туда сюда что-нибудь что-то нибудь"""
)
# одно слово, которое легко спутать с частью имени приложения («музыку» ~ «Яндекс Музыка»)
_NOT_APPS = _words(
    """музыку музыка песню трек видео звук свет интернет браузер компьютер комп пк экран систему окно окна
    программу приложение прогу игру"""
)
# что можно сказать вместо «найди файл X» — без этих слов в запросе
_FIND_STOP = _words(
    """и который которую которые которое что вчера сегодня позавчера недавно последний последние последнюю
    новый новые самый самые все всё открой удали скинь отправь"""
)

# --- глаголы (с типичными опечатками) ---------------------------------------------------------------

_OPEN = (
    r"(?:открой|откройте|открои|отрой|откой|окрой|открыть|запусти|запустите|запустм|зпусти|запустить"
    r"|включи|вкючи)"
)
_OPEN_DIR = r"(?:открой|откройте|открои|отрой|откой|окрой|открыть|перейди в|зайди в)"
_CLOSE = r"(?:закрой|закройте|закрои|зкрой|закой|закрыть)"
_MIN = r"(?:сверни|сверните|свени|свирни|свернуть)"
_MAX = r"(?:разверни|розверни|развени|развернуть|раскрой)"
_REST = r"(?:восстанови|востанови|восстановить)"
_FOCUS = (
    r"(?:переключись на|переключи на|перключись на|переключится на|переключиться на|перейди в|перейди на"
    r"|перейти в|покажи|вернись в|вернись к|вернись на)"
)
_FIND = r"(?:найди|найти|наиди|поищи|отыщи|ищи)"
_LOCK = r"(?:заблокируй|заблокировать|заблакируй|заблокирую|заблокируи|блокируй)"
_VOLW = r"(?:громкость|громкасть|грмокость|громкост|громкоть|звук)"
_CURW = r"(?:его|ее|это|окно|это окно|текущее окно|активное окно|эту программу|это приложение|эту прогу)"
_FULL = r"(?: (?:на|во) (?:весь|полный) экран)"
_TRACK = r"(?: (?:трек|трэк|песн[яюи]|композици[яю]|видео))"
_UNDO = r"(?:отмени|отмените|отменить|отмена|откати|откатить)"
_REMIND = r"(?:напомни|напомните|нопомни|напомини|напомнить)"
_CANCEL = r"(?:отмени|отмените|отменить|удали|удалите|удалить|убери|сотри)"
_REMEMBER = r"(?:запомни|запомните|запомнить|зaпомни|зпомни)"
_FORGET = r"(?:забудь|забудьте|забыть|забуть)"
_NEW_REMINDER = r"(?:поставь|создай|сделай|добавь|заведи|установи) напоминание"
# «запомни это», «забудь его» — непонятно, о чём: пусть разбираются руки
_VAGUE = _words("это его ее то вот этот эту эти тот")
# заметка памяти — не команда в несколько шагов («запомни текст и переведи его»)
_NOT_NOTE = _words("переведи напиши объясни посчитай придумай составь открой закрой найди удали")
# «длинные» команды со свободным текстом: им можно больше MAX_LEN символов
_LONG_OK = re.compile(r"^\W*(?:(?:джарвис|jarvis)\W+)?(?:запомни|напомни|поставь напоминание)", re.IGNORECASE)
_DAY_START = re.compile(r"^(?:сегодня|завтра|послезавтра)(?:\s|$)")
_DAY_END = re.compile(r"(?:^|\s)(?:сегодня|завтра|послезавтра)$")
_QUESTION_WORD = re.compile(r"^(?:что|как|кто|где|когда|какой|какая|какое|какие|сколько|почему|зачем|чем)\s")
MAX_LEN = 120
LONG_LEN = 300

# --- обработчики ---------------------------------------------------------------------------------------

Handler = Callable[[re.Match[str], str, Context], GrammarHit | None]


def _cur(ctx: Context) -> bool:
    """«его/это/окно» понятны, только если есть на что указать."""
    return ctx.cur_target() is not None


_ENDINGS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("ую", "ая"), ("юю", "яя"), ("у", "а"), ("ю", "я")),  # винительный: телегу → телега
    (("ой", "ая"), ("ы", "а"), ("и", "а")),  # родительный ж.р.: телеги, командной строки
    (("а", ""),),  # родительный м.р.: хрома → хром
    (("ом", ""), ("ем", ""), ("е", "")),  # творительный/предложный м.р.: хроме → хром
    (("ой", "а"), ("е", "а")),  # творительный/предложный ж.р.: телеге → телега
)
_CYR = re.compile(r"[а-я]{3,}")


def _deinflect(x: str, rules: tuple[tuple[str, str], ...]) -> str:
    out = []
    for w in x.split():
        if _CYR.fullmatch(w):
            for end, repl in rules:
                if w.endswith(end) and len(w) - len(end) >= 2:
                    w = w[: -len(end)] + repl
                    break
        out.append(w)
    return " ".join(out)


def _app(x: str) -> tuple[str, float] | None:
    """Имя приложения из инвентаря, если X разрешается уверенно; иначе None."""
    words = x.split()
    if not 1 <= len(words) <= 4 or len(x) > 40 or x in _NOT_APPS or any(w in _STOP for w in words):
        return None
    seen: set[str] = set()
    for cand in (x, *(_deinflect(x, r) for r in _ENDINGS)):
        if cand in seen:
            continue
        seen.add(cand)
        app, score = apps.lookup(cand)  # без обновления инвентаря: промах грамматики — дело рук
        if app is not None and score >= apps.THRESHOLD:
            return app.name, float(score)
    return None


def _clock(what: str) -> Handler:
    return lambda m, orig, ctx: GrammarHit("clock", {"what": what})


def _lock(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    return GrammarHit("lock", {})


def _vol_set(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    n = _num(m["n"])
    return GrammarHit("vol", {"set": n}) if 0 <= n <= 100 else None


def _vol_extreme(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    return GrammarHit("vol", {"set": 0 if m["ext"].startswith("мин") else 100})


def _vol_delta(sign: int) -> Handler:
    def handler(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
        n = _num(m["n"]) if m["n"] else 10
        return GrammarHit("vol", {"delta": sign * n}) if 1 <= n <= 100 else None

    return handler


def _mute(on: bool) -> Handler:
    return lambda m, orig, ctx: GrammarHit("vol", {"mute": on})


def _media(action: str) -> Handler:
    return lambda m, orig, ctx: GrammarHit("media", {"action": action})


def _folder(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    return GrammarHit("open", {"target": _FOLDERS[m["f"]], "kind": "folder"})


def _win_all(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    return GrammarHit("win", {"action": "minimize_all"})


def _win_cur(action: str) -> Handler:
    def handler(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
        return GrammarHit("win", {"action": action, "target": CUR}) if _cur(ctx) else None

    return handler


def _win_app(action: str) -> Handler:
    def handler(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
        found = _app(m["x"])
        return GrammarHit("win", {"action": action, "target": found[0]}, found[1] / 100) if found else None

    return handler


def _close_cur(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    return GrammarHit("close", {"target": CUR}) if _cur(ctx) else None


def _close_app(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    found = _app(m["x"])
    return GrammarHit("close", {"target": found[0]}, found[1] / 100) if found else None


def _open_app(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    found = _app(m["x"])
    return GrammarHit("open", {"target": found[0], "kind": "app"}, found[1] / 100) if found else None


def _focus_app(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    found = _app(m["x"])
    return GrammarHit("focus", {"target": found[0]}, found[1] / 100) if found else None


def _open_found(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    items = ctx.found_items()
    index = _ORD_STEMS[m["stem"]] if m["stem"] else int(m["n"])
    if not items or index == 0 or index > len(items):
        return None
    return GrammarHit("open_found", {"index": index})


def _find(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    query = orig[m.start("q") : m.end("q")].strip("'` ")
    words = m["q"].split()
    if len(query) < 2 or len(words) > 5 or any(w in _FIND_STOP for w in words):
        return None
    kind = "folder" if m["k"].startswith(("пап", "катал")) else "file"
    return GrammarHit("find", {"query": query, "kind": kind})


# --- отмена, напоминания, память, помощь (не действия ПК, кроме undo) ------------------------------------


def _simple(action: str, **args: Any) -> Handler:
    return lambda m, orig, ctx: GrammarHit(action, dict(args))


def _span(m: re.Match[str], orig: str, group: str) -> str:
    return orig[m.start(group) : m.end(group)].strip()


def _day_only(rest: str) -> bool:
    """«напомни завтра позвонить маме» — день без времени: спросить время. «напомни, что я делаю завтра» —
    вопрос, а не напоминание."""
    return bool(_DAY_START.match(rest)) or (bool(_DAY_END.search(rest)) and not _QUESTION_WORD.match(rest))


def _remind_hit(m: re.Match[str], orig: str, duration_ok: bool) -> GrammarHit | None:
    rest = _span(m, orig, "rest")
    parsed = reminders.parse_when(rest)
    if parsed is None and duration_ok:  # «поставь напоминание на 10 минут …»
        duration = reminders.parse_duration(rest)
        if duration is not None:
            parsed = (time.time() + duration[0], duration[2])
    if parsed is not None:
        return GrammarHit("remind", {"text": parsed[1], "due": parsed[0]})
    if _day_only(m["rest"]):
        return GrammarHit("remind", {"text": rest, "due": None})  # execute спросит время
    return None


def _remind(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    """«напомни через 10 минут вынуть пиццу», «напомни вынуть пиццу в 18:30»; без времени — None
    («напомни, как зовут актёра» — вопрос для GPT)."""
    return _remind_hit(m, orig, duration_ok=False)


def _new_reminder(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    """«поставь напоминание на 18:30 …», «создай напоминание на 10 минут …»."""
    return _remind_hit(m, orig, duration_ok=True)


def _timer(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    """«поставь таймер на 10 минут», «засеки полчаса»: напоминание «таймер на …»."""
    duration = reminders.parse_duration(_span(m, orig, "rest"))
    if duration is None:
        return None
    seconds, said, label = duration
    text = f"таймер на {said}" + (f": {label}" if label else "")
    return GrammarHit("remind", {"text": text, "due": time.time() + seconds})


def _vague(text: str) -> bool:
    return len(text) < 2 or all(w in _VAGUE for w in text.split())


def _selector(m: re.Match[str], orig: str) -> int | str | None:
    """Что выбрано в шаблоне: номер (с 1), «последнее», «все» или часть текста; None — ничего не сказано;
    "" — сказано непонятно («забудь это», «отмени нулевое напоминание»)."""
    g = m.groupdict()
    if g.get("all"):
        return "все"
    if g.get("stem") or g.get("n"):
        index = _ORD_STEMS[g["stem"]] if g.get("stem") else int(g["n"])
        return "последнее" if index == -1 else (index or "")
    if g.get("num"):
        return int(g["num"]) or ""
    if g.get("last"):
        return "последнее"
    if g.get("q"):
        if _vague(g["q"]):
            return ""
        return "последнее" if g["q"].startswith("последн") else _span(m, orig, "q")
    return None


def _cancel_reminder(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    which = _selector(m, orig)
    if which == "":
        return None
    return GrammarHit("cancel_reminder", {} if which is None else {"which": which})


def _remember(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    if _vague(m["x"]) or any(w in _NOT_NOTE for w in m["x"].split()):
        return None
    return GrammarHit("remember", {"text": _span(m, orig, "x")})


def _forget(m: re.Match[str], orig: str, ctx: Context) -> GrammarHit | None:
    which = _selector(m, orig)
    return None if which is None or which == "" else GrammarHit("forget", {"which": which})


def _raw_span(raw: str, words: str) -> str:
    """Тот же текст в исходной строке — со знаками препинания («хлеб, молоко»); не нашёлся — как есть."""
    parts = words.split()
    if not parts:
        return words
    rx = re.compile(r"(?<!\w)" + r"[\W_]+".join(re.escape(p) for p in parts) + r"(?!\w)", re.IGNORECASE)
    found = rx.search(raw)
    return found.group(0) if found else words


# --- шаблоны: порядок важен (частное раньше общего); совпал шаблон — его ответ окончательный -------------

_RULES: list[tuple[str, Handler]] = [
    # время и дата — единственные вопросы, на которые грамматика отвечает сама
    (
        r"(?:который (?:сейчас |щас )?час(?: сейчас)?|сколько (?:сейчас |щас )?(?:времени|время)(?: сейчас)?"
        r"|какое (?:сейчас |щас )?время|(?:скажи|подскажи|покажи) (?:время|который час|сколько времени)"
        r"|сколько на часах|время)",
        _clock("time"),
    ),
    (
        r"(?:какое (?:сегодня |сейчас )?число(?: сегодня)?|какой (?:сегодня |сейчас )?день(?: недели)?"
        r"(?: сегодня)?|какая (?:сегодня |сейчас )?дата(?: сегодня)?|сегодня какое число|сегодня какой день"
        r"(?: недели)?|(?:скажи|подскажи) (?:дату|число|какое сегодня число))",
        _clock("date"),
    ),
    # помощь — локальный ответ без модели
    (
        r"(?:что|чего) (?:ты )?(?:вообще |еще )?(?:умеешь|можешь)(?: делать| сделать)?"
        r"|(?:расскажи|покажи|скажи) что ты (?:умеешь|можешь)(?: делать)?|помощь|справка|help|хелп|помоги"
        r"|(?:(?:какие|покажи) )?(?:у тебя )?(?:есть )?команды|список команд"
        r"|как (?:тобой|с тобой) пользоваться|как с тобой работать",
        _simple("help"),
    ),
    # память: что помнишь
    (
        r"что ты (?:обо мне |про меня )?(?:помнишь|запомнил)(?: обо мне| про меня)?"
        r"|что ты (?:обо мне|про меня) знаешь|что ты знаешь (?:обо мне|про меня)"
        r"|(?:покажи )?(?:свою |твою )?память|что я (?:тебе )?(?:говорил|просил) запомнить",
        _simple("memory"),
    ),
    # напоминания: список, отмена, новое, таймер
    (
        r"(?:(?:какие|покажи)(?: мне)?(?: у меня)?(?: есть)?(?: мои| все)? напоминания"
        r"|(?:мои|все) напоминания|напоминания|есть (?:ли )?(?:у меня )?напоминания"
        r"|список(?: моих| всех)? напоминаний"
        r"|(?:о )?ч(?:ем|то) ты (?:мне )?напомнишь)",
        _simple("reminders"),
    ),
    (rf"{_CANCEL} (?P<all>все) напоминания", _cancel_reminder),
    (rf"{_CANCEL} {_ORD} напоминание", _cancel_reminder),
    (rf"{_CANCEL} напоминание(?: номер| под номером| №)? (?P<num>\d{{1,2}})", _cancel_reminder),
    (rf"{_CANCEL} напоминание(?: (?:про|о|об|что|насчет|с текстом))? (?P<q>.+)", _cancel_reminder),
    (rf"{_CANCEL} напоминание", _cancel_reminder),
    (r"не напоминай(?: мне)?(?: (?:про|о|об|что))? (?P<q>.+)", _cancel_reminder),
    (rf"{_REMIND}(?: (?:мне|нам))? (?P<rest>.+)", _remind),
    (rf"{_NEW_REMINDER} (?P<rest>.+)", _new_reminder),
    (r"(?:(?:поставь|заведи|запусти|включи|установи) )?таймер (?P<rest>.+)", _timer),
    (r"засеки (?P<rest>.+)", _timer),
    # память: запомнить, забыть
    (rf"{_REMEMBER}(?: (?:что|то что|себе|себе что))? (?P<x>.+)", _remember),
    (
        rf"{_FORGET} (?:все(?: обо мне| про меня| что (?:знаешь|помнишь)(?: обо мне)?)?|обо мне|про меня)"
        r"|(?:очисти|сотри) (?:свою |твою )?память",
        _simple("forget", which="все"),
    ),
    (rf"{_FORGET}(?: запись)?(?: номер| под номером| №)? (?P<num>\d{{1,2}})", _forget),
    (rf"{_FORGET} (?P<last>последн(?:ее|юю|ий|ую))(?: запись)?", _forget),
    (rf"{_FORGET}(?: все)?(?: (?:про|о|об|что|то что|насчет))? (?P<q>.+)", _forget),
    (rf"{_LOCK}(?: (?:мой |этот )?(?:компьютер|комп|пк|экран|систему|винду|windows|ноутбук|ноут))?", _lock),
    # громкость
    (rf"(?:(?:поставь|сделай|установи|выстави) )?{_VOLW}(?: на| в)? (?P<n>{_NUM}){_PCT}", _vol_set),
    (
        rf"(?:(?:поставь|сделай|установи) )?(?:{_VOLW} )?на (?P<n>{_NUM}) процент(?:ов|а)?"
        r"(?: громкости| звука)?",
        _vol_set,
    ),
    (rf"(?:(?:поставь|сделай|выкрути) )?{_VOLW} на (?P<ext>максимум|полную|минимум)", _vol_extreme),
    (
        r"(?:(?:сделай|сделать|еще) )?(?:(?:чуть|немного|немножко|чуть-чуть|капельку|слегка) )?"
        rf"(?:по)?(?:громче|гормче|грмче)(?: (?:звук|музыку))?(?: на (?P<n>{_NUM}){_PCT})?",
        _vol_delta(+1),
    ),
    (
        rf"(?:прибавь|прибавить|увеличь|подними|добавь|повысь) (?:{_VOLW}|громкости|звука)"
        rf"(?: на (?P<n>{_NUM}){_PCT})?",
        _vol_delta(+1),
    ),
    (
        r"(?:(?:сделай|сделать|еще) )?(?:(?:чуть|немного|немножко|чуть-чуть|капельку|слегка) )?"
        rf"(?:по)?(?:тише|тишэ)(?: (?:звук|музыку))?(?: на (?P<n>{_NUM}){_PCT})?",
        _vol_delta(-1),
    ),
    (
        rf"(?:убавь|убавить|уменьши|понизь|снизь|опусти) (?:{_VOLW}|громкости|звука)"
        rf"(?: на (?P<n>{_NUM}){_PCT})?",
        _vol_delta(-1),
    ),
    (
        r"(?:(?:выключи|отключи|убери) (?:весь )?(?:звук|громкость)|без звука|звук (?:выключи|выкл|отключи))",
        _mute(True),
    ),
    (r"(?:(?:включи|верни) (?:обратно )?звук(?: обратно)?|звук (?:включи|вкл|верни))", _mute(False)),
    # медиа
    (
        r"(?:пауза|паузу|на паузу|поставь (?:на )?паузу|поставь (?:музыку|трек|песню|видео) на паузу"
        r"|стоп музыка|музыку стоп|останови (?:музыку|песню|трек|видео|воспроизведение))",
        _media("play_pause"),
    ),
    (
        r"(?:продолжи|продолжай|продолжить|воспроизведи|играй|сними (?:с )?паузу|сними с паузы"
        r"|продолжи (?:музыку|воспроизведение|играть))",
        _media("play_pause"),
    ),
    (
        r"(?:(?:включи|поставь|давай|переключи на) )?след(?:ую|у|уй)щ(?:ий|ая|ую|ее|ии)"
        rf"{_TRACK}?|дальше|переключи (?:трек|песню)|пропусти(?: (?:трек|песню))?",
        _media("next"),
    ),
    (
        r"(?:(?:включи|поставь|давай|верни|переключи на) )?(?:предыдущ|предидущ|прошл)(?:ий|ый|ая|ую|ее|ое)"
        rf"{_TRACK}?",
        _media("prev"),
    ),
    # отмена последнего действия; «верни звук (обратно)» выше — это «включи звук»
    (
        rf"(?:нет )?{_UNDO}(?: (?:это|последнее|последнее действие|действие|последнюю команду|команду))?"
        r"|верни(?: все| громкость| звук| окно| окна| все окна)? (?:как (?:было|был|была|были)|назад|обратно"
        r"|на место)|верни (?:громкость|окно|окна|все окна)|(?:сделай )?как было",
        _simple("undo"),
    ),
    # окна: «покажи рабочий стол» — свернуть всё (как Win+D), раньше папок
    (r"(?:сверни (?:все|все окна|все приложения|все программы)|покажи рабочий стол)", _win_all),
    # известные папки; «покажи» — только с «папку», иначе неоднозначно
    (
        rf"(?:{_OPEN_DIR}(?: мне)?(?: (?:папку|каталог))?|покажи(?: мне)? (?:папку|каталог))(?: с)?"
        rf" (?P<f>{_alt(_FOLDERS)})",
        _folder,
    ),
    (rf"{_MIN}(?: {_CURW})?", _win_cur("minimize")),
    (rf"{_MIN} (?:окно )?(?P<x>.+)", _win_app("minimize")),
    (
        rf"(?:{_MAX}(?: {_CURW})?{_FULL}?|(?:сделай )?(?:{_CURW} )?(?:на|во) (?:весь|полный) экран)",
        _win_cur("maximize"),
    ),
    (rf"{_MAX} (?:окно )?(?P<x>.+?){_FULL}?", _win_app("maximize")),
    (rf"{_REST}(?: {_CURW})?", _win_cur("restore")),
    (rf"{_REST} (?:окно )?(?P<x>.+)", _win_app("restore")),
    (rf"{_CLOSE} {_CURW}", _close_cur),
    (rf"{_CLOSE} (?:окно |приложение |программу )?(?P<x>.+)", _close_app),
    # «открой второй» — по результатам find
    (
        r"(?:открой|открои|отрой|запусти)(?: мне)?(?: (?:файл|папку|результат|вариант|документ))?"
        rf"(?: номер| под номером| №)? {_ORD}"
        r"(?: (?:файл|папку|результат|вариант|документ|из списка|в списке|по списку|из найденного))?",
        _open_found,
    ),
    (
        rf"{_FIND}(?: мне)? (?P<k>файлы|файлик|файл|папку|папки|каталог|документ)"
        r"(?: (?:с названием|под названием|с именем|который называется|называется|про|о|об))? (?P<q>.+)",
        _find,
    ),
    (rf"{_OPEN}(?: мне)?(?: (?:приложение|программу|прогу))? (?P<x>.+)", _open_app),
    (rf"{_FOCUS}(?: окно)? (?P<x>.+)", _focus_app),
]

_COMPILED: list[tuple[re.Pattern[str], Handler]] = [(re.compile(p, re.IGNORECASE), h) for p, h in _RULES]


# ответы на вопросы: с «?» в конце они тоже узнаются
_QUESTION_OK = frozenset({"clock", "help", "memory", "reminders"})
# свободный текст этих команд берётся из исходной строки со знаками препинания
_RAW_TEXT = frozenset({"remember", "remind"})


def match(text: str, ctx: Context) -> GrammarHit | None:
    """Разобрать частую команду без модели; нет уверенности — None (команду разберут руки)."""
    if not text or len(text) > (LONG_LEN if _LONG_OK.match(text) else MAX_LEN):
        return None
    question = text.rstrip().endswith("?")
    low, orig = _normalize(text)
    if not low:
        return None
    for rx, handler in _COMPILED:
        m = rx.fullmatch(low)
        if m is None:
            continue
        hit = handler(m, orig, ctx)
        if hit is not None and question and hit.action not in _QUESTION_OK:
            return None  # «открой хром?» — похоже на вопрос, пусть решают руки
        if hit is not None and hit.action in _RAW_TEXT and isinstance(hit.args.get("text"), str):
            hit = GrammarHit(
                hit.action, {**hit.args, "text": _raw_span(text, hit.args["text"])}, hit.confidence
            )
        return hit
    return None
