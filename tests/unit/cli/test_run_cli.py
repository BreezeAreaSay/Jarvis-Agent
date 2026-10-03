"""`jarvis run` изнутри: вопрос человеку, Ctrl+C, закрытый запрос, экранирование данных в терминале."""

import asyncio
import signal
from datetime import UTC, datetime, timedelta

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.app.composition import App, build_app
from jarvis.cli import run as cli_run
from jarvis.domain.approvals import ApprovalRequest, ApprovalStatus
from jarvis.domain.ids import TaskId
from jarvis.domain.models import ModelRole
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, TaskRequest
from jarvis.domain.tools import (
    EffectKind,
    ExecutionTarget,
    TargetKind,
    ToolCall,
    ToolCallId,
    ToolEffect,
    ToolId,
)
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.evals.models import ModelReply, ScriptedModel
from tests.helpers import approval_tool

HOST = ExecutionTarget(kind=TargetKind.HOST, os_family="posix", name="local")
READ_KEY = ModelReply.model_validate(
    {
        "json": {
            "decision": "читаю",
            "action": {"type": "tool", "tool": "test.secret", "arguments": {"path": "/k"}},
        }
    }
)
FINISH = ModelReply.model_validate(
    {"json": {"decision": "всё", "action": {"type": "finish", "answer": "готово", "evidence": []}}}
)


def agent_app(clock: ManualClock, *replies: ModelReply) -> tuple[App, TaskId]:
    app = build_app(
        JarvisConfig(),
        models={ModelRole.EXECUTOR: ScriptedModel(replies)},
        storage=InMemoryStorage(),
        clock=clock,
        tools=[approval_tool()],
    )
    return app, app.tasks.submit(TaskRequest(text="покажи ключ", origin=Origin.CLI))


def test_ctrl_c_at_the_approval_prompt_cancels_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    app, task_id = agent_app(ManualClock(), READ_KEY)
    handlers: list[object] = []

    def interrupted(approval: ApprovalRequest) -> bool:
        handlers.append(signal.getsignal(signal.SIGINT))
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_run, "_ask", interrupted)
    before = signal.getsignal(signal.SIGINT)
    snapshot = asyncio.run(cli_run._drive(app, task_id))  # pyright: ignore[reportPrivateUsage]
    assert handlers == [signal.default_int_handler]  # у вопроса Ctrl+C — обычное прерывание
    assert snapshot.status is TaskStatus.CANCELLED
    assert signal.getsignal(signal.SIGINT) == before


def test_an_approval_that_closed_while_the_human_thought_lets_the_task_go_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = ManualClock()
    app, task_id = agent_app(clock, READ_KEY, FINISH)

    def too_late(approval: ApprovalRequest) -> bool:
        clock.advance(JarvisConfig().policy.approval_ttl_s + 1)
        return True

    monkeypatch.setattr(cli_run, "_ask", too_late)
    snapshot = asyncio.run(cli_run._drive(app, task_id))  # pyright: ignore[reportPrivateUsage]
    assert snapshot.status is TaskStatus.COMPLETED
    assert "истёк" in capsys.readouterr().err
    statuses = [item.status for item in app.tasks.approvals(task_id)]
    assert statuses == [ApprovalStatus.USED]


def event(kind: EventKind, **payload: object) -> TraceEvent:
    return TraceEvent(
        id="task_1.ev_1", task_id=TaskId("task_1"), seq=1, ts=datetime(2026, 1, 1, tzinfo=UTC), kind=kind,
        payload=dict(payload),  # type: ignore[arg-type]
    )  # fmt: skip


def test_progress_lines_escape_control_and_bidi_characters() -> None:
    line = cli_run.progress_line(
        event(EventKind.ACTION_PROPOSED, step=1, type="tool", tool="fs\x1b[2J", decision="ок‮тxt.exe")
    )
    assert line == "· шаг 1: fs\\x1b[2J — модель: «ок\\u202eтxt.exe»"
    denied = cli_run.progress_line(event(EventKind.POLICY_DECIDED, outcome="deny", reason="путь\x07"))
    assert denied == "  отказано: путь\\x07"
    invalid = cli_run.progress_line(event(EventKind.MODEL_CALLED, status="invalid", problems=["тег\x1b]0;x"]))
    assert invalid == "  ответ модели не принят: тег\\x1b]0;x"


def test_the_approval_card_shows_data_escaped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    approval = ApprovalRequest(
        id="task_1.appr_1",
        task_id=TaskId("task_1"),
        call=ToolCall(
            id=ToolCallId("task_1.call_1"),
            task_id=TaskId("task_1"),
            tool_id=ToolId("filesystem.read_text"),
            arguments={},
            target=HOST,
        ),
        summary="Прочитать /k/\x1b[8mскрыто",
        effects=[ToolEffect(kind=EffectKind.READ, resource="/k/‮me.pem")],
        target=HOST,
        arguments={"path": "/k/\x1b[8m\u2066evil"},
        preview_fingerprint="f" * 64,
        status=ApprovalStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    monkeypatch.setattr(cli_run.typer, "confirm", lambda *args, **kwargs: False)
    assert cli_run._ask(approval) is False  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "‮" not in out
    assert "\\x1b[8mскрыто" in out
    assert "\\u202eme.pem" in out
    assert "\\u2066evil" in out  # json.dumps не экранирует bidi — это делает очистка
