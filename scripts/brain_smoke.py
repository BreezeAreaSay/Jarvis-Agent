"""Замер мозга: время до первых слов GPT через Codex app-server (SDK openai-codex 0.160.1).

Запуск (вход в Codex уже сделан):
    uv run --no-project --python 3.12 --with openai-codex==0.160.1 python C:\\Jarvis\\scripts\\brain_smoke.py [модель]
Работает из пустой временной папки: иначе Codex подмешивает AGENTS.md репозитория в каждый ход и замер хуже правды.
"""

import sys
import tempfile
import time

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
from openai_codex.types import ReasoningEffort

if sys.stdout is not None and not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gpt-6-luna"
QUESTIONS = ["Привет, ты тут?", "Сколько будет 17*23?", "Столица Чили?", "Что такое VRAM?"]
OVERRIDES = (
    'web_search="disabled"',
    "project_doc_max_bytes=0",
    "features.shell_tool=false",
    'model_reasoning_summary="none"',
)

workdir = tempfile.mkdtemp(prefix="jarvis-brain-smoke-")
started = time.perf_counter()
codex = Codex(CodexConfig(cwd=workdir, config_overrides=OVERRIDES))
print(f"модель {MODEL}; старт app-server {time.perf_counter() - started:.2f} с")
try:
    thread = codex.thread_start(
        model=MODEL,
        sandbox=Sandbox.read_only,
        approval_mode=ApprovalMode.deny_all,
        ephemeral=True,
        developer_instructions="Отвечай по-русски одним предложением.",
    )
    for number, question in enumerate(QUESTIONS, 1):
        t0, first, parts, status, error = time.perf_counter(), None, [], "?", ""
        for ev in thread.turn(question, effort=ReasoningEffort.low).stream():
            if ev.method == "item/agentMessage/delta":
                first = first or time.perf_counter() - t0
                parts.append(ev.payload.delta)
            elif ev.method == "turn/completed":
                status = getattr(ev.payload.turn.status, "value", ev.payload.turn.status)
                if ev.payload.turn.error is not None:
                    error = ev.payload.turn.error.message
        total = time.perf_counter() - t0
        label = "первый" if number == 1 else "тёплый"
        first_s = f"{first:.2f} с" if first is not None else "нет текста"
        print(f"{number} ({label}): до первых слов {first_s}, всего {total:.2f} с | {status} {error} | {''.join(parts)[:80]}")
finally:
    codex.close()
