"""Как запускать каждую программу-провайдера: флаги, переменные окружения, файлы настроек, разбор вывода.

Сверено в октябре 2026: Claude Code — code.claude.com/docs (cli-reference, headless, mcp, permissions,
env-vars); Gemini CLI — исходники google-gemini/gemini-cli 0.66 (docs/cli, policy/config.ts, config.ts);
Qwen Code — исходники QwenLM/qwen-code 0.25 (docs/users/features/headless.md, top-level-options.ts).

Общие правила: программа работает в пустой рабочей папке Jarvis, её встроенные инструменты (оболочка, правка
файлов, веб) выключены, а инструменты Jarvis приходят с сервера MCP ядра. Текст человека — только через stdin,
системный промпт — файлом: в аргументах командной строки ничего человеческого нет.
"""

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.ai.cli.stream import ClaudeStream, GeminiStream
from jarvis.mcp.server import SERVER_NAME, TOKEN_HEADER, TURN_HEADER

MCP_TIMEOUT_MS = 600_000  # вызов инструмента ждёт и подтверждения человека (до 2 минут) — с запасом
NO_SERVER = "jarvis-none"  # имя несуществующего сервера: «никаких серверов MCP» (одиночные запросы)


@dataclass(frozen=True)
class CliInfo:
    id: str  # провайдер Jarvis AI
    name: str  # «Claude Code»
    short: str  # «Claude» — «Нет связи с Claude»
    binary: str  # имя программы
    npm: str  # пакет npm для кнопки «Установить» ("" — ставит сам человек)
    node_major: int  # нужная версия Node.js для npm
    install_url: str
    install_command: str  # официальная команда установки (подсказка)
    login_command: str  # что запустить в терминале для входа
    login_hint: str
    note: str = ""  # важное о входе и тарифах
    images: bool = False  # видит картинки из сообщения
    terminal_hint: str = ""  # что сделать в открытом окне терминала (без символов & | < > ^ %)
    socks: bool = True  # понимает SOCKS-прокси в HTTPS_PROXY


@dataclass
class CliRun:
    """Один запуск программы: ход чата, одиночный запрос или работа агента."""

    work_dir: Path
    prompt: str
    system: str
    model: str = ""
    images: list[str] = field(default_factory=list)  # data URL (только Claude Code)
    mcp_url: str = ""  # адрес сервера MCP Jarvis ("" — без инструментов)
    mcp_headers: dict[str, str] = field(default_factory=dict)
    session_id: str = ""  # id новой сессии (Claude Code, Qwen Code)
    resume: str = ""  # продолжить сессию
    ephemeral: bool = False  # не сохранять сессию (одиночный запрос, агент)
    files: list[Path] = field(default_factory=list)  # временные файлы запуска — удаляются после
    tag: str = field(default_factory=lambda: uuid.uuid4().hex[:12])  # для имён временных файлов

    def temp(self, name: str, text: str) -> Path:
        path = self.work_dir / name
        path.write_text(text, encoding="utf-8")
        self.files.append(path)
        return path


class CliKind:
    """Особенности одной программы."""

    info: CliInfo

    def args(self, run: CliRun) -> list[str]:
        raise NotImplementedError

    def env(self, run: CliRun) -> dict[str, str]:
        """Переменные окружения запуска (поверх окружения ядра)."""
        return {"NO_COLOR": "1"}

    def strip_env(self) -> tuple[str, ...]:
        """Переменные, которые программа получать не должна."""
        return ()

    def stdin(self, run: CliRun) -> bytes:
        return run.prompt.encode("utf-8")

    def parser(self) -> Any:
        return ClaudeStream()

    def prepare_dir(self, work_dir: Path, *, tools: bool) -> None:
        """Подготовить рабочую папку (файлы настроек программы)."""
        work_dir.mkdir(parents=True, exist_ok=True)

    def auth_args(self) -> list[str] | None:
        """Команда проверки входа программы (без чтения секретов) или None."""
        return None

    def resume_failed(self, text: str) -> bool:
        low = text.casefold()
        return "session" in low and any(
            m in low for m in ("not found", "no conversation", "invalid session", "no previous", "no saved")
        )


# --- Claude Code ------------------------------------------------------------------------------------------


