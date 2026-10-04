"""app.launch, url.open, folder.open (ADR 0030): запускается только известное, без shell, способом ОС.

Способ открыть подменён записывающим: тесты ничего не запускают на этом компьютере.
"""

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from jarvis.adapters.inventory import StaticInventory
from jarvis.adapters.tools import HOST
from jarvis.adapters.tools import launch as launch_module
from jarvis.adapters.tools.launch import (
    AppLaunchArgs,
    AppLaunchOutput,
    AppLaunchTool,
    FolderOpenArgs,
    FolderOpenOutput,
    FolderOpenTool,
    UrlOpenArgs,
    UrlOpenOutput,
    UrlOpenTool,
    _posix_argv,
    system_launcher,
)
from jarvis.domain.errors import ToolExecutionFailed, ToolPreviewFailed
from jarvis.domain.inventory import AppEntry
from jarvis.domain.tools import EffectKind, ToolEffect
from jarvis.ports.launcher import LaunchTarget
from jarvis.ports.tools import ToolContext

pytestmark = pytest.mark.anyio

TELEGRAM = AppEntry(
    id="telegram",
    name="Telegram Desktop",
    aliases=["телеграм"],
    target="C:/Apps/Telegram.lnk",
    kind="shortcut",
)
FIREFOX = AppEntry(
    id="firefox", name="Mozilla Firefox", aliases=[], target="/usr/bin/firefox", kind="executable"
)
PY312 = AppEntry(
    id="py312", name="Python 3.12", aliases=["питон"], target="C:/Apps/py312.lnk", kind="shortcut"
)
PY313 = AppEntry(
    id="py313", name="Python 3.13", aliases=["питон"], target="C:/Apps/py313.lnk", kind="shortcut"
)


class Recorder:
    def __init__(self, error: OSError | None = None) -> None:
        self.targets: list[LaunchTarget] = []
        self._error = error

    def __call__(self, target: LaunchTarget) -> None:
        if self._error is not None:
            raise self._error
        self.targets.append(target)


def context(cwd: Path | None = None) -> ToolContext:
    return ToolContext(target=HOST, working_directory=str(cwd) if cwd else None)


async def run(tool: object, arguments: BaseModel, ctx: ToolContext) -> tuple[list[ToolEffect], BaseModel]:
    preview = await tool.preview(arguments, ctx)  # type: ignore[attr-defined]
    normalized = type(arguments).model_validate(preview.normalized_arguments)
    output = await tool.execute(normalized, ctx)  # type: ignore[attr-defined]
    verification = await tool.verify(normalized, output, ctx)  # type: ignore[attr-defined]
    assert verification.passed, verification.checks
    return preview.effects, output


def apps(*entries: AppEntry, browser: str | None = None) -> StaticInventory:
    return StaticInventory(entries, default_browser=browser)


@pytest.mark.parametrize("wanted", ["telegram", "Telegram Desktop", "телеграм", "TELEGRAM DESKTOP"])
async def test_an_app_is_found_by_id_exact_name_or_alias(wanted: str) -> None:
    launcher = Recorder()
    effects, output = await run(AppLaunchTool(apps(TELEGRAM), launcher), AppLaunchArgs(app=wanted), context())
    assert effects == [ToolEffect(kind=EffectKind.LAUNCH, resource="app:telegram")]
    assert output == AppLaunchOutput(app="telegram", name="Telegram Desktop")
    assert launcher.targets == [LaunchTarget("app", "C:/Apps/Telegram.lnk", "shortcut")]


async def test_the_default_browser_is_part_of_the_inventory() -> None:
    launcher = Recorder()
    tool = AppLaunchTool(apps(TELEGRAM, FIREFOX, browser="firefox"), launcher)
    _, output = await run(tool, AppLaunchArgs(app="firefox"), context())
    assert output == AppLaunchOutput(app="firefox", name="Mozilla Firefox")
    assert launcher.targets == [LaunchTarget("app", "/usr/bin/firefox", "executable")]


@pytest.mark.parametrize(
    ("wanted", "message"),
    [
        ("телегарм", "нет такого приложения в инвентаре"),
        ("C:/Windows/System32/cmd.exe", "нет такого приложения в инвентаре"),
        ("cmd.exe /c del *", "нет такого приложения в инвентаре"),
        ("питон", "подходит нескольким приложениям: py312, py313"),
    ],
)
async def test_anything_outside_the_inventory_is_not_launched(wanted: str, message: str) -> None:
    launcher = Recorder()
    tool = AppLaunchTool(apps(TELEGRAM, PY312, PY313), launcher)
    with pytest.raises(ToolPreviewFailed, match=message):
        await tool.preview(AppLaunchArgs(app=wanted), context())
    with pytest.raises(ToolPreviewFailed):
        await tool.execute(AppLaunchArgs(app=wanted), context())
    assert launcher.targets == []


