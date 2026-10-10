# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: onedir-бандл Jarvis — одна папка, два exe с общим COLLECT.
#   Jarvis.exe     — оконный (console=False): без аргументов `jarvis run` (packaging/entry_gui.py);
#   jarvis-cli.exe — консольный: весь CLI и скрытая подкоманда `mcp` — сервер pc для мозга (packaging/entry_cli.py).
# Имена Jarvis.exe и jarvis.exe в Windows совпадают — поэтому второй называется jarvis-cli.exe.
#
# Сборка из корня репозитория (Windows, Linux, Windows-Python под wine — пути только через SPECPATH/os.path):
#   uv run --group build pyinstaller packaging/jarvis.spec --noconfirm --distpath build/dist --workpath build/work
# Результат: build/dist/Jarvis/{Jarvis.exe, jarvis-cli.exe, _internal/…}; проверка — `jarvis-cli.exe selftest`.
#
# Почему два Analysis и один COLLECT, а не MERGE: MERGE в PyInstaller 6 даёт exe семантику onefile (распаковка
# зависимостей во временную папку при каждом старте) — медленно. Два Analysis + общий COLLECT — обычный onedir:
# одинаковые файлы в COLLECT сливаются по пути назначения, у каждого exe свой встроенный PYZ.
#
# Раскладка: sys._MEIPASS = <папка>\_internal = pc.settings.app_root(): там scripts\start_hands.cmd,
# jarvis.example.toml, bench\phrases.ru.jsonl и codex_cli_bin\bin\codex.exe (bundled_codex_path() из
# codex_cli_bin считает путь от своего __file__ — он в _MEIPASS\codex_cli_bin). bin\es.exe кладётся рядом
# с exe (pc.settings.es_path() ищет install_dir()\bin\es.exe) — только если он есть в рабочем дереве.
import os
import shutil
import sys
import tomllib

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

IS_WIN = sys.platform == "win32"
ROOT = os.path.dirname(os.path.abspath(SPECPATH))
SRC = os.path.join(ROOT, "src")
APP_NAME = "Jarvis"


def _warn(text: str) -> None:
    # сообщения сборки — ASCII: stderr Windows-Python под pipe/wine бывает в cp1252, кириллица уронила бы сборку
    print(f"WARNING: {text}", file=sys.stderr)


with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]

# --- данные бандла (относительно _MEIPASS) ---------------------------------------------------------------
DATA_FILES = [
    (os.path.join(ROOT, "scripts", "start_hands.cmd"), "scripts"),
    (os.path.join(ROOT, "jarvis.example.toml"), "."),
    (os.path.join(ROOT, "bench", "phrases.ru.jsonl"), "bench"),
    # инвентарь приложений для `jarvis-cli bench` (jarvis.bench берёт его через app_root(), а не с ПК)
    (os.path.join(ROOT, "tests", "fixtures", "apps.json"), os.path.join("tests", "fixtures")),
]
datas = []
for src, dest in DATA_FILES:
    if os.path.isfile(src):
        datas.append((src, dest))
    else:
        _warn(f"missing data file {src} (selftest will fail)")

# codex.exe и его ресурсы (codex-resources, codex-path\rg.exe) — как в пакете, вместе с метаданными
cb_datas, cb_binaries, cb_hidden = collect_all("codex_cli_bin")
datas += cb_datas
datas += collect_data_files("openai_codex") + copy_metadata("openai-codex")
# mcp.server.fastmcp при импорте читает importlib.metadata.version("mcp")
datas += copy_metadata("mcp")
# Версия Jarvis для selftest/doctor (importlib.metadata.version("jarvis")) — только METADATA: в dist-info
# editable-установки ещё direct_url.json с путём к дереву сборки, его в бандл не берём.
try:
    from importlib.metadata import distribution

    _dist = distribution("jarvis")
    _meta = next(f for f in (_dist.files or []) if f.name == "METADATA" and f.parent.name.endswith(".dist-info"))
    datas.append((str(_dist.locate_file(_meta)), str(_meta.parent)))
except Exception as e:  # PackageNotFoundError/StopIteration: сборка из дерева без `uv sync`
    _warn(f"no jarvis package metadata ({e!r}); version will be '?'")

