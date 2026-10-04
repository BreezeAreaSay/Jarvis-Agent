"""Гибридные провайдеры (ADR 0027, ADR 0028): план, граница приватности, состояние, fallback, согласие.

Провайдеры — scripted-модели: локальная и «облачные» (вид REMOTE_MODEL_API). Сети нет: тесты проверяют
архитектуру — что и кому Model Gateway отправляет, а что нет.
"""

import pytest

from jarvis.cli.run import progress_line
from jarvis.core.models.prompt import messages_tokens
from jarvis.domain.approvals import ApprovalDecision
from jarvis.domain.audit import AuditAction
from jarvis.domain.privacy import DataClass
from jarvis.domain.providers import ProviderState
from jarvis.domain.routing import CloudMode
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus as S
from jarvis.domain.trace import EventKind
from jarvis.evals.models import ModelReply
from tests.helpers import error_of
from tests.hybrid import READER, Hybrid, finish, read

pytestmark = pytest.mark.anyio


async def test_cloud_answers_a_smart_task_with_plain_text() -> None:
    hybrid = Hybrid([], {"cloud_a": [finish("Ответ облака")]})
    snapshot = await hybrid.run("Проанализируй эту архитектуру", mode=CloudMode.SMART)
    assert snapshot.status is S.COMPLETED
    assert hybrid.called() == ["cloud_a"]
    assert hybrid.local.requests == []
    (routed,) = hybrid.events(EventKind.MODEL_ROUTED)
    assert routed.payload["level"] == "smart"
    assert routed.payload["order"] == ["cloud_a", "local"]
    (checked,) = hybrid.events(EventKind.PRIVACY_CHECKED)
    assert checked.payload["verdict"] == "allow"
    assert checked.payload["found"] == ["local_metadata"]
    (called,) = hybrid.events(EventKind.MODEL_CALLED)
    assert called.payload["remote"] is True
    assert called.payload["endpoint"] == "cloud_a"
    assert called.payload["kind"] == "remote_model_api"
    with hybrid.storage.unit_of_work() as uow:
        egress = [record for record in uow.audit.list() if record.action is AuditAction.EGRESS]
    assert [record.target for record in egress] == ["remote:cloud_a"]
    assert "classes:local_metadata" in egress[0].resources


async def test_cloud_is_off_by_default() -> None:
    hybrid = Hybrid([finish()], {"cloud_a": []}, cloud={"enabled": False})
    snapshot = await hybrid.run("Проанализируй эту архитектуру", mode=CloudMode.SMART)
    assert snapshot.status is S.COMPLETED
    assert hybrid.remotes["cloud_a"].requests == []
    (routed,) = hybrid.events(EventKind.MODEL_ROUTED)
    reasons = {item["endpoint"]: item["reason"] for item in routed.payload["candidates"]}  # type: ignore[union-attr]
    assert reasons == {"cloud_a": "cloud.disabled", "local": "plan.candidate"}
    assert JarvisConfig().cloud.enabled is False


async def test_local_only_never_calls_a_remote_provider() -> None:
    hybrid = Hybrid([finish()], {"cloud_a": [], "cloud_b": []})
    snapshot = await hybrid.run("Проанализируй эту архитектуру", mode=CloudMode.LOCAL_ONLY)
    assert snapshot.status is S.COMPLETED
    assert snapshot.routing is not None
    assert snapshot.routing.level is not None
    assert snapshot.routing.level.value == "local"
    assert all(model.requests == [] for model in hybrid.remotes.values())
    assert hybrid.called() == ["local"]


async def test_local_levels_never_call_a_remote_provider() -> None:
    hybrid = Hybrid([finish()], {"cloud_a": []})
    await hybrid.run("какая погода в файлах?")  # auto, без признаков — уровень local
    assert hybrid.remotes["cloud_a"].requests == []
    (routed,) = hybrid.events(EventKind.MODEL_ROUTED)
    assert routed.payload["level"] == "local"
    assert routed.payload["order"] == ["local"]


@pytest.mark.parametrize(
    ("error", "state"),
    [
        ("unavailable", ProviderState.UNAVAILABLE),
        ("timeout", ProviderState.UNAVAILABLE),
        ("auth", ProviderState.AUTH_REQUIRED),
        ("rate_limited", ProviderState.RATE_LIMITED),
        ("limit_exceeded", ProviderState.LIMIT_EXCEEDED),
        ("misconfigured", ProviderState.MISCONFIGURED),
    ],
)
async def test_a_failing_provider_falls_back_to_the_local_model(error: str, state: ProviderState) -> None:
    hybrid = Hybrid([finish("локально")], {"cloud_a": [ModelReply.model_validate({"error": error})]})
    snapshot = await hybrid.run("Проанализируй", mode=CloudMode.SMART)
    assert snapshot.status is S.COMPLETED
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "локально"
    assert hybrid.called() == ["cloud_a", "local"]
    (fallback,) = hybrid.events(EventKind.MODEL_FALLBACK)
    assert fallback.payload["from"] == "cloud_a"
    assert fallback.payload["to"] == "local"
    assert fallback.payload["state"] == state.value
    assert snapshot.usage.model_calls == 2  # каждая попытка — из бюджета задачи


