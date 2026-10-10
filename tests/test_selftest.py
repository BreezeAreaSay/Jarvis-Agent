"""jarvis.selftest: каждый пункт с фейками, итог и код выхода, изоляция окружения.

Qt-пункты идут в подпроцессе (QApplication в процессе pytest помешал бы другим тестам), полный selftest в
разработке — тоже в подпроцессе, с офскрином.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jarvis import selftest
from pc import settings
from pc.subproc import Completed

ROOT = Path(__file__).resolve().parents[1]


def _ctx(tmp_path: Path) -> selftest.Ctx:
    tmp = tmp_path / "selftest"
    tmp.mkdir(exist_ok=True)
    return selftest.Ctx(tmp, offscreen=True)


def _bundle(root: Path, phrases: str = '{"text": "открой блокнот"}\n{"text": "который час"}\n') -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "bench").mkdir()
    (root / "scripts" / "start_hands.cmd").write_text("@echo off\n", encoding="utf-8")
    (root / "jarvis.example.toml").write_text('mode = "normal"\n', encoding="utf-8")
    (root / "bench" / "phrases.ru.jsonl").write_text(phrases, encoding="utf-8")
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / "tests" / "fixtures" / "apps.json").write_text(
        '[{"name": "Блокнот", "app_id": "x"}]', encoding="utf-8"
    )
    return root


# --- файлы бандла -------------------------------------------------------------------------------------------


def test_files_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bundle(tmp_path / "app")
    monkeypatch.setattr(settings, "app_root", lambda: root)
    monkeypatch.setattr(settings, "es_path", lambda: tmp_path / "нет" / "es.exe")
    text = selftest.check_files(_ctx(tmp_path))
    assert "фраз в корпусе: 2" in text
    assert "es.exe нет" in text


def test_files_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bundle(tmp_path / "app")
    (root / "bench" / "phrases.ru.jsonl").unlink()
    (root / "jarvis.example.toml").write_text("", encoding="utf-8")  # пустой — тоже нет
    monkeypatch.setattr(settings, "app_root", lambda: root)
    with pytest.raises(selftest.Fail, match=r"bench/phrases\.ru\.jsonl") as e:
        selftest.check_files(_ctx(tmp_path))
    assert "jarvis.example.toml" in str(e.value)


def test_files_bad_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bundle(tmp_path / "app", phrases='{"text": "ok"}\nне json\n')
    monkeypatch.setattr(settings, "app_root", lambda: root)
    with pytest.raises(selftest.Fail, match="строка 2"):
        selftest.check_files(_ctx(tmp_path))


def test_files_bad_apps_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bundle(tmp_path / "app")
    (root / "tests" / "fixtures" / "apps.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "app_root", lambda: root)
    with pytest.raises(selftest.Fail, match=r"apps\.json: ждали непустой список"):
        selftest.check_files(_ctx(tmp_path))


def test_files_frozen_needs_both_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install = tmp_path / "Jarvis"
    root = _bundle(install / "_internal")
    (install / selftest.exe_name("Jarvis")).write_bytes(b"MZ")
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    monkeypatch.setattr(settings, "app_root", lambda: root)
    monkeypatch.setattr(settings, "install_dir", lambda: install)
    with pytest.raises(selftest.Fail, match="jarvis-cli"):
        selftest.check_files(_ctx(tmp_path))
    (install / selftest.exe_name("jarvis-cli")).write_bytes(b"MZ")
    assert "рядом" in selftest.check_files(_ctx(tmp_path))


# --- модули -------------------------------------------------------------------------------------------------


def _fake_package(base: Path, name: str, broken: bool) -> None:
    pkg = base / name
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub" / "deep.py").write_text("VALUE = 2\n", encoding="utf-8")
    # __main__ не импортируется: иначе selftest запустил бы чужую программу
    (pkg / "__main__.py").write_text("raise SystemExit('__main__ импортирован')\n", encoding="utf-8")
    if broken:
        (pkg / "broken.py").write_text("raise RuntimeError('сломан')\n", encoding="utf-8")


def test_modules_reports_missing_and_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_package(tmp_path, "selftest_fakepkg_a", broken=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(selftest, "PACKAGES", ("selftest_fakepkg_a",))
    required = ("selftest_fakepkg_a.ok", "selftest_fakepkg_a.sub.deep", "selftest_fakepkg_a.window")
    monkeypatch.setattr(selftest, "REQUIRED_MODULES", required)
    with pytest.raises(selftest.Fail) as e:
        selftest.check_modules(_ctx(tmp_path))
    text = str(e.value)
    assert "нет модулей: selftest_fakepkg_a.window" in text
    assert "selftest_fakepkg_a.broken: RuntimeError: сломан" in text
    assert "selftest_fakepkg_a.ok" not in text


def test_modules_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_package(tmp_path, "selftest_fakepkg_b", broken=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(selftest, "PACKAGES", ("selftest_fakepkg_b",))
    monkeypatch.setattr(selftest, "REQUIRED_MODULES", ("selftest_fakepkg_b.sub.deep",))
    assert selftest.check_modules(_ctx(tmp_path)) == "импортировано модулей: 4"


# --- конфиг -------------------------------------------------------------------------------------------------


def test_config_from_example(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with selftest.isolated_env(ctx.tmp, offscreen=False):
        text = selftest.check_config(ctx)
    assert "хоткей ctrl+alt+space" in text


def test_config_example_without_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / "jarvis.example.toml").write_text(
        'mode = "normal"\n[ui]\nhotkey = "ctrl+alt+j"\n', encoding="utf-8"
    )
    monkeypatch.setattr(settings, "app_root", lambda: root)
    with pytest.raises(selftest.Fail, match="нет секций: hands, brain, pc, aliases, journal"):
        selftest.check_config(_ctx(tmp_path))


# --- MCP ----------------------------------------------------------------------------------------------------

FAKE_SERVER = textwrap.dedent(
    """
    import sys
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("fake")
    for name in sys.argv[1].split(","):
        def tool() -> str:
            return "ok"
        server.add_tool(tool, name=name, description="фейк")
    server.run("stdio")
    """
)


def _fake_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str], code: str = FAKE_SERVER
) -> None:
    script = tmp_path / "fake_mcp.py"
    script.write_text(code, encoding="utf-8")
    command = (sys.executable, [str(script), ",".join(names)], tmp_path)
    monkeypatch.setattr(selftest, "mcp_command", lambda: command)
    monkeypatch.setattr(selftest, "MCP_TIMEOUT_S", 30.0)


def test_mcp_18_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_mcp(tmp_path, monkeypatch, sorted(selftest.MCP_TOOLS))
    text = selftest.check_mcp(_ctx(tmp_path))
    assert text.startswith("18 инструментов за ")


def test_mcp_wrong_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    names = [*sorted(selftest.MCP_TOOLS - {"power"}), "shell"]
    _fake_mcp(tmp_path, monkeypatch, names)
    with pytest.raises(selftest.Fail, match=r"нет: \['power'\]; лишние: \['shell'\]"):
        selftest.check_mcp(_ctx(tmp_path))


def test_mcp_server_crash_shows_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crash = "import sys\nsys.stderr.write('сервер сломался\\n')\nsys.exit(3)\n"
    _fake_mcp(tmp_path, monkeypatch, [], code=crash)
    with pytest.raises(selftest.Fail, match="сервер сломался"):
        selftest.check_mcp(_ctx(tmp_path))


def test_mcp_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    command, args, cwd = selftest.mcp_command()
    assert (command, args) == (sys.executable, ["-m", "pc.mcp"])
    assert cwd == settings.app_root()
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    monkeypatch.setattr(settings, "install_dir", lambda: tmp_path)
    command, args, cwd = selftest.mcp_command()
    assert Path(command) == tmp_path / selftest.exe_name("jarvis-cli")
    assert (args, cwd) == (["mcp"], tmp_path)


# --- codex --------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    exe = tmp_path / "codex_cli_bin" / "bin" / selftest.exe_name("codex")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    state: dict = {"exe": exe, "reply": Completed(0, b"codex-cli 0.160.1\n", b""), "calls": []}

    def run(argv, timeout, cwd=None, env=None):
        state["calls"].append((list(argv), dict(env or {})))
        return state["reply"]

    monkeypatch.setattr(selftest, "_codex_bin", lambda: exe)
    monkeypatch.setattr(selftest, "_bundled_codex", lambda: exe)
    monkeypatch.setattr(selftest.subproc, "run", run)
    return state


def test_codex_version(tmp_path: Path, fake_codex: dict) -> None:
    ctx = _ctx(tmp_path)
    text = selftest.check_codex(ctx)
    assert text.startswith("codex-cli 0.160.1 (")
    argv, env = fake_codex["calls"][0]
    assert argv == [fake_codex["exe"], "--version"]
    # не ~/.codex: codex создаёт там tmp даже на --version
    assert Path(env["CODEX_HOME"]).is_relative_to(ctx.tmp)


def test_codex_nonzero_exit(tmp_path: Path, fake_codex: dict) -> None:
    fake_codex["reply"] = Completed(2, b"", b"error: something")
    with pytest.raises(selftest.Fail, match="код 2; error: something"):
        selftest.check_codex(_ctx(tmp_path))


def test_codex_path_mismatch(tmp_path: Path, fake_codex: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other" / "codex"
    other.parent.mkdir()
    other.write_bytes(b"")
    monkeypatch.setattr(selftest, "_bundled_codex", lambda: other)
    with pytest.raises(selftest.Fail, match=r"не совпадает с codex_cli_bin\.bundled_codex_path"):
        selftest.check_codex(_ctx(tmp_path))


def test_codex_frozen_outside_bundle(
    tmp_path: Path, fake_codex: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    monkeypatch.setattr(settings, "app_root", lambda: tmp_path / "bundle")
    with pytest.raises(selftest.Fail, match="codex не из бандла"):
        selftest.check_codex(_ctx(tmp_path))


def test_codex_not_implemented(tmp_path: Path, fake_codex: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def stub() -> Path:
        raise NotImplementedError("S5")

    monkeypatch.setattr(selftest, "_codex_bin", stub)
    with pytest.raises(
        selftest.Fail, match=r"не реализовано: jarvis\.brain\.codex_bin\(\).*codex-cli 0\.160\.1"
    ):
        selftest.check_codex(_ctx(tmp_path))


def test_codex_bin_matches_package() -> None:
    """В разработке jarvis.brain.codex_bin() — тот же файл, что считает сам пакет codex_cli_bin."""
    assert selftest._codex_bin().resolve() == selftest._bundled_codex().resolve()


# --- запуск и итог ------------------------------------------------------------------------------------------


def _not_implemented(ctx: selftest.Ctx) -> str:
    raise NotImplementedError("jarvis.ui.window")


def _fails(ctx: selftest.Ctx) -> str:
    raise selftest.Fail("плохо")


def _module_missing(ctx: selftest.Ctx) -> str:
    import selftest_no_such_module  # noqa: F401

    return ""


def test_main_all_ok(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(selftest, "CHECKS", [("раз", lambda ctx: "хорошо"), ("два", lambda ctx: "тоже")])
    assert selftest.main([]) == 0
    out = capsys.readouterr().out
    assert "✓ раз: хорошо [" in out
    assert "✓ два: тоже [" in out
    assert "итог: 2/2 ✓" in out


def test_main_failures_do_not_stop_others(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checks = [
        ("не готово", _not_implemented),
        ("плохой", _fails),
        ("модуль", _module_missing),
        ("хороший", lambda ctx: "ok"),
    ]
    monkeypatch.setattr(selftest, "CHECKS", checks)
    assert selftest.main([]) == 1
    out = capsys.readouterr().out
    assert "✗ не готово: не реализовано: jarvis.ui.window [" in out
    assert "✗ плохой: плохо [" in out
    assert "✗ модуль: нет модуля selftest_no_such_module [" in out
    assert "✓ хороший: ok [" in out
    assert "итог: 1/4 ✓, 3 ✗" in out


def test_main_isolates_data_and_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {k: os.environ.get(k) for k in ("JARVIS_DATA_DIR", "JARVIS_CONFIG")}
    monkeypatch.setenv("JARVIS_CONFIRM_PIPE", r"\\.\pipe\jarvis-confirm-1")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    seen: dict = {}

    def spy(ctx: selftest.Ctx) -> str:
        seen.update({k: os.environ.get(k) for k in (*before, "JARVIS_CONFIRM_PIPE", "QT_QPA_PLATFORM")})
        seen["config_text"] = Path(os.environ["JARVIS_CONFIG"]).read_text(encoding="utf-8")
        seen["tmp"] = ctx.tmp
        return "ok"

    monkeypatch.setattr(selftest, "CHECKS", [("шпион", spy)])
    assert selftest.main(["--offscreen"]) == 0
    assert Path(seen["JARVIS_DATA_DIR"]).is_relative_to(seen["tmp"])
    assert Path(seen["JARVIS_CONFIG"]).is_relative_to(seen["tmp"])
    assert seen["config_text"] == (ROOT / "jarvis.example.toml").read_text(encoding="utf-8")
    assert seen["JARVIS_CONFIRM_PIPE"] is None
    assert seen["QT_QPA_PLATFORM"] == "offscreen"
    # после проверки — всё как было, временная папка удалена
    assert {k: os.environ.get(k) for k in before} == before
    assert os.environ["JARVIS_CONFIRM_PIPE"] == r"\\.\pipe\jarvis-confirm-1"
    assert "QT_QPA_PLATFORM" not in os.environ
    assert not seen["tmp"].exists()


def test_exc_text_unwraps_exception_group() -> None:
    group = ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("Connection closed")])
    assert selftest._exc_text(group) == "RuntimeError: Connection closed"


# --- Qt (подпроцесс) ----------------------------------------------------------------------------------------

QT_SCRIPT = textwrap.dedent(
    """
    import json, sys, types
    from pathlib import Path

    from PySide6 import QtCore, QtGui, QtWidgets

    from jarvis import selftest

    class LauncherWindow(QtWidgets.QWidget):
        def __init__(self, ui_cfg):
            super().__init__()
            self.resize(ui_cfg.width, 64)
            if SHOW:
                self.show()

    def render_icon(state, size):
        img = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QtGui.QColor("#3b82f6"))
        return img

    window = types.ModuleType("jarvis.ui.window")
    window.LauncherWindow = LauncherWindow
    icon = types.ModuleType("jarvis.ui.icon")
    icon.render_icon = render_icon
    sys.modules["jarvis.ui.window"] = window
    sys.modules["jarvis.ui.icon"] = icon

    ctx = selftest.Ctx(Path(sys.argv[1]), True)
    result = {}
    with selftest.isolated_env(ctx.tmp, True):
        SHOW = False
        result["plugins"] = selftest.run_check("плагины", selftest.check_qt_plugins, ctx)
        result["window"] = selftest.run_check("окно", selftest.check_window, ctx)
        result["icon"] = selftest.run_check("иконка", selftest.check_icon, ctx)
        SHOW = True
        result["shown"] = selftest.run_check("окно видно", selftest.check_window, ctx)
    print("RESULT " + json.dumps(result, ensure_ascii=False))
    """
)


def test_qt_checks_with_fake_window(tmp_path: Path) -> None:
    script = tmp_path / "qt_checks.py"
    script.write_text(QT_SCRIPT, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, str(script), str(work)],
        capture_output=True,
        timeout=120,
        env=env,
        cwd=ROOT,
        check=False,
    )
    out = r.stdout.decode("utf-8", errors="replace")
    line = next((x for x in out.splitlines() if x.startswith("RESULT ")), None)
    assert line, f"код {r.returncode}\n{out}\n{r.stderr.decode('utf-8', errors='replace')}"
    result = json.loads(line.removeprefix("RESULT "))
    assert result["plugins"][0], result
    ok, text = result["window"]
    assert ok, text
    assert text.startswith("720×64, PNG ") and "платформа offscreen" in text
    assert (work / "window.png").stat().st_size > 0
    assert result["icon"] == [True, "4 состояния × 16/32/256 px"]
    assert result["shown"][0] is False and "окно видно сразу" in result["shown"][1]


# --- полный selftest в разработке ---------------------------------------------------------------------------


def test_selftest_dev_offscreen() -> None:
    """`python -m jarvis.selftest --offscreen` в разработке: все пункты ✓ (настоящие модули, pc.mcp)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, "-m", "jarvis.selftest", "--offscreen"],
        capture_output=True,
        timeout=300,
        env=env,
        cwd=ROOT,
        check=False,
    )
    out = r.stdout.decode("utf-8", errors="replace")
    assert r.returncode == 0, f"{out}\n{r.stderr.decode('utf-8', errors='replace')[-2000:]}"
    assert out.count("✓ ") == len(selftest.CHECKS)
    assert f"итог: {len(selftest.CHECKS)}/{len(selftest.CHECKS)} ✓" in out