hiddenimports = [
    *collect_submodules("jarvis"),
    *collect_submodules("pc"),
    *collect_submodules("openai_codex"),
    *collect_submodules("mcp", filter=lambda name: not name.startswith(("mcp.cli", "mcp.client.websocket"))),
    *cb_hidden,
]
if IS_WIN:
    hiddenimports += [
        # pywin32 (хуки PyInstaller для pythoncom/pywintypes/win32com есть)
        "win32api",
        "win32con",
        "win32gui",
        "win32process",
        "win32clipboard",
        "win32event",
        "win32job",
        "winerror",
        "pythoncom",
        "pywintypes",
        "win32com",
        "win32com.client",
        "win32com.shell.shell",
        "win32com.shell.shellcon",
        "win32com.server.policy",
        # send2trash выбирает реализацию на лету (modern — IFileOperation через pywin32, legacy — ctypes)
        "send2trash.win",
        "send2trash.win.modern",
        "send2trash.win.legacy",
        "send2trash.win.IFileOperationProgressSink",
    ]
    # pycaw описывает COM-интерфейсы явно и создаёт объекты через comtypes.CoCreateInstance — генерация
    # модулей в comtypes.gen (comtypes.client.GetModule) не нужна. Если она всё же случится, comtypes во frozen
    # сам пишет кэш в %TEMP%\comtypes_cache\<exe>-312.
    hiddenimports += collect_submodules("pycaw")
    hiddenimports += collect_submodules("comtypes", filter=lambda name: not name.startswith("comtypes.test"))

# --- что не берём ------------------------------------------------------------------------------------------
# Окно и трей — QtCore/QtGui/QtWidgets. Иконка рисуется кодом (QtSvg не нужен), OpenGL/QML/сеть Qt не нужны.
QT_EXCLUDES = [
    f"PySide6.{name}"
    for name in (
        "Qt3DAnimation Qt3DCore Qt3DExtras Qt3DInput Qt3DLogic Qt3DRender QtAxContainer QtBluetooth QtCharts "
        "QtConcurrent QtDBus QtDataVisualization QtDesigner QtGraphs QtGraphsWidgets QtHelp QtHttpServer "
        "QtLabsStyleKit QtLocation QtMultimedia QtMultimediaWidgets QtNetwork QtNetworkAuth QtNfc QtOpenGL "
        "QtOpenGLWidgets QtPdf QtPdfWidgets QtPositioning QtPrintSupport QtQml QtQmlFeatures QtQuick QtQuick3D "
        "QtQuickControls2 QtQuickTest QtQuickWidgets QtRemoteObjects QtScxml QtSensors QtSerialBus QtSerialPort "
        "QtSpatialAudio QtSql QtStateMachine QtSvg QtSvgWidgets QtTest QtTextToSpeech QtUiTools QtWebChannel "
        "QtWebEngineCore QtWebEngineQuick QtWebEngineWidgets QtWebSockets QtWebView QtXml"
    ).split()
]
PY_EXCLUDES = [
    "tkinter",
    "_tkinter",
    "unittest",
    "lib2to3",
    "pydoc_data",
    "test",
    "idlelib",
    "ensurepip",
    "venv",
    "distutils",
    "setuptools",
    "pkg_resources",
    "pip",
    "pytest",
    "_pytest",
    "pluggy",
    "iniconfig",
    "pygments",
    "PyInstaller",
    "PySide2",
    "PyQt5",
    "PyQt6",
]

# Файлы Qt, которые хуки берут «на всякий случай»: программный OpenGL (окно рисуется растром), плагины
# картинок/иконок, тянущие Qt6Pdf/Qt6Svg, и переводы Qt (кроме русских). PyInstaller сам добавляет dist-info
# пакетов, чью версию читает код (importlib.metadata.version("jarvis") в selftest), — из них убираем пути сборки.
DROP_BASENAMES = {"opengl32sw.dll", "d3dcompiler_47.dll"}
DROP_PLUGIN_PREFIXES = ("qpdf", "qsvg", "qtiff", "qwebp", "qicns", "qtga", "qwbmp", "libqpdf", "libqsvg")
DROP_PLUGIN_PREFIXES += ("libqtiff", "libqwebp", "libqicns", "libqtga", "libqwbmp")
DROP_LIB_PREFIXES = ("qt6pdf", "qt6svg", "libqt6pdf", "libqt6svg")
# служебные файлы установщиков пакетов: в direct_url.json editable-установки — путь к дереву сборки
DROP_DIST_INFO = {"direct_url.json", "uv_cache.json", "uv_build.json"}


