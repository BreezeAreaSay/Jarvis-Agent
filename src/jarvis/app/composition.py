"""Сборка приложения (01-structure.md §6): единственное место, где создаются адаптеры.

По умолчанию задачу ведёт агент: стадии из `core.agent` с Model Gateway над бэкендами моделей из
конфига (или переданными — scripted-модель в eval и тестах). Вызывающий может передать свои стадии
(scripted-сценарии, команды, которые задачи не выполняют); стадиям, которые вызывают инструменты,
нужен Tool Runtime — их передают фабрикой, которая его получает. Хранилище по умолчанию — в памяти;
CLI открывает SQLite через `open_storage`.
"""

import os
import secrets
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from jarvis.adapters.clock import SystemClock
from jarvis.adapters.inventory import system_inventory
from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.adapters.tools import HOST, OS_FAMILY, builtin_tools, host_tools, system_launcher
from jarvis.adapters.tools.consent import CloudShareTool
from jarvis.config import resolve_secret
from jarvis.core.agent.stages import agent_stages
from jarvis.core.approvals import Approvals
from jarvis.core.leases import Leases
from jarvis.core.models.availability import ProviderAvailability
from jarvis.core.models.gateway import ModelGateway
from jarvis.core.models.privacy import CloudPrivacyPolicy
from jarvis.core.policy import PolicyEngine, PolicyZones
from jarvis.core.routing.router import Router
from jarvis.core.runner import StageHandler, TaskRunner
from jarvis.core.service import TaskService
from jarvis.core.tools.provenance import DataClassifier
from jarvis.core.tools.registry import ToolRegistry
from jarvis.core.tools.runtime import ToolRuntime
from jarvis.core.trace import Tracer
from jarvis.domain.inventory import KnownFolder
from jarvis.domain.models import ModelRole
from jarvis.domain.routing import CloudMode
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.tools import ExecutionTarget
from jarvis.ports.clock import Clock
from jarvis.ports.inventory import Inventory
from jarvis.ports.launcher import Launcher
from jarvis.ports.models import ModelBackend
from jarvis.ports.tools import Tool

Storage = InMemoryStorage | SqliteStorage
# Личные папки пользователя: файлы оттуда — personal_data, в облако без разрешения не уходят (ADR 0028).
PERSONAL_FOLDERS = (
    KnownFolder.DOCUMENTS,
    KnownFolder.DESKTOP,
    KnownFolder.DOWNLOADS,
    KnownFolder.PICTURES,
    KnownFolder.MUSIC,
    KnownFolder.VIDEOS,
)
Stages = Mapping[TaskStatus, StageHandler]
StagesFactory = Callable[[ToolRuntime], Stages]

# Папки в домашнем каталоге, где лежат ключи, токены, пароли и история команд: чтение — с
# подтверждением, запись — запрет. Пути Windows (AppData) и Linux/macOS — вместе: лишние не мешают.
SECRET_DIRS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    ".config/gcloud",
    ".config/gh",
    "AppData/Roaming/gcloud",
    "AppData/Roaming/GitHub CLI",
    "AppData/Roaming/Microsoft/Windows/PowerShell/PSReadLine",  # история PowerShell
    "AppData/Local/Google/Chrome/User Data",
    "AppData/Local/Microsoft/Edge/User Data",
    "AppData/Roaming/Mozilla/Firefox/Profiles",
    ".mozilla",
    ".config/google-chrome",
)
# Имена файлов с секретами, где бы они ни лежали (без учёта регистра на Windows).
SECRET_NAMES = (
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx",
    ".env", ".env.*", ".netrc", "_netrc", ".pgpass", ".git-credentials", "credentials", "credentials.*",
    ".npmrc", ".pypirc", "*_history", "ConsoleHost_history.txt",
)  # fmt: skip


@dataclass(frozen=True)
class App:
    config: JarvisConfig
    tasks: TaskService
    tools: ToolRuntime
    models: ModelGateway | None  # есть, когда задачу ведёт агент