class ClaudeCode(CliKind):
    """claude -p в режиме stream-json; вход — в самом Claude Code (/login), Jarvis его не касается."""

    info = CliInfo(
        id="claude_code",
        name="Claude Code",
        short="Claude",
        binary="claude",
        npm="",  # ставить и скачивать Claude Code из Jarvis нельзя — только официальным установщиком
        node_major=0,
        install_url="https://code.claude.com/docs/en/setup",
        install_command="irm https://claude.ai/install.ps1 | iex",
        login_command="claude",
        login_hint="Войдите в Claude Code: откройте терминал, запустите claude и выполните /login",
        terminal_hint=(
            "Войдите в аккаунт Claude (если вход не предложен сам, выполните /login), "
            "затем закройте это окно и вернитесь в Jarvis"
        ),
        note=(
            "Нужна подписка Claude Pro или Max. Вход выполняется в самом Claude Code — Jarvis не видит "
            "ни пароль, ни токены. Другой вариант — провайдер «Anthropic (Claude)» по ключу API."
        ),
        images=True,
        socks=False,  # code.claude.com/docs/en/network-config: «Claude Code does not support SOCKS proxies»
    )

    def args(self, run: CliRun) -> list[str]:
        system = run.temp(f"system-{run.tag}.md", run.system)
        args = [
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--input-format",
            "stream-json",
            "--system-prompt-file",
            str(system),
            # встроенные инструменты (Bash, правка файлов, WebFetch…) выключены; остальное — запрещено
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--disable-slash-commands",
            # без настроек пользователя: его хуки, плагины и разрешения в ходах Jarvis не работают
            "--setting-sources",
            "",
            "--strict-mcp-config",
        ]
        if run.mcp_url:
            config = {
                "mcpServers": {
                    SERVER_NAME: {
                        "type": "http",
                        "url": run.mcp_url,
                        "headers": run.mcp_headers,
                        "timeout": MCP_TIMEOUT_MS,
                    }
                }
            }
            path = run.temp(f"mcp-{run.tag}.json", json.dumps(config, ensure_ascii=False))
            args += ["--mcp-config", str(path), "--allowedTools", f"mcp__{SERVER_NAME}__*"]
        if run.model:
            args += ["--model", run.model]
        if run.resume:
            args += ["--resume", run.resume]
        elif run.session_id:
            args += ["--session-id", run.session_id]
        if run.ephemeral:
            args.append("--no-session-persistence")
        return args

    def env(self, run: CliRun) -> dict[str, str]:
        return {
            "NO_COLOR": "1",
            "MCP_TOOL_TIMEOUT": str(MCP_TIMEOUT_MS),
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",  # коннекторы claude.ai в ходах Jarvis не нужны
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",  # CLAUDE.md программиста — не для голосового ассистента
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        }

    def strip_env(self) -> tuple[str, ...]:
        # с ANTHROPIC_API_KEY в окружении claude -p платит ключом, а не подпиской (см. authentication)
        return ("ANTHROPIC_API_KEY",)

    def stdin(self, run: CliRun) -> bytes:
        content: list[dict[str, Any]] = [{"type": "text", "text": run.prompt}]
        for url in run.images:
            head, _, data = url.partition(",")
            media = head[5:].split(";", 1)[0] if head.startswith("data:") else "image/png"
            content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
        message = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": run.resume or run.session_id or "",
        }
        return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")

    def auth_args(self) -> list[str] | None:
        return ["auth", "status"]  # JSON и код выхода 0/1 (claude auth status, документация cli-reference)

    def resume_failed(self, text: str) -> bool:
        return "no conversation found" in text.casefold() or super().resume_failed(text)


# --- Gemini CLI -------------------------------------------------------------------------------------------

GEMINI_DENY = (
    "run_shell_command",
    "write_file",
    "replace",
    "edit",
    "web_fetch",
    "google_web_search",
    "save_memory",
    "read_file",
    "read_many_files",
    "list_directory",
    "glob",
    "grep_search",
    "search_file_content",
    "write_todos",
    "activate_skill",
)


class GeminiCli(CliKind):
    """gemini в режиме stream-json; настройки — .gemini/settings.json рабочей папки (адрес и токены сервера
    MCP подставляются из переменных окружения запуска, поэтому файл общий для всех ходов и без секретов)."""

    info = CliInfo(
        id="gemini_cli",
        name="Gemini CLI",
        short="Gemini",
        binary="gemini",
        npm="@google/gemini-cli",
        node_major=20,
        install_url="https://github.com/google-gemini/gemini-cli",
        install_command="npm install -g @google/gemini-cli",
        login_command="gemini",
        login_hint="Войдите в Gemini CLI: откройте терминал, запустите gemini и выберите способ входа",
        terminal_hint="Выберите способ входа в Gemini CLI, затем закройте это окно и вернитесь в Jarvis",
        note=(
            "С 18 июня 2026 Google не пускает в Gemini CLI личные аккаунты Google (в том числе "
            "Google AI Pro). Работают рабочие аккаунты с Gemini Code Assist Standard или Enterprise "
            "и ключ API в самом Gemini CLI. Проще всего выбрать «Google Gemini» по ключу — бесплатный ключ "
            "выдаёт Google AI Studio."
        ),
    )

    def settings(self, *, tools: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            # встроенные инструменты: список разрешённых, где нет ни одного встроенного (только наш сервер)
            "tools": {"core": [f"mcp_{SERVER_NAME}_*"], "exclude": list(GEMINI_DENY)},
            "mcp": {"allowed": [SERVER_NAME] if tools else [NO_SERVER]},
            "hooksConfig": {"enabled": False},
            "skills": {"enabled": False},
            "useWriteTodos": False,
            # GEMINI.md программиста не подмешивать: файла с таким именем нет
            "context": {"fileName": ["JARVIS-NO-CONTEXT.md"]},
            "general": {"checkpointing": {"enabled": False}},
        }
        if tools:
            out["mcpServers"] = {
                SERVER_NAME: {
                    "httpUrl": "${JARVIS_MCP_URL}",
                    "headers": {TOKEN_HEADER: "${JARVIS_MCP_AUTH}", TURN_HEADER: "${JARVIS_MCP_TURN}"},
                    "trust": True,  # подтверждения — у Jarvis (карточки «Разрешить»), а не в программе
                    "timeout": MCP_TIMEOUT_MS,
                    "description": "Инструменты Jarvis",
                }
            }
        return out

    def prepare_dir(self, work_dir: Path, *, tools: bool) -> None:
        folder = work_dir / ".gemini"
        folder.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.settings(tools=tools), ensure_ascii=False, indent=2)
        path = folder / "settings.json"
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
        path.write_text(text, encoding="utf-8")

    def args(self, run: CliRun) -> list[str]:
        args = [
            "--output-format",
            "stream-json",
            "--approval-mode",
            "default",  # не yolo, даже если так настроено у человека
            "--extensions",
            "none",
            "--allowed-mcp-server-names",
            SERVER_NAME if run.mcp_url else NO_SERVER,
        ]
        if run.model:
            args += ["--model", run.model]
        if run.resume:
            args += ["--resume", run.resume]
        return args

    def env(self, run: CliRun) -> dict[str, str]:
        system = run.temp(f"system-{run.tag}.md", run.system)
        env = {
            "NO_COLOR": "1",
            "GEMINI_SYSTEM_MD": str(system),  # системный промпт Jarvis вместо промпта программиста
            "GEMINI_CLI_TRUST_WORKSPACE": "true",  # рабочая папка Jarvis: иначе настройки папки не читаются
        }
        if run.mcp_url:
            env.update(
                JARVIS_MCP_URL=run.mcp_url,
                JARVIS_MCP_AUTH=run.mcp_headers.get(TOKEN_HEADER, ""),
                JARVIS_MCP_TURN=run.mcp_headers.get(TURN_HEADER, ""),
            )
        return env

    def parser(self) -> Any:
        return GeminiStream()


