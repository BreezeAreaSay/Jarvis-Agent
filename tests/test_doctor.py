"""jarvis doctor на фейках: httpx (MockTransport), psutil, es.exe и codex через pc.subproc."""

import importlib
import json
import os
import sys
import time
import types
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from jarvis import doctor
from jarvis.config import Config, HandsConfig
from jarvis.doctor import Check, Doctor
from jarvis.events import Done, TextChunk
from pc import settings, subproc
from pc.subproc import Completed


def install(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    package, _, attr = name.rpartition(".")
    monkeypatch.setattr(importlib.import_module(package), attr, mod, raising=False)
    return mod


def doc(**kw: Any) -> Doctor:
    return Doctor(cfg=Config(**kw))


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Фейковый сервер рук: routes[path] — (код, json) или исключение."""
    routes: dict[str, Any] = {
        "/health": (200, {"status": "ok"}),
        "/props": (
            200,
            {
                "model_path": r"C:\models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
                "default_generation_settings": {"n_ctx": 8192},
            },
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        value = routes.get(request.url.path, (404, {}))
        if isinstance(value, Exception):
            raise value
        return httpx.Response(value[0], json=value[1])

    monkeypatch.setattr(doctor, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return routes


@pytest.fixture
def es(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Фейковый es.exe: answers[ключ] — (код, вывод); calls — все argv."""
    exe = tmp_path / "es.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(settings, "es_path", lambda: exe)
    state: dict[str, Any] = {
        "calls": [],
        "-version": (0, "1.1.0.38"),
        "-get-everything-version": (0, "1.4.1.1032"),
        "C:\\Windows": (0, "359000"),
        "profile": (0, "120000"),
        "search": "exact",  # exact | other | none
    }
    monkeypatch.setenv("USERPROFILE", r"C:\Users\me")

    def run(argv: list[str], timeout: float, cwd: Any = None, env: Any = None) -> Completed:
        args = [str(a) for a in argv]
        state["calls"].append(args)
        assert args[0] == str(exe) and args[1:3] == ["-cp", "65001"], args
        rest = args[3:]
        if rest[0] in ("-version", "-get-everything-version"):
            code, out = state[rest[0]]
        elif rest[0] == "-get-result-count":
            code, out = state["C:\\Windows"] if rest[2] == "C:\\Windows" else state["profile"]
        else:  # -n 5 <stem>
            stem = rest[-1]
            probe = next((settings.data_dir() / "doctor").glob(f"{stem}.txt"), None)
            code, out = 0, ""
            if state["search"] == "exact" and probe is not None:
                out = f"{probe}\r\n{probe.parent / 'другой.txt'}"
            elif state["search"] == "other":
                out = r"C:\Users\me\Тест_ёЁ_поиска_старый.txt"
        return Completed(code, out.encode("utf-8"), b"")

    monkeypatch.setattr(subproc, "run", run)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return state


# --- конфиг, данные, установка ------------------------------------------------------------------


def test_config_states(config_file: Any) -> None:
    assert doctor.check_config(doc()).status == "warn"
    config_file('mode = "local"\n')
    res = doctor.check_config(doc(mode="local"))
    assert res.status == "ok" and "режим local" in res.text
    config_file("mode = = \n")
    res = doctor.check_config(doc())
    assert res.status == "fail" and "TOML" in res.hint


def test_data_dir_non_ascii(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # tmp_path на ПК владельца сам с кириллицей (профиль) — путь для «ok» задаём явно, на диск не пишем
    monkeypatch.setenv("JARVIS_DATA_DIR", r"C:\JarvisData")
    res = doctor.check_data_dir(doc())
    assert res.status == "ok" and "JARVIS_DATA_DIR" in res.text
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "Данные"))
    res = doctor.check_data_dir(doc())
    assert res.status == "warn" and doctor.NON_ASCII_DATA in res.text
    assert "codex login" in res.hint and "CODEX_HOME" in res.hint


