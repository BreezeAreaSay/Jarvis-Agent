"""Текст для голоса: что из ответа говорить вслух и как, чтобы синтез не спотыкался.

- prepare(text, russian=True) — без markdown, кода, таблиц, ссылок, путей, расширений файлов и эмодзи;
  для русских голосов (piper, русские голоса Windows и Edge) латиница — русскими буквами: piper с espeak-ng
  читает её плохо («Открыл Telegram» → «отрыв», замер jarvis-v1), поэтому частые имена — словарём, остальное —
  правилами; «50%» → «50 процентов» с согласованием.
- split_sentences(text) — по предложениям: следующее синтезируется, пока звучит текущее (первый звук раньше);
  слишком длинное режется по запятым.
"""

import re
import unicodedata

# частые латинские имена → как их произносить по-русски (из jarvis-v1 tts.speakable + дополнения)
LATIN_WORDS = {
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
    "usb": "ю эс би", "gpt": "джипити", "ai": "эй ай", "vk": "вэ ка", "vkontakte": "вконтакте",
    "tiktok": "тикток", "instagram": "инстаграм", "facebook": "фейсбук", "twitter": "твиттер",
    "reddit": "реддит", "wikipedia": "википедия", "amazon": "амазон", "apple": "эпл", "iphone": "айфон",
    "android": "андроид", "linux": "линукс", "mac": "мак", "macos": "макос", "ios": "ай о эс",
    "openai": "опен эй ай", "claude": "клод", "anthropic": "антропик", "gemini": "джемини",
    "deepseek": "дипсик", "ollama": "оллама", "copilot": "копайлот", "cursor": "курсор",
    "figma": "фигма", "blender": "блендер", "unity": "юнити", "unreal": "анриал", "engine": "энджин",
    "valorant": "валорант", "dota": "дота", "counter": "каунтер", "strike": "страйк", "cs": "кс",
    "battle": "батл", "net": "нет", "origin": "ориджин", "uplay": "юплей", "xbox": "иксбокс",
    "playstation": "плейстейшн", "nintendo": "нинтендо", "rutube": "рутуб", "kinopoisk": "кинопоиск",
    "ozon": "озон", "wildberries": "вайлдберриз", "avito": "авито", "sber": "сбер", "tinkoff": "тинькофф",
    "alice": "алиса", "siri": "сири", "alexa": "алекса", "hello": "хэллоу", "hi": "хай", "yes": "йес",
    "no": "ноу", "wi": "вай", "fi": "фай", "tv": "ти ви", "pc": "пи си", "cpu": "цэ пэ у", "gpu": "джи пи ю",
    "ram": "рам", "ssd": "эс эс ди", "hdd": "аш дэ дэ", "api": "эй пи ай", "url": "ю ар эл",
    "http": "аш ти ти пи", "https": "аш ти ти пи эс", "www": "", "com": "ком", "ru": "ру", "org": "орг",
    "mb": "мегабайт", "gb": "гигабайт", "kb": "килобайт", "tb": "терабайт", "fps": "фпс",
}  # fmt: skip
LETTER_NAMES = {
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
    if low in LATIN_WORDS:
        return LATIN_WORDS[low]
    if word.isupper() and 2 <= len(word) <= 5:  # GPT, USB — по буквам
        return " ".join(LETTER_NAMES.get(c, c) for c in low)
    if len(low) == 1:
        return LETTER_NAMES.get(low, low)
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


_FENCE = re.compile(r"```.*?(?:```|$)|~~~.*?(?:~~~|$)", re.DOTALL)
_TABLE_LINE = re.compile(r"(?m)^\s*\|.*$")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_WIN_PATH = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s,;«»\"']*")
_UNIX_PATH = re.compile(r"(?<![\w.])/(?:[\w.-]+/)+[\w.-]*")
_FILE_EXT = re.compile(r"(?<=\w)\.(?:[A-Za-z][A-Za-z0-9]{1,4})\b")
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_MARKUP = re.compile(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)|~~")
_LINE_MARK = re.compile(r"(?m)^\s*(?:#{1,6}\s+|>\s+|[-*•+]\s+|\d{1,2}[.)]\s+)")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_LATIN = re.compile(r"[A-Za-z][A-Za-z']*")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_DECIMAL = re.compile(r"(?<=\d)\.(?=\d)")
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_REPEAT_PUNCT = re.compile(r"([!?.,])\1+")
_KEEP_SYMBOLS = set("°№")


