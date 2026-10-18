"""Где установлены программы-провайдеры (claude, gemini, qwen, node, npm) и как их запускать.

Ищем в PATH ядра, в PATH пользователя и системы из реестра (Jarvis мог запуститься раньше, чем человек
поставил программу, — тогда его PATH устарел) и в обычных местах установки на Windows: глобальные пакеты npm
(%APPDATA%\\npm\\*.cmd), установщики (%USERPROFILE%\\.local\\bin — Claude Code,
%LOCALAPPDATA%\\qwen-code\\bin — Qwen Code), WinGet, pnpm, Scoop, Volta, Bun.

Обёртки npm (.cmd) запускать через cmd.exe нельзя: cmd разбирает аргументы по-своему (& | ^ % в тексте — это
команды), поэтому обёртка разбирается и программа запускается напрямую — node.exe со скриптом или .exe. Если
обёртку разобрать не удалось, остаётся запуск через cmd.exe, но тогда аргументы проверяются (shell_safe), а
текст человека в аргументы не попадает никогда — он идёт через stdin и файлы.

Функции принимают env/platform/home — так их можно проверить на Linux с путями в стиле Windows.
"""

import contextlib
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

WIN_EXTS = (".com", ".exe", ".bat", ".cmd")
SCRIPT_EXTS = (".js", ".mjs", ".cjs")
_QUOTED = re.compile(r'"([^"]+)"')
_UNSAFE = re.compile(r'["&|<>^%!\r\n]')


@dataclass(frozen=True)
class Launch:
    """Как запустить программу: argv без аргументов самой программы."""

    argv: tuple[str, ...]
    path: str  # найденный файл (его видит человек в настройках)
    shell: bool = False  # через cmd.exe: обёртку не удалось разобрать — аргументы обязаны быть shell_safe


def _platform(platform: str | None) -> str:
    return platform or sys.platform


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _get(env: Mapping[str, str], name: str) -> str:
    """Переменная окружения без учёта регистра (на Windows Path и PATH — одно и то же)."""
    if name in env:
        return env[name]
    upper = name.upper()
    for key, value in env.items():
        if key.upper() == upper:
            return value
    return ""


def registry_path() -> list[str]:
    """PATH пользователя и системы из реестра Windows (свежий, а не тот, что достался ядру при запуске)."""
    if sys.platform != "win32":
        return []
    import winreg

    out: list[str] = []
    places = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for root, key in places:
        with contextlib.suppress(OSError), winreg.OpenKey(root, key) as handle:
            value, _ = winreg.QueryValueEx(handle, "Path")
            out += [os.path.expandvars(p) for p in str(value).split(";") if p.strip()]
    return out


