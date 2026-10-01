"""Сборка приложения (01-structure.md §6): единственное место, где создаются адаптеры.

Стадии задачи передаёт вызывающий (eval и тесты — scripted-стадии); настоящие стадии появятся со
своими milestone. Стадиям, которые вызывают инструменты, нужен Tool Runtime — их передают фабрикой,
которая его получает. Хранилище по умолчанию — в памяти; CLI открывает SQLite через `open_storage`.
"""

import os
import secrets
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from jarvis.adapters.clock import SystemClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.adapters.tools import HOST, OS_FAMILY, builtin_tools
from jarvis.core.approvals import Approvals
from jarvis.core.leases import Leases
from jarvis.core.policy import PolicyEngine, PolicyZones
from jarvis.core.runner import StageHandler, TaskRunner
from jarvis.core.service import TaskService
from jarvis.core.tools.registry import ToolRegistry
from jarvis.core.tools.runtime import ToolRuntime
from jarvis.core.trace import Tracer
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.ports.clock import Clock
from jarvis.ports.tools import Tool

Storage = InMemoryStorage | SqliteStorage
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
    stages: Stages | StagesFactory,
    storage: Storage | None = None,
    clock: Clock | None = None,
    owner: str | None = None,
    tools: Sequence[Tool] | None = None,
    extra_tools: Sequence[Tool] = (),
    zones: PolicyZones | None = None,
    home: Path | None = None,
    config_file: Path | None = None,
) -> App:
    """`home` — JARVIS_HOME: его данные недоступны инструментам. `tools` по умолчанию — встроенные;
    `extra_tools` добавляются к ним (инструменты eval)."""
    storage = storage if storage is not None else InMemoryStorage()
    clock = clock if clock is not None else SystemClock()
    owner = owner if owner is not None else process_owner()
    zones = zones if zones is not None else host_zones(config, home=home, config_file=config_file)
    tracer = Tracer(storage.ids, clock)
    leases = Leases(uow=storage.unit_of_work, clock=clock, owner=owner, ttl_s=config.runtime.lease_ttl_s)
    runtime = ToolRuntime(
        registry=ToolRegistry([*(builtin_tools() if tools is None else tools), *extra_tools]),
        policy=PolicyEngine(zones),
        uow=storage.unit_of_work,
        tracer=tracer,
        clock=clock,
        target=HOST,
        approval_ttl_s=config.policy.approval_ttl_s,
        leases=leases,
        protected_roots=(*zones.internal, *zones.secrets),
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
    return App(config=config, tasks=tasks, tools=runtime)
