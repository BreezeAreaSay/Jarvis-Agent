"""1.1 и 1.2 на настоящей Windows: агент Jarvis с моделью по сценарию (без сети) щёлкает элемент окна так,
как модель прочла его в тексте снимка, — и попадает ли щелчок в элемент.

- строка e… и картинки (--vision): click_point по середине координат из текста снимка — они в точках картинки
  (1.2), агент переводит их в точки экрана сам;
- строка t… (распознанный текст, 1.1): ui_click по t… — щелчок мышью в середину строки.

    uv run python ..\\docs\\checks\\stage1\\agent_click.py --exe notepad.exe --name Файл --vision
    uv run python ..\\docs\\checks\\stage1\\agent_click.py --exe Telegram.exe --name Поиск --only-text

Окно выходит вперёд, мышь двигается и щёлкает — запускать только с разрешения человека и не трогать мышь,
пока идёт проверка. Разрешения агента («Разрешить?») скрипт отклоняет: ничего рискованного не нажмётся. После
щелчка — Esc (закрыть меню).
"""

import argparse
import asyncio
import re
import time
from typing import Any

import _common
import httpx

from jarvis.app import build_context
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, completion, no_sleep, reply

LINE = r"^({prefix}\d+) (\w+) «{name}»(?: = «.*?»)? \[(-?\d+),(-?\d+),(\d+)x(\d+)\]"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    ctx: Any = build_context(args.platform)
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    if args.platform == "fake":  # сухой прогон скрипта на Linux
        ctx.agent.ui = FakeUi()
        ctx.agent.settle_s = 0.0
    ctx.settings.patch({"ai": {"vision": "on" if args.vision else "off"}})
    ctx.secrets.set("ai_api_key", "sk-check")
    llm = ScriptedLlm()
    ctx.ai.set_transport(llm.transport)
    ctx.ai.llm.sleep = no_sleep
    pointer: list[dict[str, Any]] = []
    ctx.bus.on("agent.pointer", lambda event: pointer.append(dict(event.data)))
    ctx.bus.on("confirm.request", lambda event: ctx.confirmations.resolve(event.data["id"], False))
    found: dict[str, Any] = {}

    def act(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        text = "\n".join(str(m.get("content")) for m in body["messages"] if isinstance(m.get("content"), str))
        snap = text[text.rfind("[Снимок окна]") :]
        found["e_строк"] = len(re.findall(r"^e\d+ ", snap, re.M))
        found["t_строк"] = len(re.findall(r"^t\d+ ", snap, re.M))
        found["шапка_координат"] = next((s for s in snap.splitlines() if s.startswith("Координаты")), "")
        prefix = "t" if args.only_text else "[et]"
        match = re.search(LINE.format(prefix=prefix, name=re.escape(args.name)), snap, re.M)
        if match is None:
            found["ошибка"] = f"в снимке нет строки «{args.name}»"
            spec = calls(("finish", {"status": "failed", "summary": "Нет элемента."}))
            return httpx.Response(200, json=completion(spec))
        element_id = match.group(1)
        x, y, w, h = (int(v) for v in match.groups()[2:])
        element = ctx.agent.current.snapshot.find(element_id)
        found["строка_снимка"] = match.group(0)
        found["элемент_на_экране"] = element.rect if element is not None else None
        if element_id.startswith("e") and args.vision:
            call = ("click_point", {"x": x + w // 2, "y": y + h // 2, "what": args.name})
        else:
            call = ("ui_click", {"id": element_id})
        found["действие"] = call
        return httpx.Response(200, json=completion(calls(call)))

    llm.add(
        reply('{"steps": ["Нажать"]}'),
        act,
        calls(("finish", {"status": "partial", "summary": "Проверка."})),
    )
    platform = ctx.platform
    window = _common.window_by_exe(platform, args.exe) if args.platform != "fake" else None
    if window is not None:
        platform.window_action("focus", window.hwnd)
        await asyncio.sleep(0.5)
    started = time.monotonic()
    result = await ctx.agent.wait(await ctx.agent.run(f"Нажми «{args.name}»"))
    found["секунд"] = round(time.monotonic() - started, 2)
    found["итог_агента"] = result.status
    clicks = [p for p in pointer if p.get("kind") in ("click", "double")]
    if clicks:
        point = (clicks[-1]["x"], clicks[-1]["y"])
        found["щелчок_экран"] = point
        rect = found.get("элемент_на_экране")
        if rect:
            found["попал"] = rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]
    if window is not None:
        found["dpi"] = _common.dpi(window.hwnd)
        await asyncio.sleep(0.5)
        platform.press_keys("esc")
    ctx.db.close()
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default="notepad.exe")
    parser.add_argument("--name", default="Файл")
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--only-text", action="store_true", help="только строка t… (распознанный текст)")
    parser.add_argument("--platform", default="windows")
    _common.report(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
