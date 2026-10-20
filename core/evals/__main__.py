"""Командная строка: uv run python -m evals [параметры] (из папки core).

Детерминированный режим (по умолчанию) — модель по сценарию, без сети: проверяет имитатор и проверки задач.
Живой — настоящая модель: --mode live --provider openai --model gpt-5.4-mini; ключ — из переменной
окружения JARVIS_EVAL_API_KEY (или обычной переменной провайдера: OPENAI_API_KEY, ANTHROPIC_API_KEY…),
в репозиторий, отчёт и журналы он не попадает. Подробно — docs/EVALS.md.
"""

import argparse
import asyncio
import contextlib
import os
import re
import sys
import time
from pathlib import Path

from evals.harness import Options
from evals.run import finish_run, run_tasks, save_baseline
from evals.tasks import BY_ID, CATEGORIES, TASKS
from jarvis.ai.providers import PRESETS

ROOT = Path(__file__).resolve().parent
BASELINES = ROOT / "baselines"
RUNS = ROOT.parent / ".evals"  # core/.evals — в .gitignore
KEY_ENV: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "xai": ("XAI_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "qwen": ("DASHSCOPE_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "zhipu": ("ZHIPU_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "siliconflow": ("SILICONFLOW_API_KEY",),
    "yandex": ("YANDEX_API_KEY",),
}


def api_key(provider: str) -> tuple[str, str]:
    """(ключ, из какой переменной) — JARVIS_EVAL_API_KEY или переменная провайдера."""
    for name in ("JARVIS_EVAL_API_KEY", *KEY_ENV.get(provider, ())):
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    return "", ""


def baseline_path(options: Options) -> Path:
    if options.scripted:
        return BASELINES / "scripted.json"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{options.provider}-{options.model}").strip("_")
    return BASELINES / f"{name}{'' if options.vision != 'off' else '-noimg'}.json"


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals", description="Проверочные задачи агента Jarvis")
    parser.add_argument(
        "--mode", choices=("scripted", "live"), default=os.environ.get("JARVIS_EVAL_MODE", "scripted")
    )
    parser.add_argument("--provider", default=os.environ.get("JARVIS_EVAL_PROVIDER", "openai"))
    parser.add_argument("--model", default=os.environ.get("JARVIS_EVAL_MODEL", ""))
    parser.add_argument("--base-url", default=os.environ.get("JARVIS_EVAL_BASE_URL", ""))
    parser.add_argument("--tasks", default="", help="id задач через запятую (по умолчанию — все)")
    parser.add_argument("--category", default="", help=f"категории через запятую: {', '.join(CATEGORIES)}")
    parser.add_argument("--repeat", type=int, default=1, help="сколько раз каждую задачу (успех — доля из N)")
    parser.add_argument("--vision", choices=("on", "off", "auto"), default="", help="картинки экрана модели")
    parser.add_argument("--speed", choices=("fast", "balanced", "deep"), default="fast")
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--timeout", type=float, default=300.0, help="секунд на попытку")
    parser.add_argument("--mistakes", action="store_true", help="детерминированный режим: ошибочные решения")
    parser.add_argument("--journals", choices=("failed", "all", "none"), default="failed")
    parser.add_argument("--out", default="", help="папка прогона (по умолчанию core/.evals/<время>-<модель>)")
    parser.add_argument("--baseline", default="", help="файл базовой линии (по умолчанию evals/baselines/…)")
    parser.add_argument("--update-baseline", action="store_true", help="записать прогон базовой линией")
    parser.add_argument("--fail-on-regression", action="store_true", help="код 1, если что-то стало хуже")
    parser.add_argument("--list", action="store_true", help="показать задачи и выйти")
    return parser.parse_args(argv)


def _utf8_output() -> None:
    """Кириллица и значки в консоли Windows: вывод в файл — UTF-8, в консоль — без падения на незнакомом
    знаке."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(errors="replace") if stream.isatty() else reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    args = parse(sys.argv[1:] if argv is None else argv)
    if args.list:
        for task in TASKS:
            vision = " [нужны картинки]" if task.vision else ""
            print(f"{task.id:20} {CATEGORIES[task.category]:15} {task.title}{vision}\n{'':20} {task.checks}")
        return 0
    tasks = list(TASKS)
    if args.tasks:
        unknown = [t for t in args.tasks.split(",") if t.strip() and t.strip() not in BY_ID]
        if unknown:
            print(f"Нет таких задач: {', '.join(unknown)} (список — --list)", file=sys.stderr)
            return 2
        tasks = [BY_ID[t.strip()] for t in args.tasks.split(",") if t.strip()]
    if args.category:
        wanted = {c.strip() for c in args.category.split(",")}
        tasks = [t for t in tasks if t.category in wanted]
    scripted = args.mode == "scripted"
    preset = PRESETS.get(args.provider)
    if not scripted:
        if preset is None or preset.group == "account":
            print(
                "Живой режим — провайдеры с ключом API или локальные (ollama, custom), не вход в аккаунт",
                file=sys.stderr,
            )
            return 2
        key, source = api_key(args.provider)
        if preset.needs_key and not key:
            names = " или ".join(("JARVIS_EVAL_API_KEY", *KEY_ENV.get(args.provider, ())))
            print(f"Нет ключа {preset.title}: задайте переменную окружения {names}", file=sys.stderr)
            return 2
    else:
        key, source = "", ""
    model = "scripted" if scripted else (args.model or (preset.default_model if preset else ""))
    options = Options(
        mode=args.mode,
        provider="openai" if scripted else args.provider,
        model=model,
        base_url="" if scripted else args.base_url,
        api_key=key,
        vision=args.vision or ("on" if scripted else "auto"),
        speed=args.speed,
        max_steps=args.max_steps,
        timeout_s=args.timeout,
        journals=args.journals,
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = (
        "scripted" + ("-mistakes" if args.mistakes else "")
        if scripted
        else re.sub(r"[^A-Za-z0-9._-]+", "_", f"{options.provider}-{options.model}")
    )
    options.out_dir = Path(args.out) if args.out else RUNS / f"{stamp}-{label}"
    baseline = Path(args.baseline) if args.baseline else baseline_path(options)
    print(
        f"Прогон: {'детерминированный' if scripted else f'живой, {options.provider} / {options.model}'}"
        f"{' (ключ из ' + source + ')' if source else ''}, задач: {len(tasks)}, повторов: {args.repeat}"
    )
    summary, _ = asyncio.run(
        run_tasks(tasks, options, repeat=args.repeat, mistakes=args.mistakes and scripted, progress=print)
    )
    comparison = finish_run(summary, options, None if args.mistakes else baseline)
    totals = summary["totals"]
    print(
        f"\nОценка {round(totals['score'] * 100)}% · попыток успешно {totals['passed']}/"
        f"{totals['attempts']} · нарушений {totals['violations']} · токены {totals['prompt_tokens']}/"
        f"{totals['completion_tokens']} · ${totals['cost']:.4f}"
    )
    if comparison is not None:
        print(
            f"С базовой линией ({baseline.name}): лучше — {', '.join(comparison['better']) or 'ничего'}; "
            f"хуже — {', '.join(comparison['worse']) or 'ничего'}"
        )
    print(f"Отчёт: {options.out_dir / 'report.md'}")
    if args.update_baseline and not args.mistakes:
        save_baseline(summary, baseline)
        print(f"Базовая линия обновлена: {baseline}")
    if args.fail_on_regression and comparison is not None and comparison["worse"]:
        return 1
    if scripted:
        # детерминированный режим проверяет сам набор: эталон — успех, ошибочное решение — провал
        expected = (lambda r: not r["ok"]) if args.mistakes else (lambda r: r["ok"])
        broken = [r for t in summary["tasks"] for r in t["results"] if not expected(r)]
        return 1 if broken else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