async def test_a_failed_provider_is_skipped_until_its_state_expires() -> None:
    limited = ModelReply.model_validate({"error": "rate_limited", "retry_after_s": 120})
    hybrid = Hybrid([finish(), finish(), finish()], {"cloud_a": [limited, finish("облако снова")]})
    await hybrid.run("Проанализируй", mode=CloudMode.SMART)
    assert hybrid.called() == ["cloud_a", "local"]
    await hybrid.run("Проанализируй ещё", mode=CloudMode.SMART)
    assert hybrid.called() == ["local"]  # RATE_LIMITED: без запроса в облако
    (routed,) = hybrid.events(EventKind.MODEL_ROUTED)
    reasons = {item["endpoint"]: item["reason"] for item in routed.payload["candidates"]}  # type: ignore[union-attr]
    assert reasons["cloud_a"] == "state.rate_limited"
    hybrid.clock.advance(121)
    snapshot = await hybrid.run("Проанализируй в третий раз", mode=CloudMode.SMART)
    assert hybrid.called() == ["cloud_a"]
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "облако снова"


async def test_fallback_is_bounded_and_never_returns_to_a_failed_provider() -> None:
    down = ModelReply.model_validate({"error": "unavailable"})
    hybrid = Hybrid(
        [finish("локально")],
        {"cloud_a": [down], "cloud_b": [down], "cloud_c": [finish("не дошли")]},
        routing={"max_providers": 3},
    )
    snapshot = await hybrid.run("Проанализируй", mode=CloudMode.SMART)
    assert hybrid.called() == ["cloud_a", "cloud_b", "local"]  # cloud_c вне лимита попыток
    assert hybrid.remotes["cloud_c"].requests == []
    (routed,) = hybrid.events(EventKind.MODEL_ROUTED)
    reasons = {item["endpoint"]: item["reason"] for item in routed.payload["candidates"]}  # type: ignore[union-attr]
    assert reasons["cloud_c"] == "plan.max_providers"
    assert snapshot.usage.model_calls == 3


async def test_when_every_provider_fails_the_task_says_so() -> None:
    down = ModelReply.model_validate({"error": "unavailable"})
    hybrid = Hybrid([down, down], {"cloud_a": [down]})
    hybrid.app.models._retry_delay_s = 0  # type: ignore[union-attr]
    snapshot = await hybrid.run("Проанализируй", mode=CloudMode.SMART)
    assert snapshot.status is S.FAILED
    assert error_of(snapshot).category == "no_provider_available"
    assert hybrid.called() == ["cloud_a", "local", "local"]  # локальный — с одним повтором (модель грузится)


async def test_invalid_output_after_repair_is_feedback_not_fallback() -> None:
    bad = ModelReply(text="не JSON")
    hybrid = Hybrid([finish()], {"cloud_a": [bad, bad, bad, finish("со второй попытки шага")]})
    await hybrid.run("Проанализируй", mode=CloudMode.SMART)
    assert hybrid.events(EventKind.MODEL_FALLBACK) == []
    assert hybrid.local.requests == []


async def test_a_cloud_reply_is_echoed_back_only_if_it_passes_the_privacy_boundary() -> None:
    # Ответ облака не по схеме, похожий на секрет: ремонт уходит без эха, а не роняет задачу.
    bad = ModelReply(text="не JSON, но тут password=hunter2hunter2")
    hybrid = Hybrid([finish("локально")], {"cloud_a": [bad, finish("облако")]})
    snapshot = await hybrid.run("Проанализируй эту архитектуру", mode=CloudMode.SMART)
    assert snapshot.status is S.COMPLETED
    assert hybrid.called() == ["cloud_a", "cloud_a"]
    repair = hybrid.remotes["cloud_a"].requests[1]
    assert "hunter2" not in "".join(message.content for message in repair.messages)


