"""Распознавание текста на экране — запасной путь агента, когда UI Automation почти ничего не видит в окне:
строки t… в снимке окна (и для моделей без картинок), щелчок мышью по ним, без повторов элементов UI
Automation, окно, скрытое от ИИ, не распознаётся."""

import re
from typing import Any

import httpx
import pytest

from jarvis.ai import ocr
from jarvis.ai.uia import OCR_HEADER, UiElement, UiSnapshot, format_snapshot
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from jarvis.platform.windows.ocr import WindowsOcr, to_lines
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, completion, reply

PLAN = reply('{"steps": ["Нажать «Отправить»"]}')
WINDOW = (300, 80, 1600, 980)
SEND = (1450, 930, 1580, 960)


class FakeOcr:
    """Распознавание по сценарию: строки на экране; отдаёт те, что в куске экрана, который просили."""

    def __init__(self, lines: list[ocr.OcrLine]) -> None:
        self.lines = lines
        self.calls: list[tuple[tuple[int, int], tuple[int, int, int, int]]] = []

    def recognize(
        self, bgra: bytes, size: tuple[int, int], area: tuple[int, int, int, int]
    ) -> list[ocr.OcrLine]:
        assert len(bgra) == size[0] * size[1] * 4
        self.calls.append((size, area))
        return list(self.lines)


@pytest.fixture
def ui(ctx: AppContext) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    agent.pointer_delay_s = 0.0
    fake.window = "Telegram"
    fake.elements = [UiElement("e1", "Поиск", "Edit", (358, 124, 668, 160), value="")]
    platform: Any = ctx.platform
    for window in platform.windows:
        window.foreground = False
    platform.windows.append(WindowInfo(104, "Telegram", "Telegram.exe", 8100, WINDOW, foreground=True))
    return fake


@pytest.fixture
def reader(ctx: AppContext) -> FakeOcr:
    fake = FakeOcr(
        [
            ocr.OcrLine("Поиск", (370, 130, 420, 150)),  # подсказка поля «Поиск» — она же в UI Automation
            ocr.OcrLine("Мама", (310, 200, 600, 230)),
            ocr.OcrLine("Буду в 7", (900, 860, 1000, 880)),
            ocr.OcrLine("Отправить", SEND),
            ocr.OcrLine("12:30", (1700, 100, 1800, 120)),  # вне окна (часы на другом окне) — не его
            ocr.OcrLine("—", (900, 500, 910, 510)),  # без букв и цифр — шум
        ]
    )
    agent: Any = ctx.agent
    agent.ocr = fake
    return fake


def _texts(body: dict[str, Any]) -> str:
    return "\n".join(str(m.get("content")) for m in body["messages"] if isinstance(m.get("content"), str))


def _approve(ctx: AppContext) -> list[str]:
    asked: list[str] = []

    def answer(event: Any) -> None:
        asked.append(event.data["title"])
        ctx.confirmations.resolve(event.data["id"], True)

    ctx.bus.on("confirm.request", answer)
    return asked


