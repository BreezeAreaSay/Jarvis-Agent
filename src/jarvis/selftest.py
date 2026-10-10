"""Самопроверка бандла: `jarvis-cli.exe selftest` (в разработке — `uv run jarvis selftest`).

Без сети и GPU. По пунктам печатает ✓/✗ и время; код выхода 0 — всё ✓, 1 — есть ✗.
Пункты: файлы бандла, импорт всех модулей jarvis.* и pc.*, конфиг, плагины Qt, окно (LauncherWindow скрыто,
рендер в PNG), иконка, MCP-сервер pc (подпроцесс, ровно 18 инструментов), `codex --version` бинаря из SDK.

Настоящий каталог данных и ~/.codex не трогаются: на время проверки JARVIS_DATA_DIR, JARVIS_CONFIG
и CODEX_HOME указывают во временную папку. Офскрин Qt (QT_QPA_PLATFORM=offscreen) — флаг --offscreen
или нет дисплея (Linux).
"""

import argparse
import contextlib
import importlib
import json
import os
import pkgutil
import shutil
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pc import settings, subproc

# инструменты MCP-сервера pc (docs/architecture.md, pc.mcp)
MCP_TOOLS = frozenset(
    {
        "list_windows",
        "list_apps",
        "find_files",
        "open",
        "focus",
        "close",
        "window",
        "volume",
        "media",
        "processes",
        "kill_process",
        "clipboard_get",
        "clipboard_set",
        "read_text_file",
        "move_to_trash",
        "type_text",
        "lock",
        "power",
    }
)
PACKAGES = ("jarvis", "pc")
# модули из docs/architecture.md: нет в бандле — ✗ (значит, не собран или не реализован)
REQUIRED_MODULES = (
    "pc.result",
    "pc.settings",
    "pc.subproc",
    "pc.policy",
    "pc.confirm_client",
    "pc.paths",
    "pc.privacy",
    "pc.apps",
    "pc.windows",
    "pc.procs",
    "pc.files",
    "pc._input",
    "pc.audio",
    "pc.media",
    "pc.system",
    "pc.mcp",
    "jarvis.events",
    "jarvis.config",
    "jarvis.context",
    "jarvis.log",
    "jarvis.grammar",
    "jarvis.router",
    "jarvis.hands",
    "jarvis.execute",
    "jarvis.brain",
    "jarvis.core",
    "jarvis.journal",
    "jarvis.cli",
    "jarvis.winapp",
    "jarvis.app",
    "jarvis.doctor",
    "jarvis.bench",
    "jarvis.selftest",
    "jarvis.ui.logic",
    "jarvis.ui.window",
    "jarvis.ui.icon",
    "jarvis.ui.tray",
)
BUNDLE_FILES = (
    "scripts/start_hands.cmd",
    "jarvis.example.toml",
    "bench/phrases.ru.jsonl",
    "tests/fixtures/apps.json",  # инвентарь для `jarvis-cli bench`
)
EXE_STEMS = ("Jarvis", "jarvis-cli")
ICON_STATES = ("ready", "busy", "local", "warn")
MCP_TIMEOUT_S = 60.0
CODEX_TIMEOUT_S = 60.0

_qt_app: list[Any] = []  # QApplication живёт до конца процесса: второй создать нельзя


class Fail(Exception):
    """Пункт не прошёл; текст — что именно не так."""


@dataclass
class Ctx:
    tmp: Path
    offscreen: bool
    notes: list[str] = field(default_factory=list)


def exe_name(stem: str) -> str:
    return f"{stem}.exe" if sys.platform == "win32" else stem


def _out(text: str) -> None:
    if sys.stdout is not None:  # под оконным Jarvis.exe потоков нет
        print(text, flush=True)


