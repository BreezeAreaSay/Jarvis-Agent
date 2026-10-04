"""Прямые команды (ADR 0026, ADR 0030): Router → Tool Runtime с исполнителем DIRECT, без модели."""

import inspect
from pathlib import Path

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.inventory import StaticInventory
from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.tools import HOST, OS_FAMILY
from jarvis.app.composition import App, build_app
from jarvis.core.direct.commands import answer, direct_call
from jarvis.core.direct.stage import DirectStage
from jarvis.core.policy import PolicyZones
from jarvis.domain.approvals import ApprovalDecision
from jarvis.domain.intents import EntityKind, IntentId, ResolvedEntity
from jarvis.domain.inventory import AppEntry
from jarvis.domain.models import ModelRole
from jarvis.domain.routing import Route, RouteDecision
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus as S
from jarvis.domain.task import Origin, TaskRequest, TaskSnapshot
from jarvis.domain.trace import EventKind
from jarvis.evals.models import ScriptedModel
from jarvis.ports.launcher import LaunchTarget
from tests.helpers import error_of, transitions

pytestmark = pytest.mark.anyio

TELEGRAM = AppEntry(
    id="telegram",
    name="Telegram Desktop",
    aliases=["телеграм"],
    target="C:/Apps/Telegram.lnk",
    kind="shortcut",
)