def test_install_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert doctor.check_install(doc()).status == "ok"
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    monkeypatch.setattr(settings, "app_root", lambda: tmp_path / "_internal")
    monkeypatch.setattr(settings, "install_dir", lambda: tmp_path)
    for rel in ("scripts/start_hands.cmd", "jarvis.example.toml"):
        (tmp_path / "_internal" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "_internal" / rel).write_text("x", encoding="utf-8")
    res = doctor.check_install(doc())
    assert res.status == "fail" and "bench/phrases.ru.jsonl" in res.text
    (tmp_path / "_internal" / "bench").mkdir()
    (tmp_path / "_internal" / "bench" / "phrases.ru.jsonl").write_text("{}", encoding="utf-8")
    res = doctor.check_install(doc())
    assert res.status == "ok" and "es.exe не в комплекте" in res.text


def test_mode_hotkey(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, "jarvis.winapp", parse_hotkey=lambda s: (3, 0x20))
    monkeypatch.setattr(doctor, "jarvis_running", lambda: False)
    res = doctor.check_mode_hotkey(doc())
    assert res.status == "ok" and "ctrl+alt+space" in res.text and "jarvis run не запущен" in res.text

    def bad(s: str) -> Any:
        raise ValueError("нет клавиши")

    install(monkeypatch, "jarvis.winapp", parse_hotkey=bad)
    assert doctor.check_mode_hotkey(doc()).status == "fail"


def test_jarvis_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def procs(cmds: list[list[str]]) -> Any:
        return lambda attrs: [
            types.SimpleNamespace(info={"pid": 100 + i, "cmdline": c}) for i, c in enumerate(cmds)
        ]

    monkeypatch.setattr(psutil, "process_iter", procs([["uv", "run", "jarvis", "doctor"], ["python.exe"]]))
    assert doctor.jarvis_running() is False
    monkeypatch.setattr(
        psutil, "process_iter", procs([[r"C:\venv\Scripts\pythonw.exe", "-m", "jarvis", "run"]])
    )
    assert doctor.jarvis_running() is True
    monkeypatch.setattr(
        psutil, "process_iter", procs([[r"C:\Users\me\AppData\Local\Programs\Jarvis\Jarvis.exe"]])
    )
    assert doctor.jarvis_running() is True
    monkeypatch.setattr(psutil, "process_iter", procs([[r"C:\Programs\Jarvis\jarvis-cli.exe", "doctor"]]))
    assert doctor.jarvis_running() is False


def test_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, "jarvis.winapp", autostart_enabled=lambda: True)
    assert doctor.check_autostart(doc()).status == "ok"
    install(monkeypatch, "jarvis.winapp", autostart_enabled=lambda: False)
    res = doctor.check_autostart(doc())
    assert res.status == "warn" and res.hint == "jarvis autostart on"


def test_apps_cache(_isolated_data: Path) -> None:
    assert doctor.check_apps_cache(doc()).status == "warn"
    path = _isolated_data / "apps.json"
    apps = [{"name": "Telegram", "app_id": "TelegramDesktop.TelegramDesktop"}]
    path.write_text(json.dumps({"version": 1, "updated": time.time() - 3600, "apps": apps}), encoding="utf-8")
    res = doctor.check_apps_cache(doc())
    assert res.status == "ok" and "1 шт." in res.text and "1 ч" in res.text
    path.write_text(
        json.dumps({"version": 1, "updated": time.time() - 30 * 86400, "apps": apps}), encoding="utf-8"
    )
    assert doctor.check_apps_cache(doc()).status == "warn"


# --- руки ---------------------------------------------------------------------------------------


def test_hands_health_and_props(http: dict[str, Any]) -> None:
    d = doc()
    assert doctor.check_hands_health(d).status == "ok"
    res = doctor.check_hands_props(d)
    assert (
        res.status == "ok" and "Qwen3-4B-Instruct-2507-Q4_K_M.gguf" in res.text and "n_ctx 8192" in res.text
    )
    d = Doctor(cfg=Config(hands=HandsConfig(model="8b")))
    assert doctor.check_hands_props(d).status == "warn"


