"""Прогон сценариев: временное приложение, авто-клиент, проверка ожиданий, отчёт.

Каждый сценарий получает свой временный «компьютер»: рабочую папку с файлами сценария, данные Jarvis
и домашнюю папку пользователя. Зоны политики строятся по ним, а не по настоящему компьютеру.
"""

import asyncio
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, JsonValue

from jarvis.adapters.memory import InMemoryStorage
from jarvis.app.composition import App, Storage, build_app, host_zones
from jarvis.config import with_overrides
from jarvis.core.models.prompt import DATA_CLOSE, DATA_OPEN
from jarvis.domain.agent import AgentState
from jarvis.domain.approvals import ApprovalStatus
from jarvis.domain.budget import BudgetUsage
from jarvis.domain.ids import TaskId
from jarvis.domain.models import BackendRequest, ModelCapabilities, ModelRole
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, TaskRequest, TaskSnapshot
from jarvis.domain.tools import ToolOutcome, ToolOutcomeKind
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.evals.models import ScriptedModel
from jarvis.evals.scenario import TOOL_EVENTS, ClientRules, Expectation, ModelScript, Scenario, ScriptStep
from jarvis.evals.scripted import ScriptedStages
from jarvis.evals.tools import SleepTool

SCENARIO_TIMEOUT_S = 30.0
APPROVAL_ROUNDS = 5  # сколько раз авто-клиент отвечает на запросы подтверждения в одном сценарии


class ScenarioResult(BaseModel, frozen=True):
    id: str
    task_id: TaskId
    passed: bool
    status: TaskStatus
    transitions: list[TaskStatus]
    usage: BudgetUsage
    problems: list[str]
    duration_ms: int
    root: str  # временная папка сценария (после прогона удалена): метка путей при сравнении трасс


class EvalReport(BaseModel, frozen=True):
    mode: str = "scripted"
    started_at: datetime
    results: list[ScenarioResult]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


async def run_scenarios(scenarios: Sequence[Scenario], config: JarvisConfig) -> EvalReport:
    started_at = datetime.now(UTC)
    results = [await run_scenario(scenario, config) for scenario in scenarios]
    return EvalReport(started_at=started_at, results=results)


async def run_scenario(
    scenario: Scenario,
    config: JarvisConfig,
    *,
    timeout_s: float = SCENARIO_TIMEOUT_S,
    storage: Storage | None = None,
) -> ScenarioResult:
    """Прогнать сценарий в отдельном приложении. По умолчанию хранилище — в памяти; с `storage`
    прогон сохраняется (например, в SQLite), и его трассу можно открыть позже."""
    started = time.perf_counter()
    if scenario.budget:
        routes = ("direct", "chat", "agent")
        config = with_overrides(config, {"budgets": dict.fromkeys(routes, scenario.budget)})
    storage = storage if storage is not None else InMemoryStorage()
    with tempfile.TemporaryDirectory(prefix="jarvis-eval-") as temp:
        machine = _Machine.create(Path(temp).resolve(), scenario.files)
        hung = asyncio.Event()  # шаг завис или инструмент eval начал ждать — клиент может отменять
        script = ScriptedStages([machine.bind(step) for step in scenario.script or []], hung=hung)
        model = _model(scenario.model, machine, hung) if scenario.model is not None else None
        app = build_app(
            config,
            stages=script.handlers if model is None else None,
            models={ModelRole.EXECUTOR: model} if model is not None else None,
            storage=storage,
            home=machine.home,
            zones=host_zones(config, home=machine.home, user_home=machine.user),
            extra_tools=[SleepTool(hung)],
        )
        request = TaskRequest(
            text=scenario.input,
            origin=Origin.EVAL,
            working_directory=str(machine.workspace),
            dry_run=scenario.dry_run,
        )
        task_id = app.tasks.submit(request)

        problems: list[str] = []
        try:
            async with asyncio.timeout(timeout_s):
                await _drive(app, task_id, scenario.client, hung)
        except TimeoutError:
            problems.append(f"сценарий не завершился за {timeout_s} с")

        snapshot = app.tasks.get(task_id)
        events = app.tasks.trace(task_id)
        transitions = [
            TaskStatus(str(event.payload["to"]))
            for event in events
            if event.kind is EventKind.TASK_TRANSITION
        ]
        problems.extend(_check(scenario.expect, snapshot, transitions, events))
        problems.extend(_check_tools(scenario.expect, events, script.outcomes, machine.workspace))
        if model is not None:
            with storage.unit_of_work() as uow:
                state = uow.tasks.get(task_id).state or AgentState()
            problems.extend(_check_agent(scenario.expect, state, model.requests))
    if script.remaining:
        problems.append(f"не проиграно шагов сценария: {script.remaining}")
    if model is not None and model.remaining:
        problems.append(f"не проиграно реплик модели: {model.remaining}")
    return ScenarioResult(
        id=scenario.id,
        task_id=task_id,
        passed=not problems,
        status=snapshot.status,
        transitions=transitions,
        usage=snapshot.usage,
        problems=problems,
        duration_ms=round((time.perf_counter() - started) * 1000),
        root=str(machine.root),
    )


