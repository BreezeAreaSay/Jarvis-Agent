"""Упаковка: PyInstaller spec, установщик Inno Setup, workflow сборки, scripts/build.ps1, иконка .ico.

Собрать exe и установщик здесь нельзя (это CI на Windows) — проверяем статически: нужные директивы на месте,
опасных нет. Иконка рисуется по-настоящему (офскрин, в подпроцессе).
"""

import ast
import importlib.util
import ntpath
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "jarvis.spec"
ISS = ROOT / "packaging" / "jarvis.iss"
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
BUILD_PS1 = ROOT / "scripts" / "build.ps1"
MAKE_ICON = ROOT / "packaging" / "make_icon.py"


# --- PyInstaller spec ---------------------------------------------------------------------------------------


def _spec_tree() -> ast.Module:
    return ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC))


def _calls(name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(_spec_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _kw(call: ast.Call) -> dict[str, object]:
    return {k.arg: ast.literal_eval(k.value) for k in call.keywords if isinstance(k.value, ast.Constant)}


def _spec_value(name: str) -> object:
    """Значение верхнеуровневого присваивания spec (литерал или выражение без внешних имён)."""
    for node in _spec_tree().body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return eval(compile(ast.Expression(node.value), str(SPEC), "eval"), {"__builtins__": {}})
    raise AssertionError(f"в spec нет {name}")


def test_spec_two_exe_one_collect() -> None:
    exes = {_kw(c)["name"]: _kw(c) for c in _calls("EXE")}
    assert set(exes) == {"Jarvis", "jarvis-cli"}
    assert exes["Jarvis"]["console"] is False
    assert exes["jarvis-cli"]["console"] is True
    for kw in exes.values():
        assert kw["exclude_binaries"] is True  # onedir: двоичные файлы — в COLLECT, не внутри exe
        assert kw["upx"] is False
        assert kw["contents_directory"] == "_internal"
    collects = _calls("COLLECT")
    assert len(collects) == 1
    assert "name=APP_NAME" in ast.unparse(collects[0])
    assert _spec_value("APP_NAME") == "Jarvis"  # папка build/dist/Jarvis
    args = [ast.unparse(a) for a in collects[0].args]
    assert "exe_gui" in args and "exe_cli" in args
    assert not _calls("MERGE"), "MERGE даёт exe семантику onefile — медленный старт"


def test_spec_entry_points() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert 'analysis("entry_gui.py")' in text
    assert 'analysis("entry_cli.py")' in text
    gui = (ROOT / "packaging" / "entry_gui.py").read_text(encoding="utf-8")
    cli = (ROOT / "packaging" / "entry_cli.py").read_text(encoding="utf-8")
    assert 'main(sys.argv[1:] or ["run"])' in gui
    assert "main(sys.argv[1:])" in cli
    for src in (gui, cli):
        ast.parse(src)


def test_spec_collects_codex_and_mcp() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert 'collect_all("codex_cli_bin")' in text
    assert 'copy_metadata("mcp")' in text  # mcp.server.fastmcp читает версию при импорте
    assert 'copy_metadata("openai-codex")' in text
    assert 'copy_metadata("jarvis")' not in text  # direct_url.json с путём дерева сборки — не в бандл
    assert 'distribution("jarvis")' in text
    for name in ('"jarvis"', '"pc"', '"openai_codex"', '"mcp"', '"pycaw"', '"comtypes"'):
        assert f"collect_submodules({name}" in text, name
    for module in ("win32job", "pythoncom", "win32com.shell.shell", "send2trash.win.modern"):
        assert f'"{module}"' in text, module


def test_spec_data_files() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert '(os.path.join(ROOT, "scripts", "start_hands.cmd"), "scripts")' in text
    assert '(os.path.join(ROOT, "jarvis.example.toml"), ".")' in text
    assert '(os.path.join(ROOT, "bench", "phrases.ru.jsonl"), "bench")' in text
    assert '(os.path.join(ROOT, "tests", "fixtures", "apps.json"), os.path.join("tests", "fixtures"))' in text
    # es.exe — только если он есть в рабочем дереве; кладётся рядом с exe (pc.settings.es_path)
    assert 'ES = os.path.join(ROOT, "bin", "es.exe")' in text
    assert "if os.path.isfile(ES):" in text
    for rel in ("scripts/start_hands.cmd", "jarvis.example.toml", "tests/fixtures/apps.json"):
        assert (ROOT / rel).is_file(), rel


def test_spec_excludes_heavy_qt() -> None:
    qt = set(_spec_value("QT_EXCLUDES"))  # type: ignore[arg-type]
    need = {
        "QtWebEngineCore",
        "QtWebEngineWidgets",
        "QtQml",
        "QtQuick",
        "QtQuickWidgets",
        "Qt3DCore",
        "QtMultimedia",
        "QtCharts",
        "QtDataVisualization",
        "QtPdf",
        "QtSql",
        "QtTest",
        "QtNetwork",
        "QtOpenGL",
        "QtOpenGLWidgets",
        "QtSvg",
    }
    assert {f"PySide6.{n}" for n in need} <= qt
    assert not {"PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"} & qt
    py = set(_spec_value("PY_EXCLUDES"))  # type: ignore[arg-type]
    assert {"tkinter", "unittest", "lib2to3", "pytest"} <= py
    assert not {"multiprocessing", "asyncio", "email", "json"} & py


def _keep_fn():
    """Функция _keep из spec вместе с её константами DROP_*."""
    nodes = [
        node
        for node in _spec_tree().body
        if (isinstance(node, ast.FunctionDef) and node.name == "_keep")
        or (isinstance(node, ast.Assign | ast.AugAssign) and ast.unparse(node).startswith("DROP_"))
    ]
    ns: dict = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SPEC), "exec"), ns)
    return ns["_keep"]


