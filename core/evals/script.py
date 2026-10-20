"""Детерминированный режим: модель по сценарию (tests.ai.fakes.ScriptedLlm) играет эталонное решение задачи
или нарочно ошибочное — так проверяются сам имитатор и проверки задач, без сети.

Решение — шаги: каждый по запросу агента к модели отдаёт вызовы инструментов. Шаги называют элементы, как
человек: click("Написать") находит id элемента «Написать» в том снимке окна, который агент прислал модели
(нет его там — шаг падает: значит, снимок не показывает то, что должен); point("Мама") щёлкает по картинке
экрана туда, где виджет «Мама» нарисован сейчас; zoom_on(…) и point(…, zoom=True) — лупа. Проверку перед
итогом («Проверка перед итогом…») водитель проходит сам: повторяет finish.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from evals.sim.desktop import Desktop
from evals.sim.world import SCREEN, Widget, Window, norm
from tests.ai.fakes import ScriptedLlm, calls, completion, reply

Call = tuple[str, dict[str, Any]]
SNAPSHOT = "[Снимок окна]"
# строка снимка (jarvis.ai.uia.format_snapshot): «e3 Button «Написать» = «значение» [x,y,w×h] фокус»; в имени
# бывают свои кавычки («Бухгалтерия ООО «Ромашка», Счёт…»)
ELEMENT = re.compile(r"^(e\d+) (\w+) (?:«(.*?)»(?: = «.*?»)?|\(без имени\)) \[-?\d", re.M)
SCREEN_CAPTION = re.compile(r"Снимок экрана — (\d+)×(\d+) точек")
ZOOM_CAPTION = re.compile(r"Лупа: кусок экрана (\d+)×(\d+) точек, ×[\d.]+ — картинка (\d+)×(\d+)")


class ScriptError(AssertionError):
    """Шаг эталонного решения не смог выполниться: экран не такой, как ожидали."""


@dataclass
class View:
    """Что видит «модель» на этом запросе: сообщения агента и (для щелчков по картинке) мир."""

    messages: list[dict[str, Any]]
    desktop: Desktop
    state: dict[str, Any]

    def snapshot(self) -> str:
        for message in reversed(self.messages):
            content = message.get("content")
            if isinstance(content, str) and SNAPSHOT in content:
                return content[content.rfind(SNAPSHOT) :]
        raise ScriptError("агент не прислал снимок окна")

    def element(self, name: str, kind: str = "") -> str:
        """id элемента из последнего снимка: имя целиком, потом — начало имени."""
        found = [(m.group(1), m.group(2), m.group(3) or "") for m in ELEMENT.finditer(self.snapshot())]
        want = norm(name)
        for test in (lambda n: n == want, lambda n: n.startswith(want)):
            for element_id, element_kind, element_name in found:
                if (not kind or element_kind == kind) and test(norm(element_name)):
                    return element_id
        raise ScriptError(f"в снимке окна нет элемента «{name}»: {[f[2] for f in found]}")

    def last_tool(self) -> str:
        for message in reversed(self.messages):
            if message.get("role") == "tool":
                return str(message.get("content") or "")
            if message.get("role") == "assistant":
                return ""
        return ""

    def picture(self) -> tuple[int, int]:
        """Размер последней картинки экрана (не лупы)."""
        for message in reversed(self.messages):
            content = message.get("content")
            for part in content if isinstance(content, list) else []:
                found = SCREEN_CAPTION.search(str(part.get("text", ""))) if isinstance(part, dict) else None
                if found:
                    return int(found.group(1)), int(found.group(2))
        raise ScriptError("агент не прислал картинку экрана")

    def zoom_picture(self) -> tuple[int, int]:
        for message in reversed(self.messages):
            content = message.get("content")
            for part in content if isinstance(content, list) else []:
                found = ZOOM_CAPTION.search(str(part.get("text", ""))) if isinstance(part, dict) else None
                if found:
                    return int(found.group(3)), int(found.group(4))
        raise ScriptError("агент не прислал картинку лупы")

    def widget(self, name: str) -> tuple[Window, Widget]:
        """Виджет мира по имени, подписи или ключу — в верхнем окне, где он есть."""
        world = self.desktop.world
        with world.lock:
            want = norm(name)
            for window in reversed(world.windows):
                for widget in window.widgets():
                    if want in (norm(widget.name), norm(widget.text), norm(widget.key)):
                        return window, widget
        raise ScriptError(f"на экране нет «{name}»")

    def center(self, name: str) -> tuple[int, int]:
        window, widget = self.widget(name)
        left, top, right, bottom = window.screen_rect(widget.rect)
        return (left + right) // 2, (top + bottom) // 2


Step = Callable[[View], list[Call]]


def call(tool: str, /, **args: Any) -> Step:
    return lambda view: [(tool, dict(args))]


def click(name: str, *, kind: str = "", double: bool = False) -> Step:
    def step(view: View) -> list[Call]:
        args: dict[str, Any] = {"id": view.element(name, kind)}
        if double:
            args["double"] = True
        return [("ui_click", args)]

    return step


def type_into(name: str, text: str, *, kind: str = "") -> Step:
    return lambda view: [("ui_type", {"id": view.element(name, kind), "text": text})]


def keys(combo: str) -> Step:
    return call("press_keys", combo=combo)


def type_text(text: str, field_name: str = "") -> Step:
    return call("type_text", text=text, **({"field": field_name} if field_name else {}))


def point(name: str, *, double: bool = False, zoom: bool = False, what: str = "") -> Step:
    """click_point туда, где виджет name нарисован сейчас (по картинке экрана или лупы)."""

    def step(view: View) -> list[Call]:
        sx, sy = view.center(name)
        if zoom:
            left, top, right, bottom = view.state["zoom"]
            width, height = view.zoom_picture()
            x, y = (sx - left) * width / (right - left), (sy - top) * height / (bottom - top)
        else:
            width, height = view.picture()
            x, y = sx * width / SCREEN[2], sy * height / SCREEN[3]
        args: dict[str, Any] = {"x": round(x), "y": round(y), "what": what or name}
        if double:
            args["double"] = True
        return [("click_point", args)]

    return step


def zoom_on(*names: str, pad: int = 12) -> Step:
    """Лупа на кусок экрана, где нарисованы виджеты names (координаты — картинки экрана)."""

    def step(view: View) -> list[Call]:
        width, height = view.picture()
        boxes = []
        for name in names:
            window, widget = view.widget(name)
            boxes.append(window.screen_rect(widget.rect))
        left = min(b[0] for b in boxes) - pad
        top = min(b[1] for b in boxes) - pad
        right = max(b[2] for b in boxes) + pad
        bottom = max(b[3] for b in boxes) + pad
        kx, ky = width / SCREEN[2], height / SCREEN[3]
        x, y, w, h = round(left * kx), round(top * ky), round((right - left) * kx), round((bottom - top) * ky)
        # так же, как агент переводит кусок картинки в точки экрана (Shot.to_screen)
        view.state["zoom"] = (
            round(x / kx),
            round(y / ky),
            round(min(x + w, width) / kx),
            round(min(y + h, height) / ky),
        )
        return [("zoom", {"x": x, "y": y, "width": w, "height": h})]

    return step


def finish(summary: str = "Готово.", status: str = "done") -> Step:
    return call("finish", status=status, summary=summary)


def together(*steps: Step) -> Step:
    """Несколько вызовов одним ответом модели."""
    return lambda view: [c for step in steps for c in step(view)]


@dataclass
class Driver:
    """Отвечает агенту шагами решения; проверку перед итогом проходит, повторяя finish."""

    desktop: Desktop
    steps: list[Step]
    model: str = "scripted"
    state: dict[str, Any] = field(default_factory=dict)
    done: int = 0  # сколько шагов отыграно
    finished: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)

    def respond(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        view = View(body.get("messages") or [], self.desktop, self.state)
        if self.finished is not None and view.last_tool().startswith("Проверка перед итогом"):
            items: list[Call] = [("finish", dict(self.finished))]
        elif self.done >= len(self.steps):
            items = [("finish", {"status": "partial", "summary": "Сценарий решения закончился."})]
        else:
            step = self.steps[self.done]
            self.done += 1
            try:
                items = step(view)
            except ScriptError as e:
                self.errors.append(f"шаг {self.done}: {e}")
                items = [("finish", {"status": "failed", "summary": f"Сценарий не выполнился: {e}"})]
        for name, args in items:
            if name == "finish":
                self.finished = args
        spec = calls(*items)
        return httpx.Response(200, json=completion(spec, self.model))


def scripted_llm(desktop: Desktop, steps: list[Step], plan: list[str]) -> tuple[ScriptedLlm, Driver]:
    driver = Driver(desktop, steps)
    llm = ScriptedLlm(reply(json.dumps({"steps": plan}, ensure_ascii=False)))
    llm.add(*[driver.respond] * (2 * len(steps) + 20))
    return llm, driver
