"""`jarvis bench`: точность и задержка уровней на корпусе bench/phrases.ru.jsonl. Действия НЕ выполняются.

Строка корпуса: {"text", "level", "tool"?, "args"?, "ctx"?};
ctx — {"window": {"title", "exe"}, "last", "found"}.
Инвентарь приложений — из tests/fixtures/apps.json (не с ПК), чтобы числа были сравнимы между прогонами.
--level grammar — grammar.match на всём корпусе (попадания и ложные срабатывания); router — уровень роутера;
hands — фразы рук; all — всё. Без --live руки не вызываются: уровень hands проверяется только роутером.
С --live — настоящий сервер рук (Hands.decide); open↔focus для приложения или папки — совпадение.
Ошибки печатаются как «фраза → ожидалось → получено» с подсказкой, что править.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.journal import fmt_ms, percentile
from pc import settings

log = logging.getLogger("jarvis")

LEVELS = ("grammar", "hands", "router", "all")
FUZZY_ARG = 80.0  # «телегу» ≈ «телега»: rapidfuzz ratio по casefold


@dataclass
class CaseResult:
    """Итог одной фразы: что ожидали, что получили и что не совпало."""

    text: str
    expected: dict[str, Any]
    level: str = ""
    reason: str = ""
    tool: str | None = None
    args: dict[str, Any] | None = None
    error: str = ""
    source: str = ""  # кто решал инструмент: grammar | hands
    level_ok: bool = True
    tool_ok: bool | None = None  # None — не проверялось
    args_ok: bool | None = None
    bad_args: list[str] = field(default_factory=list)
    hint: str = ""
    ms: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.level_ok and self.tool_ok is not False and self.args_ok is not False


# --- корпус и контекст --------------------------------------------------------------------------


def corpus_path() -> Path:
    return settings.app_root() / "bench" / "phrases.ru.jsonl"


def load_phrases(path: Path | None = None) -> list[dict[str, Any]]:
    """Строки корпуса; пустые и битые строки пропускаются (битые — с предупреждением в лог)."""
    items: list[dict[str, Any]] = []
    src = path or corpus_path()
    for i, line in enumerate(src.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            log.warning("bench: строка %d корпуса — не JSON", i)
            continue
        if (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and isinstance(item.get("level"), str)
        ):
            items.append(item)
    return items


def make_context(raw: Any) -> Any:
    """ctx из корпуса → Context: window → WindowInfo(hwnd=0, …), last → remember, found → set_found."""
    from jarvis.context import Context
    from pc.windows import WindowInfo

    ctx = Context()
    if not isinstance(raw, dict):
        return ctx
    window = raw.get("window")
    if isinstance(window, dict):
        ctx.active_window = WindowInfo(0, str(window.get("title", "")), 0, str(window.get("exe", "")))
    last = raw.get("last")
    if isinstance(last, str) and last:
        kind = raw.get("last_kind") or ("file" if "\\" in last or "/" in last else "app")
        ctx.remember(last, str(kind))
    found = raw.get("found")
    if isinstance(found, list):
        ctx.set_found([p for p in found if isinstance(p, str)])
    return ctx


def fixture_inventory() -> int:
    """Подставить инвентарь из tests/fixtures/apps.json; нет файла (exe) — 0 и инвентарь этого ПК."""
    from pc import apps

    for path in (
        settings.app_root() / "tests" / "fixtures" / "apps.json",
        settings.app_root() / "bench" / "apps.json",
    ):
        if path.is_file():
            items = json.loads(path.read_text(encoding="utf-8"))
            apps.set_inventory([apps.App(str(d["name"]), str(d["app_id"])) for d in items])
            return len(items)
    return 0


# --- сравнение ----------------------------------------------------------------------------------


def _is_app_or_folder(args: dict[str, Any]) -> bool:
    kind = args.get("kind")
    target = str(args.get("target", ""))
    if kind is not None:
        return kind in ("app", "folder")
    return not target.startswith(("http://", "https://")) and "." not in target.rsplit("\\", 1)[-1]


def same_tool(
    exp_tool: str, got_tool: str | None, exp_args: dict[str, Any], got_args: dict[str, Any]
) -> bool:
    """Инструмент совпал; open↔focus для приложения или папки взаимозаменяемы (execute сам переключит)."""
    if got_tool == exp_tool:
        return True
    return {exp_tool, got_tool} == {"open", "focus"} and _is_app_or_folder({**got_args, **exp_args})


def _same_text(exp: str, got: str) -> bool:
    a, b = exp.casefold().replace("ё", "е").strip(), got.casefold().replace("ё", "е").strip()
    if a == b:
        return True
    if "@cur" in (a, b):
        return False
    from rapidfuzz import fuzz

    if fuzz.ratio(a, b) >= FUZZY_ARG:
        return True
    try:  # «телегу» и «Telegram» — одно приложение
        from pc import apps

        app_a, app_b = apps.resolve(exp)[0], apps.resolve(got)[0]
    except Exception:
        return False
    return app_a is not None and app_a == app_b


def same_arg(key: str, exp: Any, got: Any) -> bool:
    """Ключевой аргумент совпал: числа равны (delta — тот же знак), строки — без регистра, почти дословно."""
    if isinstance(exp, bool) or isinstance(got, bool):
        return type(exp) is type(got) and exp == got
    if isinstance(exp, int | float) and isinstance(got, int | float):
        if key == "delta":
            return (exp > 0) == (got > 0) and exp != 0 and got != 0
        return float(exp) == float(got)
    if isinstance(exp, str) and isinstance(got, str):
        return exp == got if key in ("kind", "action", "what") else _same_text(exp, got)
    return exp == got


def bad_args(exp_args: dict[str, Any], got_args: dict[str, Any], skip_kind: bool = False) -> list[str]:
    """Ключи ожидаемых аргументов, которые не совпали (лишние ключи ответа не в счёт)."""
    bad = []
    for key, exp in exp_args.items():
        if skip_kind and key == "kind":
            continue
        if key not in got_args or not same_arg(key, exp, got_args[key]):
            bad.append(key)
    return bad


# --- подсказки ----------------------------------------------------------------------------------


def hint(r: CaseResult) -> str:
    """Что править, чтобы фраза прошла: SYSTEM/примеры/TOOLS рук, шаблоны грамматики, эвристики роутера."""
    exp_level, exp_tool = r.expected["level"], r.expected.get("tool")
    from_hands = r.reason.startswith("hands→") or r.source == "hands"
    if r.error:
        return (
            f"руки вернули ошибку ({r.error}): SYSTEM рук — «reply/clarify — одна короткая фраза», "
            "схема и описания TOOLS, max_tokens"
        )
    if not r.level_ok:
        if r.level == "grammar":
            return (
                "ложное срабатывание грамматики: сузь шаблон в grammar.py (ложное срабатывание хуже промаха)"
            )
        if exp_level == "grammar":
            return (
                "грамматика не поймала: шаблон, падеж или разговорная форма в grammar.py; порог apps.resolve"
            )
        if exp_level == "brain" and from_hands:
            return f"руки взялись сами ({r.tool}): правило ask_gpt и пример такой фразы в SYSTEM рук"
        if exp_level == "brain":
            return (
                "эвристики роутера (router.py): вопросительные слова, «напиши/переведи…», «?» и длина фразы"
            )
        if exp_level == "hands" and from_hands:
            return "руки отдали в ask_gpt: пример этой команды в SYSTEM рук, описание инструмента в TOOLS"
        if exp_level == "hands":
            return "эвристики роутера слишком широкие (router.py): команда ушла мозгу без рук"
        return "уровень: проверь эвристики роутера и шаблоны грамматики"
    if r.tool_ok is False:
        if r.source == "grammar":
            return "грамматика выбрала не то действие: шаблон в grammar.py"
        if exp_tool == "kill":
            return "правило «процесс/убей/заверши → kill» и пример в SYSTEM рук"
        if exp_tool == "clarify":
            return "правило clarify и «слово темы» для media/vol/win (execute.py): непонятное — без действия"
        if exp_tool == "ask_gpt":
            return "правило ask_gpt в SYSTEM рук: вопрос, объяснение, несколько шагов, написать текст"
        if exp_tool == "reply":
            return "пример приветствия/болтовни с коротким reply в SYSTEM рук"
        return f"описания инструментов {exp_tool} и {r.tool} в TOOLS рук и пример в SYSTEM"
    if r.args_ok is False:
        keys = ", ".join(r.bad_args)
        if r.source == "grammar":
            return f"разбор аргумента ({keys}) в шаблоне grammar.py"
        return (
            f"аргумент {keys}: пример в SYSTEM рук и описание параметра в TOOLS («как сказал пользователь»)"
        )
    return ""


def _describe(level: str, tool: str | None, args: dict[str, Any] | None) -> str:
    out = level or "—"
    if tool:
        out += f":{tool}"
    if args:
        out += " " + json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    return out


# --- прогон -------------------------------------------------------------------------------------


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 3)


def _check_tool(r: CaseResult) -> None:
    exp_tool, exp_args = r.expected.get("tool"), r.expected.get("args")
    if not exp_tool or r.tool is None:
        return
    got_args = r.args or {}
    r.tool_ok = same_tool(exp_tool, r.tool, exp_args or {}, got_args)
    if r.tool_ok and isinstance(exp_args, dict) and exp_args:
        r.bad_args = bad_args(exp_args, got_args, skip_kind=r.tool != exp_tool)
        r.args_ok = not r.bad_args


def run_case(item: dict[str, Any], level: str, live: bool, hands: Any) -> CaseResult:
    """Одна фраза: без действий ПК (execute не вызывается)."""
    from jarvis import grammar, router

    ctx = make_context(item.get("ctx"))
    r = CaseResult(text=item["text"], expected=item)
    exp_level = item["level"]
    if level == "grammar":
        t = time.perf_counter()
        hit = grammar.match(item["text"], ctx)
        r.ms["grammar"] = _ms(t)
        r.level = "grammar" if hit is not None else "—"
        r.level_ok = (hit is not None) == (exp_level == "grammar")
        if hit is not None:
            r.tool, r.args, r.source = hit.action, dict(hit.args), "grammar"
    else:
        t = time.perf_counter()
        route = router.route(item["text"], ctx, "normal")
        r.ms["router"] = _ms(t)
        r.level, r.reason = route.level, route.reason
        if route.level == "grammar" and route.hit is not None:
            r.ms["grammar"] = r.ms["router"]
            r.tool, r.args, r.source = route.hit.action, dict(route.hit.args), "grammar"
        elif route.level == "hands" and live and hands is not None and level in ("hands", "all"):
            d = hands.decide(route.text, ctx)
            if isinstance(d.timings.get("total_ms"), int | float):
                r.ms["hands"] = float(d.timings["total_ms"])
            r.source = "hands"
            if d.kind == "error":
                r.level, r.reason, r.error = "brain", "hands→error", d.reason or "ошибка"
            else:
                r.tool, r.args = d.tool, dict(d.args)
                if d.tool == "ask_gpt":
                    r.level, r.reason = "brain", "hands→ask_gpt"
        # «руки → ask_gpt» — верный ответ рук для hands-фразы с tool "ask_gpt": уровень роутера совпал
        handed_over = r.reason == "hands→ask_gpt" and exp_level == "hands" and item.get("tool") == "ask_gpt"
        r.level_ok = r.level == exp_level or handed_over
    if r.level_ok:
        _check_tool(r)
    r.hint = hint(r) if not r.ok else ""
    return r


def select(items: list[dict[str, Any]], level: str, n: int | None) -> list[dict[str, Any]]:
    chosen = [it for it in items if it["level"] == "hands"] if level == "hands" else list(items)
    return chosen[:n] if n else chosen


def run(
    items: list[dict[str, Any]], level: str = "all", live: bool = False, hands: Any = None
) -> dict[str, Any]:
    """Прогнать фразы и собрать отчёт (dict, он же уходит в bench/results/*.json)."""
    results = [run_case(it, level, live, hands) for it in items]
    level_n = sum(r.level_ok for r in results)
    tool_checked = [r for r in results if r.tool_ok is not None]
    args_checked = [r for r in results if r.args_ok is not None]
    latency: dict[str, dict[str, float | int | None]] = {}
    for name in ("grammar", "router", "hands"):
        values = [r.ms[name] for r in results if name in r.ms]
        if values:
            latency[name] = {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}
    by_level: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_level.setdefault(r.expected["level"], {"n": 0, "ok": 0})
        bucket["n"] += 1
        bucket["ok"] += r.level_ok
    return {
        "date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "level": level,
        "live": live,
        "count": len(results),
        "accuracy": {
            "level": {"ok": level_n, "n": len(results)},
            "tool": {"ok": sum(bool(r.tool_ok) for r in tool_checked), "n": len(tool_checked)},
            "args": {"ok": sum(bool(r.args_ok) for r in args_checked), "n": len(args_checked)},
            "by_level": by_level,
        },
        "latency": latency,
        "errors": [
            {
                "text": r.text,
                "expected": _describe(r.expected["level"], r.expected.get("tool"), r.expected.get("args")),
                "got": _describe(r.level, r.tool, r.args) + (f" ({r.error})" if r.error else ""),
                "reason": r.reason,
                "hint": r.hint,
            }
            for r in results
            if not r.ok
        ],
        "cases": [
            {
                "text": r.text,
                "expected_level": r.expected["level"],
                "level": r.level,
                "reason": r.reason,
                "tool": r.tool,
                "args": r.args,
                "ok": r.ok,
                "ms": r.ms,
            }
            for r in results
        ],
    }


def _ratio(acc: dict[str, int]) -> str:
    return f"{acc['ok']}/{acc['n']} ({acc['ok'] / acc['n'] * 100:.1f} %)" if acc["n"] else "не проверялось"


def format_report(rep: dict[str, Any]) -> str:
    acc = rep["accuracy"]
    mode = (
        "руки — настоящий сервер (--live)" if rep["live"] else "без рук (уровень hands — только по роутеру)"
    )
    lines = [f"bench: {rep['count']} фраз, --level {rep['level']}, {mode}", ""]
    lines.append(f"уровень:     {_ratio(acc['level'])}")
    for name, b in sorted(acc["by_level"].items()):
        lines.append(f"  {name:<8} {_ratio(b)}")
    lines.append(f"инструмент:  {_ratio(acc['tool'])}")
    lines.append(f"аргументы:   {_ratio(acc['args'])}")
    if rep["latency"]:
        lines.append("задержка:")
        for name, lat in rep["latency"].items():
            lines.append(f"  {name:<8} p50 {fmt_ms(lat['p50'])}, p95 {fmt_ms(lat['p95'])} (n={lat['n']})")
    if rep["errors"]:
        lines += ["", f"ошибки ({len(rep['errors'])}): фраза → ожидалось → получено"]
        for e in rep["errors"]:
            lines.append(f"  «{e['text']}» → {e['expected']} → {e['got']}")
            if e["hint"]:
                lines.append(f"      что править: {e['hint']}")
    else:
        lines += ["", "ошибок нет"]
    return "\n".join(lines)


def results_dir() -> Path:
    """bench/results/ в репозитории (в .gitignore); в exe — <data>\\bench\\results."""
    base = settings.data_dir() if settings.is_frozen() else settings.app_root()
    return base / "bench" / "results"


def save_report(rep: dict[str, Any]) -> Path:
    path = results_dir() / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _live_hands() -> Any:
    """Сервер рук для --live: поднять, если не запущен, и прогреть (как при старте `jarvis run`)."""
    from jarvis import config
    from jarvis.hands import Hands

    hands = Hands(config.load().hands)
    hands.ensure_server()
    hands.warmup()
    st = hands.status
    if st.get("prefix_cache") == "broken" or st.get("vram") == "slow":
        print(f"⚠ руки: кэш префикса {st.get('prefix_cache')}, VRAM {st.get('vram')} ({st.get('tps')} т/с)")
    return hands


def main(live: bool = False, level: str = "all", n: int | None = None) -> int:
    """`jarvis bench`: отчёт в консоль и bench/results/<дата>.json. Код 0 — без ошибок, 1 — есть ошибки."""
    if level not in LEVELS:
        raise ValueError(f"неизвестный --level {level!r}")
    path = corpus_path()
    if not path.is_file():
        print(f"✗ нет корпуса {path}")
        return 2
    count = fixture_inventory()
    if not count:
        print("⚠ нет tests/fixtures/apps.json — инвентарь приложений этого ПК (числа несравнимы)")
    items = select(load_phrases(path), level, n)
    hands = _live_hands() if live and level in ("hands", "all") else None
    rep = run(items, level, live, hands)
    print(format_report(rep))
    print(f"\nотчёт: {save_report(rep)}")
    return 0 if not rep["errors"] else 1
