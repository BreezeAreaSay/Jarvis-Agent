"""app.launch, url.open, folder.open — открыть что-то в интерфейсе (эффект LAUNCH, ADR 0030).

Запускается только известное: приложение из инвентаря (по ID или точному имени), http(s)-адрес,
существующая папка по каноническому пути. Ни командной строки, ни shell: на Windows — системное
открытие (`os.startfile`, ShellExecute) ярлыка или файла из инвентаря, адреса или папки; на Linux —
`xdg-open`/`gtk-launch` со списком аргументов. Без человека такой вызов проходит только как прямая
команда пользователя; предложенный моделью — с подтверждением (политика).
"""

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from jarvis.adapters.tools._host import canonical, in_thread, unchanged
from jarvis.domain.errors import ToolExecutionFailed, ToolPreviewFailed
from jarvis.domain.inventory import AppEntry, name_key
from jarvis.domain.tools import (
    EffectKind,
    TargetKind,
    ToolDefinition,
    ToolEffect,
    ToolId,
    ToolPreview,
    ToolVerification,
)
from jarvis.domain.urls import normalize_web_url
from jarvis.ports.inventory import Inventory
from jarvis.ports.launcher import Launcher, LaunchTarget
from jarvis.ports.tools import ToolContext

LAUNCH = frozenset({EffectKind.LAUNCH})
HOST_ONLY = frozenset({TargetKind.HOST})
DISPATCHED = "запуск передан системе (процесс дальше не отслеживается)"


def system_launcher(target: LaunchTarget) -> None:
    """Открыть средствами ОС, без командной строки и shell."""
    if sys.platform == "win32":
        os.startfile(target.value)
        return
    argv = _posix_argv(target)
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _posix_argv(target: LaunchTarget) -> list[str]:
    if target.kind == "app" and target.app_kind == "desktop_entry":
        if (tool := shutil.which("gtk-launch")) is not None:
            return [tool, Path(target.value).stem]
        if (tool := shutil.which("gio")) is not None:
            return [tool, "launch", target.value]
        raise ToolExecutionFailed("нечем запустить приложение: нет gtk-launch и gio")
    if target.kind == "app":
        return [target.value]
    opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if opener is None:
        raise ToolExecutionFailed("нечем открыть: нет xdg-open")
    return [opener, target.value]


async def _dispatch(launcher: Launcher, target: LaunchTarget) -> None:
    def work(stop: threading.Event) -> None:
        try:
            launcher(target)
        except ToolExecutionFailed:
            raise
        except OSError as exc:
            raise ToolExecutionFailed(f"не удалось открыть: {exc.strerror or exc}") from None

    await in_thread(work)


# --- app.launch ----------------------------------------------------------------------------------


class AppLaunchArgs(BaseModel, frozen=True, extra="forbid"):
    app: str = Field(
        min_length=1, max_length=200, description="ID приложения из инвентаря или его точное имя"
    )


class AppLaunchOutput(BaseModel, frozen=True, extra="forbid"):
    app: str  # ID из инвентаря
    name: str