def test_hands_health_states(http: dict[str, Any]) -> None:
    http["/health"] = (503, {"error": "Loading model"})
    assert doctor.check_hands_health(doc()).status == "warn"
    http["/health"] = httpx.ConnectError("отказ")
    d = doc()
    res = doctor.check_hands_health(d)
    assert res.status == "fail" and "start_hands.cmd" in res.hint
    assert doctor.check_hands_props(d).status == "skip"
    assert doctor.check_hands_warm(d).status == "skip"


def _conn(port: int, pid: int | None) -> Any:
    return types.SimpleNamespace(status=psutil.CONN_LISTEN, laddr=("127.0.0.1", port), pid=pid)


def test_hands_owner(monkeypatch: pytest.MonkeyPatch, http: dict[str, Any]) -> None:
    exes = {
        10: r"C:\llama\llama-server.exe",
        20: r"C:\Users\me\AppData\Local\Programs\Ollama\llama-server.exe",
    }

    class Proc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def exe(self) -> str:
            return exes[self.pid]

    monkeypatch.setattr(psutil, "Process", Proc)
    monkeypatch.setattr(psutil, "net_connections", lambda kind: [_conn(5000, 1), _conn(8081, 10)])
    assert doctor.check_hands_owner(doc()).status == "ok"
    monkeypatch.setattr(psutil, "net_connections", lambda kind: [_conn(8081, 20)])
    res = doctor.check_hands_owner(doc())
    assert res.status == "fail" and "Ollama" in res.text
    monkeypatch.setattr(psutil, "net_connections", lambda kind: [])
    assert doctor.check_hands_owner(doc()).status == "fail"  # сервер отвечает, а порт никто не слушает?


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"server": "ready", "prefix_cache": "ok", "vram": "ok", "tps": 74.0, "prefix_tokens": 1180}, "ok"),
        ({"server": "ready", "prefix_cache": "broken", "vram": "ok", "tps": 74.0}, "fail"),
        ({"server": "ready", "prefix_cache": "ok", "vram": "slow", "tps": 20.0}, "warn"),
        ({"server": "ready", "prefix_cache": "unknown", "vram": "unknown"}, "warn"),
        ({"server": "error", "error": "Порт 8081 занят чужим процессом"}, "fail"),
    ],
)
def test_hands_warm(
    monkeypatch: pytest.MonkeyPatch, http: dict[str, Any], status: dict, expected: str
) -> None:
    closed: list[bool] = []

    class Hands:
        def __init__(self, cfg: Any) -> None:
            self.status: dict[str, Any] = {"server": "unknown"}

        def warmup(self) -> None:
            self.status = dict(status)

        def close(self) -> None:
            closed.append(True)

    install(monkeypatch, "jarvis.hands", Hands=Hands)
    res = doctor.check_hands_warm(doc())
    assert res.status == expected, res
    assert closed == [True]


def test_hands_warm_exception(monkeypatch: pytest.MonkeyPatch, http: dict[str, Any]) -> None:
    class Hands:
        def __init__(self, cfg: Any) -> None:
            self.status: dict[str, Any] = {}

        def warmup(self) -> None:
            raise RuntimeError("Порт 8081 занят чужим процессом")

        def close(self) -> None:
            pass

    install(monkeypatch, "jarvis.hands", Hands=Hands)
    res = doctor.check_hands_warm(doc())
    assert res.status == "fail" and "чужим процессом" in res.text


def test_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    def procs(names: list[str]) -> Any:
        return lambda attrs: [types.SimpleNamespace(info={"name": n}) for n in names]

    monkeypatch.setattr(psutil, "process_iter", procs(["explorer.exe", "python.exe"]))
    assert doctor.check_ollama(doc()).status == "ok"
    monkeypatch.setattr(psutil, "process_iter", procs(["ollama app.exe", "ollama.exe", "llama-server.exe"]))
    res = doctor.check_ollama(doc())
    assert res.status == "warn" and "ollama.exe" in res.text and "видеопамять" in res.text