async def test_text_only_model_reads_recognized_lines_and_clicks_them(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, reader: FakeOcr
) -> None:
    ctx.settings.patch({"ai": {"vision": "off"}})
    asked = _approve(ctx)
    seen: dict[str, str] = {}

    def click_send(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        text = _texts(body)
        seen["first"] = text
        found = re.search(r"^(t\d+) Text «Отправить» \[1450,930,130x30\]$", text, re.M)
        assert found is not None, text
        return httpx.Response(200, json=completion(calls(("ui_click", {"id": found.group(1)}))))

    def finish(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        seen["after"] = _texts(body)
        return httpx.Response(200, json=completion(calls(("finish", {"status": "done", "summary": "Ушло."}))))

    llm.add(PLAN, click_send, finish, calls(("finish", {"status": "done", "summary": "Ушло."})))
    agent: Any = ctx.agent
    result = await agent.wait(await agent.run("Нажми «Отправить» в Telegram"))
    assert result.status == "done" and result.outcome == "done"
    first = seen["first"]
    assert OCR_HEADER in first
    assert "Text «Мама» [310,200,290x30]" in first and "«Буду в 7»" in first
    assert "«12:30»" not in first  # строка за пределами окна
    assert not re.search(r"^t\d+ Text «Поиск»", first, re.M)  # повтор поля UI Automation
    assert "Text «—»" not in first
    # распознаётся только окно — снятое отдельно (картинок нет), с пикселями заглушки (в 6 раз мельче)
    assert reader.calls[0] == ((216, 150), WINDOW)
    platform: Any = ctx.platform
    assert ("screen_pixels", (WINDOW,)) in platform.calls
    # щелчок мышью — в середину строки, с разрешения (это «Отправить»); курсор человека — обратно
    assert asked and "«Отправить»" in asked[0]
    click = platform.calls.index(("mouse_click", (1515, 945, "left", False)))
    assert platform.calls[click + 1][0] == "mouse_move"
    # id строк постоянные: в следующем снимке та же строка — тот же t…
    t_send = re.search(r"^(t\d+) Text «Отправить»", first, re.M)
    assert t_send is not None and f"{t_send.group(1)} Text «Отправить»" in seen["after"]
    # сценарий повторит щелчок точкой в окне
    final = ctx.ai.get_chat(agent.current.chat_id)["messages"][-1]  # type: ignore[attr-defined]
    actions = final["meta"]["recipe"]["actions"]
    assert actions == [
        {"type": "Mouse.MoveInWindow", "value": actions[0]["value"]},
        {"type": "Mouse.ClickLeft", "value": ""},
    ]


async def test_picture_coordinates_and_no_labels_for_lines(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, reader: FakeOcr
) -> None:
    """Модель с картинками: окно вырезается из уже снятого монитора, координаты строк — точки картинки."""
    ctx.settings.patch({"ai": {"vision": "on"}})
    llm.add(PLAN, calls(("finish", {"status": "partial", "summary": "Посмотрел."})))
    agent: Any = ctx.agent
    await agent.wait(await agent.run("Посмотри Telegram"))
    platform: Any = ctx.platform
    # экран снят один раз (монитор), окно для распознавания — из тех же пикселей
    assert [c for c in platform.calls if c[0] == "screen_pixels"] == [
        ("screen_pixels", ((0, 0, 1920, 1080),))
    ]
    # окно на пикселях монитора (в 6 раз мельче): 217×150 — это экран (300, 78)–(1602, 978)
    assert reader.calls == [((217, 150), (300, 78, 1602, 978))]
    first = _texts(llm.requests[1])
    # картинка 320×180 на экран 1920×1080: «Отправить» (1450,930)–(1580,960) → (242,155) 22×5
    assert re.search(r"^t\d+ Text «Отправить» \[242,155,21x5\]$", first, re.M), first


async def test_window_seen_by_ui_automation_is_not_recognized(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, reader: FakeOcr
) -> None:
    ctx.settings.patch({"ai": {"vision": "off"}})
    ui.window = "Почта"
    ui.elements = [
        UiElement(f"e{i}", f"Кнопка {i}", "Button", (310, 100 + 40 * i, 400, 130 + 40 * i)) for i in range(8)
    ]
    platform: Any = ctx.platform
    platform.windows[-1].exe = "HxOutlook.exe"
    llm.add(PLAN, calls(("finish", {"status": "partial", "summary": "Посмотрел."})))
    agent: Any = ctx.agent
    await agent.wait(await agent.run("Посмотри почту"))
    assert reader.calls == []
    assert OCR_HEADER not in _texts(llm.requests[1])


async def test_private_window_over_the_task_window_blocks_recognition(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, reader: FakeOcr
) -> None:
    ctx.settings.patch({"ai": {"vision": "off"}})
    platform: Any = ctx.platform
    platform.windows.insert(
        0, WindowInfo(105, "Пароли.kdbx - KeePass", "KeePass.exe", 9100, (500, 300, 900, 700))
    )
    llm.add(PLAN, calls(("finish", {"status": "partial", "summary": "Посмотрел."})))
    agent: Any = ctx.agent
    await agent.wait(await agent.run("Посмотри Telegram"))
    assert reader.calls == []
    assert "«Мама»" not in _texts(llm.requests[1])


async def test_text_line_cannot_be_typed_into_and_old_lines_are_gone(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, reader: FakeOcr
) -> None:
    ctx.settings.patch({"ai": {"vision": "off"}})
    llm.add(
        PLAN,
        calls(("ui_type", {"id": "t2", "text": "привет"})),
        calls(("ui_click", {"id": "t99"})),
        calls(("finish", {"status": "partial", "summary": "Не вышло."})),
    )
    agent: Any = ctx.agent
    await agent.wait(await agent.run("Напиши Маме"))
    answers = [
        str(m.get("content"))
        for m in llm.requests[-1]["messages"]
        if m.get("role") == "tool" and str(m.get("content")).startswith("Ошибка")
    ]
    assert "текст, распознанный на картинке, а не поле" in answers[0]
    assert "строки t99 сейчас нет в окне" in answers[1]
    platform: Any = ctx.platform
    assert not any(c[0] == "mouse_click" for c in platform.calls)


async def test_click_follows_the_window_moved_after_the_snapshot(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, reader: FakeOcr
) -> None:
    ctx.settings.patch({"ai": {"vision": "off"}})
    _approve(ctx)
    platform: Any = ctx.platform
    window = platform.windows[-1]

    def move_and_click(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        found = re.search(r"^(t\d+) Text «Мама»", _texts(body), re.M)
        assert found is not None
        window.rect = (WINDOW[0] + 100, WINDOW[1] + 50, WINDOW[2] + 100, WINDOW[3] + 50)  # окно сдвинули
        return httpx.Response(200, json=completion(calls(("ui_click", {"id": found.group(1)}))))

    llm.add(PLAN, move_and_click, calls(("finish", {"status": "partial", "summary": "Открыл."})))
    agent: Any = ctx.agent
    await agent.wait(await agent.run("Открой чат «Мама»"))
    assert ("mouse_click", (455 + 100, 215 + 50, "left", False)) in platform.calls


def test_wants_ocr_crop_and_lines() -> None:
    chrome = [UiElement("e1", n, "Button", (0, 0, 10, 10)) for n in ("Свернуть", "Развернуть", "Закрыть")]
    few = UiSnapshot("Окно", "app.exe", [*chrome, UiElement("e4", "Поиск", "Edit", (0, 0, 50, 20))])
    assert ocr.named_elements(few) == 1 and ocr.wants_ocr(few, "app.exe") and ocr.wants_ocr(None, "app.exe")
    rich = UiSnapshot("Окно", "app.exe", [UiElement(f"e{i}", f"Кнопка {i}", "Button") for i in range(6)])
    assert not ocr.wants_ocr(rich, "app.exe") and ocr.wants_ocr(rich, "Telegram.exe")
    # кусок экрана из пикселей монитора: 2 точки снимка на точку экрана
    area, size = (100, 100, 200, 200), (200, 200)
    bgra = bytes(i % 251 for i in range(size[0] * size[1] * 4))
    piece = ocr.crop(bgra, size, area, (150, 120, 400, 400))
    assert piece is not None
    data, (w, h), shown = piece
    assert (w, h) == (100, 160) and shown == (150, 120, 200, 200) and len(data) == w * h * 4
    first = (40 * 200 + 100) * 4
    assert data[:4] == bgra[first : first + 4]  # первый пиксель куска — строка 40, столбец 100
    last = (199 * 200 + 199) * 4
    assert data[-4:] == bgra[last : last + 4]
    assert ocr.crop(bgra, size, area, (190, 140, 400, 400)) is None  # мелкий кусок
    assert ocr.clip((0, 0, 50, 50), (0, 0, 1920, 1080)) == (0, 0, 50, 50)
    # строки: без повторов поля, по порядку сверху вниз, не больше MAX_LINES
    snapshot = UiSnapshot("Окно", "app.exe", [UiElement("e1", "Поле", "Edit", (0, 0, 200, 40))])
    lines = [
        ocr.OcrLine(f"строка {i}", (10, 100 + 20 * i, 300, 115 + 20 * i)) for i in range(ocr.MAX_LINES + 5)
    ]
    found, cut = ocr.to_elements(
        [ocr.OcrLine("в поле", (10, 10, 100, 30)), *reversed(lines)], snapshot, (0, 0, 800, 4000)
    )
    assert cut and len(found) == ocr.MAX_LINES and found[0].name == "строка 0" and all(e.ocr for e in found)


def test_format_lists_recognized_lines_after_elements() -> None:
    snapshot = UiSnapshot(
        "Telegram",
        "Telegram.exe",
        [
            UiElement("e1", "Поиск", "Edit", (0, 0, 100, 20)),
            UiElement("t1", "Мама", "Text", (0, 40, 100, 60), ocr=True),
        ],
        ocr_truncated=True,
    )
    lines = format_snapshot(snapshot).splitlines()
    assert lines[1].startswith("e1 Edit «Поиск»")
    assert lines[2].startswith(OCR_HEADER) and lines[3] == "t1 Text «Мама» [0,40,100x20]"
    assert lines[4] == "(показаны не все строки текста)"
    only = format_snapshot(
        UiSnapshot("Игра", "game.exe", [UiElement("t1", "Начать", "Text", (0, 0, 9, 9), ocr=True)])
    )
    assert "Интерактивных элементов не найдено." in only and "t1 Text «Начать»" in only


def test_windows_lines_from_words_and_pause_after_failure() -> None:
    raw = [
        ("Привет,  мир", [(10.0, 20.0, 40.0, 12.0), (55.0, 21.0, 30.0, 12.0)]),
        ("", [(0.0, 0.0, 5.0, 5.0)]),
        ("без слов", []),
    ]
    # картинка 200×100 — кусок экрана (1000, 500)–(1400, 700): точка картинки = 2 точки экрана
    lines = to_lines(raw, (200, 100), (1000, 500, 1400, 700))
    assert lines == [ocr.OcrLine("Привет, мир", (1020, 540, 1170, 566))]

    class Broken:
        calls = 0

        def call(self, fn: Any, timeout: float) -> Any:
            Broken.calls += 1
            raise TimeoutError("завис")

    backend = WindowsOcr(Broken())  # type: ignore[arg-type]
    assert backend.recognize(b"\0" * 16, (2, 2), (0, 0, 2, 2)) == []
    assert backend.recognize(b"\0" * 16, (2, 2), (0, 0, 2, 2)) == []  # пауза: поток WinRT не дёргаем
    assert Broken.calls == 1
    assert isinstance(ocr.create_ocr(object()), ocr.NoOcr)
