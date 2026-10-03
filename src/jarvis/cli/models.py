"""`jarvis model check`: сверить объявленное в конфиге с тем, что делает сервер модели.

Для каждой роли: требования роли, доступность сервера, модели и окно контекста, которые он сообщает,
проба structured output по маленькой схеме и проба настоящей схемы решения исполнителя (сервер должен
суметь построить по ней грамматику). Несовпадение объявленного и измеренного — ошибка с объяснением.
"""

import asyncio
import json
from typing import Annotated

import typer
from pydantic import JsonValue, ValidationError

from jarvis.app.composition import build_app, model_backends
from jarvis.cli.common import load_or_exit
from jarvis.core.agent.actions import decision_output
from jarvis.core.models.gateway import ROLE_REQUIREMENTS, check_requirements, extract_json
from jarvis.core.timeline import clean_line
from jarvis.domain.errors import ConfigError, ModelError
from jarvis.domain.models import BackendRequest, BackendResponse, ChatMessage, ModelRole
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.tools import ToolDefinition
from jarvis.ports.models import ModelBackend

model_app = typer.Typer(no_args_is_help=True, help="Модель: проверка настройки.")

PROBE_ANSWERS = ["синий", "зелёный", "красный"]
PROBE_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"answer": {"enum": list[JsonValue](PROBE_ANSWERS)}},
    "required": ["answer"],
    "additionalProperties": False,
}


@model_app.command("check")
def model_check(
    probe: Annotated[
        bool, typer.Option(help="Отправить пробные запросы (иначе — только конфиг и сервер).")
    ] = True,
) -> None:
    """Проверить модели ролей: конфиг, сервер, окно контекста, structured output."""
    loaded = load_or_exit()
    config = loaded.config
    if not config.models.roles:
        typer.echo(
            "Ни одной роли не назначена модель: добавьте [models.endpoints.<id>] и [models.roles] "
            "(docs/development.md, «Настройка модели»).",
            err=True,
        )
        raise typer.Exit(2)
    definitions = build_app(
        config, stages={}, home=loaded.home, config_file=loaded.config_path
    ).tools.definitions()
    problems = asyncio.run(_check_all(config, definitions, probe=probe))
    if problems:
        typer.echo(f"\nПроблем: {problems}.", err=True)
        raise typer.Exit(1)
    typer.echo("\nМодели в порядке.")


async def _check_all(config: JarvisConfig, definitions: list[ToolDefinition], *, probe: bool) -> int:
    backends = model_backends(config)
    problems = 0
    for role, backend in backends.items():
        problems += await _check(role, backend, config, definitions, probe=probe)
    return problems


async def _check(
    role: ModelRole,
    backend: ModelBackend,
    config: JarvisConfig,
    definitions: list[ToolDefinition],
    *,
    probe: bool,
) -> int:
    info = backend.info
    caps = info.capabilities
    endpoint = config.models.endpoints[info.endpoint]
    typer.echo(f"{role}: эндпоинт {info.endpoint} — {endpoint.base_url}, модель «{info.model}»")
    typer.echo(
        f"  объявлено: structured_output={caps.structured_output}, context_window={caps.context_window}"
    )
    problems = 0
    try:
        check_requirements(role, info)
        typer.echo(f"  ✓ требования роли: окно ≥ {ROLE_REQUIREMENTS[role].min_context_window}")
    except ConfigError as exc:
        problems += _fail(exc.message)
    try:
        status = await backend.describe()
    except ModelError as exc:
        return problems + _fail(exc.message)
    build = f" ({clean_line(status.server)})" if status.server else ""
    typer.echo(f"  ✓ сервер отвечает{build}: {clean_line(', '.join(status.models)) or 'моделей нет'}")
    if status.context_window is None:
        typer.echo("  ? сервер не сообщает окно контекста: проверьте сами (у llama-server — параметр -c)")
    elif caps.context_window > status.context_window:
        problems += _fail(
            f"объявлено окно {caps.context_window}, а сервер даёт {status.context_window} на запрос: "
            f"уменьшите context_window в конфиге или запустите сервер с большим -c"
        )
    else:
        typer.echo(f"  ✓ окно контекста сервера: {status.context_window}")
    if not probe:
        return problems
    problems += await _probe_enum(backend)
    problems += await _probe_decision(backend, definitions)
    return problems


