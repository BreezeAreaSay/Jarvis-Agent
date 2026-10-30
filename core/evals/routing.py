"""Маршрутизация фраз: команда, чат или агент — набор размеченных фраз и проверка.

Голосом ложный запуск — настоящее действие на компьютере: вопрос «выключить компьютер?» не должен
выключать, «найди отели и забронируй» — открывать поиск. Поэтому здесь, кроме обычных команд, много трудных
отрицательных примеров (hard negatives): вопросы, отрицания, составные дела, просьбы о другом, похожие слова.

expect: id команды («builtin.volume.up»; цепочка — через «+»), «ai» — не команда (ответит чат или агент),
«agent» — не команда и сразу агенту (apphints.app_task). Требования: ложных запусков (ai/agent, а сработала
команда) и не тех команд — 0; полнота команд — не ниже MIN_RECALL; у «agent» — app_task.

    uv run python -m evals.routing            # отчёт по набору (платформа-заглушка, без сети)
    uv run pytest tests/evals/test_routing.py
"""

import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

MIN_RECALL = 0.95
MAX_P95_MS = 50.0

# (фраза, ожидание, метки)
CASES: tuple[tuple[str, str, str], ...] = (
    # --- программы ---------------------------------------------------------------------------------------
    ("открой телеграм", "builtin.app.telegram.open", "app"),
    ("запусти стим", "builtin.app.steam.open", "app"),
    ("ну открой хром", "builtin.app.chrome.open", "app colloquial"),
    ("джарвис открой спотифай", "builtin.app.spotify.open", "app wake"),
    ("открой блокнот", "builtin.app.notepad.open", "app"),
    ("закрой телеграм", "builtin.app.telegram.close", "app"),
    ("переключись на спотифай", "builtin.app.spotify.focus", "app"),
    ("закрой его", "builtin.windows.close_last", "app pronoun"),
    # --- звук -------------------------------------------------------------------------------------------
    ("сделай громче", "builtin.volume.up", "volume"),
    ("тише на 20", "builtin.volume.down_by", "volume"),
    ("громкость на пятьдесят", "builtin.volume.set", "volume number-words"),
    ("выключи звук", "builtin.volume.mute", "volume"),
    ("включи звук", "builtin.volume.unmute", "volume"),
    ("какая громкость", "builtin.volume.info", "volume info"),
    ("какая громкость?", "builtin.volume.info", "volume info question-mark"),
    # --- окна -------------------------------------------------------------------------------------------
    ("сверни все окна", "builtin.windows.minimize_all", "windows"),
    ("разверни окно", "builtin.windows.maximize", "windows"),
    ("закрой это окно", "builtin.windows.close", "windows"),
    # --- музыка -----------------------------------------------------------------------------------------
    ("следующий трек", "builtin.media.next", "media"),
    ("пауза", "builtin.media.pause", "media"),
    # --- время, таймеры, напоминания ------------------------------------------------------------------------
    ("сколько времени", "builtin.info.time", "info"),
    ("сколько времени?", "builtin.info.time", "info question-mark"),
    ("какое сегодня число", "builtin.info.date", "info"),
    ("какая погода", "builtin.info.weather", "info"),
    ("поставь таймер на 5 минут", "builtin.timers.minutes", "timer"),
    ("напомни через 10 минут позвонить маме", "builtin.timers.remind_minutes", "reminder"),
    ("напомни завтра в 9 позвонить маме", "builtin.timers.remind_at", "reminder when"),
    ("напомни позвонить маме завтра в 9", "builtin.timers.remind_at", "reminder when"),
    ("напомни мне в пятницу в 18:30 забрать посылку", "builtin.timers.remind_at", "reminder when"),
    ("какие у меня напоминания", "builtin.timers.reminders", "reminder"),
    ("отмени напоминание про посылку", "builtin.timers.reminder_cancel_about", "reminder"),
    # --- система ----------------------------------------------------------------------------------------
    ("сделай скриншот", "builtin.system.screenshot", "system"),
    ("заблокируй компьютер", "builtin.system.lock", "system"),
    ("выключи компьютер", "builtin.system.shutdown", "system danger"),
    ("перезагрузи компьютер", "builtin.system.restart", "system danger"),
    ("очисти корзину", "builtin.system.empty_recycle_bin", "system danger"),
    ("не выключай компьютер", "builtin.system.cancel_shutdown", "system negation"),
    (
        "очисти корзину и выключи компьютер",
        "builtin.system.empty_recycle_bin+builtin.system.shutdown",
        "chain",
    ),
    # --- папки, сайты, поиск -----------------------------------------------------------------------------
    ("открой загрузки", "builtin.folders.downloads", "folder"),
    ("открой ютуб", "builtin.site.youtube", "site"),
    ("найди в гугле рецепт борща", "builtin.search.google", "search"),
    ("найди рецепт борща", "builtin.search.default", "search"),
    # --- клавиатура -------------------------------------------------------------------------------------
    ("скопируй", "builtin.keyboard.copy", "keys"),
    ("вставь", "builtin.keyboard.paste", "keys"),
    ("отмени", "builtin.keyboard.undo", "keys undo"),
    # --- вопросы (не команда) -------------------------------------------------------------------------------
    ("почему браузер постоянно падает?", "ai", "question"),
    ("как открыть папку загрузки?", "ai", "question"),
    ("как запустить телеграм без интернета", "ai", "question"),
    ("открыть браузер?", "ai", "question-mark"),
    ("запустить телеграм?", "ai", "question-mark"),
    ("можешь открыть телеграм?", "ai", "question-mark"),
    ("выключить компьютер?", "ai", "question-mark danger"),
    ("перезагрузить компьютер?", "ai", "question-mark danger"),
    ("закрыть окно?", "ai", "question-mark"),
    ("удалить файл?", "ai", "question-mark danger"),
    ("что такое рабочий стол", "ai", "question"),
    ("что в загрузках нового", "ai", "question"),
    ("сколько места занимают загрузки", "ai", "question"),
    ("какие процессы тормозят систему", "ai", "question"),
    ("как сделать громкость больше на ноутбуке", "ai", "question"),
    ("почему у меня не работает звук", "ai", "question"),
    ("сколько стоит подписка спотифай", "ai", "question"),
    ("что лучше телеграм или вотсап", "ai", "question"),
    ("кто такой джарвис из железного человека", "ai", "question"),
    ("как выключить компьютер через командную строку", "ai", "question danger"),
    ("можно ли удалить папку виндовс", "ai", "question danger"),
    ("зачем нужна корзина", "ai", "question"),
    ("какая сейчас громкость лучше для сна", "ai", "question"),
    ("когда ты выключишь компьютер", "ai", "question danger"),
    # --- отрицания и рассказы (не команда) ------------------------------------------------------------------
    ("не закрывай телеграм", "ai", "negation"),
    ("не надо выключать звук", "ai", "negation"),
    ("не сворачивай окна", "ai", "negation"),
    ("никогда не выключай компьютер ночью", "ai", "negation danger"),
    ("я выключил компьютер вчера и он не включается", "ai", "story danger"),
    ("вчера я открывал телеграм и он завис", "ai", "story"),
    ("мама просила включить ей сериал", "ai", "story"),
    ("когда я закрыл окно все пропало", "ai", "story"),
    ("громкость моего голоса нормальная", "ai", "story"),
    # --- просьбы о другом с похожими словами ----------------------------------------------------------------
    ("выключи меня из этой беседы", "ai", "lookalike"),
    ("закрой вопрос с налоговой", "ai", "lookalike"),
    ("открой мне душу", "ai", "lookalike"),
    ("открой глаза", "ai", "lookalike"),
    ("открой секрет как ты работаешь", "ai", "lookalike"),
    ("отмени подписку на нетфликс", "agent", "lookalike"),
    ("напомни мне что я обещал маме", "ai", "lookalike reminder"),
    ("напомни купить хлеб", "ai", "reminder no-time"),
    ("удали из памяти что я люблю кофе", "ai", "lookalike"),
    ("расскажи анекдот про программистов", "ai", "chat"),
    ("переведи на английский я опаздываю", "ai", "chat"),
    ("launch the missiles", "ai", "en"),
    ("open the pod bay doors", "ai", "en"),
    # --- составные дела (не команда — агенту) ---------------------------------------------------------------
    ("открой телеграм и напиши маме что я опоздаю", "agent", "compound"),
    ("напиши Ане в телеграме что я опоздаю", "agent", "compound message"),
    ("найди отели в сочи и забронируй самый дешёвый", "agent", "compound search"),
    ("найди рецепт борща и открой первый", "agent", "compound search"),
    ("найди все pdf и перешли их Ане", "agent", "compound search"),
    ("найди файл отчёт и отправь его в телеграм", "ai", "compound search"),
    ("выключи компьютер как только закончится загрузка", "ai", "compound danger"),
    ("выключи компьютер если я не отвечу", "ai", "compound danger"),
    ("закрой все программы и выключи компьютер", "ai", "compound danger"),
    ("удали все файлы на рабочем столе", "agent", "danger"),
    ("купи билеты в кино", "agent", "danger"),
    ("что на экране", "agent", "screen"),
    ("прочитай последнее письмо", "agent", "screen"),
)