def _utf8_streams() -> None:
    """Pipe и файл (лог CI) — UTF-8, иначе «✓» в cp1252/cp866 роняет print; консоль не трогаем."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        with contextlib.suppress(AttributeError, OSError, ValueError):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def _no_display() -> bool:
    return sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


# --- пункты ------------------------------------------------------------------------------------------------


def check_files(ctx: Ctx) -> str:
    """Файлы бандла относительно app_root(); во frozen — оба exe рядом."""
    root = settings.app_root()
    missing = [rel for rel in BUNDLE_FILES if not _non_empty(root / rel)]
    if settings.is_frozen():
        missing += [exe_name(s) for s in EXE_STEMS if not (settings.install_dir() / exe_name(s)).is_file()]
    if missing:
        raise Fail(f"нет файлов: {', '.join(missing)} (app_root={root})")
    phrases = root / "bench" / "phrases.ru.jsonl"
    count = 0
    for n, line in enumerate(phrases.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                json.loads(line)
            except ValueError as e:
                raise Fail(f"{phrases.name}: строка {n} — не JSON ({e})") from e
            count += 1
    apps_file = root / "tests" / "fixtures" / "apps.json"
    try:
        apps = json.loads(apps_file.read_text(encoding="utf-8"))
    except ValueError as e:
        raise Fail(f"{apps_file.name}: не JSON ({e})") from e
    if not isinstance(apps, list) or not apps:
        raise Fail(f"{apps_file.name}: ждали непустой список приложений")
    es = settings.es_path()
    es_note = "es.exe есть" if es.is_file() else f"es.exe нет: {es} (не обязателен)"
    exes = f", {' и '.join(exe_name(s) for s in EXE_STEMS)} рядом" if settings.is_frozen() else ""
    counts = f"фраз в корпусе: {count}; приложений в apps.json: {len(apps)}"
    return f"файлов: {len(BUNDLE_FILES)}{exes}; {counts}; {es_note}"


def _non_empty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def check_modules(ctx: Ctx) -> str:
    """Импорт всех модулей пакетов jarvis и pc (pkgutil.walk_packages) и наличие модулей из контракта."""
    found: list[str] = []
    failed: list[str] = []

    def onerror(name: str) -> None:
        failed.append(f"{name}: {_exc_text(sys.exc_info()[1])}")

    for pkg_name in PACKAGES:
        pkg = importlib.import_module(pkg_name)
        found.append(pkg_name)
        for info in pkgutil.walk_packages(pkg.__path__, f"{pkg_name}.", onerror=onerror):
            if not info.name.endswith(".__main__"):
                found.append(info.name)
    for name in found:
        try:
            importlib.import_module(name)
        except Exception as e:  # модуль сломан — это и ищем
            failed.append(f"{name}: {_exc_text(e)}")
    present = set(found)
    missing = [m for m in REQUIRED_MODULES if m.split(".", 1)[0] in PACKAGES and m not in present]
    problems = []
    if missing:
        problems.append(f"нет модулей: {', '.join(missing)}")
    if failed:
        problems.append("не импортируются: " + "; ".join(failed))
    if problems:
        raise Fail(" | ".join(problems))
    return f"импортировано модулей: {len(found)}"


def check_config(ctx: Ctx) -> str:
    """jarvis.example.toml читается tomllib; jarvis.config.load() разбирает его копию без ошибок."""
    example = settings.app_root() / "jarvis.example.toml"
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    absent = [
        s for s in ("ui", "hands", "brain", "pc", "aliases", "journal") if not isinstance(data.get(s), dict)
    ]
    if absent:
        raise Fail(f"в {example.name} нет секций: {', '.join(absent)}")
    from jarvis import config

    cfg = config.load()
    error = settings.config_error()
    if error:
        raise Fail(f"config.load(): {error}")
    if cfg.ui.hotkey != data["ui"].get("hotkey") or cfg.mode != data.get("mode", "normal"):
        raise Fail(f"load() вернул не то, что в примере: mode={cfg.mode!r}, hotkey={cfg.ui.hotkey!r}")
    return f"пример читается; load(): mode={cfg.mode}, хоткей {cfg.ui.hotkey}"


def _qapp() -> Any:
    """QApplication процесса (создать, если нет). QGuiApplication без виджетов не годится — ✗."""
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([sys.argv[0] if sys.argv else "jarvis"])
        _qt_app.append(app)
    elif not isinstance(app, QtWidgets.QApplication):
        raise Fail(f"в процессе уже есть {type(app).__name__}, а окну нужен QApplication")
    return app


def check_qt_plugins(ctx: Ctx) -> str:
    """Плагин платформы на месте: qwindows (Windows; без него Jarvis.exe не покажет окно) и qoffscreen."""
    from PySide6.QtCore import QLibraryInfo

    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    platforms = plugins / "platforms"
    names = [p.name.lower() for p in platforms.iterdir()] if platforms.is_dir() else []
    need = ["qwindows", "qoffscreen"] if sys.platform == "win32" else ["qoffscreen"]
    missing = [n for n in need if not any(n in name for name in names)]
    if missing:
        raise Fail(f"нет плагинов платформы {', '.join(missing)} в {platforms}")
    return f"{', '.join(need)} в {platforms}"


def check_window(ctx: Ctx) -> str:
    """LauncherWindow(ui_cfg) создаётся скрытым, рисуется в PNG (файл > 0 байт)."""
    app = _qapp()
    from jarvis import config
    from jarvis.ui.window import LauncherWindow

    window = LauncherWindow(config.load().ui)
    try:
        app.processEvents()
        if window.isVisible():
            raise Fail("окно видно сразу после создания (должно показываться только по хоткею)")
        pixmap = window.grab()
        path = ctx.tmp / "window.png"
        if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
            raise Fail("grab() не дал картинку")
        size = path.stat().st_size
        if size <= 0:
            raise Fail("PNG окна пустой")
        return f"{pixmap.width()}×{pixmap.height()}, PNG {size} байт, платформа {app.platformName()}"
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def check_icon(ctx: Ctx) -> str:
    """render_icon рисует все состояния трея (тот же рисунок — в .ico)."""
    _qapp()
    from jarvis.ui.icon import render_icon

    for state in ICON_STATES:
        for size in (16, 32, 256):
            img = render_icon(state, size)
            if img.isNull() or img.width() != size or img.height() != size:
                raise Fail(f"render_icon({state!r}, {size}) — пустая или не того размера картинка")
    img.save(str(ctx.tmp / "icon.png"), "PNG")
    return f"{len(ICON_STATES)} состояния × 16/32/256 px"


def mcp_command() -> tuple[str, list[str], Path]:
    """Как мозг запускает сервер pc: во frozen — `<install>\\jarvis-cli.exe mcp`, иначе `python -m pc.mcp`."""
    if settings.is_frozen():
        install = settings.install_dir()
        return str(install / exe_name("jarvis-cli")), ["mcp"], install
    return sys.executable, ["-m", "pc.mcp"], settings.app_root()


def check_mcp(ctx: Ctx) -> str:
    """Сервер pc как подпроцесс, клиент mcp по stdio: ровно 18 инструментов."""
    command, args, cwd = mcp_command()
    env = {
        "JARVIS_DATA_DIR": os.environ["JARVIS_DATA_DIR"],
        "JARVIS_CONFIG": os.environ["JARVIS_CONFIG"],
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    errlog_path = ctx.tmp / "mcp-stderr.log"
    started = time.perf_counter()
    try:
        names = _mcp_list_tools(command, args, cwd, env, errlog_path)
    except Exception as e:
        raise Fail(f"{_exc_text(e)}; stderr сервера: {_tail(errlog_path)}") from e
    elapsed = time.perf_counter() - started
    missing = sorted(MCP_TOOLS - set(names))
    extra = sorted(set(names) - MCP_TOOLS)
    if missing or extra or len(names) != len(MCP_TOOLS):
        raise Fail(f"инструментов {len(names)} (ждали {len(MCP_TOOLS)}); нет: {missing}; лишние: {extra}")
    shown = " ".join([Path(command).name, *args])
    return f"{len(names)} инструментов за {elapsed:.1f} с ({shown})"


def _mcp_list_tools(
    command: str, args: list[str], cwd: Path, env: dict[str, str], errlog_path: Path
) -> list[str]:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)

    async def run() -> list[str]:
        with errlog_path.open("w", encoding="utf-8", errors="replace") as errlog:
            with anyio.fail_after(MCP_TIMEOUT_S):
                async with (
                    stdio_client(params, errlog=errlog) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    result = await session.list_tools()
        return [tool.name for tool in result.tools]

    return anyio.run(run)


def _tail(path: Path, lines: int = 3) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return "(нет)"
    return " ⏎ ".join(text[-lines:]) or "(пусто)"


def _bundled_codex() -> Path:
    """Путь, который вычисляет сам пакет codex_cli_bin (в бандле — от своего __file__ в _MEIPASS)."""
    from codex_cli_bin import bundled_codex_path

    return bundled_codex_path()


def _codex_bin() -> Path:
    from jarvis import brain

    return Path(brain.codex_bin())


def check_codex(ctx: Ctx) -> str:
    """`codex --version` бинаря из SDK (jarvis.brain.codex_bin()); во frozen — из бандла."""
    bundled = _bundled_codex()
    try:
        path = _codex_bin()
    except NotImplementedError as e:
        version = _codex_version(bundled, ctx)
        raise Fail(
            f"не реализовано: jarvis.brain.codex_bin() ({e}); бинарь пакета: {bundled} — {version}"
        ) from e
    if not path.is_file():
        raise Fail(f"нет файла {path}")
    if path.resolve() != bundled.resolve():
        raise Fail(f"codex_bin()={path} не совпадает с codex_cli_bin.bundled_codex_path()={bundled}")
    if settings.is_frozen() and not path.resolve().is_relative_to(settings.app_root().resolve()):
        raise Fail(f"codex не из бандла: {path}")
    return f"{_codex_version(path, ctx)} ({path})"


def _codex_version(path: Path, ctx: Ctx) -> str:
    home = ctx.tmp / "codex-home"  # не ~/.codex: codex создаёт там tmp даже на --version
    home.mkdir(parents=True, exist_ok=True)
    try:
        r = subproc.run(
            [path, "--version"], timeout=CODEX_TIMEOUT_S, env={**os.environ, "CODEX_HOME": str(home)}
        )
    except Exception as e:
        raise Fail(f"{path} не запустился: {_exc_text(e)}") from e
    out = r.stdout.decode("utf-8", errors="replace").strip()
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise Fail(f"{path} --version: код {r.returncode}; {err[-300:] or out[-300:]}")
    return out.splitlines()[0] if out else "(пустой вывод)"


CHECKS: list[tuple[str, Callable[[Ctx], str]]] = [
    ("файлы бандла", check_files),
    ("модули", check_modules),
    ("конфиг", check_config),
    ("Qt: плагины платформы", check_qt_plugins),
    ("окно", check_window),
    ("иконка", check_icon),
    ("MCP-сервер pc", check_mcp),
    ("codex --version", check_codex),
]


# --- запуск ------------------------------------------------------------------------------------------------


def _exc_text(e: BaseException | None) -> str:
    if e is None:
        return "ошибка"
    while isinstance(e, BaseExceptionGroup) and e.exceptions:  # anyio/mcp: настоящая причина — внутри группы
        e = e.exceptions[0]
    if isinstance(e, Fail):
        return str(e)
    if isinstance(e, NotImplementedError):
        return f"не реализовано{': ' + str(e) if str(e) else ''}"
    if isinstance(e, ModuleNotFoundError):
        return f"нет модуля {e.name}"
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def run_check(name: str, fn: Callable[[Ctx], str], ctx: Ctx) -> tuple[bool, str]:
    started = time.perf_counter()
    try:
        ok, text = True, fn(ctx)
    except Exception as e:  # любой сбой пункта — ✗ с понятным текстом, остальные пункты идут дальше
        ok, text = False, _exc_text(e)
    mark = "✓" if ok else "✗"
    _out(f"{mark} {name}: {text} [{time.perf_counter() - started:.1f} с]")
    return ok, text


@contextlib.contextmanager
def isolated_env(tmp: Path, offscreen: bool) -> Iterator[None]:
    """JARVIS_DATA_DIR, JARVIS_CONFIG (копия примера) — во временной папке; после проверки всё как было."""
    keys = ("JARVIS_DATA_DIR", "JARVIS_CONFIG", "JARVIS_CONFIRM_PIPE", "QT_QPA_PLATFORM")
    saved = {k: os.environ.get(k) for k in keys}
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "jarvis.toml"
    example = settings.app_root() / "jarvis.example.toml"
    if example.is_file():
        shutil.copyfile(example, cfg)
    os.environ["JARVIS_DATA_DIR"] = str(data)
    os.environ["JARVIS_CONFIG"] = str(cfg)
    os.environ.pop("JARVIS_CONFIRM_PIPE", None)
    if offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("jarvis")
    except Exception:
        return "?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis selftest", description="Проверка бандла без сети и GPU")
    parser.add_argument("--offscreen", action="store_true", help="Qt без экрана (QT_QPA_PLATFORM=offscreen)")
    args = parser.parse_args([] if argv is None else argv)
    _utf8_streams()
    offscreen = args.offscreen or _no_display() or os.environ.get("QT_QPA_PLATFORM") == "offscreen"
    mode = "exe" if settings.is_frozen() else "разработка"
    _out(f"Jarvis {_version()} selftest ({mode}, Python {sys.version.split()[0]}, {sys.platform})")
    _out(f"  app_root: {settings.app_root()}; install_dir: {settings.install_dir()}")
    results = []
    with (
        tempfile.TemporaryDirectory(prefix="jarvis-selftest-", ignore_cleanup_errors=True) as tmp,
        isolated_env(Path(tmp), offscreen),
    ):
        ctx = Ctx(Path(tmp), offscreen)
        for name, fn in CHECKS:
            results.append(run_check(name, fn, ctx))
    failed = sum(1 for ok, _ in results if not ok)
    total = len(results)
    _out(f"итог: {total - failed}/{total} ✓" + (f", {failed} ✗" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