# --- Everything ---------------------------------------------------------------------------------


def test_everything_all_good(es: dict[str, Any]) -> None:
    d = doc()
    assert doctor.check_es_version(d) == Check("ok", f"es.exe 1.1.0.38: {settings.es_path()}")
    assert doctor.check_everything_running(d).status == "ok"
    res = doctor.check_everything_index(d)
    assert res.status == "ok" and res.text == "весь C: в индексе"
    res = doctor.check_everything_cyrillic(d)
    assert res.status == "ok", res
    assert not list((settings.data_dir() / "doctor").glob("*.txt"))  # пробный файл удалён


def test_everything_folder_mode_and_empty(es: dict[str, Any]) -> None:
    d = doc()
    doctor.check_everything_running(d)
    es["C:\\Windows"] = (0, "0")
    res = doctor.check_everything_index(d)
    assert res.status == "ok" and res.text.startswith("режим папок")
    es["profile"] = (0, "0")
    assert doctor.check_everything_index(d).status == "fail"


def test_everything_not_running(es: dict[str, Any]) -> None:
    es["-get-everything-version"] = (8, "")
    d = doc()
    res = doctor.check_everything_running(d)
    assert res.status == "fail" and "не запущен" in res.text
    assert doctor.check_everything_index(d).status == "skip"
    assert doctor.check_everything_cyrillic(d).status == "skip"


