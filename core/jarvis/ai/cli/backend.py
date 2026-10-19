"""Провайдеры через официальные программы человека: «Claude (подписка)» — Claude Code, «Gemini CLI»,
«Qwen Code».

Jarvis ничего не знает о входе в эти программы: не открывает страницы входа, не читает и не передаёт токены,
не скачивает Claude Code. Он находит установленную программу (jarvis.ai.cli.locate), запускает её так, как
запустил бы скрипт самого человека, — в безголовом режиме с выводом stream-json (jarvis.ai.cli.specs) — и
переводит её события в события чата (chat.delta, chat.status, chat.done…). Войти человек может сам в
терминале; кнопка «Открыть терминал» только запускает программу в новом окне консоли.

Инструменты Jarvis приходят к программе с сервера MCP ядра (jarvis.mcp.server): на каждый ход — свой токен,
вызов инструмента выполняется тем же кодом, что у провайдеров по ключу (Ai._call_tool): карточки,
подтверждения, источники, «Запомнил…», режим «Чат» (инструмент, которого нет в режиме, отвечает отказом).

Каждый ход — отдельный запуск программы. Продолжение разговора — сессия программы на чат Jarvis (kv
`cli:<провайдер>:session:<chat_id>`): Claude Code и Qwen Code получают --session-id, а потом --resume;
Gemini CLI сам выдаёт id сессии (событие init) и продолжает её по --resume. Не продолжилась — новая сессия
с пересказом прошлой переписки. Стоп убивает программу вместе с дочерними процессами.

Агент работает одним запуском программы, где инструменты — действия агента (Agent._act: подтверждения, лимит
шагов, события agent.*). Одиночные запросы (complete) — запуск без инструментов и без сохранения сессии.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis import netproxy, paths
from jarvis.ai import apphints
from jarvis.ai.chatgpt import prompt as context_prompt
from jarvis.ai.chatgpt.backend import CHAT_MODE_DENIED
from jarvis.ai.cli import errors, locate, prompt
from jarvis.ai.cli.locate import Launch
from jarvis.ai.cli.process import CliProcess, CliStartError
from jarvis.ai.cli.specs import KINDS, CliKind, CliRun
from jarvis.ai.cli.stream import CliEvent, parse_line
from jarvis.ai.llm import BAD_FORMAT, DISABLED, LlmError, LlmFormatError, ToolCall, Usage, extract_json
from jarvis.ai.providers import PRESETS
from jarvis.ai.tools import Tool, ToolEnv
from jarvis.db import new_id
from jarvis.mcp.server import McpBinding, McpResult
from jarvis.services import AskResult, UserFacingError

if TYPE_CHECKING:
    from jarvis.ai.agent import Agent, AgentRun
    from jarvis.ai.service import Ai, Turn

log = logging.getLogger(__name__)

EVENT = "ai.cli"
LOCATE_TTL_S = 10.0
SHORT_TIMEOUT_S = 20.0  # --version, auth status
INACTIVE_S = 300.0  # программа молчит, и ни один инструмент не работает — считаем, что зависла
POLL_S = 2.0
INSTALL_TIMEOUT_S = 900.0
TOOL_ORDER_S = 0.02
VOICE_WINDOW_S = 3 * 3600  # голосовой чат: сессию старше трёх часов не продолжаем
VOICE_MAX_TURNS = 30
# окружение ядра, которое программам не нужно: токен API ядра и пути Python самого Jarvis
PRIVATE_ENV = ("JARVIS_CORE_TOKEN", "PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9._:/@\[\]-]{1,80}$")
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-.][0-9A-Za-z.]+)?")


@dataclass
class CliOutcome:
    """Итог запуска программы."""

    session_id: str = ""
    model: str = ""
    rounds: list[str] = field(default_factory=list)  # текст каждого ответа модели
    result: CliEvent | None = None
    errors: list[CliEvent] = field(default_factory=list)
    retry_code: str = ""
    rate_limit: dict[str, Any] | None = None
    exit_code: int | None = None
    stderr: str = ""
    hung: bool = False
    killed: bool = False  # остановили мы (лимит шагов агента)
    usage: tuple[int, int] = (0, 0)
    tools: list[str] = field(default_factory=list)  # вызванные инструменты (по выводу программы)
    _round: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.ok and not self.errors and not self.hung

    @property
    def final(self) -> str:
        for text in reversed(self.rounds):
            if text.strip():
                return text.strip()
        result = self.result.text.strip() if self.result is not None else ""
        return result

    def apply(self, event: CliEvent) -> None:
        if event.kind == "init":
            self.session_id = event.session_id or self.session_id
            self.model = event.model or self.model
        elif event.kind == "round":
            self._close_round()
        elif event.kind == "delta":
            self._round += event.text
        elif event.kind == "message":
            self._round = ""
            self.rounds.append(event.text)
        elif event.kind == "tool_use":
            self._close_round()
            self.tools.append(event.name)
        elif event.kind == "retry":
            self.retry_code = event.code
        elif event.kind == "rate_limit":
            self.rate_limit = event.data
        elif event.kind == "error":
            self.errors.append(event)
        elif event.kind == "result":
            self._close_round()
            self.result = event
            self.session_id = event.session_id or self.session_id
            self.usage = event.usage

    def _close_round(self) -> None:
        if self._round.strip():
            self.rounds.append(self._round)
        self._round = ""


class CliSink:
    """События программы → события чата (как у провайдеров по ключу)."""

    def __init__(self, ai: "Ai", turn: "Turn", name: str) -> None:
        self.ai = ai
        self.turn = turn
        self.name = name

    def __call__(self, event: CliEvent) -> None:
        ai, turn = self.ai, self.turn
        if event.kind == "round":
            turn.new_round = True
        elif event.kind == "delta":
            ai._delta(turn, event.text)
        elif event.kind == "message":
            if not event.data.get("streamed"):
                turn.new_round = True
                ai._delta(turn, event.text)
            ai._flush(turn)
            turn.rounds.append(event.text)
        elif event.kind == "tool_use":
            ai._flush(turn)
        elif event.kind == "retry":
            ai._status(turn, "thinking", f"Нет ответа от {self.name}, повторяю…")


class CliBackend:
    """Один провайдер-программа для Ai: состояние, установка, вход, ходы чата, одиночные запросы, агент."""

    def __init__(self, ai: "Ai", kind: CliKind) -> None:
        self.ai = ai
        self.ctx = ai.ctx
        self.kind = kind
        self.info = kind.info
        self.id = kind.info.id
        # тесты подменяют поиск программы (Launch с фальшивой программой) и открытие терминала
        self.locator: Callable[[], Launch | None] = self._locate
        self.terminal: Callable[[Path], Any] | None = None
        self._launch: Launch | None = None
        self._located = 0.0
        self._version: tuple[str, str] = ("", "")  # (путь, версия)
        self._detected: bool | None = None  # вход по признакам (статус программы, файлы)
        self._account = ""
        self._installing = False
        self._install_error = ""
        self._install_log = ""
        self._auth_task: asyncio.Task[Any] | None = None

    # --- папки -----------------------------------------------------------------------------------------
    def work_dir(self, *, once: bool = False) -> Path:
        """Пустая рабочая папка программы (отдельная — для одиночных запросов и агента)."""
        name = self.info.binary + ("-once" if once else "")
        return paths.sub_dir("cli", name)

    # --- состояние -------------------------------------------------------------------------------------
    @property
    def selected(self) -> bool:
        return self.ctx.settings.value.ai.provider == self.id

    def _locate(self) -> Launch | None:
        path = locate.find_program(self.info.binary)
        return locate.resolve_launch(path) if path else None

    def launch(self, *, refresh: bool = False) -> Launch | None:
        """Где программа (кэш на 10 с: проверка частая)."""
        if refresh or time.monotonic() - self._located > LOCATE_TTL_S:
            try:
                self._launch = self.locator()
            except Exception:
                log.exception("поиск %s упал", self.info.binary)
                self._launch = None
            self._located = time.monotonic()
        return self._launch

    @property
    def installed(self) -> bool:
        return self.launch() is not None

    def _auth_record(self) -> dict[str, Any]:
        value = self.ctx.db.kv_get(f"cli:{self.id}:auth")
        return value if isinstance(value, dict) else {}

    def _remember_auth(self, ok: bool | None, reason: str = "") -> None:
        before = self.logged_in()
        if ok is None:
            self.ctx.db.delete("kv", f"cli:{self.id}:auth", key="key")
        else:
            self.ctx.db.kv_set(f"cli:{self.id}:auth", {"ok": ok, "reason": reason, "ts": time.time()})
        if before != self.logged_in():
            self.ai.refresh_configured()
            self._publish("auth", logged_in=self.logged_in())  # панель настроек перечитает состояние

    def logged_in(self) -> bool | None:
        """Вход выполнен: True / False / None — неизвестно (проверьте подключение)."""
        record = self._auth_record()
        if "ok" in record:
            return bool(record["ok"])
        return self._detected

    @property
    def configured(self) -> bool:
        try:
            return self.problem() is None and self.logged_in() is not False
        except Exception:
            log.exception("состояние %s не читается", self.info.name)
            return False

    def problem(self) -> str | None:
        """Почему сейчас нельзя спросить программу (текст для человека) или None."""
        if not self.ctx.settings.value.ai.enabled:
            return DISABLED
        if self._installing:
            return f"{self.info.name} ещё устанавливается — подождите немного"
        if not self.installed:
            return errors.NOT_INSTALLED.format(name=self.info.name)
        return None

    def _tools_problem(self) -> str | None:
        if not self.ai.mcp_server.available:
            return errors.NO_MCP.format(name=self.info.name)
        return None

    def _publish(self, state: str, **data: Any) -> None:
        self.ctx.bus.publish(EVENT, {"provider": self.id, "state": state, **data})

    # --- модели ----------------------------------------------------------------------------------------
    def resolve_model(self, requested: str | None = None) -> str:
        """Модель запуска: из чата или настроек, если она похожа на модель этой программы; иначе умолчание."""
        preset = PRESETS[self.id]
        name = (requested or "").strip() or self.ctx.settings.value.ai.model.strip()
        if not name or not _MODEL_NAME.match(name):
            return preset.default_model
        if name in preset.models:
            return name
        foreign = {m for p in PRESETS.values() if p.id != self.id for m in (p.default_model, *p.models) if m}
        return preset.default_model if name in foreign else name

    def preset_json(self, base: dict[str, Any]) -> dict[str, Any]:
        return dict(base)

    async def list_models(self) -> list[str]:
        return list(PRESETS[self.id].models)

    # --- жизненный цикл --------------------------------------------------------------------------------
    async def start(self) -> None:
        if self.selected and self.ctx.settings.value.ai.enabled:
            self._check_auth_soon()

    async def stop(self) -> None:
        task, self._auth_task = self._auth_task, None
        if task is not None and not task.done():
            task.cancel()

    def on_selected(self) -> None:
        """Провайдер выбран в настройках: проверить вход в фоне."""
        self._check_auth_soon()

    def _check_auth_soon(self) -> None:
        if self._auth_task is not None and not self._auth_task.done():
            return
        with contextlib.suppress(RuntimeError):
            self._auth_task = self.ctx.spawn(self._detect_auth(), f"cli-auth-{self.id}")

    # --- вспомогательные запуски -------------------------------------------------------------------------
    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        stripped = {name.upper() for name in (*self.kind.strip_env(), *PRIVATE_ENV)}
        for name in list(env):
            if name.upper() in stripped:
                env.pop(name)
        proxy = self.proxy()
        if proxy:
            for name in [k for k in env if k.upper() in ("HTTPS_PROXY", "HTTP_PROXY")]:
                env.pop(name)  # https_proxy в нижнем регистре Claude Code предпочёл бы нашему
            env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy)
        # к серверу MCP ядра (127.0.0.1) — всегда напрямую, мимо прокси
        current = next((v for k, v in env.items() if k.upper() == "NO_PROXY"), "")
        items = [p.strip() for p in current.split(",") if p.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in items:
                items.append(host)
        for name in [k for k in env if k.upper() == "NO_PROXY"]:
            env.pop(name)
        env["NO_PROXY"] = ",".join(items)
        if sys.platform != "win32":
            env["no_proxy"] = env["NO_PROXY"]
        env.update(extra or {})
        return env

    def proxy(self) -> str:
        """Прокси для программы: из настроек Jarvis AI, иначе системный прокси Windows ("" — напрямую).

        Node.js (Claude Code, Gemini CLI, Qwen Code) не читает системный прокси, который включает
        VPN-программа, — без него запросы шли бы мимо VPN («недоступен из вашего региона»). SOCKS Claude Code
        не понимает, а поймёт ли системный SOCKS другая программа, неизвестно: такой прокси не передаём, а
        ошибку связи объясняем (_socks_problem).
        """
        proxy, system = self._wanted_proxy()
        if netproxy.is_socks(proxy) and (system or not self.info.socks):
            return ""
        return proxy

    def _wanted_proxy(self) -> tuple[str, bool]:
        """(прокси из настроек или системный, системный ли он)."""
        proxy = self.ctx.settings.value.ai.proxy.strip()
        if proxy:
            return proxy, False
        if any(k.upper() in ("HTTPS_PROXY", "HTTP_PROXY") for k in os.environ):
            return "", False  # прокси уже задан в окружении — программа возьмёт его сама
        return netproxy.system_https(), True

    def _socks_problem(self) -> str:
        """Почему, возможно, нет связи: SOCKS-прокси, который программе не передан ("" — не в этом дело)."""
        proxy, system = self._wanted_proxy()
        if not netproxy.is_socks(proxy) or self.proxy():
            return ""
        text = errors.SYSTEM_SOCKS if system else errors.NO_SOCKS
        return text.format(program=self.info.name, proxy=proxy)

    async def _short(self, launch: Launch, args: list[str]) -> tuple[int | None, str, str]:
        """Короткий запуск (--version, auth status): (код выхода, stdout, stderr)."""
        proc = CliProcess(launch, args, cwd=str(self.work_dir()), env=self._env({"NO_COLOR": "1"}))
        try:
            await proc.start(None)
        except CliStartError as e:
            return None, "", str(e)
        chunks: list[bytes] = []

        async def read() -> None:
            while True:
                line = await proc.readline()
                if not line:
                    break
                chunks.append(line)

        try:
            await asyncio.wait_for(read(), SHORT_TIMEOUT_S)
            code = await proc.wait(5.0)
        except TimeoutError:
            code = None
        finally:
            await proc.close()
        return code, b"".join(chunks).decode("utf-8", "replace"), proc.stderr_tail

    async def version(self) -> str:
        launch = self.launch()
        if launch is None:
            return ""
        if self._version[0] == launch.path and self._version[1]:
            return self._version[1]
        code, out, err = await self._short(launch, ["--version"])
        found = _VERSION.search(out) or _VERSION.search(err)
        value = found.group(0) if found and code == 0 else ""
        self._version = (launch.path, value)
        return value

    async def _detect_auth(self) -> bool | None:
        """Вход по признакам, не читая секретов: статус самой программы (Claude Code) или наличие файлов."""
        try:
            result = await self._detect()
        except Exception:
            log.exception("вход в %s не проверился", self.info.name)
            result = None
        changed = result != self._detected
        self._detected = result
        if changed:
            self.ai.refresh_configured()
        return result

    async def _detect(self) -> bool | None:
        launch = self.launch()
        if launch is None:
            return None
        args = self.kind.auth_args()
        if args is not None:
            version = await self.version()
            major = int(version.split(".", 1)[0]) if version[:1].isdigit() else 0
            if major < 2:
                return None  # старая версия без «auth status» — проверим подключением
            code, out, _ = await self._short(launch, args)
            data: Any = None
            with contextlib.suppress(ValueError):
                data = json.loads(out.strip() or "null")
            if isinstance(data, dict):
                self._account = _account_label(data)
                if code in (0, 1):
                    return code == 0
            return None
        home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
        if self.id == "gemini_cli":
            root = Path(os.environ.get("GEMINI_CLI_HOME") or home) / ".gemini"
            keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA")
            if (root / "oauth_creds.json").is_file() or any(os.environ.get(k) for k in keys):
                return True
            return None if (root / "settings.json").is_file() else False
        if self.id == "qwen_code":
            return None if (home / ".qwen" / "settings.json").is_file() else False
        return None

    # --- API настроек: состояние, установка, вход ----------------------------------------------------------
    async def status(self, *, refresh: bool = False) -> dict[str, Any]:
        launch = self.launch(refresh=refresh)
        node = locate.find_node() if self.info.npm else None
        node_version = await self._node_version(node) if node else ""
        node_ok = bool(node_version) and _major(node_version) >= self.info.node_major
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.info.name,
            "installed": launch is not None,
            "path": launch.path if launch else "",
            "version": "",
            "logged_in": None,
            "account": "",
            "installing": self._installing,
            "install_error": self._install_error,
            "install_log": self._install_log if self._installing else "",
            "can_install": bool(self.info.npm) and node_ok and not self._installing,
            "npm": self.info.npm,
            "node": node_version,
            "node_required": self.info.node_major,
            "install_url": self.info.install_url,
            "install_command": self.info.install_command,
            "login_command": self.info.login_command,
            "login_hint": self.info.login_hint,
            "note": self.info.note,
            "images": self.info.images,
            "agent": True,
            "tools": self.ai.mcp_server.available,
            "error": "",
        }
        if launch is None:
            return out
        out["version"] = await self.version()
        if refresh or self._detected is None:
            await self._detect_auth()
        out["logged_in"] = self.logged_in()
        out["account"] = self._account if out["logged_in"] else ""
        record = self._auth_record()
        if record.get("ok") is False and record.get("reason"):
            out["error"] = str(record["reason"])
        return out

    async def _node_version(self, node: str) -> str:
        code, out, _ = await self._short(Launch((node,), node), ["--version"])
        found = re.search(r"v?(\d+\.\d+\.\d+)", out)
        return ("v" + found.group(1)) if found and code == 0 else ""

    async def install(self) -> dict[str, Any]:
        """Поставить программу официальной командой npm в фоне; ход — события ai.cli."""
        if not self.info.npm:
            raise UserFacingError(
                f"{self.info.name} ставится официальным установщиком: откройте PowerShell и выполните "
                f"{self.info.install_command}"
            )
        if self.launch(refresh=True) is not None:
            return {"ok": True, "installed": True}
        if self._installing:
            return {"ok": True, "installing": True}
        node = locate.find_node()
        version = await self._node_version(node) if node else ""
        if not version or _major(version) < self.info.node_major:
            raise UserFacingError(
                f"Для установки нужен Node.js {self.info.node_major} или новее — скачайте его с nodejs.org"
            )
        npm = _npm_launch(node or "")
        if npm is None:
            raise UserFacingError("Не нашёл npm рядом с Node.js — переустановите Node.js с nodejs.org")
        self._installing, self._install_error, self._install_log = True, "", ""
        self._publish("installing")
        self.ai.refresh_configured()
        self.ctx.spawn(self._install(npm), f"cli-install-{self.id}")
        return {"ok": True, "installing": True}

    async def _install(self, npm: Launch) -> None:
        args = ["install", "-g", self.info.npm]
        proc = CliProcess(npm, args, cwd=str(self.work_dir()), env=self._env())
        error = ""
        try:
            await proc.start(None)
            last = 0.0

            async def read() -> None:
                nonlocal last
                while True:
                    raw = await proc.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    self._install_log = errors.short(line, 160)
                    if time.monotonic() - last > 0.5:
                        last = time.monotonic()
                        self._publish("log", line=self._install_log)

            await asyncio.wait_for(read(), INSTALL_TIMEOUT_S)
            code = await proc.wait(30.0)
            if code != 0:
                tail = [x for x in proc.stderr_tail.splitlines() if x.strip()][-2:]
                error = "Не удалось установить: " + (errors.short(" ".join(tail), 200) or f"код {code}")
        except TimeoutError:
            error = "Установка идёт слишком долго — проверьте интернет и попробуйте ещё раз"
        except CliStartError as e:
            error = f"npm не запустился: {e}"
        except Exception as e:
            log.exception("установка %s упала", self.info.name)
            error = f"Не удалось установить: {e}"
        finally:
            await proc.close()
        if not error and self.launch(refresh=True) is None:
            error = f"{self.info.name} установлен, но не находится — перезапустите Jarvis"
        self._installing = False
        self._install_error = error
        if error:
            log.warning("%s не установился: %s", self.info.name, error)
            self._publish("error", error=error)
        else:
            self._version = ("", "")
            version = await self.version()
            log.info("%s установлен (%s)", self.info.name, version)
            self._publish("installed", version=version)
            await self._detect_auth()
        self.ai.refresh_configured()

    async def open_login(self) -> dict[str, Any]:
        """Открыть окно терминала с программой: вход человек выполняет в ней сам."""
        launch = self.launch(refresh=True)
        if launch is None:
            raise UserFacingError(errors.NOT_INSTALLED.format(name=self.info.name))
        folder = self.work_dir()
        if sys.platform == "win32":
            script = folder / f"login-{self.info.binary}.cmd"
            target = launch.path if locate.shell_safe(launch.path) else self.info.login_command
            call = "call " if target.lower().endswith((".cmd", ".bat")) else ""
            hint = re.sub(r'[&|<>^%!"]', "", self.info.terminal_hint or self.info.login_hint)
            lines = ["@echo off", "chcp 65001 >nul", 'cd /d "%~dp0"', f"title {self.info.name}"]
            lines += [f'set "{name}="' for name in PRIVATE_ENV]
            proxy = self.proxy()
            if proxy and locate.shell_safe(proxy):
                lines += [f'set "HTTPS_PROXY={proxy}"', f'set "HTTP_PROXY={proxy}"']
            lines += [f"echo {hint}.", "echo.", f'{call}"{target}"']
            script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        else:
            script = folder / f"login-{self.info.binary}.sh"
            script.write_text(f'#!/bin/sh\ncd "$(dirname "$0")"\nexec "{launch.path}"\n', encoding="utf-8")
            with contextlib.suppress(OSError):
                script.chmod(0o755)
        try:
            if self.terminal is not None:
                result = self.terminal(script)
                if asyncio.iscoroutine(result):
                    await result
            else:
                await self.ctx.run_platform(self.ctx.platform.launch, str(script))
            opened = True
        except Exception as e:
            log.warning("терминал для входа в %s не открылся: %s", self.info.name, e)
            opened = False
        self._remember_auth(None)  # после входа проверим заново
        self._publish("login", opened=opened)
        return {
            "ok": True,
            "opened": opened,
            "command": self.info.login_command,
            "hint": self.info.login_hint,
        }

    async def test(self) -> dict[str, Any]:
        """Проверка связи: крошечный запрос без инструментов. {ok, error?, latency_ms, model}."""
        model = self.resolve_model()
        started = time.perf_counter()
        try:
            await self.complete([{"role": "user", "content": "Ответь одним словом: готов."}])
        except UserFacingError as e:
            latency = int((time.perf_counter() - started) * 1000)
            return {"ok": False, "error": str(e), "latency_ms": latency, "model": model}
        return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000), "model": model}

    # --- запуск программы -----------------------------------------------------------------------------------
    async def _execute(
        self,
        run: CliRun,
        sink: Callable[[CliEvent], None] | None,
        binding: McpBinding | None,
        holder: dict[str, Any] | None = None,
    ) -> CliOutcome:
        launch = self.launch()
        if launch is None:
            raise UserFacingError(errors.NOT_INSTALLED.format(name=self.info.name))
        self.kind.prepare_dir(run.work_dir, tools=bool(run.mcp_url))
        out = CliOutcome()
        proc: CliProcess | None = None
        try:
            args = self.kind.args(run)
            env = self._env(self.kind.env(run))
            proc = CliProcess(launch, args, cwd=str(run.work_dir), env=env)
            if holder is not None:
                holder["proc"] = proc
            try:
                await proc.start(self.kind.stdin(run))
            except CliStartError as e:
                raise UserFacingError(
                    errors.START.format(name=self.info.name, detail=errors.short(e, 200))
                ) from None
            parser = self.kind.parser()
            last = time.monotonic()
            while True:
                try:
                    line = await asyncio.wait_for(proc.readline(), POLL_S)
                except TimeoutError:
                    busy = binding is not None and binding.busy > 0
                    since = max(last, binding.last_activity if binding is not None else 0.0)
                    if not busy and time.monotonic() - since > INACTIVE_S:
                        out.hung = True
                        proc.kill()
                        break
                    continue
                if not line:
                    break
                last = time.monotonic()
                obj = parse_line(line)
                if obj is None:
                    continue
                for event in parser.feed(obj):
                    out.apply(event)
                    if sink is not None:
                        try:
                            sink(event)
                        except Exception:
                            log.exception("событие %s не разобрано", self.info.name)
            out.exit_code = await proc.wait(10.0)
        except asyncio.CancelledError:
            if proc is not None:
                proc.kill()
            raise
        finally:
            if proc is not None:
                await proc.close()
                out.stderr = proc.stderr_tail
            for path in run.files:
                with contextlib.suppress(OSError):
                    path.unlink()
        if holder is not None and holder.get("killed"):
            out.killed = True
        if out.result is None and not out.errors and not out.hung and not out.killed:
            log.warning(
                "%s завершился без итога (код %s): %s", self.info.name, out.exit_code, out.stderr[-500:]
            )
        return out

    def _failure(self, out: CliOutcome, model: str) -> tuple[str, str]:
        """(reason, текст) неудачного запуска."""
        if out.hung:
            return "hung", errors.HUNG.format(name=self.info.short)
        error = out.errors[-1] if out.errors else None
        text = (error.text if error else "") or (out.result.text if out.result else "")
        code = (error.code if error else "") or (
            out.result.code if out.result and out.result.code not in ("success", "") else ""
        )
        if out.rate_limit is not None:
            code = code or "rate_limit"
        if not text and out.retry_code:
            code = code or out.retry_code
        if not text and out.result is None:
            text = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else ""
            if not text and out.exit_code not in (None, 0):
                text = f"программа завершилась с кодом {out.exit_code}"
        resets = (out.rate_limit or {}).get("resetsAt")
        reason, message = errors.classify(
            self.id,
            self.info.short,
            text,
            code=code,
            model=model,
            exit_code=out.exit_code,
            stderr=out.stderr if out.result is None else "",
            resets_at=resets,
        )
        if reason in ("auth", "gemini_consumer", "qwen_oauth", "plan", "org", "hold"):
            self._remember_auth(False, message)
        if reason in ("region", "network"):
            message = self._socks_problem() or message
        return reason, message

    def _record_usage(self, out: CliOutcome, model: str) -> Usage:
        prompt_tokens, completion_tokens = out.usage
        usage = Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost=0.0)
        if prompt_tokens or completion_tokens:
            try:
                self.ctx.db.insert(
                    "usage",
                    {
                        "ts": time.time(),
                        "provider": self.id,
                        "model": out.model or model,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "cost": 0.0,  # по подписке — без оплаты за токены
                    },
                )
            except Exception:
                log.exception("расход токенов не записан")
        return usage

    def _succeeded(self) -> None:
        if self.logged_in() is not True:
            self._remember_auth(True)

    # --- сессии чатов ---------------------------------------------------------------------------------------
    def _session_key(self, chat_id: str) -> str:
        return f"cli:{self.id}:session:{chat_id}"

    def _saved_session(self, turn: "Turn") -> dict[str, Any] | None:
        saved = self.ctx.db.kv_get(self._session_key(turn.chat_id))
        if not isinstance(saved, dict) or not saved.get("id"):
            return None
        if turn.kind == "voice":
            stale = time.time() - float(saved.get("updated") or 0) > VOICE_WINDOW_S
            if stale or int(saved.get("turns") or 0) >= VOICE_MAX_TURNS:
                return None
        return saved

    def forget_chat(self, chat_id: str) -> None:
        """Чат удалён: забыть его сессию — и у Jarvis, и файлы переписки, которые хранит сама программа."""
        saved = self.ctx.db.kv_get(self._session_key(chat_id))
        self.ctx.db.delete("kv", self._session_key(chat_id), key="key")
        session_id = str(saved.get("id") or "") if isinstance(saved, dict) else ""
        if not session_id:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.forget_session(session_id)
            return
        self.ctx.spawn(self.ctx.run_io(self.forget_session, session_id), f"cli-forget-{self.id}")

    def forget_session(self, session_id: str) -> int:
        """Удалить файлы сессии у самой программы (переписка, история правок). Вернёт, сколько удалено."""
        removed = 0
        for path in session_files(self.id, session_id):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed += 1
            except OSError as e:
                log.warning("файл сессии %s не удалён: %s", path.name, e)
        for log_file in session_logs(self.id):
            removed += int(_drop_session_entries(log_file, session_id))
        if removed:
            log.info("%s: забыта сессия удалённого чата (%d)", self.id, removed)
        return removed

    def _history(self, turn: "Turn") -> str:
        try:
            messages = self.ai._history(turn)
        except Exception:
            log.exception("история чата не собралась")
            return ""
        name = self.ctx.settings.value.profile.assistant_name.strip() or "Джарвис"
        return context_prompt.history_text(messages, name)

    # --- ход чата -------------------------------------------------------------------------------------------
    async def _chat_tool(
        self,
        turn: "Turn",
        env: ToolEnv,
        allowed: dict[str, Tool],
        everything: dict[str, Tool],
        name: str,
        args: dict[str, Any],
    ) -> McpResult:
        ai = self.ai
        # текст до вызова идёт выводом программы — пусть он дойдёт раньше карточки
        await asyncio.sleep(TOOL_ORDER_S)
        ai._flush(turn)
        call = ToolCall("call_" + new_id(), name, json.dumps(args, ensure_ascii=False))
        ai.chats.add_message(
            turn.chat_id,
            "assistant",
            "",
            tool_calls=[call.to_message()],
            meta={"turn": turn.user_id, "hidden": True},
        )
        if name in everything and name not in allowed:
            content = CHAT_MODE_DENIED
            ai._save_tool_result(turn, call, content)
        else:
            content = await ai._call_tool(turn, env, allowed, call)
        ai._status(turn, "thinking", "Формирую ответ…")
        return McpResult(content, is_error=content.startswith("Ошибка"))

    async def run_chat(self, turn: "Turn") -> AskResult:
        """Ответ в чате запуском программы: те же события chat.*, что у провайдеров по ключу."""
        ai = self.ai
        problem = self.problem() or self._tools_problem()
        if problem:
            return ai._finish(turn, error=problem)
        ai._status(turn, "thinking", "Думаю…")
        model = self.resolve_model(turn.model or None)
        everything = ai.tools.available("all", source=turn.source)
        allowed = ai.tools.available(turn.mode, source=turn.source)
        env = ToolEnv(self.ctx, ai.web, turn.source, turn.chat_id, user_text=turn.text)
        specs = [
            {"name": t.name, "description": t.description, "inputSchema": t.parameters}
            for t in sorted(everything.values(), key=lambda t: t.name)
        ]

        async def handler(name: str, args: dict[str, Any]) -> McpResult:
            return await self._chat_tool(turn, env, allowed, everything, name, args)

        binding = self.ai.mcp_server.bind(specs, handler, label=f"{self.id}:{turn.chat_id}")
        try:
            saved = self._saved_session(turn)
            out = await self._chat_run(turn, model, binding, saved, allowed)
            if saved is not None and not out.ok and self.kind.resume_failed(self._failure_text(out)):
                log.info("сессия %s не продолжилась — новая с пересказом", saved.get("id"))
                self.forget_chat(turn.chat_id)
                turn.new_round = True
                out = await self._chat_run(turn, model, binding, None, allowed)
        except asyncio.CancelledError:
            _uncancel()
            return ai._finish(turn, stopped=True)
        except UserFacingError as e:
            return ai._finish(turn, error=str(e))
        except Exception as e:
            log.exception("ход %s упал", self.info.name)
            return ai._finish(turn, error=f"Внутренняя ошибка Jarvis AI: {e}")
        finally:
            self.ai.mcp_server.unbind(binding)
        turn.usage.add(self._record_usage(out, model))
        if out.ok:
            self._succeeded()
            return ai._finish(turn)
        return ai._finish(turn, error=self._failure(out, model)[1])

    @staticmethod
    def _failure_text(out: CliOutcome) -> str:
        parts = [e.text for e in out.errors]
        if out.result is not None:
            parts.append(out.result.text)
        parts.append(out.stderr)
        return "\n".join(p for p in parts if p)

    def _save_session(self, chat_id: str, session_id: str, turns: int, sent: dict[str, str]) -> None:
        self.ctx.db.kv_set(
            self._session_key(chat_id),
            {"id": session_id, "updated": time.time(), "turns": turns, "sent": sent},
        )

    async def _chat_run(
        self,
        turn: "Turn",
        model: str,
        binding: McpBinding,
        saved: dict[str, Any] | None,
        allowed: dict[str, Tool],
    ) -> CliOutcome:
        ai = self.ai
        memory = ""
        if self.ctx.settings.value.ai.memory:
            try:
                memory = self.ctx.notes.memory_prompt()
            except Exception:
                log.exception("память не прочиталась")
        sections = context_prompt.context_sections(
            self.ctx.settings.value,
            source=turn.source,
            mode=turn.mode,
            memory=memory,
            has_agent_tool="run_agent" in allowed,
            apps=await apphints.chat_hints(self.ctx, turn.text, allowed),
        )
        sent: dict[str, str] = dict(saved.get("sent") or {}) if saved else {}
        context = context_prompt.context_text(sections, sent)
        history = "" if saved else self._history(turn)
        text = turn.text or "Что на изображении?"
        images = turn.images if self.info.images else []
        if turn.images and not self.info.images:
            text = f"{text}\n\n{prompt.NO_IMAGES}"
        # id новой сессии задаёт Jarvis (Claude Code, Qwen Code); Gemini CLI выдаёт его сам в событии init
        session_id = "" if saved or self.id == "gemini_cli" else str(uuid.uuid4())
        turns = int(saved.get("turns") or 0) + 1 if saved else 1
        if session_id:
            # запомнить сразу: даже если ход оборвут «Стопом», сессию можно продолжить
            self._save_session(turn.chat_id, session_id, turns, sent)
        run = CliRun(
            work_dir=self.work_dir(),
            prompt=prompt.turn_text(context, history, text),
            system=prompt.chat_system(),
            model=model,
            images=images,
            mcp_url=ai.mcp_server.url,
            mcp_headers=ai.mcp_server.headers(binding),
            session_id=session_id,
            resume=str(saved["id"]) if saved else "",
        )
        out = await self._execute(run, CliSink(ai, turn, self.info.short), binding)
        final_id = out.session_id or (str(saved["id"]) if saved else session_id)
        if final_id and (out.ok or out.result is not None or session_id):
            self._save_session(turn.chat_id, final_id, turns, sent)
        return out

    # --- одиночный запрос -----------------------------------------------------------------------------------
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        usage: Usage | None = None,
    ) -> Any:
        """Запуск без инструментов и без сохранения сессии. С json_schema — dict (JSON из ответа)."""
        problem = self.problem()
        if problem:
            raise LlmError(problem)
        system, text, images = _complete_input(messages)
        if json_schema is not None:
            body = json_schema.get("schema") if isinstance(json_schema.get("schema"), dict) else json_schema
            text += "\n\n" + prompt.JSON_ONLY.format(schema=json.dumps(body, ensure_ascii=False))
        if images and not self.info.images:
            text += "\n\n" + prompt.NO_IMAGES
            images = []
        model = self.resolve_model()
        run = CliRun(
            work_dir=self.work_dir(once=True),
            prompt=text,
            system=f"{system}\n\n{prompt.COMPLETE_TAIL}",
            model=model,
            images=images,
            ephemeral=True,
        )
        out = await self._execute(run, None, None)
        spent = self._record_usage(out, model)
        if usage is not None:
            usage.add(spent)
        if not out.ok:
            raise LlmError(self._failure(out, model)[1])
        self._succeeded()
        answer = out.final
        if json_schema is None:
            return answer
        parsed = extract_json(answer)
        if not isinstance(parsed, dict):
            raise LlmFormatError(BAD_FORMAT)
        return parsed

    # --- агент ----------------------------------------------------------------------------------------------
    async def run_agent(
        self, agent: "Agent", run: "AgentRun", *, system: str, first: str, specs: list[dict[str, Any]]
    ) -> str:
        """Работа агента одним запуском программы: инструменты — действия агента; вернёт итог (finish)."""
        from jarvis.ai.agent import AgentLimit

        problem = self.problem() or self._tools_problem()
        if problem:
            raise UserFacingError(problem)
        model = self.resolve_model()
        limit = self.ctx.settings.value.ai.max_agent_steps
        finish: dict[str, Any] = {"summary": None, "limit": False}
        holder: dict[str, Any] = {}
        one_at_a_time = asyncio.Lock()  # окно одно — действия по очереди

        async def handler(name: str, args: dict[str, Any]) -> McpResult:
            async with one_at_a_time:
                return await act(name, args)

        def result(content: str, extras: list[dict[str, Any]]) -> McpResult:
            texts: list[str] = [content]
            images: list[str] = []
            for extra in extras:
                parts = extra.get("content") if isinstance(extra.get("content"), list) else []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url")
                        if isinstance(url, str):
                            images.append(url)
                    elif part.get("type") == "text" and part.get("text"):
                        texts.append(str(part["text"]))
            return McpResult("\n\n".join(texts), is_error=content.startswith("Ошибка"), images=images)

        async def act(name: str, args: dict[str, Any]) -> McpResult:
            call = ToolCall("call_" + new_id(), name, json.dumps(args, ensure_ascii=False))
            if name == "finish":
                check = await agent.check_finish(run, args)
                if check is not None:  # реплика пользователя или проверка по снимку — работа продолжается
                    return result(*check)
                finish["summary"] = agent.finish_summary(run, args)
                return McpResult("ok")
            if finish["summary"] is not None:
                return McpResult("Пропущено: работа уже завершена", is_error=True)
            if run.count >= limit:
                finish["limit"] = True
                proc = holder.get("proc")
                if proc is not None:
                    holder["killed"] = True
                    proc.kill()
                return McpResult(f"Лимит шагов ({limit}) исчерпан", is_error=True)
            return result(*await agent._act(run, call))

        tools = [
            {
                "name": str((spec.get("function") or {}).get("name")),
                "description": str((spec.get("function") or {}).get("description") or ""),
                "inputSchema": (spec.get("function") or {}).get("parameters")
                or {"type": "object", "properties": {}},
            }
            for spec in specs
        ]
        binding = self.ai.mcp_server.bind(tools, handler, label=f"{self.id}:agent:{run.id}")
        try:
            cli_run = CliRun(
                work_dir=self.work_dir(once=True),
                prompt=first,
                system=prompt.agent_system(system),
                model=model,
                mcp_url=self.ai.mcp_server.url,
                mcp_headers=self.ai.mcp_server.headers(binding),
                ephemeral=True,
            )
            out = await self._execute(cli_run, None, binding, holder)
        finally:
            self.ai.mcp_server.unbind(binding)
        run.usage.add(self._record_usage(out, model))
        if finish["limit"]:
            raise AgentLimit(f"Агент не уложился в {limit} шагов")
        if finish["summary"] is not None:
            self._succeeded()
            return str(finish["summary"])
        if out.ok:
            self._succeeded()
            return out.final or "Агент закончил без итога — проверьте результат на экране."
        raise UserFacingError(self._failure(out, model)[1])


class CliProviders:
    """Все провайдеры-программы."""

    def __init__(self, ai: "Ai") -> None:
        self.ai = ai
        self.backends: dict[str, CliBackend] = {pid: CliBackend(ai, kind()) for pid, kind in KINDS.items()}

    def get(self, provider: str) -> CliBackend | None:
        return self.backends.get(provider)

    def selected(self) -> CliBackend | None:
        return self.backends.get(self.ai.ctx.settings.value.ai.provider)

    async def start(self) -> None:
        for backend in self.backends.values():
            await backend.start()

    async def stop(self) -> None:
        for backend in self.backends.values():
            await backend.stop()

    def forget_chat(self, chat_id: str) -> None:
        for backend in self.backends.values():
            with contextlib.suppress(Exception):
                backend.forget_chat(chat_id)


# --- мелочи -------------------------------------------------------------------------------------------------


def _uncancel() -> None:
    task = asyncio.current_task()
    if task is not None:
        with contextlib.suppress(Exception):
            task.uncancel()


def _major(version: str) -> int:
    found = re.search(r"(\d+)", version or "")
    return int(found.group(1)) if found else 0


def _account_label(data: dict[str, Any]) -> str:
    """Подпись входа из JSON «claude auth status» (способ входа, тариф и почта — без токенов; поля сверены
    с Claude Code 2.1.302: loggedIn, authMethod, email, subscriptionType; код выхода 0 — вход выполнен)."""
    method = str(data.get("authMethod") or "")
    labels = {
        "claude.ai": "подписка Claude",
        "oauth_token": "токен CLAUDE_CODE_OAUTH_TOKEN",
        "api_key": "ключ API Anthropic",
        "api_key_helper": "ключ API (apiKeyHelper)",
        "third_party": "облачный провайдер (Bedrock, Vertex…)",
    }
    label = labels.get(method, "")
    plan = {"pro": "Pro", "max": "Max", "team": "Team", "enterprise": "Enterprise"}.get(
        str(data.get("subscriptionType") or "").lower()
    )
    if method == "claude.ai" and plan:
        label = f"подписка Claude {plan}"
    email = next(
        (
            str(data[k])
            for k in ("email", "emailAddress", "account")
            if isinstance(data.get(k), str) and "@" in data[k]
        ),
        "",
    )
    if email and label:
        return f"{email} ({label})"
    return email or label


def _npm_launch(node: str) -> Launch | None:
    """npm без cmd.exe: node.exe + node_modules/npm/bin/npm-cli.js рядом с Node.js; иначе обёртка npm."""
    if node:
        script = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if script.is_file():
            return Launch((node, str(script)), str(script))
    path = locate.find_program("npm")
    if path is None:
        return None
    return locate.resolve_launch(path)


def _complete_input(messages: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    """Сообщения OpenAI → (системный промпт, текст запроса, картинки)."""
    system: list[str] = []
    dialog: list[dict[str, Any]] = []
    images: list[str] = []
    for message in messages:
        if message.get("role") == "system":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                system.append(content.strip())
        elif message.get("role") in ("user", "assistant"):
            dialog.append(message)

    def text_of(content: Any) -> str:
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url")
                    if isinstance(url, str) and url.startswith("data:image/"):
                        images.append(url)
                elif isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return "\n".join(parts)
        return str(content or "")

    if len(dialog) == 1:
        text = text_of(dialog[0].get("content"))
    else:
        lines = []
        for message in dialog[:-1]:
            who = "Пользователь" if message["role"] == "user" else "Ассистент"
            lines.append(f"{who}: {text_of(message.get('content'))}")
        last = text_of(dialog[-1].get("content")) if dialog else ""
        text = ("Предыдущие сообщения:\n" + "\n".join(lines) + "\n\n" + last) if lines else last
    return "\n\n".join(system) or "Отвечай по-русски, кратко и по делу.", text, images


# --- файлы сессий программ (удаление вместе с чатом) -------------------------------------------------------

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,80}$")


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())


def program_dir(provider: str) -> Path | None:
    """Папка, где программа-провайдер хранит свои сессии и журналы."""
    if provider == "claude_code":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or _home() / ".claude")
    if provider == "gemini_cli":
        return Path(os.environ.get("GEMINI_CLI_HOME") or _home()) / ".gemini"
    if provider == "qwen_code":
        return _home() / ".qwen"
    return None


def _mentions(path: Path, text: str) -> bool:
    try:
        with open(path, "rb") as f:
            return text.encode() in f.read(65536)
    except OSError:
        return False


def session_files(provider: str, session_id: str) -> list[Path]:
    """Файлы и папки сессии у программы: с её id в имени (Claude Code) или внутри (Gemini CLI, Qwen Code)."""
    root = program_dir(provider)
    if root is None or not root.is_dir() or not _SESSION_ID.match(session_id):
        return []
    sid = session_id
    if provider == "claude_code":
        patterns = (
            f"projects/*/{sid}.jsonl",
            f"projects/*/{sid}",
            f"todos/{sid}*.json",
            f"file-history/{sid}",
            f"session-env/{sid}",
            f"debug/{sid}.txt",
        )
        return [p for pattern in patterns for p in root.glob(pattern)]
    # Gemini CLI и Qwen Code: chats/session-<время>-<начало id>.json(l) — полный id ищем внутри файла
    found: list[Path] = []
    for pattern in ("tmp/*/chats/*", "projects/*/chats/*"):
        for path in root.glob(pattern):
            if not path.is_file() or sid[:8] not in path.name:
                continue
            if sid in path.name or _mentions(path, sid):
                found.append(path)
    return found


def session_logs(provider: str) -> list[Path]:
    """Журналы запросов Gemini CLI и Qwen Code (tmp/<проект>/logs.json): там сообщения всех сессий проекта."""
    root = program_dir(provider)
    if provider not in ("gemini_cli", "qwen_code") or root is None or not root.is_dir():
        return []
    return [p for p in root.glob("tmp/*/logs.json") if p.is_file()]


def _drop_session_entries(path: Path, session_id: str) -> bool:
    """Убрать из журнала программы записи одной сессии. True — что-то убрано."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, list):
        return False
    kept = [e for e in data if not (isinstance(e, dict) and e.get("sessionId") == session_id)]
    if len(kept) == len(data):
        return False
    try:
        path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True