class Direct:
    """Приложение с настоящими стадиями, scripted-моделью без реплик и записывающим запуском."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path.resolve()
        self.secrets = self.root / "user" / ".ssh"
        self.secrets.mkdir(parents=True)
        self.launched: list[LaunchTarget] = []
        self.model = ScriptedModel([])
        self.app: App = build_app(
            JarvisConfig(),
            models={ModelRole.EXECUTOR: self.model},
            storage=InMemoryStorage(),
            clock=ManualClock(),
            inventory=StaticInventory([TELEGRAM]),
            launcher=self.launched.append,
            target=HOST,
            zones=PolicyZones(os_family=OS_FAMILY, secrets=(str(self.secrets),)),
        )

    async def run(self, text: str, *, dry_run: bool = False) -> TaskSnapshot:
        request = TaskRequest(
            text=text, origin=Origin.EVAL, working_directory=str(self.root), dry_run=dry_run
        )
        self.task_id = self.app.tasks.submit(request)
        return await self.app.tasks.run_until_blocked(self.task_id)

    def kinds(self) -> list[EventKind]:
        skip = (EventKind.TASK_CREATED, EventKind.TASK_TRANSITION)
        return [event.kind for event in self.app.tasks.trace(self.task_id) if event.kind not in skip]


async def test_a_direct_command_runs_without_a_model(tmp_path: Path) -> None:
    direct = Direct(tmp_path)
    snapshot = await direct.run("открой телеграм")
    assert snapshot.status is S.COMPLETED
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "Запускаю Telegram Desktop."
    assert transitions(direct.app, direct.task_id) == [S.ROUTING, S.EXECUTING, S.VERIFYING, S.COMPLETED]
    assert direct.kinds() == [
        EventKind.ROUTE_DECIDED,
        EventKind.ACTION_PROPOSED,
        EventKind.TOOL_PREVIEWED,
        EventKind.POLICY_DECIDED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FINISHED,
        EventKind.TOOL_VERIFIED,
        EventKind.TASK_FINISHED,
    ]
    assert snapshot.usage.model_calls == 0
    assert snapshot.usage.tool_calls == 1
    assert direct.model.requests == []
    assert direct.launched == [LaunchTarget("app", "C:/Apps/Telegram.lnk", "shortcut")]
    assert snapshot.route is Route.DIRECT
    assert snapshot.routing is not None
    assert snapshot.routing.intent is IntentId.APP_LAUNCH
    assert snapshot.budget == JarvisConfig().budgets.direct
    assert snapshot.budget.max_model_calls == 0
    policy = [e for e in direct.app.tasks.trace(direct.task_id) if e.kind is EventKind.POLICY_DECIDED]
    assert policy[0].payload["rules"] == ["launch.app.direct"]


async def test_a_direct_folder_with_secrets_waits_for_a_human(tmp_path: Path) -> None:
    direct = Direct(tmp_path)
    snapshot = await direct.run(f"открой папку {direct.secrets}")
    assert snapshot.status is S.WAITING_CONFIRMATION
    assert direct.launched == []
    (approval,) = direct.app.tasks.approvals(direct.task_id)
    direct.app.tasks.resolve_approval(approval.id, ApprovalDecision.APPROVE, via="test")
    snapshot = await direct.app.tasks.run_until_blocked(direct.task_id)
    assert snapshot.status is S.COMPLETED
    assert direct.launched == [LaunchTarget("folder", str(direct.secrets))]
    assert snapshot.usage.model_calls == 0


async def test_a_denied_direct_command_ends_the_task_without_a_model(tmp_path: Path) -> None:
    direct = Direct(tmp_path)
    await direct.run(f"открой папку {direct.secrets}")
    (approval,) = direct.app.tasks.approvals(direct.task_id)
    direct.app.tasks.resolve_approval(approval.id, ApprovalDecision.DENY, via="test")
    snapshot = await direct.app.tasks.run_until_blocked(direct.task_id)
    assert snapshot.status is S.FAILED
    assert error_of(snapshot).category == "tool_denied"
    assert direct.launched == []
    assert direct.model.requests == []


async def test_dry_run_launches_nothing(tmp_path: Path) -> None:
    direct = Direct(tmp_path)
    snapshot = await direct.run("открой телеграм", dry_run=True)
    assert snapshot.status is S.COMPLETED
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "Dry run: Запустить Telegram Desktop — не исполнялось."
    assert direct.launched == []


async def test_a_clarifying_question_is_the_answer(tmp_path: Path) -> None:
    direct = Direct(tmp_path)
    twin = TELEGRAM.model_copy(update={"id": "telegram-beta", "name": "Telegram Beta"})
    direct.app = build_app(
        JarvisConfig(),
        models={ModelRole.EXECUTOR: direct.model},
        storage=InMemoryStorage(),
        clock=ManualClock(),
        inventory=StaticInventory([TELEGRAM, twin]),
        launcher=direct.launched.append,
    )
    snapshot = await direct.run("открой телеграм")
    assert snapshot.status is S.COMPLETED
    assert snapshot.route is Route.CLARIFY
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "Какое приложение открыть: «Telegram Desktop», «Telegram Beta»?"
    assert direct.launched == []
    assert direct.kinds() == [EventKind.ROUTE_DECIDED, EventKind.TASK_FINISHED]


def test_the_direct_stage_has_no_way_to_call_a_model() -> None:
    assert set(inspect.signature(DirectStage).parameters) == {"tools", "uow", "tracer"}


def decision(intent: IntentId, *entities: tuple[EntityKind, str]) -> RouteDecision:
    return RouteDecision(
        strategy=Route.DIRECT,
        intent=intent,
        entities=[
            ResolvedEntity(kind=kind, value=value, label=value, source="command") for kind, value in entities
        ],
        rules=["test"],
        reason="test",
    )


@pytest.mark.parametrize(
    ("made", "tool", "arguments"),
    [
        (decision(IntentId.APP_LAUNCH, (EntityKind.APP, "telegram")), "app.launch", {"app": "telegram"}),
        (
            decision(IntentId.URL_OPEN, (EntityKind.URL, "https://ya.ru")),
            "url.open",
            {"url": "https://ya.ru"},
        ),
        (decision(IntentId.FOLDER_OPEN, (EntityKind.FOLDER, "/w")), "folder.open", {"path": "/w"}),
        (decision(IntentId.FS_CURRENT), "system.cwd", {}),
        (
            decision(IntentId.FS_LIST, (EntityKind.FOLDER, "/w")),
            "filesystem.list",
            {"path": "/w", "max_entries": 200},
        ),
        (
            decision(IntentId.FS_SEARCH, (EntityKind.PATTERN, "*.pdf"), (EntityKind.FOLDER, "/w")),
            "filesystem.search",
            {"root": "/w", "pattern": "*.pdf", "max_results": 100},
        ),
        (decision(IntentId.PROCESS_LIST), "process.list", {"max_results": 200}),
        (
            decision(IntentId.PROCESS_LIST, (EntityKind.PROCESS, "python")),
            "process.list",
            {"max_results": 200, "name": "python"},
        ),
    ],
)
def test_arguments_come_only_from_the_router_decision(
    made: RouteDecision, tool: str, arguments: dict[str, object]
) -> None:
    call = direct_call(made)
    assert (call.tool, call.arguments) == (tool, arguments)


def test_answers_are_templates_over_the_tool_output() -> None:
    listing = {
        "path": "/w",
        "entries": [{"name": "docs", "kind": "dir"}, {"name": "a.txt", "kind": "file", "size": 2048}],
        "total": 2,
    }
    assert answer(decision(IntentId.FS_LIST, (EntityKind.FOLDER, "/w")), listing) == (
        "/w — 2 элемента:\n  docs/\n  a.txt (2.0 КБ)"
    )
    found = {"root": "/w", "pattern": "*.pdf", "matches": [{"path": "/w/sub/b.pdf", "kind": "file"}]}
    assert answer(decision(IntentId.FS_SEARCH, (EntityKind.PATTERN, "*.pdf")), found) == (
        "Найдено 1 по шаблону «*.pdf» в /w:\n  sub/b.pdf"
    )
    empty = {"root": "/w", "pattern": "*.pdf", "matches": [], "limits_hit": ["max_depth"]}
    assert answer(decision(IntentId.FS_SEARCH, (EntityKind.PATTERN, "*.pdf")), empty) == (
        "По шаблону «*.pdf» в /w ничего не найдено. "
        "Поиск остановлен на пределе — результат может быть неполным."
    )
    processes = {"processes": [{"name": "python", "pid": 7}], "total": 1}
    assert answer(decision(IntentId.PROCESS_LIST, (EntityKind.PROCESS, "python")), processes) == (
        "Процессы «python»: 1\n  python (pid 7)"
    )