@pytest.mark.parametrize(
    ("dest", "kept"),
    [
        ("PySide6/Qt6Core.dll", True),
        ("PySide6/Qt6Widgets.dll", True),
        ("PySide6/plugins/platforms/qwindows.dll", True),
        ("PySide6/plugins/platforms/qoffscreen.dll", True),
        ("PySide6/plugins/styles/qmodernwindowsstyle.dll", True),
        ("PySide6/plugins/imageformats/qico.dll", True),
        ("PySide6/translations/qtbase_ru.qm", True),
        ("codex_cli_bin/bin/codex.exe", True),
        ("PySide6/opengl32sw.dll", False),
        ("PySide6/plugins/imageformats/qsvg.dll", False),
        ("PySide6/plugins/imageformats/qpdf.dll", False),
        ("PySide6/plugins/iconengines/qsvgicon.dll", False),
        ("PySide6/Qt6Pdf.dll", False),
        ("PySide6/Qt6Svg.dll", False),
        ("PySide6/translations/qtbase_de.qm", False),
        ("PySide6/Qt/lib/libQt6Svg.so.6", False),
        ("jarvis-1.0.0.dist-info/METADATA", True),
        ("jarvis-1.0.0.dist-info/direct_url.json", False),
        ("jarvis-1.0.0.dist-info/uv_cache.json", False),
        ("mcp-1.30.0.dist-info/METADATA", True),
    ],
)
def test_spec_drops_unused_qt_files(dest: str, kept: bool) -> None:
    keep = _keep_fn()
    assert keep(dest) is kept
    assert keep(dest.replace("/", "\\")) is kept


def test_spec_cli_without_pyside_runtime_hook() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert 'cli_scripts = [entry for entry in a_cli.scripts if entry[0] != "pyi_rth_pyside6"]' in text
    cli = next(c for c in _calls("EXE") if _kw(c)["name"] == "jarvis-cli")
    assert ast.unparse(cli.args[1]) == "cli_scripts"
    gui = next(c for c in _calls("EXE") if _kw(c)["name"] == "Jarvis")
    assert ast.unparse(gui.args[1]) == "a_gui.scripts"
    assert "QT_PLUGIN_PATH" in (ROOT / "packaging" / "entry_cli.py").read_text(encoding="utf-8")