def model_backends(config: JarvisConfig) -> dict[ModelRole, ModelBackend]:
    """Бэкенды ролей по конфигу: у каждой роли — свой эндпоинт."""
    # HTTP-клиент грузится, только когда модели нужны: `jarvis route`, `tools`, `tasks` обходятся без него.
    from jarvis.adapters.models import OpenAICompatibleBackend

    endpoints = config.models.endpoints
    return {
        role: OpenAICompatibleBackend(name, endpoints[name]) for role, name in config.models.roles.items()
    }


def cloud_allowed(config: JarvisConfig) -> bool:
    """Облако включено и режим не local_only: только тогда создаются удалённые адаптеры (ADR 0027)."""
    return config.cloud.enabled and config.models.routing.mode is not CloudMode.LOCAL_ONLY


def model_endpoints(config: JarvisConfig, *, env: Mapping[str, str] | None = None) -> dict[str, ModelBackend]:
    """Эндпоинты цепочек уровней по ID: локальные и — если облако разрешено — удалённые. Ключ удалённого
    провайдера читается из окружения по ссылке `env:ИМЯ` только здесь; нет переменной — адаптер отвечает
    MISCONFIGURED без обращения к сети."""
    from jarvis.adapters.models import OpenAICompatibleBackend, RemoteOpenAICompatibleBackend

    found: dict[str, ModelBackend] = {
        name: OpenAICompatibleBackend(name, settings) for name, settings in config.models.endpoints.items()
    }
    if cloud_allowed(config):
        for name, settings in config.models.remote.items():
            key = resolve_secret(settings.api_key, env=env)
            found[name] = RemoteOpenAICompatibleBackend(name, settings, key)
    return found


def build_router(
    *, inventory: Inventory | None = None, direct_commands: bool = True, mode: CloudMode = CloudMode.AUTO
) -> Router:
    """Router без остального приложения — для `jarvis route`: решение без хранилища, моделей и исполнения."""
    return Router(
        inventory if inventory is not None else system_inventory(), direct=direct_commands, mode=mode
    )


def read_only_tools() -> list[Tool]:
    """Встроенные инструменты только для чтения — набор, на котором снят бенчмарк модели (ADR 0025)."""
    return builtin_tools()


def database_path(home: Path) -> Path:
    """База лежит в данных Jarvis: JARVIS_HOME/data/jarvis.db."""
    return home / "data" / "jarvis.db"


def open_storage(home: Path) -> SqliteStorage:
    return SqliteStorage(database_path(home))