def _keep(dest: str) -> bool:
    parts = dest.replace("\\", "/").lower().split("/")
    base = parts[-1]
    if base in DROP_BASENAMES:
        return False
    if "imageformats" in parts or "iconengines" in parts:
        return not base.startswith(DROP_PLUGIN_PREFIXES) and not base.startswith(("qsvgicon", "libqsvgicon"))
    if base.startswith(DROP_LIB_PREFIXES):
        return False
    if "translations" in parts and base.endswith(".qm"):
        return base.endswith("_ru.qm")
    if len(parts) > 1 and parts[-2].endswith(".dist-info") and base in DROP_DIST_INFO:
        return False
    return True


def _filtered(toc):
    return [entry for entry in toc if _keep(entry[0])]


def analysis(script: str) -> Analysis:
    a = Analysis(
        [os.path.join(SPECPATH, script)],
        pathex=[SRC],
        binaries=cb_binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=QT_EXCLUDES + PY_EXCLUDES,
        noarchive=False,
        optimize=0,
    )
    a.binaries = _filtered(a.binaries)
    a.datas = _filtered(a.datas)
    return a


# --- иконка и версия exe (только Windows) ------------------------------------------------------------------
ICON = os.path.join(ROOT, "build", "jarvis.ico")
icon = None
if IS_WIN:
    if os.path.isfile(ICON):
        icon = ICON
    else:
        _warn(f"missing {ICON}: exe without icon (run packaging/make_icon.py)")


def version_info(exe_name: str, description: str):
    """VERSIONINFO exe из [project].version pyproject.toml (только Windows)."""
    if not IS_WIN:
        return None
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    nums = tuple(([int(x) for x in VERSION.split(".") if x.isdigit()] + [0, 0, 0, 0])[:4])
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "041904B0",
                        [
                            StringStruct("ProductName", APP_NAME),
                            StringStruct("FileDescription", description),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("ProductVersion", VERSION),
                            StringStruct("InternalName", exe_name),
                            StringStruct("OriginalFilename", f"{exe_name}.exe"),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0419, 1200])]),
        ],
    )


# --- сборка ------------------------------------------------------------------------------------------------
a_gui = analysis("entry_gui.py")
a_cli = analysis("entry_cli.py")

exe_gui = EXE(
    PYZ(a_gui.pure),
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="Jarvis",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon,
    version=version_info("Jarvis", "Jarvis — помощник"),
    contents_directory="_internal",
)
# Рантайм-хук PySide6 при КАЖДОМ старте exe импортирует PySide6.QtCore (~0,1–0,2 с) — а jarvis-cli чаще всего
# сервер mcp или короткая команда без Qt. Для jarvis-cli хук убираем; его единственная нужная часть (путь к
# плагинам Qt) — в entry_cli.py без импорта Qt. Встроенный qt.conf PySide6 из PyPI создаёт сам при импорте.
# Проверяют это пункты selftest «Qt: плагины», «окно», «иконка» — они идут именно под jarvis-cli.
cli_scripts = [entry for entry in a_cli.scripts if entry[0] != "pyi_rth_pyside6"]

exe_cli = EXE(
    PYZ(a_cli.pure),
    cli_scripts,
    [],
    exclude_binaries=True,
    name="jarvis-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=icon,
    version=version_info("jarvis-cli", "Jarvis — командная строка"),
    contents_directory="_internal",
)
coll = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

# es.exe (Everything CLI) — рядом с exe, если лежит в рабочем дереве (в git его нет)
ES = os.path.join(ROOT, "bin", "es.exe")
if os.path.isfile(ES):
    os.makedirs(os.path.join(DISTPATH, APP_NAME, "bin"), exist_ok=True)
    shutil.copy2(ES, os.path.join(DISTPATH, APP_NAME, "bin", "es.exe"))
    print(f"es.exe copied to {os.path.join(DISTPATH, APP_NAME, 'bin')}")
