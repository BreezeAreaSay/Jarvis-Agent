"""Разведка мозга (S5): Codex app-server с теми же overrides и CODEX_HOME, что у jarvis.brain.Brain.

Запускает только владелец на своём ПК: пишет в каталог данных, отправляет в облако список окон, тратит квоту.
    uv run python scripts\\brain_probe.py [модель]
Что делает (один процесс codex):
  1. старт Codex: overrides из jarvis.brain.build_overrides, CODEX_HOME = <data>\\codex-home,
     cwd = <data>\\brain;
  2. thread_start с теми же параметрами, что у Brain (deny_all, read_only, ephemeral);
  3. ход «Привет» — печатает repr каждого события; ход «какие окна сейчас открыты?» — должен вызвать pc;
  4. тёплый ход в том же треде;
  5. второй тред: первый ход сразу после thread_start (холодный pc.mcp), третий тред — после 5 с прогрева
     (так работает запасной тред Brain). Итог — t_first_token каждого хода.
Глобальные уведомления (статус MCP pc, configWarning) печатаются с пометкой [глобально].
В репозиторий и «Заметки» из вывода переносить только числа (в событиях — заголовки ваших окон).
"""

import sys
import threading
import time
from typing import Any

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
from openai_codex.types import ReasoningEffort

from jarvis import config
from jarvis.brain import DEVELOPER_INSTRUCTIONS, brain_cwd, build_overrides, codex_env, codex_home
from pc import settings
from pc.confirm_client import ConfirmServer

if sys.stdout is not None and not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

T0 = time.perf_counter()


def ask_console(summary: str, details: str, caller: str) -> bool:
    """Подтверждение действия pc из MCP-процесса — вопрос в консоли (по умолчанию «нет»)."""
    print(f"\n[подтверждение, {caller}] {summary}\n{details}")
    try:
        return input("Разрешить? (y/N): ").strip().casefold() in ("y", "yes", "д", "да")
    except EOFError:
        return False


def pump(codex: Codex) -> None:
    """Глобальные уведомления: mcpServer/startupStatus/updated, configWarning и прочие."""
    while True:
        try:
            note = codex._client.next_notification()
        except Exception as e:
            print(f"[глобально] поток уведомлений закрыт: {type(e).__name__}")
            return
        print(f"[глобально +{time.perf_counter() - T0:.2f} с] {note.method}: {note.payload!r}")


def start_thread(codex: Codex, model: str) -> Any:
    t0 = time.perf_counter()
    thread = codex.thread_start(
        approval_mode=ApprovalMode.deny_all,
        sandbox=Sandbox.read_only,
        ephemeral=True,
        developer_instructions=DEVELOPER_INSTRUCTIONS,
        model=model,
    )
    print(f"thread_start {time.perf_counter() - t0:.2f} с")
    return thread


def run_turn(thread: Any, text: str, model: str, verbose: bool) -> float | None:
    """Один ход; возвращает t_first_token (с) или None, если текста не было."""
    print(f"\n>>> {text}")
    t0 = time.perf_counter()
    first: float | None = None
    tools: list[str] = []
    parts: list[str] = []
    status, error = "?", ""
    for ev in thread.turn(text, model=model, effort=ReasoningEffort.low).stream():
        if verbose:
            print(f"  +{time.perf_counter() - t0:.2f} с {ev.method}: {ev.payload!r}")
        if ev.method == "item/agentMessage/delta":
            first = first if first is not None else time.perf_counter() - t0
            parts.append(ev.payload.delta)
        elif ev.method == "item/started" and getattr(ev.payload.item.root, "type", "") == "mcpToolCall":
            tools.append(f"{ev.payload.item.root.server}/{ev.payload.item.root.tool}")
        elif ev.method == "turn/completed":
            status = str(getattr(ev.payload.turn.status, "value", ev.payload.turn.status))
            if ev.payload.turn.error is not None:
                error = ev.payload.turn.error.message
    total = time.perf_counter() - t0
    first_s = f"{first:.2f} с" if first is not None else "нет текста"
    print(f"<<< {''.join(parts)[:300]}")
    print(
        f"    до первых слов {first_s}, всего {total:.2f} с | {status} {error} | инструменты: {tools or '—'}"
    )
    return first


def main() -> int:
    cfg = config.load().brain
    model = sys.argv[1] if len(sys.argv) > 1 else cfg.model_quick
    home = codex_home()
    print(f"каталог данных: {settings.data_dir()}")
    login = "есть" if (home / "auth.json").exists() else "НЕТ — нужен вход"
    print(f"CODEX_HOME мозга: {home} (auth.json {login})")
    print(f"модель: {model}; proxy: {'задан' if cfg.proxy.strip() else 'нет'}")
    brain_cwd().mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    server = ConfirmServer(ask_console)
    server.start()
    overrides = build_overrides(cfg, server.address)
    print(f"overrides: {len(overrides)} шт. (как у Brain)")
    results: dict[str, float | None] = {}
    t0 = time.perf_counter()
    codex = Codex(CodexConfig(cwd=str(brain_cwd()), env=codex_env(cfg), config_overrides=overrides))
    results["codex_start_s"] = time.perf_counter() - t0
    print(f"старт app-server {results['codex_start_s']:.2f} с")
    threading.Thread(target=pump, args=(codex,), daemon=True).start()
    try:
        account = codex.account().account
        kind = getattr(getattr(account, "root", None), "type", None)
        plan = getattr(getattr(account, "root", None), "plan_type", None)
        print(f"вход: {kind or 'НЕТ'} {getattr(plan, 'value', plan) or ''}")

        thread = start_thread(codex, model)
        results["t1_first_turn"] = run_turn(thread, "Привет! Ответь одним словом.", model, verbose=True)
        results["t1_windows"] = run_turn(thread, "какие окна сейчас открыты?", model, verbose=True)
        results["t1_warm"] = run_turn(thread, "Сколько будет 17*23?", model, verbose=False)

        print("\n--- второй тред: первый ход сразу после thread_start ---")
        second = start_thread(codex, model)
        results["t2_cold_thread"] = run_turn(second, "Столица Чили?", model, verbose=False)

        print("\n--- третий тред: первый ход после 5 с прогрева (как запасной тред Brain) ---")
        third = start_thread(codex, model)
        time.sleep(5)
        results["t3_warm_thread"] = run_turn(
            third, "Что такое VRAM? Одним предложением.", model, verbose=False
        )
    finally:
        codex.close()
        server.close()

    print("\n=== итог (только числа — можно прислать) ===")
    for key, value in results.items():
        print(f"{key}: {'нет текста' if value is None else f'{value:.2f} с'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