@dataclass(frozen=True)
class _Machine:
    root: Path
    workspace: Path
    home: Path  # JARVIS_HOME сценария: его данные — внутренняя зона
    user: Path  # домашняя папка пользователя: её .ssh и другие — зона секретов

    @classmethod
    def create(cls, root: Path, files: dict[str, str]) -> "_Machine":
        machine = cls(root=root, workspace=root / "workspace", home=root / "jarvis-home", user=root / "user")
        for folder in (machine.workspace, machine.home / "data", machine.user / ".ssh"):
            folder.mkdir(parents=True)
        (machine.home / "data" / "jarvis.db").write_bytes(b"SQLite format 3\x00")
        (machine.user / ".ssh" / "id_ed25519").write_text("PRIVATE KEY", encoding="utf-8")
        for name, content in files.items():
            path = machine.workspace / name
            if name.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return machine

    def bind(self, step: ScriptStep) -> ScriptStep:
        """Подставить пути этого «компьютера» в аргументы вызова."""
        if step.tool is None:
            return step
        arguments = {key: self.substitute(value) for key, value in step.tool.arguments.items()}
        return step.model_copy(update={"tool": step.tool.model_copy(update={"arguments": arguments})})

    def substitute(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return value.replace("{workspace}", str(self.workspace)).replace("{jarvis_home}", str(self.home))
        if isinstance(value, list):
            return [self.substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: self.substitute(item) for key, item in value.items()}
        return value


def _model(script: ModelScript, machine: "_Machine", hung: asyncio.Event) -> ScriptedModel:
    replies = [
        reply.model_copy(update={"json_": machine.substitute(reply.json_)})
        if reply.json_ is not None
        else reply
        for reply in script.replies
    ]
    capabilities = ModelCapabilities(
        structured_output=script.structured_output, context_window=script.context_window
    )
    return ScriptedModel(replies, capabilities=capabilities, hung=hung)


def _check_agent(expect: Expectation, state: AgentState, requests: list[BackendRequest]) -> list[str]:
    problems: list[str] = []
    if expect.observations is not None:
        actual = [step.observation.status for step in state.steps if step.observation is not None]
        if actual != expect.observations:
            problems.append(f"итоги вызовов агента {_join(actual)}, ожидались {_join(expect.observations)}")
    for text in expect.data_only or []:
        seen = False
        for request in requests:
            for message in request.messages:
                outside, inside = _split_data(message.content)
                if text in outside:
                    problems.append(f"«{text}» попало в промпт вне блока DATA")
                seen = seen or text in inside
        if not seen:
            problems.append(f"«{text}» ни разу не попало в блок DATA")
    return sorted(set(problems), key=problems.index)


def _split_data(content: str) -> tuple[str, str]:
    """Текст сообщения вне блоков DATA и внутри них."""
    outside: list[str] = []
    inside: list[str] = []
    rest = content
    while DATA_OPEN in rest:
        before, _, block = rest.partition(DATA_OPEN)
        outside.append(before)
        data, _, rest = block.partition(DATA_CLOSE)
        inside.append(data)
    outside.append(rest)
    return "".join(outside), "".join(inside)


async def _drive(app: App, task_id: TaskId, client: ClientRules, hung: asyncio.Event) -> None:
    """Авто-клиент: продвигает задачу, отменяет зависшую, отвечает на запросы подтверждения."""
    for _ in range(APPROVAL_ROUNDS + 1):
        run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
        if client.cancel_on_hang:
            waiter = asyncio.create_task(hung.wait())
            await asyncio.wait({run, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter.done():
                app.tasks.cancel(task_id, "отмена клиентом eval")
            waiter.cancel()
        snapshot = await run
        if snapshot.status is not TaskStatus.WAITING_CONFIRMATION or client.approval is None:
            return
        pending = [item for item in app.tasks.approvals(task_id) if item.status is ApprovalStatus.PENDING]
        if not pending:
            return
        app.tasks.resolve_approval(pending[-1].id, client.approval, via="eval-auto")


def _check_tools(
    expect: Expectation, events: list[TraceEvent], outcomes: list[ToolOutcome], workspace: Path
) -> list[str]:
    problems: list[str] = []
    if expect.tool_events is not None:
        actual = [event.kind for event in events if event.kind in TOOL_EVENTS]
        if actual != expect.tool_events:
            problems.append(f"события инструментов {_join(actual)}, ожидались {_join(expect.tool_events)}")
    if expect.tool_outcomes is not None:
        kinds = [outcome.kind for outcome in outcomes]
        if kinds != expect.tool_outcomes:
            problems.append(f"итоги вызовов {_join(kinds)}, ожидались {_join(expect.tool_outcomes)}")
    if expect.rules is not None:
        decided = [event.payload.get("rules") for event in events if event.kind is EventKind.POLICY_DECIDED]
        last = decided[-1] if decided else None
        if last != expect.rules:
            problems.append(f"правила политики {last}, ожидались {expect.rules}")
    if expect.found is not None:
        executed = [outcome for outcome in outcomes if outcome.kind is ToolOutcomeKind.EXECUTED]
        output = executed[-1].result.output if executed and executed[-1].result else {}
        found = [_relative(path, workspace) for path in _paths(output)]
        if found != expect.found:
            problems.append(f"найдено {found}, ожидалось {expect.found}")
    return problems


def _paths(output: dict[str, JsonValue]) -> list[str]:
    for key in ("matches", "entries"):
        items = output.get(key)
        if isinstance(items, list):
            return [str(item["path"]) for item in items if isinstance(item, dict) and "path" in item]
    path = output.get("path")
    return [str(path)] if path is not None else []


def _relative(path: str, workspace: Path) -> str:
    try:
        return Path(path).relative_to(workspace).as_posix() or "."
    except ValueError:
        return path


def _join(items: Sequence[object]) -> str:
    return " → ".join(str(item) for item in items) or "(нет)"


def _check(
    expect: Expectation,
    snapshot: TaskSnapshot,
    transitions: list[TaskStatus],
    events: list[TraceEvent],
) -> list[str]:
    problems: list[str] = []
    if snapshot.status is not expect.status:
        problems.append(f"статус {snapshot.status}, ожидался {expect.status}")
    if expect.transitions is not None and transitions != expect.transitions:
        actual = " → ".join(transitions)
        expected = " → ".join(expect.transitions)
        problems.append(f"переходы {actual}, ожидались {expected}")
    for field, expected_value in (expect.usage or {}).items():
        actual_value = getattr(snapshot.usage, field)
        if actual_value != expected_value:
            problems.append(f"usage.{field} = {actual_value}, ожидалось {expected_value}")
    if expect.error_category is not None:
        error = snapshot.outcome.error if snapshot.outcome else None
        category = error.category if error else None
        if category != expect.error_category:
            problems.append(f"категория ошибки {category}, ожидалась {expect.error_category}")
    answer = snapshot.outcome.answer if snapshot.outcome else None
    for part in expect.answer_contains or []:
        if answer is None or part not in answer:
            problems.append(f"в ответе нет «{part}»: {answer!r}")
    if expect.budget_limit is not None:
        limits = [event.payload.get("limit") for event in events if event.kind is EventKind.BUDGET_EXCEEDED]
        if limits != [expect.budget_limit.value]:
            problems.append(f"события budget.exceeded {limits}, ожидалось [{expect.budget_limit.value}]")
    return problems
