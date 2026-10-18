"""Готовые сценарии (GET /api/scenarios/templates): «Доброе утро», «Ухожу», «Игровой режим»…

В текстах {address} — обращение из профиля («сэр»): подставляется, когда сценарий создаётся из шаблона, чтобы
в редакторе был обычный текст.
"""

import copy
from typing import Any

from jarvis.scenarios.model import WORKDAYS, fill_address

TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "good_morning",
        "name": "Доброе утро",
        "icon": "sunrise",
        "description": "По будням в 8:00 и по фразе «доброе утро»: погода, встречи и задачи на сегодня, "
        "курсы и новости",
        "triggers": [
            {"type": "time", "at": "08:00", "weekdays": WORKDAYS},
            {"type": "phrase", "phrases": ["доброе утро"]},
        ],
        "actions": [{"type": "Jarvis.Briefing", "value": ""}],
        "say": "",
    },
    {
        "id": "leaving",
        "name": "Ухожу",
        "icon": "door",
        "description": "Скажите «я ухожу» — Jarvis выключит звук и заблокирует компьютер",
        "triggers": [{"type": "phrase", "phrases": ["я ухожу", "ухожу"]}],
        "actions": [{"type": "System.Mute", "value": ""}, {"type": "System.Lock", "value": ""}],
        "say": "",
    },
    {
        "id": "game_mode",
        "name": "Игровой режим",
        "icon": "gamepad",
        "description": "Открыли Steam — Jarvis переходит в тихий режим и ставит громкость 60%",
        "triggers": [{"type": "app_open", "app": "Steam"}],
        "actions": [
            {"type": "Jarvis.SilentMode", "value": "on"},
            {"type": "System.VolumeSet", "value": "60"},
        ],
        "say": "",
    },
    {
        "id": "end_of_day",
        "name": "Конец рабочего дня",
        "icon": "sunset",
        "description": "По будням в 18:30 напомнит закругляться и перечислит незакрытые задачи",
        "triggers": [{"type": "time", "at": "18:30", "weekdays": WORKDAYS}],
        "actions": [
            {"type": "Speak", "value": "Рабочий день окончен, {address}. Пора закругляться."},
            {"type": "Jarvis.Briefing", "value": "tasks"},
        ],
        "say": "",
    },
    {
        "id": "eye_break",
        "name": "Перерыв для глаз",
        "icon": "eye",
        "description": "Каждые 50 минут работы за компьютером напомнит дать глазам отдохнуть",
        "triggers": [{"type": "interval", "minutes": 50, "only_active": True}],
        "actions": [],
        "say": "Отдохните, {address}: 20 секунд посмотрите вдаль",
    },
    {
        "id": "welcome_back",
        "name": "С возвращением",
        "icon": "wave",
        "description": "Вернулись после 15 минут отсутствия — Jarvis поздоровается и расскажет, что вы "
        "пропустили",
        "triggers": [{"type": "idle_return", "after_min": 15}],
        "actions": [{"type": "Jarvis.Briefing", "value": "missed"}],
        "say": "",
    },
)

BY_ID: dict[str, dict[str, Any]] = {t["id"]: t for t in TEMPLATES}


def get(template_id: str) -> dict[str, Any] | None:
    return BY_ID.get(template_id)


def render(template: dict[str, Any], address: str) -> dict[str, Any]:
    """Шаблон с подставленным обращением: {id, name, icon, description, triggers, conditions, actions,
    say}."""
    out = copy.deepcopy(template)
    out.setdefault("conditions", {"weekdays": [], "time_from": "", "time_to": ""})
    out["say"] = fill_address(out.get("say", ""), address)
    out["actions"] = [{**a, "value": fill_address(str(a.get("value", "")), address)} for a in out["actions"]]
    return out