def _percent_word(number: str) -> str:
    """Согласование: 1 процент, 2 процента, 5 процентов; дробь — «процента»."""
    if "," in number or "." in number:
        return "процента"
    n = int(number[-2:]) if len(number) > 1 else int(number)
    if 11 <= n % 100 <= 14:
        return "процентов"
    last = n % 10
    if last == 1:
        return "процент"
    if 2 <= last <= 4:
        return "процента"
    return "процентов"


def _strip_symbols(text: str) -> str:
    """Эмодзи, пиктограммы, служебные символы — прочь (буквы, цифры и пунктуация остаются)."""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in _KEEP_SYMBOLS or not cat.startswith(("So", "Sk", "Cs", "Co", "Cn", "Cf")):
            out.append(ch)
        elif cat.startswith("So"):
            out.append(" ")
    return "".join(out)


def prepare(text: str, *, russian: bool = True, accents: bool = True) -> str:
    """Текст для синтеза; russian — голос русский (латиницу — русскими буквами, % — словом); accents=False —
    без знаков ударения U+0301 (голос их не понимает: понимает только piper со словарём Jarvis)."""
    t = str(text or "")
    if not accents:
        t = t.replace("\u0301", "")
    t = _FENCE.sub(" ", t)
    t = _TABLE_LINE.sub(" ", t)
    t = _HTML_TAG.sub(" ", t)
    t = _MD_LINK.sub(lambda m: m.group(1), t)
    t = _INLINE_CODE.sub(lambda m: m.group(1) if len(m.group(1)) <= 24 else " ", t)
    t = _URL.sub(" ", t)
    t = _EMAIL.sub(" ", t)
    t = _WIN_PATH.sub(" ", t)
    t = _UNIX_PATH.sub(" ", t)
    t = _FILE_EXT.sub("", t)
    t = _LINE_MARK.sub("", t)
    t = _MARKUP.sub("", t)
    t = _strip_symbols(t)
    t = t.replace(" ", " ").replace("…", "...")
    if russian:
        t = _PERCENT.sub(lambda m: f"{m.group(1)} {_percent_word(m.group(1))}", t)
        t = _DECIMAL.sub(",", t)  # 2.5 → 2,5: иначе «два точка пять»
        t = t.replace("%", " процентов").replace("№", "номер ").replace("&", " и ").replace("+", " плюс ")
        t = _LATIN.sub(lambda m: transliterate(m.group(0)), t)
    t = t.replace("*", " ").replace("#", " ").replace("|", " ").replace("_", " ")
    t = _REPEAT_PUNCT.sub(r"\1", t.replace("...", "…")).replace("…", "...")
    lines = [_SPACES.sub(" ", line).strip(" ,;:—–-") for line in t.splitlines()]
    t = "\n".join(line for line in lines if line)
    return t if any(ch.isalnum() for ch in t) else ""


_SENTENCE_END = re.compile(r"(?<=[.!?…])[»\"')\]]*\s+|\n+")
_CLAUSE = re.compile(r"(?<=[,;:—–])\s+")


def split_sentences(text: str, max_len: int = 220, min_len: int = 12) -> list[str]:
    """Предложения для синтеза по очереди; длинные — по запятым, короткие («Да.») — вместе со следующим."""
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]
    pieces: list[str] = []
    for part in parts:
        if len(part) <= max_len:
            pieces.append(part)
            continue
        current = ""
        for clause in _CLAUSE.split(part):
            if current and len(current) + 1 + len(clause) > max_len:
                pieces.append(current)
                current = clause
            else:
                current = f"{current} {clause}".strip()
            while len(current) > max_len:  # без запятых — по словам
                cut = current.rfind(" ", 0, max_len)
                cut = cut if cut > 0 else max_len
                pieces.append(current[:cut].strip())
                current = current[cut:].strip()
        if current:
            pieces.append(current)
    merged: list[str] = []
    for piece in pieces:
        if merged and (len(merged[-1]) < min_len) and len(merged[-1]) + 1 + len(piece) <= max_len:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return [m for m in merged if any(ch.isalnum() for ch in m)]
