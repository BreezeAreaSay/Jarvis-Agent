"""«Создать с помощью AI»: описание словами → название, фразы и шаги новой команды (POST /api/commands/build).

С настроенным ИИ — ctx.ai.complete(…, json_schema=SCHEMA); без ИИ или при ошибке — разбор правилами:
«Открой OBS, подожди 2 секунды и включи музыку в Spotify» → Запустить программу — OBS Studio,
Пауза — 2 секунды, Запустить программу — Spotify, Нажать клавишу — Play/Pause.
Команда не сохраняется — её правят в редакторе.
"""

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from jarvis import text as textops
from jarvis.commands import catalog, numbers, validate
from jarvis.commands.executor import substitute
from jarvis.commands.keys import humanize, normalize_keys, parse_spoken, validate_keys
from jarvis.commands.model import Action
from jarvis.commands.sites import SITES, looks_like_url, to_url
from jarvis.commands.textutil import normalize
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.commands.service import Commands

log = logging.getLogger(__name__)

AI_TIMEOUT_S = 45.0
MAX_NAME = 60

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Короткое название команды по-русски"},
        "phrases": {"type": "array", "items": {"type": "string"}, "description": "2–4 фразы активации"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": sorted(catalog.TYPES)},
                    "value": {"type": "string"},
                },
                "required": ["type", "value"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "phrases", "actions"],
    "additionalProperties": False,
}

_CLAUSES = re.compile(
    r"(\s*(?:[,;]|\.(?=\s|$)|\s(?:и потом|а потом|и затем|а затем|после этого|после чего|потом|затем|и|а)\s)"
    r"\s*)",
    re.IGNORECASE,
)
_CONNECTOR = re.compile(
    r"^(?:и потом|а потом|и затем|а затем|после этого|после чего|потом|затем|и|а)\s+", re.I
)
_WAIT = re.compile(
    r"^(?:подожди|подождать|подождём|подождем|жди|ждать|выжди|пауза|сделай паузу|задержка)"
    r"(?:\s+на)?\s*(.*)$"
)
_KEYS = re.compile(
    r"^(?:нажми|нажать|нажмите|жми)\s+(?:на\s+)?(?:клавишу|клавиши|кнопку|кнопки|сочетание|"
    r"комбинацию)?\s*(.+)$"
)
_TYPE = re.compile(
    r"^(?:напечатай|напечатать|введи|ввести|набери|набрать|напиши|написать)\s+(?:текст\s+)?(.+)$",
    re.IGNORECASE,
)
_MUSIC = re.compile(
    r"^(?:включи|включить|запусти|запустить|поставь|поставить|играй)\s+(?:музыку|музон|песню|"
    r"трек|плейлист|радио)\s+(?:в|на|через)\s+(.+)$"
)
_OPEN = re.compile(
    r"^(?:открой|открыть|запусти|запустить|включи|включить|зайди на|зайди в|перейди на|перейди в)"
    r"\s+(?:программу\s+|приложение\s+|сайт\s+|игру\s+)?(.+)$",
    re.IGNORECASE,
)
_UNITS = (
    ("милли", 0.001),
    ("мс", 0.001),
    ("сек", 1.0),
    ("с", 1.0),
    ("мин", 60.0),
    ("час", 3600.0),
    ("ч", 3600.0),
)
_MEDIA_DETAIL = {
    "Media.PlayPause": "Play/Pause",
    "Media.Next": "Следующий трек",
    "Media.Prev": "Предыдущий трек",
    "Media.Stop": "Стоп",
}