async def test_a_repair_blocked_by_the_privacy_boundary_falls_back_without_sending() -> None:
    probe = Hybrid([], {"cloud_a": [finish()]})
    await probe.run("Проанализируй эту архитектуру", mode=CloudMode.SMART)
    (first,) = probe.remotes["cloud_a"].requests
    exact = messages_tokens(first.messages)  # первая попытка проходит, ремонт уже нет
    bad = ModelReply(text="не JSON")
    hybrid = Hybrid(
        [finish("локально")], {"cloud_a": [bad, finish("не дойдёт")]}, cloud={"max_prompt_tokens": exact}
    )
    snapshot = await hybrid.run("Проанализируй эту архитектуру", mode=CloudMode.SMART)
    assert snapshot.status is S.COMPLETED
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "локально"
    assert hybrid.called() == ["cloud_a", "local"]
    assert len(hybrid.remotes["cloud_a"].requests) == 1  # ремонт не ушёл
    (fallback,) = hybrid.events(EventKind.MODEL_FALLBACK)
    assert (fallback.payload["category"], fallback.payload["state"]) == ("privacy_violation", None)
    assert progress_line(fallback) == "  cloud_a не вызван (privacy_violation) → local"
    with hybrid.storage.unit_of_work() as uow:
        assert uow.provider_states.get("cloud_a") is None  # провайдер исправен: состояние не меняется


async def test_file_content_needs_consent_and_a_refusal_keeps_the_task_local() -> None:
    hybrid = Hybrid([finish("локальный анализ")], {"cloud_a": [read("/data/main.py")]})
    snapshot = await hybrid.run("Проанализируй main.py", mode=CloudMode.SMART)
    assert snapshot.status is S.WAITING_CONFIRMATION
    (approval,) = hybrid.app.tasks.approvals(hybrid.task_id)
    assert approval.call.tool_id == "cloud.share"
    assert "cloud_a" in approval.summary
    hybrid.app.tasks.resolve_approval(approval.id, ApprovalDecision.DENY, via="test")
    snapshot = await hybrid.run_resume()
    assert snapshot.status is S.COMPLETED
    assert hybrid.called() == ["cloud_a", "local"]
    assert len(hybrid.remotes["cloud_a"].requests) == 1  # содержимое файла в облако не ушло
    checked = hybrid.events(EventKind.PRIVACY_CHECKED)
    assert checked[-1].payload["verdict"] == "consent"
    assert set(checked[-1].payload["blocked"]) == {"file_content", "source_code"}  # type: ignore[arg-type]


async def test_a_refused_class_is_not_asked_again_together_with_a_new_one() -> None:
    # Отказ по коду, затем локальная модель читает личный файл: облаку код всё равно не уйдёт —
    # спрашивать снова (код + личные данные) бессмысленно.
    local = [read("/data/personal/notes.txt"), finish("локально")]
    hybrid = Hybrid(local, {"cloud_a": [read("/data/main.py")]}, cloud={"personal_roots": ["/data/personal"]})
    await hybrid.run("Проанализируй main.py", mode=CloudMode.SMART)
    (approval,) = hybrid.app.tasks.approvals(hybrid.task_id)
    hybrid.app.tasks.resolve_approval(approval.id, ApprovalDecision.DENY, via="test")
    snapshot = await hybrid.run_resume()
    assert snapshot.status is S.COMPLETED
    assert [item.call.tool_id for item in hybrid.app.tasks.approvals(hybrid.task_id)] == ["cloud.share"]
    assert hybrid.called() == ["cloud_a", "local", "local"]


async def test_consent_is_asked_once_and_lasts_until_the_end_of_the_task() -> None:
    replies = [read("/data/main.py"), read("/data/util.py"), finish("облачный анализ")]
    hybrid = Hybrid([], {"cloud_a": replies})
    await hybrid.run("Проанализируй main.py", mode=CloudMode.SMART)
    (approval,) = hybrid.app.tasks.approvals(hybrid.task_id)
    hybrid.app.tasks.resolve_approval(approval.id, ApprovalDecision.APPROVE, via="test")
    snapshot = await hybrid.run_resume()
    assert snapshot.status is S.COMPLETED
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "облачный анализ"
    assert hybrid.called() == ["cloud_a", "cloud_a", "cloud_a"]
    assert len(hybrid.app.tasks.approvals(hybrid.task_id)) == 1  # второй файл того же класса — без вопроса


async def test_a_grant_belongs_to_one_task() -> None:
    hybrid = Hybrid([], {"cloud_a": [read("/data/main.py"), finish("первая"), read("/data/main.py")]})
    await hybrid.run("Проанализируй main.py", mode=CloudMode.SMART)
    (approval,) = hybrid.app.tasks.approvals(hybrid.task_id)
    hybrid.app.tasks.resolve_approval(approval.id, ApprovalDecision.APPROVE, via="test")
    await hybrid.run_resume()
    snapshot = await hybrid.run("Проанализируй main.py ещё раз", mode=CloudMode.SMART)
    assert snapshot.status is S.WAITING_CONFIRMATION  # новая задача — новое согласие
    (second,) = hybrid.app.tasks.approvals(hybrid.task_id)
    assert second.call.tool_id == "cloud.share"


