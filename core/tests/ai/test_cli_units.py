"""Программы-провайдеры по частям: поиск программ (в том числе раскладка Windows), разбор обёрток npm,
разбор stream-json Claude Code / Qwen Code / Gemini CLI, ошибки по-русски, флаги запуска."""

import json
import os
import stat
import time
from pathlib import Path

import pytest

from jarvis.ai.cli import errors, locate, prompt
from jarvis.ai.cli.specs import KINDS, CliRun
from jarvis.ai.cli.stream import ClaudeStream, GeminiStream, parse_line

WIN = "win32"
PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.JS"

# обёртки ровно в том виде, в каком их пишут npm (cmd-shim), pnpm и установщик Qwen Code
NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

"""
NPM_SHIM += r'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
NPM_SHIM += r'"%dp0%\node_modules\@google\gemini-cli\dist\index.js" %*' + "\n"
NPM_EXE_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
"%dp0%\node_modules\@anthropic-ai\claude-code\bin\claude.exe"   %*
"""
PNPM_SHIM = r"""@SETLOCAL
@IF NOT DEFINED NODE_PATH (
  @SET "NODE_PATH=%~dp0\global\5\node_modules"
) ELSE (
  @SET "NODE_PATH=%NODE_PATH%;%~dp0\global\5\node_modules"
)
@IF EXIST "%~dp0\node.exe" (
  "%~dp0\node.exe"  "%~dp0\global\5\node_modules\@qwen-code\qwen-code\cli.js" %*
) ELSE (
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\global\5\node_modules\@qwen-code\qwen-code\cli.js" %*
)
"""