def test_spec_version_from_pyproject() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert 'tomllib.load(f)["project"]["version"]' in text
    assert 'version=version_info("Jarvis"' in text and 'version=version_info("jarvis-cli"' in text


# --- Inno Setup ---------------------------------------------------------------------------------------------


def _iss_text() -> str:
    raw = ISS.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "jarvis.iss — UTF-8 с BOM (кириллица в старых ISCC)"
    return raw.decode("utf-8-sig")


def _iss_sections() -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in _iss_text().splitlines():
        m = re.fullmatch(r"\[(\w+)\]", line.strip())
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
        elif current and line.strip() and not line.lstrip().startswith(";"):
            sections[current].append(line.strip())
    return sections


def _iss_setup() -> dict[str, str]:
    return dict(line.split("=", 1) for line in _iss_sections()["Setup"])


def test_iss_setup_directives() -> None:
    setup = _iss_setup()
    expected = {
        "PrivilegesRequired": "lowest",
        "DefaultDirName": r"{localappdata}\Programs\Jarvis",
        "DisableProgramGroupPage": "yes",
        "OutputBaseFilename": "Jarvis-Setup-{#Version}",
        "SetupIconFile": "{#IconFile}",
        "OutputDir": "{#OutputDir}",
        "UninstallDisplayIcon": r"{app}\{#AppExe}",
        "Compression": "lzma2/ultra64",
        "SolidCompression": "yes",
        "ArchitecturesAllowed": "x64compatible",
        "ArchitecturesInstallIn64BitMode": "x64compatible",
        "CloseApplications": "yes",
        "RestartApplications": "no",
        "AppVersion": "{#Version}",
    }
    for key, value in expected.items():
        assert setup.get(key) == value, key
    assert re.fullmatch(r"\{\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", setup["AppId"])
    assert "PrivilegesRequiredOverridesAllowed" not in setup  # без предложения поставить «для всех» с UAC


def test_iss_defines_documented() -> None:
    text = _iss_text()
    assert '#define AppExe "Jarvis.exe"' in text
    assert '#define CliExe "jarvis-cli.exe"' in text
    assert re.search(r"#ifndef Version\s+#error", text), "без /DVersion сборка должна падать"
    for name in ("SourceDir", "OutputDir", "IconFile"):
        assert f"#ifndef {name}" in text, name
    header = text.split("#ifndef Version")[0]
    for name in ("Version", "SourceDir", "OutputDir", "IconFile"):
        assert f"/D{name}=" in header, f"/D{name} не описан в шапке"


def test_iss_files_icons_run_language() -> None:
    s = _iss_sections()
    assert s["Languages"] == ['Name: "russian"; MessagesFile: "compiler:Languages\\Russian.isl"']
    assert s["Files"] == [
        'Source: "{#SourceDir}\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs'
    ]
    assert any(line.startswith('Name: "{userprograms}\\Jarvis"') for line in s["Icons"])
    run = " ".join(s["Run"])
    assert 'Filename: "{app}\\{#AppExe}"' in run and 'Description: "Запустить Jarvis"' in run
    assert "Flags: postinstall nowait skipifsilent" in run


def test_iss_autostart_task() -> None:
    s = _iss_sections()
    assert any('Name: "autostart"' in t and "Запускать при входе в Windows" in t for t in s["Tasks"])
    assert '#define RunKey "Software\\Microsoft\\Windows\\CurrentVersion\\Run"' in _iss_text()
    on = [r for r in s["Registry"] if "Tasks: autostart" in r]
    off = [r for r in s["Registry"] if "Tasks: not autostart" in r]
    assert len(on) == 1 and len(off) == 1
    assert 'Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "Jarvis"' in on[0]
    assert 'ValueData: """{app}\\{#AppExe}"""' in on[0]
    assert "uninsdeletevalue" in on[0]
    assert "ValueType: none" in off[0] and "deletevalue uninsdeletevalue" in off[0]