def test_es_old_or_missing(es: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    es["-version"] = (0, "ES 1.1.0.30")
    assert doctor.check_es_version(doc()).status == "fail"
    monkeypatch.setattr(settings, "es_path", lambda: tmp_path / "нет" / "es.exe")
    d = doc()
    res = doctor.check_es_version(d)
    assert res.status == "fail" and "es_path" in res.hint
    assert doctor.check_everything_running(d).status == "skip"


def test_cyrillic_not_exact(es: dict[str, Any]) -> None:
    d = doc()
    doctor.check_everything_running(d)
    es["search"] = "other"
    assert doctor.check_everything_cyrillic(d).status == "fail"
    es["search"] = "none"
    assert doctor.check_everything_cyrillic(d).status == "fail"


# --- мозг ---------------------------------------------------------------------------------------


@pytest.fixture
def codex(monkeypatch: pytest.MonkeyPatch, _isolated_data: Path) -> dict[str, Any]:
    """Фейковый codex login status через pc.subproc; jarvis.brain — фейк с codex_bin/env/home."""
    home = _isolated_data / "codex-home"
    home.mkdir()
    state: dict[str, Any] = {"calls": [], "answer": (0, "Logged in using ChatGPT\n")}

    def run(argv: list[str], timeout: float, cwd: Any = None, env: Any = None) -> Completed:
        state["calls"].append(([str(a) for a in argv], env))
        code, err = state["answer"]
        warn = "WARNING: proceeding, even though we could not create PATH aliases\n"
        return Completed(code, b"", (warn + err).encode("utf-8"))

    monkeypatch.setattr(subproc, "run", run)
    install(
        monkeypatch,
        "jarvis.brain",
        codex_bin=lambda: Path(r"C:\venv\Lib\site-packages\codex_cli_bin\bin\codex.exe"),
        codex_env=lambda cfg: {"CODEX_HOME": str(home)},
        codex_home=lambda: home,
    )
    return state


def test_brain_logged_in(codex: dict[str, Any]) -> None:
    res = doctor.check_brain_login(doc())
    assert res.status == "ok" and "Logged in using ChatGPT" in res.text
    argv, env = codex["calls"][0]
    assert argv[1:] == ["login", "status"] and env["CODEX_HOME"].endswith("codex-home")
    assert env.get("PATH") == os.environ.get("PATH")  # окружение сливается, а не заменяется


def test_brain_not_logged_in(codex: dict[str, Any]) -> None:
    codex["answer"] = (1, "Not logged in\n")
    res = doctor.check_brain_login(doc())
    assert res.status == "fail" and "codex login" in res.hint and "codex-home" in res.hint


def test_brain_config_error(codex: dict[str, Any]) -> None:
    codex["answer"] = (1, "Error loading configuration: bad key\n")
    res = doctor.check_brain_login(doc())
    assert res.status == "fail" and "Error loading configuration" in res.text and "config.toml" in res.hint


def test_brain_no_codex_home(codex: dict[str, Any], _isolated_data: Path) -> None:
    (_isolated_data / "codex-home").rmdir()
    res = doctor.check_brain_login(doc())
    assert res.status == "fail" and "codex login" in res.hint
    assert codex["calls"] == []


def test_brain_local_mode_never_runs_codex(codex: dict[str, Any]) -> None:
    res = doctor.check_brain_login(doc(mode="local"))
    assert res.status == "ok" and "локальный режим" in res.text
    d = Doctor(cfg=Config(mode="local"), brain=True)
    res = doctor.check_brain_login(d)
    assert res.status == "warn" and "--brain не выполняется" in res.text
    assert doctor.check_brain_turn(d).status == "skip"
    assert codex["calls"] == []


def test_brain_turn(monkeypatch: pytest.MonkeyPatch, codex: dict[str, Any]) -> None:
    created: list[str] = []

    class Brain:
        def __init__(self, cfg: Any, confirm_address: str) -> None:
            created.append(confirm_address)

        def start(self) -> None:
            pass

        def ask(self, text: str, ctx: Any, deep: bool = False):
            yield TextChunk("Готов")
            yield Done(ok=True, text="")

        def close(self) -> None:
            created.append("closed")

    class Server:
        def __init__(self, callback: Any) -> None:
            assert callback("Удалить?", "", "brain") is False
            self.address = "pipe-1"

        def start(self) -> None:
            pass

        def close(self) -> None:
            created.append("server closed")

    monkeypatch.setattr(sys.modules["jarvis.brain"], "Brain", Brain, raising=False)
    from pc import confirm_client

    monkeypatch.setattr(confirm_client, "ConfirmServer", Server)
    d = Doctor(cfg=Config(), brain=True)
    assert doctor.check_brain_turn(d).status == "skip"  # сначала вход
    doctor.check_brain_login(d)
    res = doctor.check_brain_turn(d)
    assert res.status == "ok" and "до первых слов" in res.text
    assert created == ["pipe-1", "closed", "server closed"]
    assert doctor.check_brain_turn(Doctor(cfg=Config())).status == "skip"  # без --brain квоту не тратим


def test_login_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "; codex login;" in doctor.login_command()
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    install(monkeypatch, "jarvis.brain", codex_bin=lambda: Path(r"C:\Programs\Jarvis\_internal\codex.exe"))
    cmd = doctor.login_command()
    assert r"& 'C:\Programs\Jarvis\_internal\codex.exe' login" in cmd and "JARVIS_DATA_DIR" in cmd


# --- прогон -------------------------------------------------------------------------------------


def test_checks_are_independent() -> None:
    def broken(d: Doctor) -> Check:
        raise OSError("нет доступа")

    def fine(d: Doctor) -> Check:
        return Check("ok", "всё хорошо")

    results = doctor.run_checks(doc(), [broken, fine])
    assert results[0].status == "fail" and "broken: проверка упала: OSError: нет доступа" in results[0].text
    assert results[1] == Check("ok", "всё хорошо")


def test_main_prints_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checks = [
        lambda d: Check("ok", "раз"),
        lambda d: Check("warn", "два", "сделай так"),
        lambda d: Check("skip", "три"),
    ]
    monkeypatch.setattr(doctor, "CHECKS", checks)
    assert doctor.main() == 0
    out = capsys.readouterr().out
    assert (
        "✓ раз" in out
        and "⚠ два\n    → сделай так" in out
        and "– три" in out
        and "итог: ✓ 1, ⚠ 1, ✗ 0" in out
    )
    monkeypatch.setattr(doctor, "CHECKS", [*checks, lambda d: Check("fail", "четыре")])
    assert doctor.main() == 1