@dataclass
class RoutingReport:
    total: int = 0
    commands: int = 0  # фраз, где ждём команду
    hits: int = 0  # из них — та самая команда
    false_launches: list[str] = field(default_factory=list)  # ждали чат/агента, а сработала команда
    wrong: list[str] = field(default_factory=list)  # сработала не та команда
    missed: list[str] = field(default_factory=list)  # ждали команду — ушло к ИИ
    agent_mismatch: list[str] = field(default_factory=list)  # ждали агента, а app_task — нет
    p95_ms: float = 0.0
    dataset_sha256: str = ""

    @property
    def recall(self) -> float:
        return self.hits / self.commands if self.commands else 1.0

    @property
    def ok(self) -> bool:
        return (
            not self.false_launches
            and not self.wrong
            and not self.agent_mismatch
            and self.recall >= MIN_RECALL
            and self.p95_ms <= MAX_P95_MS
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total": self.total,
            "recall": round(self.recall, 4),
            "false_launches": self.false_launches,
            "wrong": self.wrong,
            "missed": self.missed,
            "agent_mismatch": self.agent_mismatch,
            "p95_ms": round(self.p95_ms, 2),
            "dataset_sha256": self.dataset_sha256,
        }


def dataset_sha256() -> str:
    """Отпечаток набора: в отчётах видно, на каком наборе получены цифры."""
    return hashlib.sha256(json.dumps(CASES, ensure_ascii=False).encode("utf-8")).hexdigest()


