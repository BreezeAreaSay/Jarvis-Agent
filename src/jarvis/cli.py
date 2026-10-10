"""Командная строка Jarvis: `jarvis <команда>` (в exe — jarvis-cli.exe).

Подкоманды: run, ask, route, apps, bench, stats, doctor, autostart, selftest;
скрытая mcp — сервер pc для мозга.
Всё тяжёлое импортируется внутри подкоманды: `jarvis route` не тянет httpx, Qt и openai_codex.
Если stdout/stderr не консоль (pipe, файл) — UTF-8; под pythonw/windowed потоков нет — ничего не печатаем.
"""

import argparse
import json
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

log = logging.getLogger("jarvis")

LEVEL_NAMES = {"grammar": "грамматика", "hands": "руки", "brain": "GPT", "local": "локально"}


def _utf8_streams() -> None:
    """Pipe и файл — UTF-8 (иначе на Windows cp1251 и «⚙» роняет print); консоль не трогаем."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass


def _out(text: str = "", end: str = "\n") -> None:
    if sys.stdout is not None:
        print(text, end=end, flush=True)


def _err(text: str) -> None:
    if sys.stderr is not None:
        print(text, file=sys.stderr, flush=True)


def _clean(text: Any) -> str:
    """Недоверенный текст (окна, файлы, ответ модели) — без управляющих символов: ESC-последовательности
    могут переписать строку терминала."""
    return "".join(ch for ch in str(text) if ch in "\n\t" or (ch >= " " and not 0x7F <= ord(ch) <= 0x9F))


# --- разбор аргументов --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jarvis", description="Jarvis — персональный помощник для Windows.")
    sub = p.add_subparsers(dest="cmd", metavar="команда")

    sub.add_parser("run", help="запустить Jarvis (трей, хоткей, окно)")  # аргументы — как есть в app.main

    ask = sub.add_parser("ask", help="выполнить команду: полный путь или один уровень")
    ask.add_argument("--level", choices=("grammar", "hands", "brain"), help="только этот уровень")
    ask.add_argument("--dry", action="store_true", help="решение без действия на ПК")
    ask.add_argument("--window", metavar="ЗАГОЛОВОК", help="активное окно для «его/это» (по умолчанию нет)")
    ask.add_argument("text", nargs="+", help="команда")

    route = sub.add_parser("route", help="какой уровень ответит и почему (без выполнения)")
    route.add_argument("--window", metavar="ЗАГОЛОВОК", help="активное окно")
    route.add_argument("text", nargs="+", help="команда")

    apps = sub.add_parser("apps", help="инвентарь приложений; с запросом — топ-5 и время resolve")
    apps.add_argument("--refresh", action="store_true", help="перечитать приложения из Windows")
    apps.add_argument("query", nargs="*", help="название приложения")

    bench = sub.add_parser("bench", help="точность и задержка уровней на корпусе фраз")
    bench.add_argument("--live", action="store_true", help="спрашивать настоящий сервер рук")
    bench.add_argument("--level", choices=("grammar", "hands", "router", "all"), default="all")
    bench.add_argument("-n", type=int, default=None, metavar="N", help="только первые N фраз")

    stats = sub.add_parser("stats", help="сводка журнала: задержки, ошибки, бюджеты")
    stats.add_argument("--days", type=int, default=7, help="за сколько дней (по умолчанию 7)")

    doctor = sub.add_parser("doctor", help="проверить окружение: руки, Everything, мозг, конфиг")
    doctor.add_argument("--brain", action="store_true", help="один тестовый ход GPT (тратит квоту)")

    autostart = sub.add_parser("autostart", help="автозапуск при входе в Windows")
    autostart.add_argument("state", choices=("on", "off"))

    st = sub.add_parser("selftest", help="проверка собранного бандла без сети и GPU")
    st.add_argument("--offscreen", action="store_true", help="Qt без экрана")
    sub.add_parser("mcp")  # скрытая: сервер pc для мозга (stdio); без help — не видна в списке
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list and sys.stdout is None:  # windowed Jarvis.exe без аргументов — это `run`
        return _run([])
    if args_list and args_list[0] == "run":  # свой лог у приложения; аргументы — app.main как есть
        return _run(args_list[1:])
    parser = _parser()
    args = parser.parse_args(args_list)
    if args.cmd is None:
        parser.print_help()
        return 0
    if args.cmd == "mcp":  # stdout mcp — канал протокола: ни лога в консоль, ни печати
        return _cmd_mcp(args)
    from jarvis import log as jlog

    jlog.setup("jarvis-cli")
    try:
        return _COMMANDS[args.cmd](args)
    except KeyboardInterrupt:
        _err("прервано")
        return 130
    except Exception as e:
        log.exception("cli: команда %s упала", args.cmd)
        _err(f"✗ {args.cmd}: {_clean(e) or type(e).__name__}")
        return 1


# --- простые подкоманды -------------------------------------------------------------------------


def _run(rest: list[str]) -> int:
    from jarvis import app

    return int(app.main(rest) or 0)


def _cmd_mcp(args: argparse.Namespace) -> int:
    from pc import mcp

    return int(mcp.main() or 0)


def _cmd_selftest(args: argparse.Namespace) -> int:
    from jarvis import selftest

    return int(selftest.main(["--offscreen"] if args.offscreen else []) or 0)


def _cmd_autostart(args: argparse.Namespace) -> int:
    from jarvis import winapp

    _out(_clean(winapp.set_autostart(args.state == "on")))
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    from jarvis import config, journal

    cfg = config.load()
    _out(journal.format_stats(journal.stats(args.days), cfg.hands.min_tokens_per_s.get(cfg.hands.model)))
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    from jarvis import bench

    return bench.main(live=args.live, level=args.level, n=args.n)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from jarvis import doctor

    return doctor.main(brain=args.brain)


def _cmd_apps(args: argparse.Namespace) -> int:
    from pc import apps

    if args.refresh:
        t = time.perf_counter()
        found = apps.refresh()
        _out(f"✓ инвентарь обновлён: {len(found)} приложений за {time.perf_counter() - t:.1f} с")
    query = " ".join(args.query).strip()
    if not query:
        items = apps.inventory()
        for app in sorted(items, key=lambda a: a.name.casefold()):
            _out(f"  {_clean(app.name)}  ·  {_clean(app.app_id)}")
        _out(f"всего: {len(items)}" + ("" if items else " — обнови кэш: jarvis apps --refresh"))
        return 0
    apps.inventory()  # кэш с диска — до замера
    t = time.perf_counter()
    app, score = apps.resolve(query)
    ms = (time.perf_counter() - t) * 1000
    for cand, s in apps.top(query, 5):
        mark = "✓" if s >= apps.THRESHOLD else " "
        _out(f"{mark} {s:5.1f}  {_clean(cand.name)}  ·  {_clean(cand.app_id)}")
    name = _clean(app.name) if app else "не найдено"
    _out(f"resolve «{_clean(query)}» → {name} (score {score:.1f}, порог {apps.THRESHOLD:g}) за {ms:.2f} мс")
    return 0 if app else 1


def _window(title: str | None) -> Any:
    """Активное окно для CLI: без --window — None (иначе «закрой его» закроет сам терминал)."""
    if not title:
        return None
    from pc import windows

    try:
        found = windows.find_window(title)
    except Exception as e:
        log.info("cli: окно «%s» не найдено: %s", title, e)
        found = None
    return found if found is not None else windows.WindowInfo(0, title, 0, "")


def _cmd_route(args: argparse.Namespace) -> int:
    from jarvis import config, context, router

    text = " ".join(args.text).strip()
    _out(f"запрос: {_clean(text)}")
    cfg = config.load()
    ctx = context.load_cli()
    ctx.active_window = _window(args.window)
    t = time.perf_counter()
    r = router.route(text, ctx, cfg.mode)
    ms = (time.perf_counter() - t) * 1000
    flags = [f for f, on in (("только локально", r.local_only), ("думай", r.deep)) if on]
    _out(
        f"уровень: {r.level} ({LEVEL_NAMES.get(r.level, r.level)}){' · ' + ', '.join(flags) if flags else ''}"
    )
    _out(f"причина: {_clean(r.reason)}")
    if r.text != text:
        _out(f"текст для уровня: {_clean(r.text)}")
    if r.hit is not None:
        hit_args = _clean(json.dumps(r.hit.args, ensure_ascii=False))
        _out(f"грамматика: {r.hit.action} {hit_args} (уверенность {r.hit.confidence:g})")
    _out(f"время: {ms:.2f} мс (режим {cfg.mode})")
    return 0


# --- ask ----------------------------------------------------------------------------------------

_confirm_lock = threading.Lock()


def console_confirm(summary: str, details: str, caller: str) -> bool:
    """Подтверждение в консоли: y/N; не TTY — «нет»."""
    with _confirm_lock:
        who = " — просит GPT" if caller == "brain" else ""
        _out(f"\n? {_clean(summary)}{who}")
        if details:
            _out(f"  {_clean(details)}")
        if sys.stdin is None or not sys.stdin.isatty():
            _out("  не консоль — отвечаю «нет»")
            return False
        try:
            answer = input("  Выполнить? [y/N]: ")
        except (EOFError, OSError):
            return False
    return answer.strip().casefold() in ("y", "yes", "д", "да")


class _LazyHands:
    """Руки для одного вызова CLI: httpx и сервер — только если команда до них дошла.

    Сервер рук CLI в Job Object не кладёт и не останавливает: он нужен следующим вызовам.
    """

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        self._hands: Any = None

    @property
    def status(self) -> dict[str, Any]:
        return self._get().status

    def decide(self, text: str, ctx: Any) -> Any:
        return self._get().decide(text, ctx)

    def _get(self) -> Any:
        if self._hands is None:
            from jarvis.hands import Hands

            self._hands = Hands(self._cfg.hands)
        return self._hands


class _LazyBrain:
    """Мозг для одного вызова CLI: Codex и канал подтверждений поднимаются только при первом вопросе GPT."""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        self._brain: Any = None
        self._server: Any = None

    @property
    def created(self) -> bool:
        return self._brain is not None

    def ask(self, text: str, ctx: Any, deep: bool = False) -> Any:
        return self._get().ask(text, ctx, deep=deep)

    def cancel(self) -> None:
        if self._brain is not None:
            self._brain.cancel()

    def close(self) -> None:
        for obj in (self._brain, self._server):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    log.exception("cli: закрытие мозга")
        self._brain = self._server = None

    def _get(self) -> Any:
        if self._brain is None:
            from jarvis.brain import Brain
            from pc.confirm_client import ConfirmServer

            self._server = ConfirmServer(console_confirm)
            self._server.start()
            self._brain = Brain(self._cfg.brain, confirm_address=self._server.address)
            self._brain.start()
        return self._brain


def fmt_timings(t: dict[str, Any]) -> str:
    """Строка таймингов для CLI: route, руки, exec, мозг, всего."""
    from jarvis.journal import fmt_ms

    parts: list[str] = []
    for key, name in (("hotkey_to_window", "хоткей→окно"), ("route", "роутер"), ("grammar", "до действия")):
        if isinstance(t.get(key), int | float):
            parts.append(f"{name} {fmt_ms(t[key])}")
    hands = t.get("hands")
    if isinstance(hands, dict) and hands:
        extra = [
            f"{k} {hands[k]:g}" for k in ("prompt_n", "cache_n") if isinstance(hands.get(k), int | float)
        ]
        if isinstance(hands.get("predicted_per_second"), int | float):
            extra.append(f"{hands['predicted_per_second']:.0f} т/с")
        total = fmt_ms(hands["total_ms"]) if isinstance(hands.get("total_ms"), int | float) else "—"
        parts.append(f"руки {total}" + (f" ({', '.join(extra)})" if extra else ""))
    if isinstance(t.get("exec"), int | float):
        parts.append(f"действие {fmt_ms(t['exec'])}")
    brain = t.get("brain")
    if isinstance(brain, dict) and brain:
        first = fmt_ms(brain.get("first_token")) if isinstance(brain.get("first_token"), int | float) else "—"
        thread = " (новый тред)" if brain.get("new_thread") else ""
        parts.append(f"GPT до первых слов {first}{thread}")
    if isinstance(t.get("total"), int | float):
        parts.append(f"всего {fmt_ms(t['total'])}")
    return " · ".join(parts)


class _Printer:
    """Печать событий по мере прихода: Status — «⚙ …», TextChunk — без перевода строки, Items — список."""

    def __init__(self) -> None:
        self.ok = False
        self._open = False  # строка потока текста не закрыта

    def _close_line(self) -> None:
        if self._open:
            _out()
            self._open = False

    def event(self, ev: Any) -> None:
        from jarvis.events import Done, Items, Level, Status, TextChunk

        if isinstance(ev, TextChunk):
            text = _clean(ev.text)
            _out(text, end="")
            self._open = bool(text) and not text.endswith("\n")
            return
        self._close_line()
        if isinstance(ev, Level):
            model = f" · {ev.model}" if ev.model else ""
            cold = " · холодный старт Codex (в CLI — на каждый вызов)" if ev.level == "brain" else ""
            _out(f"[{LEVEL_NAMES.get(ev.level, ev.level)}{model} · {_clean(ev.reason)}{cold}]")
        elif isinstance(ev, Status):
            text = _clean(ev.text)
            mark = "" if not ev.done else (" ✓" if ev.ok else " ✗")
            _out(f"{text if text.startswith('⚙') else '⚙ ' + text}{mark}")
        elif isinstance(ev, Items):
            for i, item in enumerate(ev.items, 1):
                _out(f"  {i}. {_clean(item)}")
        elif isinstance(ev, Done):
            self.ok = ev.ok
            mark = "⊘" if ev.cancelled else ("✓" if ev.ok else "✗")
            text = _clean(ev.text) or ("отменено" if ev.cancelled else "")
            _out(f"{mark} {text}".rstrip())
            line = fmt_timings(ev.timings)
            if line:
                _out(f"  {line}")


def _cmd_ask(args: argparse.Namespace) -> int:
    from jarvis import config, context
    from pc import confirm_client

    cfg = config.load()
    text = " ".join(args.text).strip()
    ctx = context.load_cli()
    ctx.active_window = _window(args.window)
    confirm_client.set_confirm_handler(console_confirm)
    hands = _LazyHands(cfg)
    brain = None if cfg.mode == "local" else _LazyBrain(cfg)
    printer = _Printer()
    if args.dry:
        _out("(dry: действия на ПК не выполняются)")
    try:
        if args.level == "grammar":
            _ask_grammar(text, ctx, args.dry, printer)
        elif args.level == "hands":
            _ask_hands(cfg, hands, text, ctx, args.dry, printer)
        elif args.level == "brain":
            _ask_brain(cfg, brain, text, ctx, printer)
        else:
            from jarvis.core import Core

            core = Core(cfg, hands, brain)
            _stream(core.handle(text, ctx, dry=args.dry), printer, core.cancel)
    finally:
        confirm_client.set_confirm_handler(None)
        context.save_cli(ctx)
        if brain is not None:
            brain.close()
    return 0 if printer.ok else 1


def _stream(events: Any, printer: _Printer, cancel: Callable[[], None]) -> None:
    """Печатать события по мере прихода; Ctrl+C — отменить запрос (ход мозга прерывается) и закончить."""
    try:
        for ev in events:
            printer.event(ev)
    except KeyboardInterrupt:
        cancel()
        printer.event(_cancelled_done())
        events.close()


def _cancelled_done() -> Any:
    from jarvis.events import Done

    return Done(ok=False, text="отменено", cancelled=True)


def _print_outcome(out: Any, printer: _Printer, timings: dict[str, Any]) -> None:
    from jarvis.events import Done, Items

    if out.items:
        printer.event(Items(list(out.items)))
    kind = "" if out.kind == "done" else f"[{out.kind}] "
    printer.event(Done(ok=out.ok, text=f"{kind}{out.text}", timings=timings))


def _ask_grammar(text: str, ctx: Any, dry: bool, printer: _Printer) -> None:
    from jarvis import execute, grammar
    from jarvis.events import Done

    t = time.perf_counter()
    hit = grammar.match(text, ctx)
    match_ms = round((time.perf_counter() - t) * 1000, 2)
    if hit is None:
        printer.event(Done(ok=False, text="грамматика не узнала команду", timings={"grammar": match_ms}))
        return
    _out(f"грамматика: {hit.action} {_clean(json.dumps(hit.args, ensure_ascii=False))} ({hit.confidence:g})")
    t = time.perf_counter()
    out = execute.run(hit.action, hit.args, text, ctx, "grammar", dry)
    _print_outcome(out, printer, {"grammar": match_ms, "exec": round((time.perf_counter() - t) * 1000, 1)})


def _ask_hands(cfg: Any, hands: _LazyHands, text: str, ctx: Any, dry: bool, printer: _Printer) -> None:
    from jarvis import execute
    from jarvis.core import HANDS_TIMINGS
    from jarvis.events import Done

    _out(f"[руки · {cfg.hands.model}]")
    d = hands.decide(text, ctx)
    timings: dict[str, Any] = {"hands": {k: v for k, v in d.timings.items() if k in HANDS_TIMINGS}}
    if d.kind == "error":
        printer.event(Done(ok=False, text=f"ошибка рук: {d.reason}", timings=timings))
        return
    _out(f"решение: {d.tool} {_clean(json.dumps(d.args, ensure_ascii=False))}")
    if d.tool in ("reply", "clarify", "ask_gpt"):
        answer = d.args.get("text") or d.args.get("question") or "это вопрос для GPT"
        printer.event(Done(ok=True, text=f"[{d.tool}] {answer}", timings=timings))
        return
    t = time.perf_counter()
    out = execute.run(d.tool, d.args, text, ctx, "hands", dry)
    timings["exec"] = round((time.perf_counter() - t) * 1000, 1)
    _print_outcome(out, printer, timings)


def _ask_brain(cfg: Any, brain: _LazyBrain | None, text: str, ctx: Any, printer: _Printer) -> None:
    import dataclasses

    from jarvis.core import NEEDS_GPT, brain_timings
    from jarvis.events import Done, Level

    if brain is None:
        printer.event(Done(ok=False, text=f"{NEEDS_GPT}: codex не запускается"))
        return

    def events() -> Iterator[Any]:
        yield Level("brain", "cli:--level brain", model=cfg.brain.model_quick)
        for ev in brain.ask(text, ctx):
            if isinstance(ev, Done):
                ev = dataclasses.replace(ev, timings={**ev.timings, "brain": brain_timings(ev.timings)})
            if not isinstance(ev, Level):
                yield ev

    _stream(events(), printer, brain.cancel)


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "ask": _cmd_ask,
    "route": _cmd_route,
    "apps": _cmd_apps,
    "bench": _cmd_bench,
    "stats": _cmd_stats,
    "doctor": _cmd_doctor,
    "autostart": _cmd_autostart,
    "selftest": _cmd_selftest,
    "mcp": _cmd_mcp,
}