def touch(path: Path, text: str = "", executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def win_env(home: Path, **extra: str) -> dict[str, str]:
    """Окружение Windows: регистр имён как у настоящего (Path, ProgramFiles), PATH пустой — «устаревший»."""
    return {
        "Path": "",
        "PATHEXT": PATHEXT,
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "ProgramFiles": str(home / "Program Files"),
        "ComSpec": "C:\\Windows\\system32\\cmd.exe",
        **extra,
    }


# --- поиск программ ----------------------------------------------------------------------------------------


def test_finds_npm_global_shim_and_runs_node_with_the_script_directly(tmp_path: Path) -> None:
    env = win_env(tmp_path)
    npm = tmp_path / "AppData" / "Roaming" / "npm"
    shim = touch(npm / "gemini.cmd", NPM_SHIM)
    touch(npm / "gemini")  # sh-обёртка рядом не мешает: на Windows ищем по PATHEXT
    script = touch(npm / "node_modules" / "@google" / "gemini-cli" / "dist" / "index.js")
    node = touch(tmp_path / "Program Files" / "nodejs" / "node.exe")
    found = locate.find_program("gemini", env=env, platform=WIN, extra=[])
    assert found == str(shim)
    launch = locate.resolve_launch(found, env=env, platform=WIN)
    # не через cmd.exe: node.exe + скрипт, аргументы уходят программе как есть
    assert launch == locate.Launch((str(node), str(script)), str(shim), shell=False)
    # node.exe рядом с обёрткой важнее найденного в системе
    local_node = touch(npm / "node.exe")
    assert locate.resolve_launch(found, env=env, platform=WIN).argv == (str(local_node), str(script))


def test_npm_shim_for_a_native_binary_runs_the_exe(tmp_path: Path) -> None:
    env = win_env(tmp_path)
    npm = tmp_path / "AppData" / "Roaming" / "npm"
    shim = touch(npm / "claude.cmd", NPM_EXE_SHIM)
    exe = touch(npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    launch = locate.resolve_launch(str(shim), env=env, platform=WIN)
    assert launch.argv == (str(exe),) and launch.path == str(shim) and not launch.shell


def test_installer_locations_are_searched_when_path_is_stale(tmp_path: Path) -> None:
    env = win_env(tmp_path)
    # Claude Code (официальный установщик) — %USERPROFILE%\.local\bin\claude.exe
    claude = touch(tmp_path / ".local" / "bin" / "claude.exe")
    assert locate.find_program("claude", env=env, platform=WIN, extra=[]) == str(claude)
    # .exe раньше .cmd в той же папке (порядок PATHEXT)
    touch(tmp_path / ".local" / "bin" / "claude.cmd", NPM_EXE_SHIM)
    assert locate.find_program("claude", env=env, platform=WIN, extra=[]) == str(claude)
    # Qwen Code (установщик) — %LOCALAPPDATA%\qwen-code\bin\qwen.cmd → вложенная обёртка pnpm → node + cli.js
    inner_dir = tmp_path / "AppData" / "Local" / "qwen-code" / "current"
    inner = touch(inner_dir / "qwen.cmd", PNPM_SHIM)
    script = touch(inner_dir / "global" / "5" / "node_modules" / "@qwen-code" / "qwen-code" / "cli.js")
    outer = touch(
        tmp_path / "AppData" / "Local" / "qwen-code" / "bin" / "qwen.cmd", f'@echo off\ncall "{inner}" %*\n'
    )
    node = touch(tmp_path / "Program Files" / "nodejs" / "node.exe")
    assert locate.find_program("qwen", env=env, platform=WIN, extra=[]) == str(outer)
    launch = locate.resolve_launch(str(outer), env=env, platform=WIN)
    assert launch.argv == (str(node), str(script)) and launch.path == str(outer) and not launch.shell
    # PATH (в любом регистре имени) и папки из реестра идут раньше обычных мест
    first = touch(tmp_path / "custom" / "qwen.exe")
    env["Path"] = f"{tmp_path / 'custom'};"
    assert locate.find_program("qwen", env=env, platform=WIN, extra=[]) == str(first)
    other = touch(tmp_path / "registry" / "claude.exe")
    assert locate.find_program("claude", env=env, platform=WIN, extra=[str(other.parent)]) == str(other)
    assert locate.find_program("codex", env=env, platform=WIN, extra=[]) is None


def test_unknown_shim_falls_back_to_cmd_with_safe_arguments_only(tmp_path: Path) -> None:
    env = win_env(tmp_path)
    shim = touch(tmp_path / "bin" / "gemini.cmd", "@echo off\nnode %~dp0\\gemini.js %*\n")
    launch = locate.resolve_launch(str(shim), env=env, platform=WIN)
    assert launch.shell is True and launch.argv == ("C:\\Windows\\system32\\cmd.exe",)
    assert locate.shell_safe("--output-format") and locate.shell_safe("mcp__jarvis__*")
    for bad in ('a"b', "a&b", "a|b", "a>b", "a^b", "%PATH%", "a!b", "a\nb"):
        assert not locate.shell_safe(bad)
    command = locate.shell_command(launch, ["--model", "", "C:\\Program Files\\x"])
    assert command == f'{shim} --model "" "C:\\Program Files\\x"'


def test_posix_search_needs_the_executable_bit_and_runs_js_with_node(tmp_path: Path) -> None:
    env = {"PATH": str(tmp_path / "bin"), "HOME": str(tmp_path)}
    touch(tmp_path / "bin" / "claude")  # не исполняемый — не программа
    assert locate.find_program("claude", env=env, platform="linux", home=str(tmp_path)) is None
    program = touch(tmp_path / ".local" / "bin" / "claude", executable=True)
    assert locate.find_program("claude", env=env, platform="linux", home=str(tmp_path)) == str(program)
    node = touch(tmp_path / "bin" / "node", executable=True)
    script = touch(tmp_path / "lib" / "cli.js")
    launch = locate.resolve_launch(str(script), env=env, platform="linux", home=str(tmp_path))
    assert launch.argv == (str(node), str(script))


def test_parse_cmd_shim_skips_node_and_prog_variables() -> None:
    assert locate.parse_cmd_shim(NPM_SHIM) == r"%dp0%\node_modules\@google\gemini-cli\dist\index.js"
    assert locate.parse_cmd_shim(PNPM_SHIM) == r"%~dp0\global\5\node_modules\@qwen-code\qwen-code\cli.js"
    assert locate.parse_cmd_shim("@echo off\necho hi\n") is None


# --- разбор вывода -----------------------------------------------------------------------------------------


def test_claude_stream_events_in_order() -> None:
    parser = ClaudeStream()
    lines = [
        "Warning: something non-json",
        '{"type":"system","subtype":"init","session_id":"s1","model":"claude-sonnet-5",'
        '"mcp_servers":[{"name":"jarvis","status":"connected"}]}',
        '{"type":"stream_event","event":{"type":"message_start","message":{"id":"m1"}}}',
        '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"При"}}}',
        '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"input_json_delta",'
        '"partial_json":"{"}}}',
        '{"type":"assistant","message":{"id":"m1","content":[{"type":"text","text":"Привет"},'
        '{"type":"tool_use","id":"t1","name":"mcp__jarvis__get_time","input":{}}]}}',
        '{"type":"assistant","parent_tool_use_id":"t0","message":{"id":"sub","content":[{"type":"text",'
        '"text":"под-агент"}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":[{"type":"text",'
        '"text":"12:00"}],"is_error":false}]}}',
        '{"type":"system","subtype":"api_retry","attempt":1,"error":"overloaded","error_status":529}',
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning"}}',
        '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1790000000}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"Привет","session_id":"s1",'
        '"usage":{"input_tokens":10,"cache_read_input_tokens":90,"cache_creation_input_tokens":5,"output_tokens":7}}',
    ]
    events = [e for line in lines if (obj := parse_line(line)) for e in parser.feed(obj)]
    assert [e.kind for e in events] == [
        "init",
        "round",
        "delta",
        "message",
        "tool_use",
        "tool_result",
        "retry",
        "rate_limit",
        "result",
    ]
    init, _, delta, message, tool, result_tool, retry, limit, result = events
    assert init.session_id == "s1" and init.data["mcp_servers"][0]["status"] == "connected"
    assert delta.text == "При" and message.data["streamed"] is True
    assert tool.name == "mcp__jarvis__get_time" and tool.item == "t1"
    assert result_tool.text == "12:00" and result_tool.ok
    assert retry.code == "overloaded" and limit.data["resetsAt"] == 1790000000
    assert result.ok and result.usage == (105, 7) and result.session_id == "s1"


def test_claude_stream_errors_and_failed_results() -> None:
    parser = ClaudeStream()
    (error,) = parser.feed(
        {
            "type": "assistant",
            "error": "authentication_failed",
            "message": {
                "id": "m",
                "content": [{"type": "text", "text": "Invalid API key · Please run /login"}],
            },
        }
    )
    assert error.kind == "error" and error.code == "authentication_failed" and "/login" in error.text
    (failed,) = parser.feed(
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "errors": ["x"]}
    )
    assert failed.ok is False and failed.code == "error_max_turns" and failed.text == "x"
    (bad,) = parser.feed(
        {"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 500"}
    )
    assert bad.ok is False and bad.text == "API Error: 500"
    # сообщение без потоковых кусочков (без --include-partial-messages) — целиком
    (whole,) = parser.feed(
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Да"}]}}
    )
    assert whole.kind == "message" and whole.data["streamed"] is False


def test_gemini_stream_rounds_tools_and_result() -> None:
    parser = GeminiStream()
    objs = [
        {"type": "init", "session_id": "g1", "model": "auto"},
        {"type": "message", "role": "user", "content": "Привет"},
        {"type": "message", "role": "assistant", "content": "Смотрю", "delta": True},
        {"type": "message", "role": "assistant", "content": "…", "delta": True},
        {"type": "tool_use", "tool_name": "mcp_jarvis_get_time", "tool_id": "c1", "parameters": {}},
        {"type": "tool_result", "tool_id": "c1", "status": "error", "error": {"message": "нет"}},
        {"type": "message", "role": "assistant", "content": "Готово", "delta": True},
        {"type": "error", "severity": "warning", "message": "Loop detected"},
        {"type": "result", "status": "success", "stats": {"input_tokens": 50, "output_tokens": 5}},
    ]
    events = [e for obj in objs for e in parser.feed(obj)]
    kinds = [e.kind for e in events]
    assert kinds == [
        "init",
        "round",
        "delta",
        "delta",
        "tool_use",
        "tool_result",
        "round",
        "delta",
        "warning",
        "result",
    ]
    assert events[5].ok is False and events[5].text == "нет"
    assert events[-1].ok and events[-1].usage == (50, 5)
    (failed,) = GeminiStream().feed(
        {"type": "result", "status": "error", "error": {"type": "X", "message": "boom"}}
    )
    assert failed.ok is False and failed.text == "boom" and failed.code == "X"


# --- ошибки по-русски --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "kwargs", "reason"),
    [
        ("claude_code", {"code": "authentication_failed", "text": "Please run /login"}, "auth"),
        ("claude_code", {"code": "oauth_org_not_allowed"}, "org"),
        ("claude_code", {"code": "account_on_hold"}, "hold"),
        ("claude_code", {"code": "billing_error", "text": "Credit balance is too low"}, "billing"),
        ("claude_code", {"code": "rate_limit", "text": "Claude AI usage limit reached"}, "limit"),
        ("claude_code", {"text": "429 Too Many Requests"}, "rate"),
        (
            "claude_code",
            {"text": 'API Error: 403 {"error":{"type":"forbidden","message":"Request not allowed"}}'},
            "region",
        ),
        ("claude_code", {"code": "model_not_found", "text": "model: x"}, "model"),
        ("claude_code", {"text": "Prompt is too long"}, "too_long"),
        ("claude_code", {"code": "max_output_tokens"}, "too_long"),
        ("claude_code", {"text": "API Error: 529 Overloaded"}, "server"),
        ("claude_code", {"stderr": "Error: getaddrinfo ENOTFOUND api.anthropic.com"}, "network"),
        ("gemini_cli", {"exit_code": 41, "stderr": "Please set an Auth method"}, "auth"),
        (
            "gemini_cli",
            {"text": "This client is no longer supported for Gemini Code Assist for individuals"},
            "gemini_consumer",
        ),
        ("gemini_cli", {"text": "RESOURCE_EXHAUSTED: Quota exceeded"}, "limit"),
        ("gemini_cli", {"text": "User location is not supported for the API use."}, "region"),
        ("qwen_code", {"text": "Qwen OAuth free tier was discontinued on 2026-04-15"}, "qwen_oauth"),
        ("qwen_code", {"text": "401 Incorrect API key provided"}, "auth"),
        ("qwen_code", {"text": "Arrearage: insufficient balance"}, "billing"),
        ("qwen_code", {"text": "что-то странное"}, "error"),
    ],
)
def test_error_classification(provider: str, kwargs: dict[str, object], reason: str) -> None:
    name = {"claude_code": "Claude", "gemini_cli": "Gemini", "qwen_code": "Qwen"}[provider]
    got, text = errors.classify(provider, name, **kwargs)  # type: ignore[arg-type]
    assert got == reason
    assert text and "{" not in text  # всё подставлено
    if reason == "auth":
        assert text == errors.NOT_LOGGED_IN[provider]
    if reason == "error":
        assert text == "Ошибка Qwen: что-то странное"