def evaluate(commands: Any, cases: tuple[tuple[str, str, str], ...] = CASES) -> RoutingReport:
    """commands — сервис команд (ctx.commands): match(text) → CommandMatch | None."""
    from jarvis.ai.apphints import app_task

    report = RoutingReport(total=len(cases), dataset_sha256=dataset_sha256())
    timings: list[float] = []
    for text, expect, _tags in cases:
        started = time.perf_counter()
        found = commands.match(text)
        timings.append((time.perf_counter() - started) * 1000)
        got = "+".join(p.command_id for p in found.parts) if found is not None else ""
        if expect in ("ai", "agent"):
            if got:
                report.false_launches.append(f"{text!r} → {got}")
            elif expect == "agent" and not app_task(text):
                report.agent_mismatch.append(repr(text))
            continue
        report.commands += 1
        if got == expect:
            report.hits += 1
        elif got:
            report.wrong.append(f"{text!r} → {got} (ждали {expect})")
        else:
            report.missed.append(f"{text!r} (ждали {expect})")
    timings.sort()
    report.p95_ms = timings[min(len(timings) - 1, int(len(timings) * 0.95))] if timings else 0.0
    return report


def main() -> int:
    import asyncio
    import tempfile

    from jarvis import paths

    paths.set_data_dir(tempfile.mkdtemp(prefix="jarvis-routing-"))
    from jarvis.app import build_context

    ctx = build_context("fake")
    asyncio.run(ctx.commands.refresh_apps())  # type: ignore[attr-defined]
    report = evaluate(ctx.commands)
    for stream in (sys.stdout,):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    print(json.dumps(report.to_json(), ensure_ascii=False, indent=1))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