def known_dirs(
    env: Mapping[str, str] | None = None, platform: str | None = None, home: str | None = None
) -> list[str]:
    """Обычные места установки программ (кроме PATH)."""
    env = _env(env)
    home = home or _get(env, "USERPROFILE") or _get(env, "HOME") or str(Path.home())
    if _platform(platform) == "win32":
        appdata = _get(env, "APPDATA") or os.path.join(home, "AppData", "Roaming")
        local = _get(env, "LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        program_files = _get(env, "ProgramFiles") or "C:\\Program Files"
        dirs = [
            os.path.join(appdata, "npm"),  # npm install -g (по умолчанию)
            os.path.join(home, ".local", "bin"),  # Claude Code (установщик)
            os.path.join(local, "qwen-code", "bin"),  # Qwen Code (установщик)
            os.path.join(local, "Microsoft", "WinGet", "Links"),
            os.path.join(local, "pnpm"),
            os.path.join(home, "scoop", "shims"),
            os.path.join(local, "Volta", "bin"),
            os.path.join(home, ".bun", "bin"),
            os.path.join(program_files, "nodejs"),  # nvm-windows и prefix в папке Node
        ]
        nvm = _get(env, "NVM_SYMLINK")
        if nvm:
            dirs.append(nvm)
        return dirs
    return [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".claude", "local"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]


def search_dirs(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    env = _env(env)
    sep = ";" if _platform(platform) == "win32" else ":"
    dirs = [d for d in _get(env, "PATH").split(sep) if d.strip()]
    dirs += extra if extra is not None else registry_path()
    dirs += known_dirs(env, platform, home)
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        key = os.path.normcase(os.path.normpath(d.strip().strip('"')))
        if key and key not in seen:
            seen.add(key)
            out.append(d.strip().strip('"'))
    return out


def find_program(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | None = None,
    extra: list[str] | None = None,
) -> str | None:
    """Полный путь к программе (на Windows — с расширением .exe/.cmd) или None."""
    plat = _platform(platform)
    exts: tuple[str, ...] = ("",)
    if plat == "win32":
        pathext = [e.lower() for e in _get(_env(env), "PATHEXT").split(";") if e.strip()]
        exts = tuple(e for e in pathext if e in WIN_EXTS) or WIN_EXTS
        exts = tuple(sorted(exts, key=lambda e: WIN_EXTS.index(e)))
    for folder in search_dirs(env, plat, home, extra):
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate) and (plat == "win32" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def parse_cmd_shim(text: str) -> str | None:
    """Цель обёртки .cmd/.bat (npm, pnpm, установщик Qwen): путь к .js/.exe/.cmd, %dp0% — папка обёртки."""
    candidates = []
    for match in _QUOTED.finditer(text):
        value = match.group(1).strip()
        low = value.lower()
        if "%_prog%" in low or low.endswith("\\node.exe") or low.endswith("/node.exe"):
            continue
        if low.endswith((*SCRIPT_EXTS, ".exe", ".cmd", ".bat")):
            candidates.append(value)
    return candidates[-1] if candidates else None


def _expand_dp0(value: str, folder: str) -> str:
    out = value
    for token in ("%dp0%\\", "%dp0%/", "%~dp0\\", "%~dp0/", "%dp0%", "%~dp0"):
        out = re.sub(re.escape(token), lambda _m: folder + os.sep, out, flags=re.I)
    out = out.replace("\\", os.sep) if os.sep != "\\" else out
    return os.path.normpath(out)


def find_node(
    env: Mapping[str, str] | None = None, platform: str | None = None, home: str | None = None
) -> str | None:
    return find_program("node", env=env, platform=platform, home=home)


def resolve_launch(
    path: str,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | None = None,
    depth: int = 0,
) -> Launch:
    """Найденный файл → как его запускать (обёртки npm разбираются: node.exe + скрипт или .exe)."""
    plat = _platform(platform)
    low = path.lower()
    if plat != "win32" or not low.endswith((".cmd", ".bat")):
        if low.endswith(SCRIPT_EXTS):
            node = find_node(env, plat, home)
            if node:
                return Launch((node, path), path)
        return Launch((path,), path)
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    folder = os.path.dirname(path)
    target = parse_cmd_shim(text)
    if target:
        resolved = _expand_dp0(target, folder)
        if not os.path.isabs(resolved):
            resolved = os.path.normpath(os.path.join(folder, resolved))
        if os.path.isfile(resolved):
            rlow = resolved.lower()
            if rlow.endswith(SCRIPT_EXTS):
                local_node = os.path.join(folder, "node.exe")
                node = local_node if os.path.isfile(local_node) else find_node(env, plat, home)
                if node:
                    return Launch((node, resolved), path)
            elif rlow.endswith(".exe"):
                return Launch((resolved,), path)
            elif (
                rlow.endswith((".cmd", ".bat"))
                and depth < 3
                and os.path.normcase(resolved) != os.path.normcase(path)
            ):
                inner = resolve_launch(resolved, env=env, platform=plat, home=home, depth=depth + 1)
                return Launch(inner.argv, path, inner.shell)
    comspec = _get(_env(env), "ComSpec") or "cmd.exe"
    return Launch((comspec,), path, shell=True)


def shell_safe(value: str) -> bool:
    """Аргумент можно передать через cmd.exe: без кавычек и спецсимволов (& | < > ^ % !)."""
    return not _UNSAFE.search(value)


def shell_command(launch: Launch, args: list[str]) -> str:
    """Строка команды для cmd.exe /c (только для Launch.shell; аргументы — shell_safe)."""

    def quote(value: str) -> str:
        return f'"{value}"' if (not value or " " in value or "(" in value or ")" in value) else value

    return " ".join([quote(launch.path), *(quote(a) for a in args)])
