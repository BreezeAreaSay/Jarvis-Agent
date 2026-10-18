"""Клавиша «держи и говори» (settings.voice.ptt_key): глобальный хук клавиатуры pynput — лениво.

Держишь — Jarvis слушает (без «Джарвис»), отпустил — фраза закончилась. Нажата другая клавиша, пока держишь
(правый Ctrl+C) — это сочетание, а не голос: фраза отменяется. Автоповтор нажатия игнорируется.
Колбэки — из потока pynput: только быстрые действия (команда в очередь слушателя).

parse_key не зовёт pynput: «right ctrl», «правый ctrl», «RightCtrl», «rctrl», «f9», «caps lock», «alt gr»,
одна буква — в имя клавиши pynput (ctrl_r, f9, caps_lock, alt_gr, char:x).
"""

import logging
import re
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

_WORDS = {
    "правый": "right",
    "правая": "right",
    "левый": "left",
    "левая": "left",
    "контрол": "ctrl",
    "ктрл": "ctrl",
    "control": "ctrl",
    "шифт": "shift",
    "альт": "alt",
    "вин": "win",
    "windows": "win",
    "cmd": "win",
    "капс": "caps",
}
_KEYS = {
    **dict.fromkeys(("rightctrl", "rctrl", "ctrlr", "ctrlright"), "ctrl_r"),
    **dict.fromkeys(("leftctrl", "lctrl", "ctrll", "ctrlleft"), "ctrl_l"),
    **dict.fromkeys(("rightshift", "rshift", "shiftr", "shiftright"), "shift_r"),
    **dict.fromkeys(("leftshift", "lshift", "shiftl", "shiftleft"), "shift_l"),
    **dict.fromkeys(("rightalt", "ralt", "altr", "altright"), "alt_r"),
    **dict.fromkeys(("leftalt", "lalt", "altl", "altleft"), "alt_l"),
    **dict.fromkeys(("altgr", "altgraph"), "alt_gr"),
    **dict.fromkeys(("rightwin", "rwin", "winr", "winright"), "cmd_r"),
    **dict.fromkeys(("leftwin", "lwin", "winl", "winleft"), "cmd_l"),
    **dict.fromkeys(("capslock", "caps"), "caps_lock"),
    **dict.fromkeys(("scrolllock", "scroll"), "scroll_lock"),
    **dict.fromkeys(("pause", "break"), "pause"),
    **dict.fromkeys(("insert", "ins"), "insert"),
    **dict.fromkeys(("menu", "apps", "contextmenu"), "menu"),
    "numlock": "num_lock",
    **{f"f{n}": f"f{n}" for n in range(1, 25)},
}
_AMBIGUOUS = {"ctrl", "shift", "alt", "win"}
_LABELS = {
    "ctrl_r": "правый Ctrl",
    "ctrl_l": "левый Ctrl",
    "shift_r": "правый Shift",
    "shift_l": "левый Shift",
    "alt_r": "правый Alt",
    "alt_l": "левый Alt",
    "alt_gr": "AltGr",
    "cmd_r": "правая Win",
    "cmd_l": "левая Win",
    "caps_lock": "Caps Lock",
    "scroll_lock": "Scroll Lock",
    "pause": "Pause",
    "insert": "Insert",
    "menu": "Menu",
    "num_lock": "Num Lock",
}


class PttError(RuntimeError):
    """Хук клавиатуры не встал (нет pynput / нет дисплея); текст — для человека."""


def parse_key(name: str) -> str:
    """Имя клавиши из настроек → имя pynput (ctrl_r, f9, char:x). Непонятное — ValueError с подсказкой."""
    raw = str(name).strip().casefold().replace("ё", "е")
    if not raw:
        raise ValueError("клавиша не задана")
    if len(raw) == 1 and raw.isprintable() and not raw.isspace():
        return f"char:{raw}"
    words = [_WORDS.get(w, w) for w in re.split(r"[\s_+\-]+", raw) if w]
    key = "".join(words)
    if key in _KEYS:
        return _KEYS[key]
    if key in _AMBIGUOUS:
        raise ValueError(f"укажите сторону: right {key} или left {key}")
    raise ValueError(f"неизвестная клавиша: {name!r} (пример: right ctrl, f9, caps lock)")


def key_label(spec: str) -> str:
    if spec.startswith("char:"):
        return spec[5:].upper()
    if re.fullmatch(r"f\d{1,2}", spec):
        return spec.upper()
    return _LABELS.get(spec, spec)


def key_matches(spec: str, key: Any) -> bool:
    """Нажатая клавиша pynput (Key или KeyCode) — та, что в настройке?"""
    if spec.startswith("char:"):
        char = getattr(key, "char", None)
        return isinstance(char, str) and char.casefold() == spec[5:]
    name = getattr(key, "name", None)
    if name is None:  # KeyCode без имени
        return False
    if name == spec:
        return True
    # Windows: правый Alt на русской раскладке приходит как alt_gr, и наоборот
    return {name, spec} == {"alt_r", "alt_gr"}


class PttHook:
    """Глобальная клавиша: on_down / on_up / on_other зовутся из потока pynput."""

    def __init__(
        self,
        key: str,
        on_down: Callable[[], None],
        on_up: Callable[[], None],
        on_other: Callable[[], None] | None = None,
    ) -> None:
        self.spec = parse_key(key)
        self.label = key_label(self.spec)
        self.on_down = on_down
        self.on_up = on_up
        self.on_other = on_other
        self.down = False
        self._listener: Any = None
        self._lock = threading.Lock()

    def start(self) -> None:
        try:
            from pynput import keyboard
        except Exception as e:  # ImportError: нет pynput или нет дисплея (Linux без X)
            raise PttError(f"клавиша «держи и говори» недоступна: {e}") from e
        listener = keyboard.Listener(on_press=self.press, on_release=self.release)
        listener.daemon = True
        listener.start()
        self._listener = listener
        log.info("голос: клавиша «держи и говори» — %s", self.label)

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception as e:
                log.debug("голос: хук клавиатуры не снялся: %s", e)
        if self.down:
            self.down = False
            self._call(self.on_up)

    def press(self, key: Any) -> None:
        with self._lock:
            if key_matches(self.spec, key):
                if self.down:
                    return  # автоповтор
                self.down = True
                callback: Callable[[], None] | None = self.on_down
            elif self.down:
                callback = self.on_other
            else:
                return
        self._call(callback)

    def release(self, key: Any) -> None:
        with self._lock:
            if not (self.down and key_matches(self.spec, key)):
                return
            self.down = False
        self._call(self.on_up)

    @staticmethod
    def _call(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            log.exception("голос: обработчик клавиши упал")