def process_owner() -> str:
    """Уникальный владелец аренд: хост и pid для человека, случайный токен — против повтора pid."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


def host_zones(
    config: JarvisConfig,
    *,
    home: Path | None,
    user_home: Path | None = None,
    config_file: Path | None = None,
) -> PolicyZones:
    """Зоны политики этого компьютера: канонические пути, как их увидит preview инструмента. Файл
    конфига (в нём рабочие папки политики) — данные Jarvis, даже если он лежит вне JARVIS_HOME."""
    user = user_home if user_home is not None else Path.home()
    internal = [path for path in (home, config_file) if path is not None]
    return PolicyZones(
        os_family=OS_FAMILY,
        internal=tuple(_canonical(path) for path in internal),
        secrets=tuple(_canonical(user / folder) for folder in SECRET_DIRS),
        secret_names=SECRET_NAMES,
        workspaces=tuple(_canonical(Path(root).expanduser()) for root in config.policy.workspace_roots),
    )


def _canonical(path: Path) -> str:
    return os.path.realpath(path)  # путь может ещё не существовать: тогда раскрывается то, что есть


def build_app(
    config: JarvisConfig,
    *,
    stages: Stages | StagesFactory | None = None,
    models: Mapping[ModelRole, ModelBackend] | None = None,
    storage: Storage | None = None,
    clock: Clock | None = None,
    owner: str | None = None,
    remote_models: Mapping[str, ModelBackend] | None = None,
    tools: Sequence[Tool] | None = None,
    extra_tools: Sequence[Tool] = (),
    zones: PolicyZones | None = None,
    target: ExecutionTarget | None = None,
    inventory: Inventory | None = None,
    launcher: Launcher | None = None,
    direct_commands: bool = True,
    home: Path | None = None,
    config_file: Path | None = None,
) -> App:
    """`stages` не задан — задачу ведёт агент, модели — `models` или из конфига (ConfigError, если
    модель не подходит роли). `home` — JARVIS_HOME: его данные недоступны инструментам. `tools` по
    умолчанию — встроенные; `extra_tools` добавляются к ним (инструменты eval). `target` и `zones` по
    умолчанию — этот компьютер; тесты с фейковыми инструментами задают их явно, чтобы не зависеть от ОС.
    `inventory` и `launcher` — приложения и папки для прямых команд и способ их открыть (по умолчанию —
    этого компьютера); `direct_commands=False` — Router отдаёт всё агенту (бенчмарк модели).
    `remote_models` — эндпоинты цепочек уровней по ID вместо созданных по конфигу (тесты и eval с
    поддельными провайдерами); облако при этом всё равно решает конфиг (`cloud`, `models.routing`)."""
    storage = storage if storage is not None else InMemoryStorage()
    clock = clock if clock is not None else SystemClock()
    owner = owner if owner is not None else process_owner()
    zones = zones if zones is not None else host_zones(config, home=home, config_file=config_file)
    tracer = Tracer(storage.ids, clock)
    leases = Leases(uow=storage.unit_of_work, clock=clock, owner=owner, ttl_s=config.runtime.lease_ttl_s)
    inventory = inventory if inventory is not None else system_inventory()
    if tools is None:
        tools = host_tools(inventory, launcher if launcher is not None else system_launcher)
    private_roots = tuple(_canonical(Path(root).expanduser()) for root in config.cloud.private_roots)
    # Канонические, как пути чтения в preview: «Документы» по ссылке — всё равно личные.
    personal_roots = (
        *(
            _canonical(Path(path))
            for folder in PERSONAL_FOLDERS
            if (path := inventory.known_folder(folder)) is not None
        ),
        *(_canonical(Path(root).expanduser()) for root in config.cloud.personal_roots),
    )
    runtime = ToolRuntime(
        # Служебный инструмент согласия на облако — всегда: модель его не видит (ADR 0028).
        registry=ToolRegistry([*tools, *extra_tools, CloudShareTool()]),
        policy=PolicyEngine(zones),
        uow=storage.unit_of_work,
        tracer=tracer,
        clock=clock,
        target=target if target is not None else HOST,
        approval_ttl_s=config.policy.approval_ttl_s,
        leases=leases,
        protected_roots=(*zones.internal, *zones.secrets),
        classifier=DataClassifier(zones, private_roots=private_roots, personal_roots=personal_roots),
    )
    gateway: ModelGateway | None = None
    if stages is None:
        gateway = ModelGateway(
            backends=models if models is not None else model_backends(config),
            endpoints=remote_models if remote_models is not None else model_endpoints(config),
            routing=config.models.routing,
            privacy=CloudPrivacyPolicy(config.cloud, os_family=zones.os_family, private_roots=private_roots),
            availability=ProviderAvailability(uow=storage.unit_of_work, clock=clock),
            uow=storage.unit_of_work,
            tracer=tracer,
            clock=clock,
            repair_attempts=config.models.repair_attempts,
        )
        stages = agent_stages(
            gateway=gateway,
            tools=runtime,
            router=Router(inventory, direct=direct_commands, mode=config.models.routing.mode),
            uow=storage.unit_of_work,
            tracer=tracer,
        )
    runner = TaskRunner(
        stages=stages(runtime) if callable(stages) else stages,
        uow=storage.unit_of_work,
        tracer=tracer,
        budgets=config.budgets,
        clock=clock,
        leases=leases,
    )
    tasks = TaskService(
        runner=runner,
        uow=storage.unit_of_work,
        ids=storage.ids,
        tracer=tracer,
        budgets=config.budgets,
        clock=clock,
        leases=leases,
        approvals=Approvals(uow=storage.unit_of_work, tracer=tracer, clock=clock),
    )
    return App(config=config, tasks=tasks, tools=runtime, models=gateway)