def test_iss_run_value_matches_winapp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Значение Run от установщика — то же, что пишет `jarvis-cli autostart on` в exe (имя и команда)."""
    from jarvis import winapp
    from pc import settings

    app = r"C:\Users\me\AppData\Local\Programs\Jarvis"
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    monkeypatch.setattr(settings, "install_dir", lambda: Path(app))
    assert winapp.RUN_VALUE == "Jarvis"
    assert ntpath.normcase(winapp.autostart_command()) == ntpath.normcase(f'"{app}\\Jarvis.exe"')


def test_iss_stops_processes_without_tree_kill() -> None:
    text = _iss_text()
    code = text.split("[Code]", 1)[1]
    assert "function PrepareToInstall(var NeedsRestart: Boolean): String;" in code
    assert "StopJarvis();" in code.split("function PrepareToInstall")[1]
    uninstall = _iss_sections()["UninstallRun"]
    assert len(uninstall) == 1 and 'Filename: "{sys}\\taskkill.exe"' in uninstall[0]
    assert "runhidden" in uninstall[0]
    kills = re.findall(r"taskkill\.exe['\"][^\n]*", text)
    assert len(kills) == 2
    for line in kills:
        assert "/F /IM {#AppExe} /IM {#CliExe}" in line
        # /T убил бы и приложения, которые человек открыл через Jarvis (Chrome, Telegram)
        assert "/T" not in line


def test_iss_keeps_data_dir() -> None:
    text = _iss_text()
    sections = _iss_sections()
    assert "UninstallDelete" not in sections
    assert "InstallDelete" not in sections
    assert "DelTree" not in text
    for marker in ("JarvisData", "JARVIS_DATA_DIR}", r"{localappdata}\Jarvis"):
        assert marker not in "\n".join(line for lines in sections.values() for line in lines), marker


# --- workflow сборки ----------------------------------------------------------------------------------------


def test_build_workflow_triggers_and_permissions() -> None:
    text = BUILD_YML.read_text(encoding="utf-8")
    assert "branches: [jarvis-v1]" in text
    assert 'tags: ["v*"]' in text
    assert "workflow_dispatch:" in text
    assert "runs-on: windows-latest" in text
    assert re.search(r"permissions:\n\s+contents: read\n", text)
    assert "timeout-minutes:" in text


def test_build_workflow_steps_in_order() -> None:
    text = BUILD_YML.read_text(encoding="utf-8")
    steps = [
        "uses: actions/checkout@v4",
        "uses: astral-sh/setup-uv@v6",
        "uv sync --locked --group build",
        'uv run --no-sync pytest -q -m "not live"',
        "python packaging/make_icon.py",
        "pyinstaller packaging/jarvis.spec --noconfirm --distpath build/dist --workpath build/work",
        r"build\dist\Jarvis\jarvis-cli.exe selftest",
        "GITHUB_STEP_SUMMARY",
        "choco install innosetup -y --no-progress",
        '"/DVersion=$version" "/DSourceDir=',
        "Compress-Archive -Path build\\dist\\Jarvis",
        "uses: actions/upload-artifact@v4",
    ]
    positions = [text.find(step) for step in steps]
    assert -1 not in positions, [s for s, p in zip(steps, positions, strict=True) if p == -1]
    assert positions == sorted(positions)
    selftest_step = text.split("name: selftest бандла", 1)[1].split("- name:", 1)[0]
    assert "QT_QPA_PLATFORM: offscreen" in selftest_step
    artifact = text.split("uses: actions/upload-artifact@v4", 1)[1]
    assert "name: Jarvis-Setup" in artifact
    assert "Jarvis-Setup-*.exe" in artifact and "onedir.zip" in artifact
    for name in ("/DOutputDir=", "/DIconFile=", "packaging\\jarvis.iss"):
        assert name in text


# --- scripts/build.ps1 --------------------------------------------------------------------------------------


def test_build_ps1_ascii_only() -> None:
    data = BUILD_PS1.read_bytes()
    bad = sorted({b for b in data if b > 127})
    assert not bad, "в build.ps1 не-ASCII: Windows PowerShell 5.1 без BOM ломает кириллицу"


def test_build_ps1_structure() -> None:
    text = BUILD_PS1.read_text(encoding="ascii")
    assert '$ErrorActionPreference = "Stop"' in text
    assert "[switch]$SkipTests" in text
    for part in (
        "uv sync --locked --group build",
        'pytest -q -m "not live"',
        "packaging/make_icon.py",
        "pyinstaller packaging/jarvis.spec",
        '"jarvis-cli.exe") selftest',
        '"/DVersion=$version" "/DSourceDir=$Dist" "/DOutputDir=$OutDir" "/DIconFile=$Icon"',
        "winget install JRSoftware.InnoSetup --scope user",
        "choco install innosetup",
        r'"$env:LOCALAPPDATA\Programs"',
        "${env:ProgramFiles(x86)}",
        'Join-Path $base "Inno Setup 6\\ISCC.exe"',
    ):
        assert part in text, part


def test_build_ps1_checks_every_native_exit_code() -> None:
    lines = [line.strip() for line in BUILD_PS1.read_text(encoding="ascii").splitlines()]
    native = [i for i, line in enumerate(lines) if re.match(r"^(\$\w+ = )?(uv |& )", line)]
    assert len(native) >= 6
    for i in native:
        follow = next(line for line in lines[i + 1 :] if line)
        assert follow.startswith("Assert-ExitCode"), f"после «{lines[i]}» нет проверки $LASTEXITCODE"


# --- иконка -------------------------------------------------------------------------------------------------


def _make_icon_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_icon_under_test", MAKE_ICON)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _png_blob(size: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", size, size) + b"\0" * 20


def _dib_blob(size: int) -> bytes:
    return struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0) + b"\0" * (
        size * size * 4
    )


def test_ico_container_roundtrip() -> None:
    mi = _make_icon_module()
    data = mi.build_ico([(16, _dib_blob(16)), (48, _dib_blob(48)), (256, _png_blob(256))])
    assert struct.unpack_from("<HHH", data, 0) == (0, 1, 3)
    assert data[6 + 16 * 2] == 0  # 256 px в каталоге записывается как 0
    assert mi.parse_ico(data) == [16, 48, 256]


def test_ico_parse_rejects_broken() -> None:
    mi = _make_icon_module()
    good = mi.build_ico([(32, _dib_blob(32))])
    with pytest.raises(ValueError):
        mi.parse_ico(good[:40])  # данные за концом файла
    with pytest.raises(ValueError):
        mi.parse_ico(b"\0\0\2\0\1\0" + good[6:])  # тип 2 — это .cur, не .ico
    with pytest.raises(ValueError):
        mi.parse_ico(mi.build_ico([(32, _dib_blob(16))]))  # каталог врёт о размере


def test_make_icon_writes_all_sizes(tmp_path: Path) -> None:
    out = tmp_path / "jarvis.ico"
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, str(MAKE_ICON), "--out", str(out)],
        capture_output=True,
        timeout=120,
        env=env,
        cwd=ROOT,
        check=False,
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", errors="replace")
    mi = _make_icon_module()
    assert mi.parse_ico(out.read_bytes()) == [16, 20, 24, 32, 40, 48, 64, 128, 256]
    assert "временный рисунок" not in r.stderr.decode("utf-8", errors="replace"), (
        "нет jarvis.ui.icon.render_icon"
    )
