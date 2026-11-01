"""Одна система координат: в тексте снимка окна координаты элементов — точки той картинки, что модель видит
рядом (по ним же click_point); в точки экрана они переводятся только при щелчке."""

import re
from typing import Any

import httpx
import pytest

from jarvis.ai import vision
from jarvis.ai.uia import UiElement, UiSnapshot, format_snapshot
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, completion, reply

PLAN = reply('{"steps": ["Нажать «Отправить»"]}')
ELEMENT = re.compile(r"^e\d+ Button «Отправить» \[(-?\d+),(-?\d+),(\d+)x(\d+)\]", re.M)


@pytest.fixture
def ui(ctx: AppContext) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    agent.pointer_delay_s = 0.0
    return fake


def _wide_screen(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Один монитор 2560×1440 (шире MAX_SIDE), пиксели — в полном разрешении, как на Windows; впереди окно
    программы почти во весь экран."""
    platform: Any = ctx.platform
    monkeypatch.setattr(platform, "virtual_screen", lambda: (0, 0, 2560, 1440))
    monkeypatch.setattr(platform, "monitor_rect", lambda hwnd: (0, 0, 2560, 1440))
    monkeypatch.setattr(platform, "monitors", lambda: [(0, 0, 2560, 1440)])

    def pixels(rect: tuple[int, int, int, int] | None = None) -> tuple[bytes, int, int]:
        platform.calls.append(("screen_pixels", (rect,)))
        left, top, right, bottom = rect or (0, 0, 2560, 1440)
        return bytes([40, 40, 40, 255]) * ((right - left) * (bottom - top)), right - left, bottom - top

    monkeypatch.setattr(platform, "screen_pixels", pixels)
    for window in platform.windows:
        window.foreground = False
    platform.windows.append(
        WindowInfo(104, "Рабочий чат", "TeamChat.exe", 8100, (0, 0, 2560, 1400), foreground=True)
    )
    return platform


async def test_snapshot_coordinates_are_picture_points_and_click_point_hits(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    platform = _wide_screen(ctx, monkeypatch)
    ui.window = "Рабочий чат"
    ui.elements = [UiElement("e1", "Отправить", "Button", (2300, 700, 2400, 740))]
    seen: dict[str, Any] = {}
    ctx.bus.on("confirm.request", lambda event: ctx.confirmations.resolve(event.data["id"], True))

    def click_from_text(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        """Модель жмёт по центру элемента так, как он записан в тексте снимка."""
        text = "\n".join(str(m.get("content")) for m in body["messages"] if isinstance(m.get("content"), str))
        found = ELEMENT.search(text)
        assert found is not None, text
        x, y, w, h = (int(v) for v in found.groups())
        seen["rect"] = (x, y, w, h)
        seen["text"] = text
        spec = calls(("click_point", {"x": x + w // 2, "y": y + h // 2, "what": "кнопка «Отправить»"}))
        return httpx.Response(200, json=completion(spec))

    llm.add(
        PLAN,
        click_from_text,
        calls(("finish", {"status": "partial", "summary": "Нажал «Отправить»."})),
    )
    result = await ctx.agent.wait(await ctx.agent.run("Нажми «Отправить»"))  # type: ignore[attr-defined]
    assert result.status == "done", result
    # картинка экрана — 1568×882 (2560 точек сжаты до MAX_SIDE): координаты в тексте — её точки
    k = vision.MAX_SIDE / 2560
    assert seen["rect"] == (round(2300 * k), round(700 * k), round(2400 * k) - round(2300 * k), 24)
    assert "Координаты [x,y,ш×в] — точки картинки экрана 1568×882" in seen["text"]
    # щелчок по ним — внутри элемента на экране
    (click,) = [args for name, args in platform.calls if name == "mouse_click"]
    assert 2300 <= click[0] < 2400 and 700 <= click[1] < 740


async def test_without_pictures_coordinates_stay_screen_points(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Модель без картинок click_point не получает: координаты в тексте — точки экрана, как раньше."""
    ctx.settings.patch({"ai": {"vision": "off"}})
    _wide_screen(ctx, monkeypatch)
    ui.elements = [UiElement("e1", "Отправить", "Button", (2300, 700, 2400, 740))]
    llm.add(PLAN, calls(("finish", {"status": "partial", "summary": "Посмотрел."})))
    await ctx.agent.wait(await ctx.agent.run("Посмотри окно"))  # type: ignore[attr-defined]
    first = "\n".join(str(m.get("content")) for m in llm.requests[1]["messages"])
    found = ELEMENT.search(first)
    assert found is not None and found.groups() == ("2300", "700", "100", "40")
    assert "Координаты [x,y" not in first


def test_format_snapshot_maps_rectangles_to_the_picture() -> None:
    shot = vision.Shot("", 1568, 882, (0, 0, 2560, 1440))
    snapshot = UiSnapshot("Окно", "app.exe", [UiElement("e1", "Кнопка", "Button", (1280, 720, 1380, 760))])
    text = format_snapshot(snapshot, shot.to_image, shot.coords_note())
    assert "e1 Button «Кнопка» [784,441,61x25]" in text
    assert text.splitlines()[1].startswith("Координаты [x,y,ш×в] — точки картинки экрана 1568×882")
    # картинка окна (screenshot): точки — от левого верхнего угла окна
    window = vision.Shot("", 800, 600, (1000, 500, 1800, 1100), hwnd=7)
    text = format_snapshot(snapshot, window.to_image, window.coords_note())
    assert "e1 Button «Кнопка» [280,220,100x40]" in text and "точки картинки окна 800×600" in text
    assert format_snapshot(snapshot).count("[1280,720,100x40]") == 1