async def test_a_failed_launch_is_a_tool_error() -> None:
    tool = AppLaunchTool(apps(TELEGRAM), Recorder(FileNotFoundError(2, "Не найден файл")))
    with pytest.raises(ToolExecutionFailed, match="не удалось открыть: Не найден файл"):
        await tool.execute(AppLaunchArgs(app="telegram"), context())


async def test_a_web_address_is_normalized_before_it_is_opened() -> None:
    launcher = Recorder()
    effects, output = await run(UrlOpenTool(launcher), UrlOpenArgs(url="GitHub.com/anthropics"), context())
    assert effects == [ToolEffect(kind=EffectKind.LAUNCH, resource="url:https://github.com/anthropics")]
    assert output == UrlOpenOutput(url="https://github.com/anthropics")
    assert launcher.targets == [LaunchTarget("url", "https://github.com/anthropics")]


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "javascript:alert(1)", "http://user:pass@evil.com", "README.md", "evil.exe"]
)
async def test_only_http_addresses_are_opened(url: str) -> None:
    launcher = Recorder()
    tool = UrlOpenTool(launcher)
    with pytest.raises(ToolPreviewFailed, match="только http"):
        await tool.preview(UrlOpenArgs(url=url), context())
    with pytest.raises(ToolPreviewFailed):
        await tool.execute(UrlOpenArgs(url=url), context())
    assert launcher.targets == []


async def test_a_folder_is_opened_by_its_canonical_path(tmp_path: Path) -> None:
    folder = (tmp_path / "docs").resolve()
    folder.mkdir()
    launcher = Recorder()
    effects, output = await run(
        FolderOpenTool(launcher), FolderOpenArgs(path="./docs/../docs"), context(tmp_path)
    )
    assert effects == [ToolEffect(kind=EffectKind.LAUNCH, resource=str(folder))]
    assert output == FolderOpenOutput(path=str(folder))
    assert launcher.targets == [LaunchTarget("folder", str(folder))]


@pytest.mark.parametrize("name", ["missing", "notes.txt"])
async def test_only_an_existing_folder_is_opened(tmp_path: Path, name: str) -> None:
    (tmp_path / "notes.txt").write_bytes(b"x")
    launcher = Recorder()
    with pytest.raises(ToolPreviewFailed):
        await FolderOpenTool(launcher).preview(FolderOpenArgs(path=name), context(tmp_path))
    assert launcher.targets == []


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows открывает ShellExecute, без командной строки")
def test_posix_launch_is_an_argument_list_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    app = LaunchTarget("app", "/usr/share/applications/org.telegram.desktop.desktop", "desktop_entry")
    assert _posix_argv(app) == ["/usr/bin/gtk-launch", "org.telegram.desktop"]
    url = LaunchTarget("url", "https://example.com/?q=$(rm -rf /)")
    assert _posix_argv(url)[1] == "https://example.com/?q=$(rm -rf /)"  # один аргумент, не строка для shell
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ToolExecutionFailed, match="нет xdg-open"):
        _posix_argv(url)


@pytest.mark.parametrize("bundle", ["Evil.app", "Plugin.bundle"])
async def test_an_app_bundle_is_not_a_folder(tmp_path: Path, bundle: str) -> None:
    """На macOS `open Name.app` запустил бы программу вне инвентаря: folder.open такой «папки» не откроет,
    в том числе через ссылку."""
    (tmp_path / bundle / "Contents").mkdir(parents=True)
    launcher = Recorder()
    tool = FolderOpenTool(launcher)
    with pytest.raises(ToolPreviewFailed, match="пакет приложения"):
        await tool.preview(FolderOpenArgs(path=bundle), context(tmp_path))
    if sys.platform != "win32":
        (tmp_path / "link").symlink_to(tmp_path / bundle)
        with pytest.raises(ToolPreviewFailed, match="пакет приложения"):
            await tool.preview(FolderOpenArgs(path="link"), context(tmp_path))
    assert launcher.targets == []


def test_on_windows_a_folder_is_opened_with_the_explore_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(launch_module.sys, "platform", "win32")
    monkeypatch.setattr(
        launch_module.os, "startfile", lambda path, verb: calls.append((path, verb)), raising=False
    )
    system_launcher(LaunchTarget("folder", "C:\\Users\\me\\Downloads"))
    system_launcher(LaunchTarget("app", "C:/Apps/Telegram.lnk", "shortcut"))
    system_launcher(LaunchTarget("url", "https://github.com"))
    assert calls == [
        ("C:\\Users\\me\\Downloads", "explore"),  # файл вместо папки проводник не исполнит
        ("C:/Apps/Telegram.lnk", "open"),
        ("https://github.com", "open"),
    ]