async def test_secrets_never_leave_even_with_consent() -> None:
    hybrid = Hybrid([finish("локально")], {"cloud_a": [read("/project/.env")]})
    snapshot = await hybrid.run(
        "Проанализируй .env", mode=CloudMode.SMART, allow_cloud=[DataClass.FILE_CONTENT]
    )
    # .env — имя файла секрета: чтение требует подтверждения, а данные — никогда не уходят в облако.
    assert snapshot.status is S.WAITING_CONFIRMATION
    (approval,) = hybrid.app.tasks.approvals(hybrid.task_id)
    assert approval.call.tool_id == READER
    hybrid.app.tasks.resolve_approval(approval.id, ApprovalDecision.APPROVE, via="test")
    snapshot = await hybrid.run_resume()
    assert snapshot.status is S.COMPLETED
    assert hybrid.called() == ["cloud_a", "local"]
    checked = hybrid.events(EventKind.PRIVACY_CHECKED)
    assert checked[-1].payload["verdict"] == "deny"
    assert "secrets" in checked[-1].payload["blocked"]  # type: ignore[operator]
    assert [item.call.tool_id for item in hybrid.app.tasks.approvals(hybrid.task_id)] == [
        READER
    ]  # согласия нет


async def test_a_key_in_the_request_keeps_the_task_local() -> None:
    hybrid = Hybrid([finish()], {"cloud_a": []})
    await hybrid.run(
        "Проанализируй, почему ключ sk-abcdefghijklmnopqrstuvwxyz123456 не работает", mode=CloudMode.SMART
    )
    assert hybrid.remotes["cloud_a"].requests == []
    (checked,) = hybrid.events(EventKind.PRIVACY_CHECKED)
    assert checked.payload["verdict"] == "deny"
    assert checked.payload["secrets_found"] == ["api_key"]
    assert "sk-abcdef" not in str(hybrid.app.tasks.trace(hybrid.task_id)[-1].payload)


async def test_a_private_path_named_in_the_request_stays_local() -> None:
    hybrid = Hybrid([finish("локально")], {"cloud_a": []}, cloud={"private_roots": ["/data/private"]})
    snapshot = await hybrid.run("Проанализируй /data/private/plan-merger.md", mode=CloudMode.SMART)
    assert snapshot.status is S.COMPLETED
    assert hybrid.remotes["cloud_a"].requests == []
    assert hybrid.called() == ["local"]
    (checked,) = hybrid.events(EventKind.PRIVACY_CHECKED)
    assert checked.payload["verdict"] == "deny"
    assert "privacy.private_path" in checked.payload["rules"]  # type: ignore[operator]


async def test_pasted_code_is_source_code_and_needs_consent() -> None:
    hybrid = Hybrid([finish("локально")], {"cloud_a": []})
    snapshot = await hybrid.run("Проанализируй:\n```\ndef f():\n    return 1\n```", mode=CloudMode.SMART)
    assert snapshot.status is S.WAITING_CONFIRMATION
    (approval,) = hybrid.app.tasks.approvals(hybrid.task_id)
    assert approval.call.tool_id == "cloud.share"
    assert approval.arguments == {"provider": "cloud_a", "data_classes": ["source_code"]}


async def test_launch_grant_allows_the_class_for_the_task() -> None:
    hybrid = Hybrid([], {"cloud_a": [finish("облако")]})
    snapshot = await hybrid.run(
        "Проанализируй:\n```\ndef f():\n    return 1\n```",
        mode=CloudMode.SMART,
        allow_cloud=[DataClass.SOURCE_CODE],
    )
    assert snapshot.status is S.COMPLETED
    assert hybrid.called() == ["cloud_a"]
    (checked,) = hybrid.events(EventKind.PRIVACY_CHECKED)
    assert checked.payload["granted"] == ["source_code"]


async def test_the_model_cannot_call_the_consent_tool() -> None:
    sneaky = ModelReply.model_validate(
        {
            "json": {
                "decision": "разрешу себе облако",
                "action": {"type": "tool", "tool": "cloud.share", "arguments": {"provider": "cloud_a"}},
            }
        }
    )
    hybrid = Hybrid([sneaky, sneaky, sneaky, finish()], {"cloud_a": []})
    await hybrid.run("что-нибудь")
    assert hybrid.app.tasks.approvals(hybrid.task_id) == []
    assert hybrid.remotes["cloud_a"].requests == []


def test_the_consent_tool_is_hidden_from_the_model() -> None:
    hybrid = Hybrid([], {"cloud_a": []})
    definitions = {definition.id: definition for definition in hybrid.app.tools.definitions()}
    assert definitions["cloud.share"].model_visible is False  # type: ignore[index]
