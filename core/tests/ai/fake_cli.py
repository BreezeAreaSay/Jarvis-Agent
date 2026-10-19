"""Фальшивая программа-провайдер для тестов: притворяется Claude Code, Gemini CLI или Qwen Code.

Пишет в stdout строки stream-json в формате нужной программы, а инструменты Jarvis вызывает по-настоящему —
клиентом MCP (SDK mcp) по настройкам, которые ей передал Jarvis: файл --mcp-config (Claude Code, Qwen Code)
или .gemini/settings.json рабочей папки с подстановкой переменных окружения (Gemini CLI).

Окружение: FAKE_CLI_KIND — claude | gemini | qwen; FAKE_CLI_SCENARIO — JSON-файл с очередью сценариев
(каждый запуск берёт первый; запуск с просьбой ответить одним JSON — первый сценарий-ответ JSON: план агента
составляется параллельно с его работой); FAKE_CLI_LOG — JSON-строки о запусках (argv, stdin, окружение,
настройки). Очередь и журнал правятся под блокировкой файла: запуски идут одновременно.

Шаги сценария: {"say": текст}, {"tool": имя, "args": {...}}, {"error": {"code", "text"}},
{"result_error": текст}, {"rate_limit": {"resetsAt": …}}, {"retry": категория}, {"sleep": секунды},
{"exit": код, "stderr": текст}, {"resume_fail": true}, {"session": id} (Gemini: id сессии в init).
"""

import asyncio
import contextlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

KIND = os.environ.get("FAKE_CLI_KIND", "claude")
NAME_PREFIX = {"claude": "mcp__jarvis__", "qwen": "mcp__jarvis__", "gemini": "mcp_jarvis_"}[KIND]


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def arg(argv: list[str], name: str) -> str | None:
    if name in argv:
        index = argv.index(name)
        return argv[index + 1] if index + 1 < len(argv) else ""
    return None


@contextlib.contextmanager
def locked(path: str) -> Any:
    """Блокировка файла на время «прочитать — поправить — записать» (запуски идут одновременно)."""
    try:
        import fcntl
    except ImportError:  # Windows: тесты здесь идут по одному
        yield
        return
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _json_answer(scenario: dict[str, Any]) -> bool:
    steps = scenario.get("steps") or []
    say = steps[0].get("say") if len(steps) == 1 and isinstance(steps[0], dict) else None
    return isinstance(say, str) and say.lstrip().startswith("{")


def next_scenario(stdin: str = "") -> dict[str, Any]:
    path = os.environ.get("FAKE_CLI_SCENARIO")
    if not path or not Path(path).exists():
        return {"steps": [{"say": "Готов."}]}
    with locked(path):
        queue = json.loads(Path(path).read_text(encoding="utf-8"))
        if not queue:
            return {"steps": [{"say": "Готов."}]}
        wants_json = "одним JSON" in stdin
        index = next((i for i, item in enumerate(queue) if _json_answer(item) == wants_json), 0)
        current = queue.pop(index)
        Path(path).write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
    return current


def mcp_settings(argv: list[str]) -> tuple[str, dict[str, str]] | None:
    """Адрес и заголовки сервера jarvis так, как их прочитала бы программа."""
    if KIND in ("claude", "qwen"):
        path = arg(argv, "--mcp-config")
        if not path:
            return None
        server = json.loads(Path(path).read_text(encoding="utf-8"))["mcpServers"]["jarvis"]
        url = server.get("url") or server.get("httpUrl")
        return str(url), dict(server.get("headers") or {})
    settings_path = Path.cwd() / ".gemini" / "settings.json"
    if not settings_path.exists():
        return None
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    server = (settings.get("mcpServers") or {}).get("jarvis")
    allowed = arg(argv, "--allowed-mcp-server-names")
    if not server or allowed != "jarvis":
        return None

    def expand(value: str) -> str:
        return re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: os.environ.get(m.group(1) or m.group(2), ""), value)

    return expand(server["httpUrl"]), {k: expand(v) for k, v in (server.get("headers") or {}).items()}


