"""«Запись действий»: клики, сочетания клавиш и набранный текст превращаются в шаги команды.

ActionRecorder — чистая логика (её проверяют тесты): клики с координатами, двойной клик, прокрутка, сочетания
(«ctrl+c»), набранный текст склеивается в один Keys.Type, паузы дольше 0,7 с становятся шагом Wait.
Recorder слушает клавиатуру и мышь через pynput (импорт ленивый: на Linux без экрана запись недоступна).

Ограничение: щелчок «Остановить запись» в окне Jarvis тоже попадает в запись — поэтому последний клик, если он
был меньше чем за 1,5 с до остановки, отбрасывается. Окно, в котором сделан клик, не проверяется.
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

WAIT_MIN_S = 0.7  # паузы короче не записываются
DOUBLE_CLICK_S = 0.45
DOUBLE_CLICK_PX = 6
DROP_LAST_CLICK_S = 1.5
MAX_ACTIONS = 300
MODIFIERS = ("ctrl", "alt", "shift", "win")
_ORDER = {m: i for i, m in enumerate(MODIFIERS)}


class ActionRecorder:
    """Собирает шаги из событий ввода. Время — монотонные секунды (t); по умолчанию — сейчас."""

    def __init__(self, on_change: Callable[[int], None] | None = None) -> None:
        self.actions: list[dict[str, str]] = []
        self._on_change = on_change
        self._mods: set[str] = set()
        self._text: list[str] = []
        self._text_index: int | None = None  # шаг Keys.Type, в который сейчас дописывается текст
        self._last_t: float | None = None
        self._last_click: tuple[float, int, int] | None = None
        self._last_kind = ""

    def _now(self, t: float | None) -> float:
        return time.monotonic() if t is None else t

    def _gap(self, t: float) -> None:
        """Пауза с прошлого события — шаг Wait (если дольше WAIT_MIN_S)."""
        if self._last_t is not None:
            gap = t - self._last_t
            if gap > WAIT_MIN_S:
                self._flush_text()
                self._append("Wait", f"{round(gap, 1):g}")
        self._last_t = t

    def _append(self, type_: str, value: str) -> None:
        if len(self.actions) >= MAX_ACTIONS:
            return
        self.actions.append({"type": type_, "value": value})
        self._last_kind = type_
        if self._on_change is not None:
            self._on_change(len(self.actions))

    def _flush_text(self) -> None:
        self._text_index = None

    # --- клавиатура -------------------------------------------------------------------------------
    def key_down(self, key: str, char: str | None = None, t: float | None = None) -> None:
        """key — каноническое имя (a, f5, enter, ctrl…); char — напечатанный символ с учётом раскладки."""
        now = self._now(t)
        if key in MODIFIERS:
            self._mods.add(key)
            return
        command_mods = self._mods - {"shift"}
        if char is not None and len(char) == 1 and char.isprintable() and not command_mods:
            if self._text_index is None:
                self._gap(now)
                self._append("Keys.Type", "")
                self._text_index = len(self.actions) - 1
            self.actions[self._text_index]["value"] += char
            self._last_t = now
            return
        if not key:
            return
        if key == "backspace" and self._text_index is not None and not self._mods:
            value = self.actions[self._text_index]["value"]
            self.actions[self._text_index]["value"] = value[:-1]
            self._last_t = now
            return
        self._gap(now)
        self._flush_text()
        mods = sorted(self._mods, key=_ORDER.__getitem__)
        combo = "+".join([*mods, key])
        last = self.actions[-1] if self.actions else None
        if last is not None and last["type"] == "Keys.Press" and self._last_kind == "Keys.Press":
            base, _, count = last["value"].partition("*")
            if base == combo:
                last["value"] = f"{combo}*{int(count or 1) + 1}"
                return
        self._append("Keys.Press", combo)

    def key_up(self, key: str, t: float | None = None) -> None:
        self._mods.discard(key)

    # --- мышь -------------------------------------------------------------------------------------
    def click(self, x: int, y: int, button: str = "left", t: float | None = None) -> None:
        now = self._now(t)
        if button not in ("left", "right"):
            return
        last = self.actions[-1] if self.actions else None
        if (
            button == "left"
            and last is not None
            and last["type"] == "Mouse.ClickLeft"
            and self._last_click is not None
            and now - self._last_click[0] <= DOUBLE_CLICK_S
            and abs(x - self._last_click[1]) <= DOUBLE_CLICK_PX
            and abs(y - self._last_click[2]) <= DOUBLE_CLICK_PX
        ):
            last["type"] = "Mouse.DoubleClick"
            self._last_click = None
            self._last_t = now
            return
        self._gap(now)
        self._flush_text()
        self._append("Mouse.ClickLeft" if button == "left" else "Mouse.ClickRight", f"{int(x)} {int(y)}")
        self._last_click = (now, int(x), int(y))

    def scroll(self, dy: int, t: float | None = None) -> None:
        now = self._now(t)
        if not dy:
            return
        last = self.actions[-1] if self.actions else None
        if (
            last is not None
            and last["type"] == "Mouse.Scroll"
            and self._last_t is not None
            and now - self._last_t <= WAIT_MIN_S
        ):
            last["value"] = str(int(last["value"]) + int(dy))
            self._last_t = now
            return
        self._gap(now)
        self._flush_text()
        self._append("Mouse.Scroll", str(int(dy)))

    # --- итог -------------------------------------------------------------------------------------
    def finish(self, t: float | None = None) -> list[dict[str, str]]:
        """Шаги записи; последний клик незадолго до остановки (по кнопке «Стоп» в Jarvis) отбрасывается."""
        now = self._now(t)
        actions = [dict(a) for a in self.actions if not (a["type"] == "Keys.Type" and not a["value"])]
        if (
            actions
            and actions[-1]["type"] in ("Mouse.ClickLeft", "Mouse.DoubleClick")
            and self._last_click is not None
            and now - self._last_click[0] <= DROP_LAST_CLICK_S
        ):
            actions.pop()
        while actions and actions[-1]["type"] == "Wait":
            actions.pop()
        while actions and actions[0]["type"] == "Wait":
            actions.pop(0)
        return actions


# --- pynput -----------------------------------------------------------------------------------------

_SPECIAL = {
    "ctrl": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd": "win",
    "cmd_l": "win",
    "cmd_r": "win",
    "enter": "enter",
    "space": "space",
    "tab": "tab",
    "esc": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "insert": "insert",
    "home": "home",
    "end": "end",
    "page_up": "page_up",
    "page_down": "page_down",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "caps_lock": "caps_lock",
    "num_lock": "num_lock",
    "scroll_lock": "scroll_lock",
    "print_screen": "print_screen",
    "pause": "pause",
    "menu": "menu",
    "media_play_pause": "play_pause",
    "media_next": "next_track",
    "media_previous": "prev_track",
    "media_volume_up": "volume_up",
    "media_volume_down": "volume_down",
    "media_volume_mute": "volume_mute",
}


# знаки на клавишах (коды Windows VK_OEM_*) — для сочетаний вроде ctrl+, при любой раскладке
_OEM_VK = {
    186: "semicolon",
    187: "equal",
    188: "comma",
    189: "minus",
    190: "period",
    191: "slash",
    192: "backquote",
    219: "bracket_left",
    220: "backslash",
    221: "bracket_right",
    222: "quote",
}


def pynput_key(key: Any) -> tuple[str | None, str | None]:
    """Клавиша pynput → (каноническое имя, напечатанный символ)."""
    name = getattr(key, "name", None)
    if name:  # Key.*
        if name in _SPECIAL:
            return _SPECIAL[name], (" " if name == "space" else None)
        if name.startswith("f") and name[1:].isdigit():
            return name, None
        return None, None
    char = getattr(key, "char", None)
    vk = getattr(key, "vk", None)
    canonical: str | None = None
    if isinstance(vk, int):
        if 65 <= vk <= 90:
            canonical = chr(vk).lower()
        elif 48 <= vk <= 57:
            canonical = chr(vk)
        elif 96 <= vk <= 105:
            canonical = f"num{vk - 96}"
        else:
            canonical = _OEM_VK.get(vk)
    if canonical is None and isinstance(char, str) and len(char) == 1 and char.isascii() and char.isalnum():
        canonical = char.lower()
    printable = char if isinstance(char, str) and len(char) == 1 and char.isprintable() else None
    return canonical, printable


class Recorder:
    """Запись действий для редактора команд: start() / stop() → шаги. Событие record.state
    {recording, count}."""

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._core: ActionRecorder | None = None
        self._listeners: list[Any] = []

    @property
    def recording(self) -> bool:
        return self._core is not None

    def _publish(self, count: int) -> None:
        self.ctx.bus.publish("record.state", {"recording": self.recording, "count": count})

    def start(self) -> None:
        with self._lock:
            if self._core is not None:
                raise UserFacingError("Запись уже идёт")
            try:
                from pynput import keyboard, mouse  # type: ignore[import-untyped]
            except Exception as e:
                raise UserFacingError(f"Запись действий недоступна на этом компьютере: {e}") from None
            core = ActionRecorder(self._publish)
            self._core = core

            def on_press(key: Any) -> None:
                name, char = pynput_key(key)
                if name or char:
                    with self._lock:
                        if self._core is core:
                            core.key_down(name or "", char)

            def on_release(key: Any) -> None:
                name, _ = pynput_key(key)
                if name:
                    with self._lock:
                        if self._core is core:
                            core.key_up(name)

            def on_click(x: int, y: int, button: Any, pressed: bool) -> None:
                if pressed:
                    with self._lock:
                        if self._core is core:
                            core.click(int(x), int(y), getattr(button, "name", "left"))

            def on_scroll(x: int, y: int, dx: int, dy: int) -> None:
                with self._lock:
                    if self._core is core:
                        core.scroll(int(dy))

            try:
                kb = keyboard.Listener(on_press=on_press, on_release=on_release)
                ms = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
                kb.start()
                ms.start()
            except Exception as e:
                self._core = None
                raise UserFacingError(f"Не удалось начать запись: {e}") from None
            self._listeners = [kb, ms]
        log.info("запись действий начата")
        self._publish(0)

    def stop(self) -> list[dict[str, str]]:
        with self._lock:
            core, listeners = self._core, self._listeners
            self._core, self._listeners = None, []
        for listener in listeners:
            try:
                listener.stop()
            except Exception:
                log.exception("слушатель ввода не остановился")
        actions = core.finish() if core is not None else []
        log.info("запись действий остановлена: %d шагов", len(actions))
        self._publish(len(actions))
        return actions