class AppLaunchTool:
    definition = ToolDefinition(
        id=ToolId("app.launch"),
        description=(
            "Запустить установленное приложение из инвентаря (меню «Пуск»): по ID или точному имени.\n"
            "Произвольный файл или команду так не запустить. Если запуск предложил не сам пользователь "
            "прямой командой, нужно подтверждение человека."
        ),
        input_model=AppLaunchArgs,
        output_model=AppLaunchOutput,
        effects=LAUNCH,
        targets=HOST_ONLY,
        timeout_s=10.0,
    )

    def __init__(self, inventory: Inventory, launcher: Launcher = system_launcher) -> None:
        self._inventory = inventory
        self._launcher = launcher

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        assert isinstance(arguments, AppLaunchArgs)
        entry = await in_thread(lambda stop: self._find(arguments.app))
        return ToolPreview(
            summary=f"Запустить {entry.name}",
            normalized_arguments={"app": entry.id},
            effects=[ToolEffect(kind=EffectKind.LAUNCH, resource=f"app:{entry.id}")],
            target=context.target,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        assert isinstance(arguments, AppLaunchArgs)
        entry = await in_thread(lambda stop: self._find(arguments.app))
        await _dispatch(self._launcher, LaunchTarget("app", entry.target, entry.kind))
        return AppLaunchOutput(app=entry.id, name=entry.name)

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        assert isinstance(output, AppLaunchOutput)
        return ToolVerification(passed=bool(output.app), checks=[DISPATCHED])

    def _find(self, wanted: str) -> AppEntry:
        """Приложение из инвентаря по ID, а если такого нет — по точному имени или алиасу."""
        apps = list(self._inventory.apps())
        browser = self._inventory.default_browser()
        if browser is not None and all(app.id != browser.id for app in apps):
            apps.append(browser)
        exact = [app for app in apps if app.id == wanted]
        if exact:
            return exact[0]
        key = name_key(wanted)
        named = {
            app.id: app
            for app in apps
            if key == name_key(app.name) or key in {name_key(alias) for alias in app.aliases}
        }
        if len(named) == 1:
            return next(iter(named.values()))
        if named:
            raise ToolPreviewFailed(
                f"имя «{wanted}» подходит нескольким приложениям: {', '.join(sorted(named))}"
            )
        raise ToolPreviewFailed(f"нет такого приложения в инвентаре: {wanted}")


# --- url.open ------------------------------------------------------------------------------------


class UrlOpenArgs(BaseModel, frozen=True, extra="forbid"):
    url: str = Field(min_length=1, max_length=2000, description="http(s)-адрес или домен: github.com")


class UrlOpenOutput(BaseModel, frozen=True, extra="forbid"):
    url: str


class UrlOpenTool:
    definition = ToolDefinition(
        id=ToolId("url.open"),
        description=(
            "Открыть веб-адрес в браузере по умолчанию. Только http и https.\n"
            "Если открыть адрес предложил не сам пользователь прямой командой, нужно подтверждение человека."
        ),
        input_model=UrlOpenArgs,
        output_model=UrlOpenOutput,
        effects=LAUNCH,
        targets=HOST_ONLY,
        timeout_s=10.0,
    )

    def __init__(self, launcher: Launcher = system_launcher) -> None:
        self._launcher = launcher

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        assert isinstance(arguments, UrlOpenArgs)
        url = _web_url(arguments.url)
        return ToolPreview(
            summary=f"Открыть {url} в браузере",
            normalized_arguments={"url": url},
            effects=[ToolEffect(kind=EffectKind.LAUNCH, resource=f"url:{url}")],
            target=context.target,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        assert isinstance(arguments, UrlOpenArgs)
        url = _web_url(arguments.url)
        await _dispatch(self._launcher, LaunchTarget("url", url))
        return UrlOpenOutput(url=url)

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        assert isinstance(output, UrlOpenOutput)
        return ToolVerification(
            passed=normalize_web_url(output.url, allow_bare=False) == output.url, checks=[DISPATCHED]
        )


def _web_url(raw: str) -> str:
    url = normalize_web_url(raw)
    if url is None:
        raise ToolPreviewFailed(f"открыть можно только http(s)-адрес: {raw[:200]}")
    return url


# --- folder.open ---------------------------------------------------------------------------------


class FolderOpenArgs(BaseModel, frozen=True, extra="forbid"):
    path: str = Field(min_length=1, description="Папка; относительный путь — от рабочей папки задачи")


class FolderOpenOutput(BaseModel, frozen=True, extra="forbid"):
    path: str  # канонический путь


class FolderOpenTool:
    definition = ToolDefinition(
        id=ToolId("folder.open"),
        description=(
            "Открыть папку в проводнике (окно с файлами). Ничего не читает и не меняет.\n"
            "Если открыть папку предложил не сам пользователь прямой командой, нужно подтверждение человека."
        ),
        input_model=FolderOpenArgs,
        output_model=FolderOpenOutput,
        effects=LAUNCH,
        targets=HOST_ONLY,
        timeout_s=10.0,
    )

    def __init__(self, launcher: Launcher = system_launcher) -> None:
        self._launcher = launcher

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        assert isinstance(arguments, FolderOpenArgs)
        path = str(await canonical(arguments.path, context, expect="dir"))
        return ToolPreview(
            summary=f"Открыть папку {path} в проводнике",
            normalized_arguments={"path": path},
            effects=[ToolEffect(kind=EffectKind.LAUNCH, resource=path)],
            target=context.target,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        assert isinstance(arguments, FolderOpenArgs)
        path = str(await in_thread(lambda stop: unchanged(arguments.path, expect="dir")))
        await _dispatch(self._launcher, LaunchTarget("folder", path))
        return FolderOpenOutput(path=path)

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        assert isinstance(output, FolderOpenOutput)
        return ToolVerification(passed=bool(output.path), checks=[DISPATCHED])
