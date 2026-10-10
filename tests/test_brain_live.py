"""Мозг вживую (@live — запускает только владелец: нужен вход в GPT, тратит квоту, открывает блокнот).

    uv run pytest -q -s -m live tests\\test_brain_live.py

Тест работает с НАСТОЯЩИМ каталогом данных (там CODEX_HOME мозга со входом): переменные JARVIS_DATA_DIR и
JARVIS_CONFIG берутся такими, какими были до тестовой фикстуры. Итог печатается и дописывается в
bench/results/brain-live-<дата>.txt — только числа, без текста ответов и заголовков окон.
"""

import datetime
import math
import os
import time
from pathlib import Path

import pytest

from jarvis import config
from jarvis.brain import Brain
from jarvis.context import Context
from jarvis.events import Done, Event, Status
from pc.confirm_client import ConfirmServer

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[1]
REAL_ENV = {
    name: os.environ.get(name) for name in ("JARVIS_DATA_DIR", "JARVIS_CONFIG")
}  # до фикстуры conftest
BUDGET_S = 3.0  # p95 до первых слов — и в новом треде, и в повторных вопросах
# пять разговоров: первый вопрос — в новом треде (запасной), второй — повторный в том же треде
QUESTIONS = [
    ("Привет! Ответь одним словом.", "А теперь двумя словами."),
    ("Сколько будет 17*23?", "А умножить на два?"),
    ("Столица Чили?", "А её население примерно?"),
    ("Что такое VRAM? Одним предложением.", "Сколько её у RX 7600?"),
    ("Назови один цвет радуги.", "Ещё один."),
]


@pytest.fixture
def real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REAL_ENV.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    if config.load().mode == "local":
        pytest.skip("в jarvis.toml локальный режим — мозг не запускается")


@pytest.fixture
def live_brain(real_env: None):
    server = ConfirmServer(lambda summary, details, caller: False)  # подтверждений не ждём — всё «нет»
    server.start()
    brain = Brain(config.load().brain, server.address)
    t0 = time.perf_counter()
    brain.start()
    wait_for(lambda: brain.status["state"] != "starting", 60)
    assert brain.ready, brain.status
    brain.cold_start_s = time.perf_counter() - t0  # type: ignore[attr-defined]
    yield brain
    brain.close()
    server.close()


def wait_for(cond, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def spare_ready(brain: Brain) -> bool:
    spare = brain.status["threads"]["spare"]
    return spare is not None and spare["mcp"] != "starting"


def ask(brain: Brain, text: str) -> tuple[Done, list[Event]]:
    events = list(brain.ask(text, Context()))
    done = events[-1]
    assert isinstance(done, Done), events
    return done, events


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def series(name: str, values: list[float]) -> str:
    if not values:
        return f"{name}: нет данных"
    return f"{name}: {', '.join(f'{x:.2f}' for x in values)} с; p95 {p95(values):.2f} с (бюджет {BUDGET_S} с)"


def report(lines: list[str]) -> None:
    out = ROOT / "bench" / "results" / f"brain-live-{datetime.date.today():%Y-%m-%d}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    print("\n" + text)
    with out.open("a", encoding="utf-8") as f:
        f.write(text)


def test_first_words_latency(live_brain: Brain) -> None:
    brain = live_brain
    wait_for(lambda: spare_ready(brain), 20)  # Jarvis стартует задолго до первого вопроса
    first: list[float] = []
    repeat: list[float] = []
    failures: list[str] = []
    for number, (question, follow_up) in enumerate(QUESTIONS, 1):
        if number > 1:
            brain.new_conversation()
            wait_for(lambda: spare_ready(brain), 20)
        for text, bucket, new_thread in ((question, first, True), (follow_up, repeat, False)):
            done, _ = ask(brain, text)
            t = done.timings.get("t_first_token")
            if not done.ok or t is None or done.timings.get("new_thread") is not new_thread:
                failures.append(
                    f"разговор {number}: ok={done.ok} new_thread={done.timings.get('new_thread')}"
                )
                continue
            bucket.append(float(t))
    lines = [
        f"=== brain live {datetime.datetime.now():%Y-%m-%d %H:%M} ===",
        f"холодный старт codex (start → ready): {brain.cold_start_s:.2f} с",  # type: ignore[attr-defined]
        series("первый вопрос в новом треде", first),
        series("повторные вопросы", repeat),
        f"ошибки: {len(failures)}",
    ]
    report(lines)
    assert not failures, failures
    assert first and repeat
    assert p95(first) <= BUDGET_S, f"первые слова в новом треде p95 {p95(first):.2f} с > {BUDGET_S} с"
    assert p95(repeat) <= BUDGET_S, f"первые слова в повторных p95 {p95(repeat):.2f} с > {BUDGET_S} с"


def test_open_notepad_calls_pc_open(live_brain: Brain) -> None:
    done, events = ask(live_brain, "открой блокнот")
    tools = [e.text for e in events if isinstance(e, Status) and e.kind == "tool" and not e.done]
    called = any(t.startswith("⚙ open") for t in tools)
    verdict = "был" if called else "НЕ был"
    report([f"«открой блокнот»: вызов pc/open {verdict}; инструментов {len(tools)}; ok={done.ok}"])
    assert called, tools


def test_cannot_write_file_on_desktop(live_brain: Brain) -> None:
    from pc import files

    desktop = Path(files.known_folder("рабочий стол") or Path.home() / "Desktop")
    target = desktop / "x.txt"
    if target.exists():
        pytest.skip(f"{target} уже есть — удалите его и повторите")
    done, _ = ask(live_brain, "создай на рабочем столе файл x.txt")
    created = target.exists()
    report([f"«создай файл x.txt»: файл {'СОЗДАН' if created else 'не создан'}; ok={done.ok}"])
    assert not created, "мозг записал файл — sandbox=read_only/deny_all не сработали"
