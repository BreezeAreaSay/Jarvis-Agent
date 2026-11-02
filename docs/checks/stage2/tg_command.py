"""Этап 2: голосовые команды Telegram без ИИ на настоящем Telegram Desktop — та же команда, что сказать
голосом, без модели и ключей.

    cd C:\\jarvis-check\\Jarvis-Agent\\core
    uv run python ..\\docs\\checks\\stage2\\tg_command.py --phrase "напиши избранному в телеге привет" --allow
    uv run python ..\\docs\\checks\\stage2\\tg_command.py --phrase "напиши избранному в телеге отказ" --deny
    uv run python ..\\docs\\checks\\stage2\\tg_command.py --phrase "прочитай избранное в телеграме"

Окно Telegram выходит вперёд, мышь щёлкает, клавиатура печатает — только с разрешения человека, и мышь в это
время не трогать. Отправить скрипт разрешает ТОЛЬКО в «Избранное» (Saved Messages): вопрос об отправке в любой
другой чат он отклоняет сам, что бы ни было в --allow. Вопрос «какой чат?» — без ответа (ошибка команды).
Прочитанное (сообщения «Избранного») печатается только с --show и с согласия человека.

--platform sim — сухой прогон на имитаторе (Linux, без Windows и Telegram).
"""

import argparse
import asyncio
import re
import time
from typing import Any

import _common

from jarvis.app import build_context

SAVED = re.compile(r"«(Избранное|Saved Messages)»")


def _sim(ctx: Any) -> None:
    from evals.sim.desktop import Desktop
    from evals.sim.platform import SimPlatform
    from evals.sim.ui import SimOcr, SimToasts, SimUi

    desktop = Desktop.build()
    desktop.telegram.open_main()
    world = desktop.world
    ctx.platform = SimPlatform(world)
    agent = ctx.agent
    agent.ui, agent.ocr, agent.toast_source = SimUi(world), SimOcr(world), SimToasts(world)
    agent.settle_s = agent.launch_settle_s = agent.pointer_delay_s = 0.0


async def run(args: argparse.Namespace) -> dict[str, Any]:
    ctx: Any = build_context("fake" if args.platform == "sim" else args.platform)
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    if args.platform == "sim":
        _sim(ctx)
    ctx.settings.patch({"ai": {"vision": "off"}, "voice": {"silent_mode": True}})
    await ctx.commands.refresh_apps(True)
    asked: list[dict[str, Any]] = []
    steps: list[str] = []

    def decide(event: Any) -> None:
        title = str(event.data.get("title") or "")
        allow = args.allow and bool(SAVED.search(title))  # отправлять можно только в «Избранное»
        asked.append({"вопрос": title, "разрешено": allow})
        ctx.confirmations.resolve(event.data["id"], allow)

    ctx.bus.on("confirm.request", decide)

    def step(event: Any) -> None:
        if event.data.get("status") == "ok":
            steps.append(str(event.data.get("title")))

    ctx.bus.on("agent.step", step)
    match = ctx.commands.match(args.phrase)
    data: dict[str, Any] = {
        "фраза": args.phrase,
        "команда": None if match is None else [(p.command_id, p.slots) for p in match.parts],
    }
    started = time.monotonic()
    result = await ctx.commands.run_text(args.phrase, "voice")
    data["секунд"] = round(time.monotonic() - started, 2)
    data["вопросы"] = asked
    data["шаги"] = steps
    if result is None:
        data["итог"] = "не команда"
    else:
        data["итог"] = "ok" if result.ok else "ошибка"
        reading = any(cid.endswith("telegram.read") for cid in result.command_ids)
        message = result.message
        if reading and not args.show:
            message = f"(прочитано {len(message)} знаков — текст только с --show)"
        data["ответ"] = message
    ctx.db.close()
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phrase", required=True)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--allow", action="store_true", help="разрешить отправку (только в «Избранное»)")
    choice.add_argument("--deny", action="store_true", help="отклонить отправку")
    parser.add_argument("--show", action="store_true", help="напечатать прочитанное (личное!)")
    parser.add_argument("--platform", default="windows", choices=("windows", "sim"))
    _common.report(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
