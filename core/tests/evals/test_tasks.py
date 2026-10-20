"""Задачи проверки агента в детерминированном режиме: настоящий агент, модель по сценарию, мир — имитатор.

Эталонное решение каждой задачи — успех без замечаний; каждое нарочно ошибочное — провал с ожидаемой
причиной. Так проверяются и сам имитатор (по снимку окна и картинке экрана решение находит нужное), и
проверки задач (ловят именно эти ошибки)."""

import pytest

from evals.harness import Options, attempt
from evals.scoring import score
from evals.tasks import TASKS, Task

MISTAKES = [(task, name) for task in TASKS for name in task.mistakes]


def test_catalogue() -> None:
    assert 15 <= len(TASKS) <= 20
    assert len({t.id for t in TASKS}) == len(TASKS)
    assert {t.category for t in TASKS} == {"everyday", "hard", "safety"}
    assert all(t.mistakes for t in TASKS)  # у каждой проверки есть ошибка, которую она должна поймать
    ids = {t.id for t in TASKS}
    assert {"tg_mama", "music_play", "volume_30", "bt_on", "notepad_note", "find_file", "mail_draft"} <= ids
    assert {
        "ambiguous_anya",
        "popup_window",
        "window_switched",
        "no_uia_playlist",
        "slow_folder",
        "tiny_text",
    } <= ids
    assert {"injection_mail", "risky_delete", "foreign_window"} <= ids


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
async def test_reference_solution_passes(task: Task) -> None:
    outcome = await attempt(task, Options(journals="none"))
    result = score(task, outcome.desktop, outcome.trace, script_errors=outcome.script_errors)
    assert result.ok, result.reason
    assert result.warnings == []
    assert result.steps >= 1 and result.prompt_tokens > 0


@pytest.mark.parametrize(("task", "mistake"), MISTAKES, ids=[f"{t.id}-{m}" for t, m in MISTAKES])
async def test_mistake_is_caught(task: Task, mistake: str) -> None:
    outcome = await attempt(task, Options(variant=mistake, journals="none"))
    result = score(task, outcome.desktop, outcome.trace, variant=mistake, script_errors=outcome.script_errors)
    expected = task.mistakes[mistake][1]
    assert not result.ok
    assert expected in result.reason, result.reason


async def test_approvals_and_questions_are_recorded() -> None:
    from evals.tasks import BY_ID

    task = BY_ID["ambiguous_anya"]
    outcome = await attempt(task, Options(journals="none"))
    trace = outcome.trace
    assert [(q.answer, q.text.startswith("Нашлись две Ани")) for q in trace.questions] == [
        ("Ане Смирновой", True)
    ]
    (approval,) = trace.approvals
    assert (
        approval.allowed
        and approval.title.startswith("Нажать enter")
        and "Перезвоню позже" in approval.detail
    )
    assert outcome.desktop.telegram.sent() == {"Аня Смирнова": ["Перезвоню позже"]}
