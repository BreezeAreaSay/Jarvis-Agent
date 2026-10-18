"""Текстовые мелочи ИИ: речь без markdown, название чата, даты по-русски, разбор ISO-времени."""

import re
from datetime import date, datetime, timedelta

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_EMOJI = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f900-\U0001f9ff\U00002b00-\U00002bff️‍]")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """plural(5, ("минуту", "минуты", "минут")) → «минут»."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def one_line(text: str, limit: int) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: max(1, limit - 1)].rstrip() + "…"


def speech_text(text: str, limit: int = 450) -> str:
    """Ответ для голоса: без markdown, ссылок, эмодзи и списков; длинный — первые предложения."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*•+]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\|.*\|\s*$", " ", t, flags=re.M)  # таблицы не читаем
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"(?<!\w)[*_~](?=\S)|(?<=\S)[*_~](?!\w)", "", t)
    t = t.replace(">", " ").replace("#", " ")
    t = _EMOJI.sub("", t)
    lines = [line.strip() for line in t.splitlines() if line.strip()]
    # строки без знака в конце (бывшие пункты списка) — отдельные фразы
    t = " ".join(line if line[-1] in ".!?…:;," else line + "." for line in lines)
    t = " ".join(t.split())
    if len(t) <= limit:
        return t
    out = ""
    for sentence in _SENTENCE_END.split(t):
        if out and len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    if not out or len(out) > limit:
        out = t[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return out


def chat_title(text: str, limit: int = 48) -> str:
    """Короткое название чата по первому сообщению: первые фразы первой строки, с заглавной буквы.

    Слишком короткая первая фраза («Привет!») дополняется следующими, пока название не станет понятным.
    """
    line = next((ln for ln in str(text).splitlines() if ln.strip()), "")
    t = " ".join(line.split())
    t = re.sub(r"^(?:джарвис|jarvis)[\s,!:]+", "", t, flags=re.I)
    t = re.sub(r"^(?:агент|agent)\s*[,:]\s*", "", t, flags=re.I)
    t = re.sub(r"https?://\S+", "ссылка", t)
    first = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", t):
        first = f"{first} {sentence}".strip()
        if len(first) >= 20:
            break
    first = first.strip(" .,!?…:;—-\"'«»")
    if not first:
        return "Новый чат"
    if len(first) > limit:
        first = first[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
    return first[0].upper() + first[1:]


def tz_label(moment: datetime) -> str:
    offset = moment.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    label = f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"
    name = moment.tzname() or ""
    return f"{label} ({name})" if name and not name.startswith(("UTC", "+", "-")) else label


def now_text(moment: datetime | None = None) -> str:
    """«воскресенье, 18 октября 2026 года, 14:05 (UTC+03:00 (MSK))»."""
    m = (moment or datetime.now()).astimezone()
    return (
        f"{WEEKDAYS[m.weekday()]}, {m.day} {MONTHS_GEN[m.month - 1]} {m.year} года, {m:%H:%M} ({tz_label(m)})"
    )


def human_time(ts: float, now: datetime | None = None) -> str:
    """Время события для человека: «сегодня в 15:00», «завтра в 9:30», «20 октября в 18:00»."""
    moment = datetime.fromtimestamp(ts)
    today = (now or datetime.now()).date()
    clock = f"{moment.hour}:{moment.minute:02d}"
    if moment.date() == today:
        return f"сегодня в {clock}"
    if moment.date() == today + timedelta(days=1):
        return f"завтра в {clock}"
    if moment.date() == today - timedelta(days=1):
        return f"вчера в {clock}"
    year = f" {moment.year} года" if moment.year != today.year else ""
    return f"{moment.day} {MONTHS_GEN[moment.month - 1]}{year} в {clock}"


def iso_local(ts: float | None) -> str | None:
    """unix-время → «2026-10-18T15:00 (воскресенье)» для модели."""
    if ts is None:
        return None
    moment = datetime.fromtimestamp(ts)
    return f"{moment:%Y-%m-%dT%H:%M} ({WEEKDAYS[moment.weekday()]})"


def parse_when(value: str | float | int, *, date_only_hour: int = 9) -> tuple[float, bool]:
    """ISO 8601 («2026-10-18T15:00», «2026-10-18 15:00+03:00», «2026-10-19») → (unix-время, только дата).

    Время без часового пояса — местное. Дата без времени — date_only_hour часов утра.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value), False
    raw = str(value).strip()
    if not raw:
        raise ValueError("пустое время")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        day = date.fromisoformat(raw)
        return datetime(day.year, day.month, day.day, date_only_hour).timestamp(), True
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"не понимаю время «{raw}» — нужен формат ISO, например 2026-10-18T15:00") from None
    return moment.timestamp(), False