def test_limit_text_names_the_reset_time() -> None:
    soon = time.time() + 1800
    assert errors.until_text(soon).startswith(" — обновится в ")
    assert " — обновится " in errors.until_text(time.time() + 3 * 86400) and "." in errors.until_text(
        time.time() + 3 * 86400
    )
    assert errors.until_text(None) == " — попробуйте позже"
    _, text = errors.classify("claude_code", "Claude", code="rate_limit", resets_at=soon)
    assert text.startswith("Лимит Claude исчерпан — обновится в ")


# --- флаги запуска -----------------------------------------------------------------------------------------


def test_turn_text_puts_context_first_and_user_text_last() -> None:
    assert prompt.turn_text("Сейчас: 10:00", "", "Привет") == "<jarvis>\nСейчас: 10:00\n</jarvis>\n\nПривет"
    assert prompt.turn_text("", "Ранее в этом чате:\n…", "Да") == "Ранее в этом чате:\n…\n\nДа"
    assert "Jarvis" in prompt.chat_system() and "finish" in prompt.agent_system("Задача")


def test_programs_never_get_their_own_shell_or_file_tools(tmp_path: Path) -> None:
    run = CliRun(
        work_dir=tmp_path,
        prompt="& del /q *",
        system="Правила",
        model="sonnet",
        mcp_url="http://127.0.0.1:1/api/ai/mcp",
        mcp_headers={"X-Jarvis-Token": "t", "X-Jarvis-Turn": "u"},
        session_id="11111111-1111-1111-1111-111111111111",
    )
    claude = KINDS["claude_code"]()
    args = claude.args(run)
    assert args[args.index("--tools") + 1] == "" and args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--dangerously-skip-permissions" not in args and "--bare" not in args
    assert all(run.prompt not in a for a in args)  # текст человека — только через stdin
    assert "ANTHROPIC_API_KEY" in claude.strip_env()
    config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    assert config["mcpServers"]["jarvis"]["url"] == run.mcp_url
    stdin = json.loads(claude.stdin(run).decode("utf-8"))
    assert stdin["message"]["content"][0]["text"] == "& del /q *"

    gemini = KINDS["gemini_cli"]()
    gemini.prepare_dir(tmp_path, tools=True)
    gargs = gemini.args(run)
    assert "--yolo" not in gargs and gargs[gargs.index("--approval-mode") + 1] == "default"
    settings = json.loads((tmp_path / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    excluded = set(settings["tools"]["exclude"])
    assert {"run_shell_command", "write_file", "replace", "web_fetch"} <= excluded
    # в файле только ссылки на переменные окружения, самих токенов в нём нет
    headers = settings["mcpServers"]["jarvis"]["headers"]
    assert all(v.startswith("${") and v.endswith("}") for v in headers.values()) and len(headers) == 2
    genv = gemini.env(run)
    assert genv["JARVIS_MCP_AUTH"] == "t" and genv["JARVIS_MCP_TURN"] == "u"

    qwen = KINDS["qwen_code"]()
    qargs = qwen.args(run)
    assert qargs[qargs.index("--approval-mode") + 1] == "default" and "--yolo" not in qargs
    assert "run_shell_command" in qargs[qargs.index("--exclude-tools") + 1].split(",")
    assert all(run.prompt not in a for a in qargs) and os.path.isfile(qargs[qargs.index("--mcp-config") + 1])