async def build(service: "Commands", description: str) -> dict[str, Any]:
    text = textops.clean(description)[:1000]
    if not text:
        raise UserFacingError("Опишите, что должна делать команда")
    result: dict[str, Any] | None = None
    ai = service.ctx.ai
    if ai.configured:
        try:
            raw = await asyncio.wait_for(
                ai.complete(_messages(service, text), json_schema=SCHEMA, temperature=0.2), AI_TIMEOUT_S
            )
            result = from_ai(service, raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("ИИ не собрал команду (%s) — разбираю правилами", e)
    if result is None:
        result = offline(service, text)
    result["steps"] = steps(service, result["actions"])
    return result


def _messages(service: "Commands", text: str) -> list[dict[str, Any]]:
    lines = [f"- {a.type}: {a.title}. {a.description}".rstrip() for a in catalog.ACTIONS if not a.user_only]
    apps = ", ".join(a.name for a in service.apps.apps[:80]) or "неизвестно"
    system = (
        "Ты собираешь голосовую команду для ассистента Jarvis на Windows. Верни JSON: name — короткое "
        "название по-русски; phrases — 2–4 короткие фразы активации строчными буквами без имени ассистента; "
        "actions — шаги "
        "по порядку, value — всегда строка. Программы называй как в меню «Пуск». Клавиши — «ctrl+shift+s», "
        "паузы Wait — в секундах. Не придумывай шаги, которых нет в описании.\n"
        f"Действия:\n{chr(10).join(lines)}\n"
        f"Установленные программы: {apps}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


def from_ai(service: "Commands", raw: Any) -> dict[str, Any] | None:
    """Ответ ИИ → команда (неизвестные шаги отброшены, программы сверены с «Пуском») или None."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return None
    actions: list[dict[str, str]] = []
    skipped: list[str] = []
    for item in raw.get("actions") or []:
        if not isinstance(item, dict):
            continue
        type_ = _canonical_type(str(item.get("type", "")))
        value = str(item.get("value") if item.get("value") is not None else "").strip()
        if type_ is None:
            skipped.append(str(item.get("type", "")))
            continue
        if type_ == "Keys.Press" and value and validate_keys(value) is not None:
            value = parse_spoken(value) or value
        action = Action(type=type_, value=value)
        error = validate.action_error(action, user=True)
        if error:
            skipped.append(error)
            continue
        actions.append({"type": type_, "value": value})
    if not actions:
        return None
    actions = _resolve_apps(
        service, [a.model_dump() for a in validate.clean_actions([Action(**a) for a in actions])]
    )
    name = " ".join(str(raw.get("name") or "").split())[:MAX_NAME] or "Новая команда"
    phrases = []
    for p in raw.get("phrases") or []:
        phrase = " ".join(normalize(str(p)).split())
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return {"name": name, "phrases": phrases[:6] or [normalize(name)], "actions": actions, "skipped": skipped}


def _canonical_type(type_: str) -> str | None:
    t = type_.strip()
    if t in catalog.TYPES:
        return t
    low = t.casefold()
    return next((x for x in catalog.TYPES if x.casefold() == low), None)


# --- разбор правилами --------------------------------------------------------------------------------


def _pieces(text: str) -> list[tuple[str, str]]:
    """[(разделитель перед предложением, предложение)] — разделитель нужен, чтобы склеить текст для печати."""
    parts = _CLAUSES.split(re.sub(r"(\d),(\d)", r"\1.\2", text))
    out: list[tuple[str, str]] = []
    sep = ""
    for i, part in enumerate(parts):
        if i % 2:
            sep = part
        elif part and part.strip(" .,;"):
            out.append((sep, _CONNECTOR.sub("", part.strip())))
            sep = ""
    return out


def split_clauses(text: str) -> list[str]:
    return [clause for _, clause in _pieces(text)]


def offline(service: "Commands", text: str) -> dict[str, Any]:
    actions: list[dict[str, str]] = []
    skipped: list[str] = []
    for sep, clause in _pieces(text):
        found = parse_clause(service, clause)
        if found is None and actions and actions[-1]["type"] == "Keys.Type":
            actions[-1]["value"] += sep + clause  # «напечатай Привет, мир» — запятая внутри текста
            continue
        if found is None:
            skipped.append(clause)
        else:
            actions.extend(found)
    if not actions:
        raise UserFacingError(
            "Не понял, что должна делать команда. Опишите шаги проще: «открой Spotify, подожди 2 секунды, "
            "нажми ctrl+s»"
        )
    name = _name(text)
    return {
        "name": name,
        "phrases": [normalize(name)],
        "actions": _resolve_apps(service, actions),
        "skipped": skipped,
    }


def parse_clause(service: "Commands", clause: str) -> list[dict[str, str]] | None:
    """Одно предложение описания → шаги или None, если не понял."""
    n = normalize(clause)
    if not n:
        return []
    if m := _WAIT.match(n):
        seconds = _duration(m.group(1))
        return [{"type": "Wait", "value": numbers.fmt_number(seconds)}] if seconds is not None else None
    if m := _KEYS.match(n):
        combo = _keys(m.group(1))
        if combo:
            return [{"type": "Keys.Press", "value": combo}]
    if m := _TYPE.match(clause.strip()):
        typed = m.group(1).strip().strip("«»\"'“”")
        if typed:
            return [{"type": "Keys.Type", "value": typed}]
    if m := _MUSIC.match(n):
        target = m.group(1)
        found = service.apps.resolve(target)
        name = found.name if found is not None else _original(clause, target)
        return [{"type": "Launch.App", "value": name}, {"type": "Media.PlayPause", "value": "play"}]
    opened = _OPEN.match(clause.strip())
    if opened and looks_like_url(opened.group(1).strip()):
        return [{"type": "Launch.Url", "value": to_url(opened.group(1).strip())}]
    inline = _from_commands(service, clause)
    if inline is not None:
        return inline
    if opened:
        target = opened.group(1).strip()
        found = service.apps.resolve(target)
        if found is not None:
            return [{"type": "Launch.App", "value": found.name}]
        site = service.sites.resolve(target)
        if site is not None:
            return [{"type": "Launch.Url", "value": site.site.url}]
        return [{"type": "Launch.App", "value": target}]
    return None


def _from_commands(service: "Commands", clause: str) -> list[dict[str, str]] | None:
    """Предложение — это уже известная команда («сверни все окна», «громкость на 30»): её шаги."""
    match = service.match(clause)
    if match is None or match.score < 88:
        return None
    out: list[dict[str, str]] = []
    for part in match.parts:
        entry = service.get_entry(part.command_id)
        if entry is None:
            return None
        if entry.source != "builtin" or entry.handler is not None:
            out.append({"type": "Jarvis.Command", "value": entry.id})
            continue
        for action in entry.command.actions:
            try:
                value = substitute(action.value, part.slots)
            except UserFacingError:
                value = action.value
            out.append({"type": action.type, "value": value})
    return out


def _keys(text: str) -> str | None:
    raw = text.strip().strip("«»\"'")
    if validate_keys(raw) is None:
        return normalize_keys(raw)
    return parse_spoken(raw)


def _duration(text: str) -> float | None:
    """«2 секунды» → 2, «полсекунды» → 0.5, «минуту» → 60, «» → 1."""
    words = text.split()
    if not words:
        return 1.0
    if words[0] in ("полсекунды", "пол"):
        return 0.5
    found = numbers.find_number(words)
    value, rest = (found[0], words[found[2] :]) if found else (1.0, words)
    unit = rest[0] if rest else "сек"
    for prefix, factor in _UNITS:
        if unit.startswith(prefix):
            return value * factor
    return value if found else None


def _original(clause: str, normalized_part: str) -> str:
    """Кусок исходного текста (с регистром), соответствующий нормализованному хвосту."""
    words = clause.split()
    count = len(normalized_part.split())
    return " ".join(words[-count:]) if count and len(words) >= count else normalized_part


def _name(text: str) -> str:
    name = text.strip(" .!?,;")
    if len(name) > MAX_NAME:
        cut = name[:MAX_NAME]
        name = cut[: cut.rfind(" ")] if " " in cut else cut
        name = name.rstrip(" ,;")
    return name[:1].upper() + name[1:]


def _resolve_apps(service: "Commands", actions: list[dict[str, str]]) -> list[dict[str, str]]:
    """Названия программ — как в «Пуске» («obs» → «OBS Studio»)."""
    out = []
    for a in actions:
        if (
            a["type"] in ("Launch.App", "App.Close", "App.Focus", "App.Kill")
            and a["value"]
            and "{" not in a["value"]
        ):
            found = service.apps.resolve(a["value"])
            if found is not None and found.score >= 85:
                a = {**a, "value": found.name}
        out.append(a)
    return out


# --- шаги для интерфейса ------------------------------------------------------------------------------


def steps(service: "Commands", actions: list[dict[str, str]]) -> list[dict[str, str]]:
    """[{title, detail}]: «Запустить программу — OBS Studio», «Пауза — 2 секунды»."""
    out = []
    for a in actions:
        spec = catalog.get(a["type"])
        out.append({"title": spec.step if spec else a["type"], "detail": _detail(service, a)})
    return out


def _detail(service: "Commands", action: dict[str, str]) -> str:
    t, v = action["type"], action.get("value", "")
    if t in _MEDIA_DETAIL:
        return _MEDIA_DETAIL[t]
    if t == "Wait":
        seconds = numbers.parse_duration(v)
        return numbers.duration_text(seconds) if seconds is not None and seconds >= 1 else f"{v} с"
    if t == "Keys.Press":
        return humanize(v)
    if t == "Keys.Type":
        return f"«{v}»"
    if t == "Launch.Url":
        site = next((s for s in SITES if s.url == v), None)
        return site.name if site else (urlparse(v).netloc or v)
    if t in ("System.VolumeSet", "System.BrightnessSet") and v and v[0].isdigit():
        return f"{v}%"
    if t == "System.VolumeUp":
        return f"+{v or 10}%"
    if t == "System.VolumeDown":
        return f"−{v or 10}%"
    if t == "Web.Search":
        return v.partition(":")[2].strip() if ":" in v else v
    if t == "Jarvis.Command":
        entry = service.get_entry(v)
        return entry.command.name if entry is not None else v
    spec = catalog.get(t)
    if spec and spec.params and spec.params[0].kind == "select":
        return next((label for value, label in spec.params[0].options if value == v), v)
    return v


__all__ = ["SCHEMA", "build", "from_ai", "offline", "parse_clause", "split_clauses", "steps"]
