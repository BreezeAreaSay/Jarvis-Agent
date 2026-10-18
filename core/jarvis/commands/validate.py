"""Проверка команд: действия (тип, значение, клавиши) и своя команда из редактора (CommandIn → Command)."""

import re

from jarvis.commands import catalog, numbers
from jarvis.commands.keys import normalize_keys, validate_keys
from jarvis.commands.model import Action, Command, CommandIn
from jarvis.commands.textutil import normalize
from jarvis.services import UserFacingError

MAX_ACTIONS = 60
MAX_PHRASES = 40
MAX_NAME = 80
MAX_PHRASE = 150

_COORDS = re.compile(r"^\s*-?\d+\s*[ ,;x×]\s*-?\d+\s*$")
_NUMBER = re.compile(r"^[+-]?\d+(?:[.,]\d+)?%?$")
_NUMERIC = (
    "System.VolumeSet",
    "System.VolumeUp",
    "System.VolumeDown",
    "System.BrightnessSet",
    "Mouse.Scroll",
)
_CHOICES = {
    "Media.PlayPause": ("", "play", "pause"),
    "System.Screenshot": ("", "window"),
    "Jarvis.SilentMode": ("", "on", "off", "toggle"),
    "Jarvis.Microphone": ("", "on", "off", "toggle"),
}


def _is_point(value: str) -> bool:
    return value.casefold() in ("center", "центр") or bool(_COORDS.match(value))


def action_error(action: Action, *, user: bool) -> str | None:
    """Что не так с действием (текст для человека) или None."""
    spec = catalog.get(action.type)
    if spec is None:
        return f"Неизвестное действие «{action.type}»"
    if spec.user_only and not user:
        return f"Действие «{spec.title}» доступно только в своих командах"
    value = action.value.strip()
    if spec.required and not value:
        return f"«{spec.title}»: не заполнено значение"
    if not value or "{" in value:
        return None  # пусто (можно) или слот — проверится при выполнении
    t = action.type
    if t == "Keys.Press":
        error = validate_keys(value)
        return f"«{spec.title}»: {error}" if error else None
    if (
        t in ("Wait", "Jarvis.Timer", "System.Shutdown", "System.Restart")
        and numbers.parse_duration(value) is None
    ):
        return f"«{spec.title}»: нужно число секунд (или «5m», «1h»)"
    if t in _NUMERIC and not _NUMBER.match(value):
        return f"«{spec.title}»: нужно число"
    if t.startswith("Mouse.") and t != "Mouse.Scroll" and not _is_point(value):
        return f"«{spec.title}»: координаты пишутся как «960 540»"
    if t == "Jarvis.Reminder" and "|" not in value:
        return f"«{spec.title}»: нужно «через | текст», например «10m | Позвонить маме»"
    if t in _CHOICES and value.casefold() not in _CHOICES[t]:
        return f"«{spec.title}»: допустимо {', '.join(v or '(пусто)' for v in _CHOICES[t])}"
    return None


def check_actions(actions: list[Action], *, user: bool) -> list[str]:
    return [f"Шаг {i}: {err}" for i, a in enumerate(actions, 1) if (err := action_error(a, user=user))]


def clean_actions(actions: list[Action]) -> list[Action]:
    """Привести значения к виду, который понимает исполнитель: клавиши — «ctrl+shift+s»."""
    out = []
    for a in actions:
        value = a.value.strip()
        if a.type == "Keys.Press" and value and "{" not in value and validate_keys(value) is None:
            value = normalize_keys(value)
        out.append(
            Action(
                type=a.type, value=value if a.type not in ("Keys.Type", "Keys.Paste", "Speak") else a.value
            )
        )
    return out


def user_command(data: CommandIn, command_id: str) -> Command:
    """Своя команда из редактора: проверить и почистить. Ошибка — UserFacingError с понятным текстом."""
    name = " ".join(data.name.split())
    if not name:
        raise UserFacingError("Укажите название команды")
    if len(name) > MAX_NAME:
        raise UserFacingError(f"Название — не длиннее {MAX_NAME} символов")
    phrases: list[str] = []
    seen: set[str] = set()
    for raw in data.phrases:
        phrase = " ".join(str(raw).split())
        key = normalize(phrase.replace("{", " {").replace("}", "} "))
        if not phrase or not key or key in seen:
            continue
        if len(phrase) > MAX_PHRASE:
            raise UserFacingError(f"Фраза «{phrase[:40]}…» слишком длинная")
        seen.add(key)
        phrases.append(phrase)
    if not phrases:
        raise UserFacingError("Добавьте хотя бы одну фразу активации")
    if len(phrases) > MAX_PHRASES:
        raise UserFacingError(f"Фраз — не больше {MAX_PHRASES}")
    if not data.actions:
        raise UserFacingError("Добавьте хотя бы одно действие")
    if len(data.actions) > MAX_ACTIONS:
        raise UserFacingError(f"Действий — не больше {MAX_ACTIONS}")
    errors = check_actions(data.actions, user=True)
    if errors:
        raise UserFacingError("; ".join(errors[:3]))
    optional = list(dict.fromkeys(" ".join(o.split()) for o in data.optional if str(o).strip()))
    return Command(
        id=command_id,
        name=name,
        category="user",
        phrases=phrases,
        optional=optional,
        actions=clean_actions(data.actions),
        confirm=data.confirm,
        chain=data.chain,
        icon=(data.icon or "").strip()[:40],
        enabled=data.enabled,
        say=data.say.strip()[:300],
    )
