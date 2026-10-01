"""Проверка на здравый смысл, а не бенчмарк: большие папки, большое дерево поиска и сотни вызовов
не должны приводить к зависанию или квадратичному росту. Пороги щедрые — ловится порядок."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.adapters.tools import HOST
from jarvis.adapters.tools.filesystem import (
    ListArgs,
    ListOutput,
    ListTool,
    SearchArgs,
    SearchOutput,
    SearchTool,
)
from jarvis.app.composition import build_app
from jarvis.core.budget import BudgetMeter
from jarvis.domain.budget import Budget, BudgetUsage
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.task import Origin, TaskRequest
from jarvis.domain.tools import ToolOutcomeKind
from jarvis.ports.tools import ToolContext

pytestmark = pytest.mark.anyio

FILES_IN_FOLDER = 5000
TREE = (40, 50)  # папок × файлов в каждой
CALLS = 300

Storage = InMemoryStorage | SqliteStorage


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    if request.param == "memory":
        yield InMemoryStorage()
        return
    with SqliteStorage(tmp_path / "jarvis.db") as sqlite:
        yield sqlite


def context(root: Path) -> ToolContext:
    return ToolContext(target=HOST, working_directory=str(root))


async def test_listing_a_huge_folder_is_bounded(tmp_path: Path) -> None:
    for n in range(FILES_IN_FOLDER):
        (tmp_path / f"file-{n:05}.txt").touch()
    tool = ListTool()
    started = time.perf_counter()
    preview = await tool.preview(ListArgs(path=".", max_entries=1000), context(tmp_path))
    output = await tool.execute(ListArgs.model_validate(preview.normalized_arguments), context(tmp_path))
    elapsed = time.perf_counter() - started
    assert isinstance(output, ListOutput)
    assert (len(output.entries), output.total, output.truncated) == (1000, FILES_IN_FOLDER, True)
    assert elapsed < 5.0, elapsed


async def test_searching_a_large_tree(tmp_path: Path) -> None:
    folders, files = TREE
    for d in range(folders):
        folder = tmp_path / f"dir-{d:03}" / "nested"
        folder.mkdir(parents=True)
        for n in range(files):
            (folder / f"doc-{n:03}.{'pdf' if n % 10 == 0 else 'txt'}").touch()
    tool = SearchTool()
    arguments = SearchArgs(root=str(tmp_path), pattern="*.pdf", max_results=1000)
    started = time.perf_counter()
    output = await tool.execute(arguments, context(tmp_path))
    elapsed = time.perf_counter() - started
    assert isinstance(output, SearchOutput)
    assert len(output.matches) == folders * files // 10
    assert output.scanned == folders * (files + 2)  # файлы, папка nested и сама папка dir-NNN
    assert output.limits_hit == []
    assert elapsed < 10.0, elapsed


async def test_hundreds_of_calls_do_not_slow_down(storage: Storage, tmp_path: Path) -> None:
    app = build_app(JarvisConfig(), stages={}, storage=storage, clock=ManualClock(), owner="A")
    task_id = app.tasks.submit(TaskRequest(text="t", origin=Origin.EVAL, working_directory=str(tmp_path)))
    with storage.unit_of_work() as uow:
        task = uow.tasks.get(task_id)
    budget = Budget(
        max_steps=1,
        max_tool_calls=CALLS,
        max_failures=1,
        max_replans=0,
        max_wall_time_s=600,
        max_model_calls=0,
        max_model_tokens=0,
    )
    meter = BudgetMeter(budget, BudgetUsage())

    async def chunk() -> float:
        started = time.perf_counter()
        for _ in range(CALLS // 6):
            outcome = await app.tools.call(task, meter, "system.cwd", {})
            assert outcome.kind is ToolOutcomeKind.EXECUTED
        return time.perf_counter() - started

    durations = [await chunk() for _ in range(6)]
    assert max(durations[-2:]) < 4 * max(durations[0], 0.05), durations
    assert sum(durations) < 30.0, durations
    assert app.tasks.inspect(task_id).metrics.tool_calls == CALLS