# --- Qwen Code --------------------------------------------------------------------------------------------

QWEN_DENY = (
    "run_shell_command",
    "exec",
    "edit",
    "write_file",
    "notebook_edit",
    "web_fetch",
    "web_search",
    "agent",
    "skill",
    "save_memory",
    "manage_memory",
    "todo_write",
    "cron_create",
    "monitor",
    "create_sub_session",
    "image_gen",
    "workflow",
    "lsp",
    "read_file",
    "list_directory",
    "glob",
    "grep_search",
)


class QwenCode(CliKind):
    """qwen в режиме stream-json (формат как у Claude Code); вход — /auth в самом Qwen Code."""

    info = CliInfo(
        id="qwen_code",
        name="Qwen Code",
        short="Qwen",
        binary="qwen",
        npm="@qwen-code/qwen-code@latest",
        node_major=22,
        install_url="https://github.com/QwenLM/qwen-code",
        install_command="npm install -g @qwen-code/qwen-code@latest",
        login_command="qwen",
        login_hint="Настройте вход в Qwen Code: откройте терминал, запустите qwen и выполните /auth",
        terminal_hint=(
            "Выполните /auth и выберите тариф Coding Plan или ключ API, "
            "затем закройте это окно и вернитесь в Jarvis"
        ),
        note=(
            "Бесплатный вход Qwen OAuth закрыт с 15 апреля 2026. В Qwen Code нужен тариф Alibaba Cloud "
            "Coding Plan или Token Plan либо ключ API — их подключают командой /auth в самой программе."
        ),
    )

    def args(self, run: CliRun) -> list[str]:
        args = [
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--approval-mode",
            "default",
            # список разрешённых встроенных инструментов из одного безвредного — остальные не регистрируются
            "--core-tools",
            "structured_output",
            "--exclude-tools",
            ",".join(QWEN_DENY),
        ]
        if run.mcp_url:
            config = {
                "mcpServers": {
                    SERVER_NAME: {
                        "httpUrl": run.mcp_url,
                        "headers": run.mcp_headers,
                        "trust": True,
                        "timeout": MCP_TIMEOUT_MS,
                    }
                }
            }
            path = run.temp(f"mcp-{run.tag}.json", json.dumps(config, ensure_ascii=False))
            args += [
                "--mcp-config",
                str(path),
                "--allowed-mcp-server-names",
                SERVER_NAME,
                "--allowed-tools",
                f"mcp__{SERVER_NAME}__*",
            ]
        else:
            args += ["--allowed-mcp-server-names", NO_SERVER]
        if run.model:
            args += ["--model", run.model]
        if run.resume:
            args += ["--resume", run.resume]
        elif run.session_id:
            args += ["--session-id", run.session_id]
        if run.ephemeral:
            args.append("--no-chat-recording")
        return args

    def env(self, run: CliRun) -> dict[str, str]:
        system = run.temp(f"system-{run.tag}.md", run.system)
        return {"NO_COLOR": "1", "QWEN_SYSTEM_MD": str(system)}


KINDS: dict[str, type[CliKind]] = {"claude_code": ClaudeCode, "gemini_cli": GeminiCli, "qwen_code": QwenCode}
