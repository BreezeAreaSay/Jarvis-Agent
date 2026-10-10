"""Медиаклавиши: пауза/продолжить, следующий и предыдущий трек (SendInput через pc._input)."""

from typing import Literal

from pc import _input, policy
from pc.result import Caller, Result, fail, ok

MediaAction = Literal["play_pause", "next", "prev"]

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

# действие → (виртуальная клавиша, текст для человека)
KEYS: dict[str, tuple[int, str]] = {
    "play_pause": (VK_MEDIA_PLAY_PAUSE, "Пауза/продолжаю"),
    "next": (VK_MEDIA_NEXT_TRACK, "Следующий трек"),
    "prev": (VK_MEDIA_PREV_TRACK, "Предыдущий трек"),
}


def media(action: MediaAction, caller: Caller) -> Result:
    """Нажать и отпустить медиаклавишу."""
    if action not in KEYS:
        return fail("Не понял, что сделать с музыкой: пауза, следующий или предыдущий трек?")
    vk, text = KEYS[action]
    denied = policy.require("media", caller, f"{text}?")
    if denied:
        return denied
    try:
        down = _input.send_key(vk)
        up = _input.send_key(vk, up=True)
    except OSError:
        return fail("Не удалось нажать медиаклавишу")
    if not (down and up):
        return fail("Не удалось нажать медиаклавишу")
    return ok(text)