async def call_tool(
    settings: tuple[str, dict[str, str]], name: str, args: dict[str, Any]
) -> tuple[str, bool, int]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url, headers = settings
    async with (
        httpx.AsyncClient(headers=headers, trust_env=False, timeout=60) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        if name not in names:
            return f"нет инструмента {name} среди {names}", True, 0
        result = await session.call_tool(name, args)
        texts = [b.text for b in result.content if getattr(b, "type", "") == "text"]
        images = sum(1 for b in result.content if getattr(b, "type", "") == "image")
        return "\n".join(texts), bool(result.isError), images


def say(text: str, message_id: str) -> None:
    if KIND == "gemini":
        for i in range(0, len(text), 4):
            emit({"type": "message", "role": "assistant", "content": text[i : i + 4], "delta": True})
        return
    emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": message_id}}})
    for i in range(0, len(text), 4):
        delta = {"type": "text_delta", "text": text[i : i + 4]}
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": delta}})
    emit(
        {
            "type": "assistant",
            "message": {"id": message_id, "content": [{"type": "text", "text": text}]},
            "parent_tool_use_id": None,
        }
    )


def short_run(argv: list[str]) -> int | None:
    """Короткие запуски (--version, auth status) сценарии не тратят и в журнал не пишутся."""
    if argv == ["--version"]:
        print({"claude": "2.1.98 (Claude Code)", "gemini": "0.66.0", "qwen": "0.25.1"}[KIND])
        return 0
    if argv[:2] == ["auth", "status"]:
        if os.environ.get("FAKE_CLI_AUTH", "in") == "in":
            status = {"loggedIn": True, "authMethod": "claude.ai", "email": "tony@stark.example"}
            print(
                json.dumps({**status, "subscriptionType": "max"}, indent=2)
            )  # как у Claude Code: JSON с отступами
            return 0
        print(json.dumps({"loggedIn": False, "authMethod": "none"}))
        return 1
    return None


async def main() -> int:
    argv = sys.argv[1:]
    short = short_run(argv)
    if short is not None:
        return short
    stdin = sys.stdin.read()
    scenario = next_scenario(stdin + " ".join(argv))
    log_path = os.environ.get("FAKE_CLI_LOG")
    record: dict[str, Any] = {
        "kind": KIND,
        "argv": argv,
        "cwd": str(Path.cwd()),
        "stdin": stdin,
        "env": {
            k: os.environ.get(k)
            for k in (
                "ANTHROPIC_API_KEY",
                "NO_PROXY",
                "HTTPS_PROXY",
                "GEMINI_SYSTEM_MD",
                "GEMINI_CLI_TRUST_WORKSPACE",
                "QWEN_SYSTEM_MD",
                "MCP_TOOL_TIMEOUT",
                "ENABLE_CLAUDEAI_MCP_SERVERS",
                "JARVIS_MCP_TURN",
                "JARVIS_CORE_TOKEN",
                "PYTHONPATH",
            )
        },
        "tools": [],
    }
    system_file = (
        arg(argv, "--system-prompt-file")
        or os.environ.get("GEMINI_SYSTEM_MD")
        or os.environ.get("QWEN_SYSTEM_MD")
    )
    if system_file and Path(system_file).exists():
        record["system"] = Path(system_file).read_text(encoding="utf-8")
    config = arg(argv, "--mcp-config")
    if config and Path(config).exists():
        record["mcp_config"] = json.loads(Path(config).read_text(encoding="utf-8"))
    gemini_settings = Path.cwd() / ".gemini" / "settings.json"
    if KIND == "gemini" and gemini_settings.exists():
        record["gemini_settings"] = json.loads(gemini_settings.read_text(encoding="utf-8"))

    record["run"] = uuid.uuid4().hex

    def save() -> None:
        """Записать (или обновить) строку этого запуска: программу могут убить посреди сценария."""
        if not log_path:
            return
        path = Path(log_path)
        with locked(log_path):
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines = [line for line in lines if line.strip() and json.loads(line).get("run") != record["run"]]
            lines.append(json.dumps(record, ensure_ascii=False))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    save()

    session = arg(argv, "--resume") or arg(argv, "--session-id") or str(uuid.uuid4())
    steps = scenario.get("steps") or []
    if any(s.get("resume_fail") for s in steps) and arg(argv, "--resume"):
        save()
        text = f"No conversation found with session ID: {session}"
        if KIND == "gemini":
            sys.stderr.write(f'Invalid session identifier "{session}".\n')
            return 1
        emit(
            {"type": "result", "subtype": "success", "is_error": True, "result": text, "session_id": session}
        )
        return 1
    for step in steps:
        if "session" in step:
            session = step["session"]
    if KIND == "gemini":
        emit({"type": "init", "session_id": session, "model": arg(argv, "--model") or "auto"})
    else:
        emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session,
                "model": arg(argv, "--model") or "default",
                "mcp_servers": [{"name": "jarvis", "status": "connected"}] if config else [],
            }
        )
    settings = mcp_settings(argv)
    failed = False
    for n, step in enumerate(steps):
        message_id = f"msg_{n}"
        if "say" in step:
            say(step["say"], message_id)
        elif "tool" in step:
            name = NAME_PREFIX + step["tool"]
            tool_id = f"toolu_{n}"
            if KIND == "gemini":
                emit(
                    {
                        "type": "tool_use",
                        "tool_name": name,
                        "tool_id": tool_id,
                        "parameters": step.get("args"),
                    }
                )
            else:
                emit(
                    {
                        "type": "stream_event",
                        "event": {"type": "message_start", "message": {"id": message_id}},
                    }
                )
                block = {"type": "tool_use", "id": tool_id, "name": name, "input": step.get("args") or {}}
                emit(
                    {
                        "type": "assistant",
                        "message": {"id": message_id, "content": [block]},
                        "parent_tool_use_id": None,
                    }
                )
            if settings is None:
                text, is_error, images = "нет сервера MCP", True, 0
            else:
                text, is_error, images = await call_tool(settings, step["tool"], step.get("args") or {})
            record["tools"].append({"name": step["tool"], "text": text, "error": is_error, "images": images})
            save()
            if KIND == "gemini":
                emit(
                    {
                        "type": "tool_result",
                        "tool_id": tool_id,
                        "status": "error" if is_error else "success",
                        "output": text,
                    }
                )
            else:
                block = {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}
                emit(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": [block]},
                        "parent_tool_use_id": None,
                    }
                )
        elif "retry" in step:
            if KIND != "gemini":
                emit(
                    {
                        "type": "system",
                        "subtype": "api_retry",
                        "attempt": 1,
                        "error": step["retry"],
                        "error_status": 529,
                    }
                )
        elif "rate_limit" in step:
            emit(
                {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", **step["rate_limit"]}}
            )
        elif "error" in step:
            failed = True
            error = step["error"]
            if KIND == "gemini":
                save()
                emit(
                    {
                        "type": "result",
                        "status": "error",
                        "error": {"type": "Error", "message": error["text"]},
                    }
                )
                return int(error.get("exit", 1))
            emit(
                {
                    "type": "assistant",
                    "message": {"id": message_id, "content": [{"type": "text", "text": error["text"]}]},
                    "parent_tool_use_id": None,
                    "error": error.get("code", "unknown"),
                }
            )
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": error["text"],
                    "session_id": session,
                }
            )
        elif "result_error" in step:
            failed = True
            if KIND == "gemini":
                emit(
                    {
                        "type": "result",
                        "status": "error",
                        "error": {"type": "ApiError", "message": step["result_error"]},
                    }
                )
            else:
                emit(
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "errors": [step["result_error"]],
                        "session_id": session,
                    }
                )
        elif "sleep" in step:
            save()
            time.sleep(float(step["sleep"]))
        elif "exit" in step:
            save()
            if step.get("stderr"):
                sys.stderr.write(step["stderr"] + "\n")
                sys.stderr.flush()
            return int(step["exit"])
    save()
    if not failed:
        if KIND == "gemini":
            stats = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
            emit({"type": "result", "status": "success", "stats": stats})
        else:
            usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5}
            final = next((s["say"] for s in reversed(steps) if "say" in s), "")
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": final,
                    "session_id": session,
                    "usage": usage,
                }
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
