"""Инварианты гибридных провайдеров (ADR 0027, ADR 0028) на случайных конфигурациях:

- LOCAL_ONLY — ни одного вызова провайдера вне компьютера, что бы ни было настроено и как бы ни
  выглядел запрос;
- уровни fast и local — тоже: простой запрос не уходит в облако по построению;
- fallback ограничен: каждый провайдер — не больше одного раза за вызов модели, попыток — не больше
  max_providers;
- выключенное облако — ни одного удалённого вызова.
"""

import asyncio
from itertools import pairwise

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jarvis.domain.privacy import DataClass
from jarvis.domain.routing import CloudMode
from jarvis.domain.trace import EventKind
from jarvis.evals.models import ModelReply
from tests.hybrid import Hybrid, finish

REQUESTS = st.sampled_from(
    [
        "Проанализируй эту архитектуру",
        "Напиши функцию сортировки",
        "Объясни, что такое Docker",
        "привет",
        "Проанализируй:\n```\ndef f():\n    return 1\n```",
    ]
)
REMOTE_REPLIES = st.lists(
    st.sampled_from(["ok", "unavailable", "auth", "rate_limited", "limit_exceeded", "misconfigured"]),
    min_size=1,
    max_size=3,
)
FAST = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def reply(kind: str) -> ModelReply:
    return finish("облако") if kind == "ok" else ModelReply.model_validate({"error": kind})


def remote_calls(hybrid: Hybrid) -> list[str]:
    return [name for name in hybrid.called() if name != "local"]


@FAST
@given(
    REQUESTS,
    st.dictionaries(
        st.sampled_from(["cloud_a", "cloud_b", "cloud_c"]), REMOTE_REPLIES, min_size=1, max_size=3
    ),
    st.booleans(),
    st.lists(
        st.sampled_from([DataClass.FILE_CONTENT, DataClass.SOURCE_CODE, DataClass.PERSONAL_DATA]), max_size=3
    ),
)
def test_local_only_never_reaches_a_remote_provider(
    text: str, remotes: dict[str, list[str]], allow_everything: bool, grants: list[DataClass]
) -> None:
    cloud = (
        {"allow_file_content": True, "allow_source_code": True, "allow_personal_data": True}
        if allow_everything
        else {}
    )
    hybrid = Hybrid(
        [finish("локально")] * 3,
        {name: [reply(kind) for kind in kinds] for name, kinds in remotes.items()},
        cloud=cloud,
    )
    asyncio.run(hybrid.run(text, mode=CloudMode.LOCAL_ONLY, allow_cloud=grants))
    assert all(model.requests == [] for model in hybrid.remotes.values())
    assert remote_calls(hybrid) == []


@FAST
@given(REQUESTS, st.sampled_from([CloudMode.AUTO, CloudMode.SMART, CloudMode.CODING]))
def test_a_disabled_cloud_is_never_called(text: str, mode: CloudMode) -> None:
    hybrid = Hybrid([finish("локально")] * 3, {"cloud_a": [finish("облако")]}, cloud={"enabled": False})
    asyncio.run(hybrid.run(text, mode=mode))
    assert hybrid.remotes["cloud_a"].requests == []


@FAST
@given(st.sampled_from(["Объясни, что такое Docker", "привет", "какие файлы тут?", "что делать дальше"]))
def test_the_local_level_never_reaches_a_remote_provider(text: str) -> None:
    hybrid = Hybrid([finish("локально")] * 3, {"cloud_a": [finish("облако")]})
    asyncio.run(hybrid.run(text))  # auto без признаков — уровень local
    routed = hybrid.events(EventKind.MODEL_ROUTED)
    assert all(event.payload["level"] in ("local", "fast") for event in routed)
    assert hybrid.remotes["cloud_a"].requests == []


@FAST
@given(
    st.dictionaries(
        st.sampled_from(["cloud_a", "cloud_b", "cloud_c", "cloud_d"]), REMOTE_REPLIES, min_size=1, max_size=4
    ),
    st.integers(min_value=1, max_value=5),
)
def test_fallback_is_bounded_and_never_loops(remotes: dict[str, list[str]], max_providers: int) -> None:
    hybrid = Hybrid(
        [finish("локально")] * 3,
        {name: [reply(kind) for kind in kinds] for name, kinds in remotes.items()},
        routing={"max_providers": max_providers},
    )
    asyncio.run(hybrid.run("Проанализируй эту архитектуру", mode=CloudMode.SMART))
    for event in hybrid.events(EventKind.MODEL_ROUTED):
        order = event.payload["order"]
        assert isinstance(order, list)
        assert len(order) <= max_providers
        assert len(order) == len(set(order))  # каждый провайдер — не больше одного раза за вызов
    called = hybrid.called()
    # Подряд один и тот же облачный провайдер не вызывается: туда-обратно не бывает.
    assert all(a != b or a == "local" for a, b in pairwise(called))