async def _probe_enum(backend: ModelBackend) -> int:
    caps = backend.info.capabilities
    system = 'Отвечай одним JSON-объектом вида {"answer": "..."}.'
    if not caps.structured_output:
        system += f" Схема: {json.dumps(PROBE_SCHEMA, ensure_ascii=False)}"
    request = BackendRequest(
        messages=[
            ChatMessage(role="system", content=system),
            ChatMessage(
                role="user", content=f"Какого цвета ясное небо днём? Выбери из: {', '.join(PROBE_ANSWERS)}."
            ),
        ],
        json_schema=PROBE_SCHEMA if caps.structured_output else None,
        max_tokens=64,
    )
    try:
        response = await backend.complete(request)
    except ModelError as exc:
        return _fail(f"проба structured output: {exc.message}")
    answer = _probe_answer(response.text)
    speed = _speed(response)
    if answer is not None:
        typer.echo(f"  ✓ проба structured output: «{answer}»; {speed}")
        return 0
    if caps.structured_output:
        return _fail(
            f"объявлен structured_output, но ответ не по схеме: {clean_line(response.text[:200])!r} — "
            "сервер не применяет JSON Schema; поставьте structured_output = false"
        )
    shown = clean_line(response.text[:200])
    typer.echo(
        f"  ? без structured_output ответ не по схеме: {shown!r} — Jarvis будет его ремонтировать; {speed}"
    )
    return 0


async def _probe_decision(backend: ModelBackend, definitions: list[ToolDefinition]) -> int:
    """Настоящая схема решения исполнителя: сервер должен её принять, ответ — пройти разбор."""
    if not backend.info.capabilities.structured_output:
        return 0
    output = decision_output(definitions, [])
    request = BackendRequest(
        messages=[
            ChatMessage(
                role="system", content="Ты — исполнитель. Ответь действием finish с ответом «готово»."
            ),
            ChatMessage(role="user", content="Проверка связи: закончи сразу."),
        ],
        json_schema=output.schema,
        max_tokens=ROLE_REQUIREMENTS[ModelRole.EXECUTOR].reply_tokens,
    )
    try:
        response = await backend.complete(request)
    except ModelError as exc:
        return _fail(f"схема решения исполнителя: {exc.message}")
    try:
        value = output.model.model_validate_json(extract_json(response.text))
    except ValidationError as exc:
        return _fail(
            f"схема решения исполнителя: ответ не прошёл проверку ({exc.error_count()} ошибок, "
            f"finish_reason={response.finish_reason}) — модель или сервер не справляются со схемой"
        )
    problems = output.check(value) if output.check else []
    if problems:
        return _fail(f"схема решения исполнителя: {'; '.join(problems)}")
    typer.echo(
        f"  ✓ схема решения исполнителя принята сервером; действие: {value.action.type}; {_speed(response)}"
    )
    return 0


def _probe_answer(text: str) -> str | None:
    try:
        data = json.loads(extract_json(text))
    except json.JSONDecodeError:
        return None
    answer = data.get("answer") if isinstance(data, dict) else None
    return answer if answer in PROBE_ANSWERS else None


def _speed(response: BackendResponse) -> str:
    parts = [f"{response.latency_ms} мс"]
    if response.completion_tokens:
        generation_ms = response.latency_ms - (response.prompt_ms or 0)
        if generation_ms > 0:
            parts.append(f"{response.completion_tokens / generation_ms * 1000:.1f} ток/с")
    return ", ".join(parts)


def _fail(message: str) -> int:
    typer.echo(f"  ✗ {clean_line(message)}", err=True)  # в сообщении бывает текст сервера
    return 1
