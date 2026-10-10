#!/usr/bin/env bash
# Запасная Windows-линия Jarvis без Windows: настоящий Windows-Python 3.12 под Wine в Linux-контейнере.
# Нужна, когда CI (GitHub Actions windows-latest) недоступен. Подробности и ограничения — docs/wine-build.md.
#
#   scripts/wine_build.sh setup        Wine, префикс, Windows-Python, колёса из uv.lock, Inno Setup (вне репозитория)
#   scripts/wine_build.sh test [args]  pytest -m "not live" под Windows-Python (+ аргументы pytest)
#   scripts/wine_build.sh build        иконка → PyInstaller (onedir) → jarvis-cli.exe selftest → zip папки onedir
#   scripts/wine_build.sh installer    Inno Setup → Jarvis-Setup-<версия>.exe → тихая установка/selftest/удаление
#   scripts/wine_build.sh all          setup, test, build, installer (падения тестов не останавливают сборку)
#   scripts/wine_build.sh py [args]    Windows-Python под Wine с тем же окружением (для отладки)
#
# Переменные: WINBUILD (/opt/winbuild), WINE_LANG (ru_RU.UTF-8 — кодовые страницы 1251/866, как у владельца;
# C.UTF-8 — 1252/437, как windows-latest), WINE_USER (me), CLEAN=1 (PyInstaller --clean),
# ALLOW_SELFTEST_FAIL=1 (не останавливать build на ✗ selftest).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

WINBUILD="${WINBUILD:-/opt/winbuild}"
export WINEPREFIX="${WINEPREFIX:-$WINBUILD/prefix}"
export WINEARCH=win64
export WINEDEBUG="${WINEDEBUG:--all}"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree,mshtml=}" # без диалогов установки Mono/Gecko
WINE_LANG="${WINE_LANG:-ru_RU.UTF-8}"
export LANG="$WINE_LANG" LC_ALL="$WINE_LANG" # UTF-8-локаль Linux: иначе Wine не создаёт файлы с кириллицей
export USER="${WINE_USER:-me}" LOGNAME="${WINE_USER:-me}" # имя пользователя Windows (C:\users\<имя>)
export WINEPATH='C:\winbuild\bin'                          # git.exe-шим для tests/test_hygiene.py
export PYTHONPYCACHEPREFIX='C:\winbuild\pycache'           # .pyc Windows-Python — вне репозитория
unset PYTHONUTF8 PYTHONIOENCODING PYTHONPATH PYTHONHOME VIRTUAL_ENV

PY_VERSION=3.12.10
INNO_VERSION=6.7.3
ICU_VERSION=72.1.0.3 # только для Wine: в Windows 10 1903+ icuuc.dll системная, Qt 6.12 её импортирует
ICU_MAJOR=72
NUGET=https://api.nuget.org/v3-flatcontainer

DL="$WINBUILD/dl"
WORK="$WINBUILD/work"
CROOT="$WINEPREFIX/drive_c/winbuild" # C:\winbuild
WPY_DIR="$CROOT/python"
SITE="$WPY_DIR/Lib/site-packages"
WPY='C:\winbuild\python\python.exe'
RUNNER='C:\winbuild\runner\utf8run.py'
ISCC='C:\winbuild\innosetup\ISCC.exe'
OUT="$REPO/build/win"
REPO_W="Z:${REPO//\//\\}"
XVFB_PID=""

# --- вывод -------------------------------------------------------------------------------------------------
if [[ -t 1 ]]; then C_STEP=$'\033[1;36m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_OK=$'\033[1;32m' C_0=$'\033[0m'
else C_STEP="" C_WARN="" C_ERR="" C_OK="" C_0=""; fi
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_0"; }
info() { printf '    %s\n' "$*"; }
ok() { printf '%s    ✓ %s%s\n' "$C_OK" "$*" "$C_0"; }
warn() { printf '%s    ! %s%s\n' "$C_WARN" "$*" "$C_0" >&2; }
die() {
    printf '%sОШИБКА: %s%s\n' "$C_ERR" "$*" "$C_0" >&2
    exit 1
}

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }

# --- Wine --------------------------------------------------------------------------------------------------
# stdout/stderr Windows-процесса — только в pipe: с обычным файлом Wine даёт «Invalid handle» при старте Python.
w() { wine "$@" </dev/null 2>&1 | cat; }
# Windows-Python: stdout/stderr — UTF-8 только у этого процесса (кодировка локали и дочерних — как в Windows)
wpy() { w "$WPY" "$RUNNER" "$@"; }
wpath() { printf 'Z:%s' "${1//\//\\}"; } # абсолютный путь Linux → путь Wine

start_display() {
    # Setup.exe Inno Setup создаёт окно даже с /VERYSILENT — без X-сервера падает (ошибка 1400); на Xvfb
    # и окна Qt/Win32 в тестах ведут себя ближе к рабочему столу windows-latest.
    [[ -n "${DISPLAY:-}" ]] && return 0
    command -v Xvfb >/dev/null || die "нет Xvfb — запусти: scripts/wine_build.sh setup"
    local n
    for n in $(seq 90 130); do
        [[ -e /tmp/.X11-unix/X$n || -e /tmp/.X$n-lock ]] && continue
        Xvfb ":$n" -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
        XVFB_PID=$!
        for _ in $(seq 100); do
            [[ -S /tmp/.X11-unix/X$n ]] && break
            kill -0 "$XVFB_PID" 2>/dev/null || break
            sleep 0.1
        done
        if [[ -S /tmp/.X11-unix/X$n ]]; then
            export DISPLAY=":$n"
            return 0
        fi
        kill "$XVFB_PID" 2>/dev/null || true
        XVFB_PID=""
    done
    die "не удалось запустить Xvfb"
}

cleanup() {
    if command -v wineserver >/dev/null && [[ -d "$WINEPREFIX" ]]; then wineserver -k 2>/dev/null || true; fi
    if [[ -n "$XVFB_PID" ]]; then kill "$XVFB_PID" 2>/dev/null || true; fi
}

project_version() {
    python3 -I -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
        "$REPO/pyproject.toml"
}

human_size() { du -sh "$1" | cut -f1; }
mib() { awk -v b="$(stat -c %s "$1")" 'BEGIN { printf "%.1f MiB", b / 1048576 }'; }

# --- setup -------------------------------------------------------------------------------------------------
ensure_packages() {
    local missing=() need_i386=0
    command -v wine >/dev/null || missing+=(wine64 wine)
    # Inno Setup 6 (ISCC.exe, Setup.exe) — 32-битные программы: нужен wine32
    if [[ ! -x /usr/lib/wine/wine ]]; then
        missing+=(wine32:i386)
        need_i386=1
    fi
    command -v x86_64-w64-mingw32-gcc >/dev/null || missing+=(gcc-mingw-w64-x86-64)
    command -v Xvfb >/dev/null || missing+=(xvfb)
    command -v unzip >/dev/null || missing+=(unzip)
    command -v curl >/dev/null || missing+=(curl)
    command -v locale-gen >/dev/null || missing+=(locales)
    command -v uv >/dev/null || die "нет uv (https://docs.astral.sh/uv/) — он ставит колёса из uv.lock"
    if ((${#missing[@]})); then
        step "apt: ${missing[*]}"
        [[ $EUID -eq 0 ]] || die "нужны пакеты ${missing[*]} — запусти setup от root"
        if ((need_i386)) && ! dpkg --print-foreign-architectures | grep -qx i386; then
            dpkg --add-architecture i386
        fi
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}" >/dev/null
        ok "пакеты установлены"
    fi
    local want have
    want="$(printf '%s' "$WINE_LANG" | tr '[:upper:]' '[:lower:]' | tr -d '-')"
    have="$(locale -a 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -d '-')"
    if ! grep -qx "$want" <<<"$have"; then
        [[ $EUID -eq 0 ]] || die "нет локали $WINE_LANG — запусти setup от root (locale-gen)"
        locale-gen "$WINE_LANG" >/dev/null
        ok "локаль $WINE_LANG"
    fi
}

init_prefix() {
    if [[ ! -f "$WINEPREFIX/system.reg" ]]; then
        step "Wine-префикс $WINEPREFIX"
        mkdir -p "$(dirname "$WINEPREFIX")"
        w wineboot --init >/dev/null || die "wineboot --init"
        wineserver -w
        ok "$(wine --version) — префикс создан"
    fi
    mkdir -p "$CROOT"/{bin,runner,logs} "$DL" "$WORK"
}

# fetch <id> <версия> → путь к .nupkg (скачивает один раз; .nupkg — это zip)
fetch() {
    local id="$1" ver="$2" f="$DL/$1.$2.nupkg"
    if [[ ! -s "$f" ]] || ! unzip -tqq "$f" >/dev/null 2>&1; then
        info "скачиваю $id $ver" >&2
        curl -fsSL --retry 3 -o "$f.part" "$NUGET/$id/$ver/$id.$ver.nupkg" || die "не скачался $id $ver"
        mv "$f.part" "$f"
    fi
    printf '%s' "$f"
}

# unpack_tools <nupkg> <папка> <версия>: содержимое tools/ пакета → папка (пусто или другая версия — заново)
unpack_tools() {
    local pkg="$1" dest="$2" ver="$3" tmp
    [[ -f "$dest/.version" && "$(cat "$dest/.version")" == "$ver" ]] && return 0
    [[ -s "$pkg" ]] || die "нет пакета для $dest"
    tmp="$(mktemp -d "$WORK/unpack.XXXXXX")"
    unzip -q -o "$pkg" 'tools/*' -d "$tmp"
    rm -rf "$dest"
    mv "$tmp/tools" "$dest"
    rm -rf "$tmp"
    printf '%s' "$ver" >"$dest/.version"
}

install_python() {
    unpack_tools "$(fetch python "$PY_VERSION")" "$WPY_DIR" "$PY_VERSION"
    [[ -f "$WPY_DIR/python.exe" ]] || die "нет python.exe в $WPY_DIR"
}

install_inno() {
    unpack_tools "$(fetch tools.innosetup "$INNO_VERSION")" "$CROOT/innosetup" "$INNO_VERSION"
    [[ -f "$CROOT/innosetup/ISCC.exe" && -f "$CROOT/innosetup/Languages/Russian.isl" ]] ||
        die "в Tools.InnoSetup $INNO_VERSION нет ISCC.exe или Languages\\Russian.isl"
}

# Qt6Core.dll (PySide6 6.12) импортирует системную icuuc.dll Windows 10 1903+ (ucnv_* без суффикса версии).
# В Wine её нет — кладём в system32 префикса шим: icuuc.dll пересылает имена на icuuc72.dll из NuGet
# Microsoft.ICU.ICU4C.Runtime. PyInstaller не берёт DLL из %WINDIR%, поэтому в бандл шим не попадает.
install_icu_shim() {
    local s32="$WINEPREFIX/drive_c/windows/system32" src="$WORK/icu"
    [[ -f "$s32/icuuc.dll" && -f "$s32/icuuc$ICU_MAJOR.dll" && -f "$s32/icudt$ICU_MAJOR.dll" ]] && return 0
    step "ICU для Qt под Wine (шим icuuc.dll → icuuc$ICU_MAJOR.dll)"
    rm -rf "$src" && mkdir -p "$src"
    unzip -q -j -o "$(fetch microsoft.icu.icu4c.runtime.win-x64 "$ICU_VERSION")" \
        "runtimes/win-x64/native/*.dll" -d "$src"
    {
        echo "LIBRARY icuuc.dll"
        echo "EXPORTS"
        x86_64-w64-mingw32-objdump -p "$src/icuuc$ICU_MAJOR.dll" | awk -v v="_$ICU_MAJOR" -v m="icuuc$ICU_MAJOR" \
            '/^\t\[ *[0-9]+\] / && $NF ~ ("^[A-Za-z_][A-Za-z0-9_]*" v "$") { n = $NF; sub(v "$", "", n); print "  " n "=" m "." $NF }'
    } >"$src/icuuc.def"
    cat >"$src/dllmain.c" <<'EOF'
/* Шим только для Wine: системные имена ICU (как в icuuc.dll Windows) пересылаются в ICU из NuGet. */
#include <windows.h>
BOOL WINAPI DllMainCRTStartup(HINSTANCE h, DWORD r, LPVOID p) { (void)h; (void)r; (void)p; return TRUE; }
EOF
    x86_64-w64-mingw32-gcc -shared -nostdlib -O2 -o "$src/icuuc.dll" "$src/dllmain.c" "$src/icuuc.def" \
        -Wl,-e,DllMainCRTStartup -lkernel32
    cp "$src/icuuc.dll" "$src/icuuc$ICU_MAJOR.dll" "$src/icudt$ICU_MAJOR.dll" "$s32/"
    ok "$(grep -c '=' "$src/icuuc.def") функций ICU"
}

# tests/test_hygiene.py зовёт `git ls-files ...`; git для Windows в префиксе нет, а Linux-git из Wine
# не запустить с захватом вывода. Шим печатает список, снятый Linux-git перед тестами (git_snapshot).
GIT_LS_ARGS=(ls-files --cached --others --exclude-standard)
install_git_shim() {
    local exe="$CROOT/bin/git.exe" src="$WORK/git-shim.c"
    cat >"$src" <<'EOF'
/* git.exe для Wine: знает только `git ls-files --cached --others --exclude-standard` (снимок Linux-git). */
#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    const char *want[] = {"ls-files", "--cached", "--others", "--exclude-standard"};
    int same = argc == 5;
    for (int i = 0; same && i < 4; i++) same = strcmp(argv[i + 1], want[i]) == 0;
    if (argc == 2 && strcmp(argv[1], "--version") == 0) { puts("git version 0.0.0 (wine shim)"); return 0; }
    if (!same) { fputs("git-shim (wine): only `git ls-files --cached --others --exclude-standard`\n", stderr); return 129; }
    FILE *f = fopen("C:\\winbuild\\git-ls-files.txt", "rb");
    if (!f) { fputs("git-shim (wine): no C:\\winbuild\\git-ls-files.txt\n", stderr); return 128; }
    _setmode(_fileno(stdout), _O_BINARY);
    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) fwrite(buf, 1, n, stdout);
    fclose(f);
    return 0;
}
EOF
    if [[ ! -f "$exe" || "$src" -nt "$exe" ]]; then
        x86_64-w64-mingw32-gcc -O2 -o "$exe" "$src" || die "git-шим не собрался"
    fi
}

git_snapshot() {
    if git -C "$REPO" "${GIT_LS_ARGS[@]}" >"$CROOT/git-ls-files.txt" 2>/dev/null; then return 0; fi
    : >"$CROOT/git-ls-files.txt"
    warn "git ls-files не сработал — tests/test_hygiene.py увидит пустой список"
}

install_runner() {
    cat >"$CROOT/runner/utf8run.py" <<'EOF'
"""Запуск `-c код`, `-m модуль ...` или `скрипт.py ...` под Windows-Python: stdout/stderr ЭТОГО процесса — UTF-8 (журнал
читается в Linux), кодовая страница локали и окружение дочерних процессов не меняются (как в Windows)."""

import os
import runpy
import sys

for stream in (sys.stdout, sys.stderr):
    if stream is not None:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
args = sys.argv[1:]
if args[:1] == ["-c"] and len(args) > 1:
    sys.argv = ["-c", *args[2:]]
    sys.path[0] = ""  # как у `python -c`
    exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
elif args[:1] == ["-m"] and len(args) > 1:
    sys.argv = [args[1], *args[2:]]
    sys.path[0] = os.getcwd()  # как у `python -m`
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
elif args:
    sys.argv = args
    sys.path[0] = os.path.dirname(os.path.abspath(args[0]))
    runpy.run_path(args[0], run_name="__main__")
else:
    sys.exit("utf8run: -c код | -m модуль [аргументы] | скрипт.py [аргументы]")
EOF
    cat >"$CROOT/runner/smoke.py" <<'EOF'
"""Проверка окружения Windows-Python под Wine: импорты, окно Qt, codex.exe из SDK."""

import importlib
import subprocess
import sys

MODULES = (
    "PySide6.QtWidgets win32api win32gui win32job win32process win32clipboard pythoncom pywintypes comtypes "
    "comtypes.client pycaw.pycaw psutil rapidfuzz httpx send2trash mcp.server.fastmcp openai_codex codex_cli_bin "
    "pytest PyInstaller jarvis pc"
).split()
bad = []
for name in MODULES:
    try:
        importlib.import_module(name)
    except Exception as e:  # noqa: BLE001 — печатаем всё, что не импортировалось
        bad.append(f"{name}: {e!r}")
from PySide6.QtWidgets import QApplication, QLabel

app = QApplication([])
label = QLabel("Jarvis")
label.resize(120, 40)
img = label.grab().toImage()
import codex_cli_bin

codex = subprocess.run([str(codex_cli_bin.bundled_codex_path()), "--version"], capture_output=True, timeout=120)
print(f"Python {sys.version.split()[0]} {sys.platform}; Qt {app.platformName()} {img.width()}x{img.height()}; "
      f"{codex.stdout.decode(errors='replace').strip() or codex.returncode}; модулей {len(MODULES) - len(bad)}/{len(MODULES)}")
for line in bad:
    print("НЕ ИМПОРТИРУЕТСЯ", line)
sys.exit(1 if bad or codex.returncode or img.isNull() else 0)
EOF
}

install_wheels() {
    local req="$WORK/requirements-win.txt" stamp="$SITE/.jarvis-requirements.sha256" sum
    (cd "$REPO" && uv export --frozen --all-groups --no-emit-project --no-hashes --format requirements.txt --quiet) |
        grep -v -E '^ruff==' >"$req.new" || die "uv export не сработал (uv.lock на месте?)"
    mv "$req.new" "$req"
    sum="$(grep -v '^ *#' "$req" | sha256sum | cut -d' ' -f1)"
    if [[ -f "$stamp" && "$(cat "$stamp")" == "$sum" ]]; then return 0; fi
    step "Колёса Windows из uv.lock (группы dev и build, без ruff)"
    uv pip install --quiet --python-platform x86_64-pc-windows-msvc --python-version 3.12 \
        --target "$SITE" -r "$req" || die "uv pip install (колёса win_amd64)"
    printf '%s' "$sum" >"$stamp"
    ok "$(grep -c '==' "$req") пакетов"
}

# Проект — как editable-установка uv sync: .pth на src репозитория + dist-info (версия для importlib.metadata)
sync_project() {
    local ver dist
    ver="$(project_version)"
    printf '%s\\src\n' "$REPO_W" >"$SITE/jarvis-src.pth"
    dist="$SITE/jarvis-$ver.dist-info"
    if [[ ! -f "$dist/METADATA" ]]; then
        rm -rf "$SITE"/jarvis-*.dist-info
        mkdir -p "$dist"
        printf 'Metadata-Version: 2.1\nName: jarvis\nVersion: %s\n' "$ver" >"$dist/METADATA"
        printf '[console_scripts]\njarvis = jarvis.cli:main\n' >"$dist/entry_points.txt"
        printf 'wine_build.sh\n' >"$dist/INSTALLER"
        : >"$dist/RECORD"
    fi
}

require_setup() {
    if ! command -v wine >/dev/null || [[ ! -f "$WPY_DIR/python.exe" || ! -f "$SITE/.jarvis-requirements.sha256" ]]; then
        die "окружение Wine не готово — запусти: scripts/wine_build.sh setup"
    fi
    install_wheels # uv.lock поменялся — доставить колёса (иначе ничего не делает)
    sync_project
}

cmd_setup() {
    step "Окружение Wine ($WINBUILD)"
    ensure_packages
    init_prefix
    install_python
    install_inno
    install_icu_shim
    install_git_shim
    install_runner
    install_wheels
    sync_project
    start_display
    step "Проверка Windows-Python"
    wpy "C:\\winbuild\\runner\\smoke.py" || die "Windows-Python под Wine не готов (см. выше)"
    ok "$(wine --version); Inno Setup $INNO_VERSION; префикс $WINEPREFIX"
}

# --- test --------------------------------------------------------------------------------------------------
cmd_test() {
    require_setup
    git_snapshot
    mkdir -p "$OUT"
    step "pytest под Windows-Python ($PY_VERSION, локаль $WINE_LANG, пользователь $USER)"
    local rc=0
    wpy -m pytest -q -m "not live" -p no:cacheprovider -rfEs "$@" | tee "$OUT/pytest.log" || rc=$?
    info "журнал: build/win/pytest.log"
    return "$rc"
}

# --- build -------------------------------------------------------------------------------------------------
cmd_build() {
    require_setup
    [[ -f packaging/jarvis.spec ]] || die "нет packaging/jarvis.spec"
    local ver dist="$OUT/dist/Jarvis" rc=0
    ver="$(project_version)"
    mkdir -p "$OUT"
    if [[ -f packaging/make_icon.py ]]; then
        step "Иконка (packaging/make_icon.py)"
        WINEQT_QPA_PLATFORM=offscreen wpy packaging/make_icon.py || die "make_icon.py"
    fi

    step "PyInstaller (onedir) — несколько минут"
    local args=(-m PyInstaller packaging/jarvis.spec --noconfirm --distpath build/win/dist --workpath build/win/work)
    [[ "${CLEAN:-0}" == 1 ]] && args+=(--clean)
    wpy "${args[@]}" >"$OUT/pyinstaller.log" || rc=$?
    if ((rc)); then
        tail -40 "$OUT/pyinstaller.log"
        die "PyInstaller: код $rc (журнал build/win/pyinstaller.log)"
    fi
    grep -E ' (WARNING|ERROR): ' "$OUT/pyinstaller.log" | sed 's/^[0-9]* /    /' | sort -u || true
    [[ -f "$dist/Jarvis.exe" && -f "$dist/jarvis-cli.exe" ]] || die "в $dist нет Jarvis.exe или jarvis-cli.exe"
    local leak
    leak="$(find "$dist" \( -iname 'icuuc*.dll' -o -iname 'icudt*.dll' -o -iname 'git.exe' \) -print)"
    [[ -z "$leak" ]] || die "в бандл попали шимы Wine: $leak"
    ok "onedir: build/win/dist/Jarvis — $(human_size "$dist"), файлов $(find "$dist" -type f | wc -l)"

    step "jarvis-cli.exe selftest (QT_QPA_PLATFORM=offscreen, как в build.yml)"
    rc=0
    WINEQT_QPA_PLATFORM=offscreen w "$(wpath "$dist/jarvis-cli.exe")" selftest | tee "$OUT/selftest.log" || rc=$?
    if ((rc)); then
        [[ "${ALLOW_SELFTEST_FAIL:-0}" == 1 ]] || die "selftest: код $rc (ALLOW_SELFTEST_FAIL=1 — продолжить)"
        warn "selftest: код $rc — продолжаю (ALLOW_SELFTEST_FAIL=1)"
    fi

    step "zip папки onedir"
    local zip="$OUT/Jarvis-onedir-$ver.zip"
    python3 -I - "$dist" "$zip" <<'EOF'
import os
import sys
import zipfile

src, dst = sys.argv[1], sys.argv[2]
base = os.path.dirname(src)
with zipfile.ZipFile(dst + ".part", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            z.write(path, os.path.relpath(path, base))
os.replace(dst + ".part", dst)
EOF
    ok "${zip#"$REPO"/} — $(mib "$zip")"
}

# --- installer ---------------------------------------------------------------------------------------------
RUN_KEY='HKCU\Software\Microsoft\Windows\CurrentVersion\Run'

run_value() {
    { w reg query "$RUN_KEY" /v Jarvis || true; } | tr -d '\r' | awk '$1 == "Jarvis" { $1 = $2 = ""; sub(/^ +/, ""); print }'
}

check_installer() {
    local setup="$1" la_w la app data start log
    la_w="$(w cmd /c 'echo %LOCALAPPDATA%' | tr -d '\r')"
    la="$(winepath -u "$la_w" 2>/dev/null)"
    app="$la/Programs/Jarvis"
    data="$la/Jarvis"
    start="$(winepath -u "$(w cmd /c 'echo %APPDATA%' | tr -d '\r')" 2>/dev/null)/Microsoft/Windows/Start Menu/Programs"
    log='C:\winbuild\logs'

    if [[ -f "$app/unins000.exe" ]]; then
        info "удаляю прошлую установку из префикса"
        w "$(wpath "$app/unins000.exe")" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART >/dev/null || true
        wineserver -w
    fi
    mkdir -p "$data"
    printf 'данные пользователя — удаление их не трогает\n' >"$data/wine-check.txt"

    step "Тихая установка с автозапуском ($la_w\\Programs\\Jarvis)"
    w "$(wpath "$setup")" /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /NORESTART /TASKS=autostart \
        "/LOG=$log\\install.log" || die "установка: код $? (журнал $CROOT/logs/install.log)"
    wineserver -w
    [[ -f "$app/Jarvis.exe" && -f "$app/jarvis-cli.exe" && -d "$app/_internal" && -f "$app/unins000.exe" ]] ||
        die "после установки нет файлов в $app"
    ok "файлы: $(find "$app" -type f | wc -l), $(human_size "$app")"
    [[ -f "$start/Jarvis.lnk" ]] || die "нет ярлыка $start/Jarvis.lnk"
    ok "ярлык в «Пуск»: Jarvis.lnk"
    local val
    val="$(run_value)"
    [[ "$val" == "\"$la_w\\Programs\\Jarvis\\Jarvis.exe\"" ]] || die "ключ Run: «$val»"
    ok "ключ Run: Jarvis = $val"
    WINEQT_QPA_PLATFORM=offscreen w "$(wpath "$app/jarvis-cli.exe")" selftest | tee "$OUT/selftest-installed.log" ||
        warn "selftest установленной копии: код ${PIPESTATUS[0]}"

    step "Повторная установка поверх, флажок автозапуска снят"
    w "$(wpath "$setup")" /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /NORESTART '/MERGETASKS=!autostart' \
        "/LOG=$log\\reinstall.log" || die "повторная установка: код $?"
    wineserver -w
    [[ -z "$(run_value)" ]] || die "флажок снят, а ключ Run остался: $(run_value)"
    ok "ключ Run убран"

    step "Тихое удаление"
    w "$(wpath "$app/unins000.exe")" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART "/LOG=$log\\uninstall.log" ||
        die "удаление: код $?"
    wineserver -w # деинсталлятор перезапускает себя из %TEMP% и сразу выходит
    [[ ! -e "$app" ]] || die "после удаления осталось: $(find "$app" | head -5)"
    [[ ! -e "$start/Jarvis.lnk" ]] || die "после удаления остался ярлык"
    [[ -f "$data/wine-check.txt" ]] || die "удаление задело каталог данных $data"
    ok "папка установки и ярлык удалены, каталог данных на месте"
}

cmd_installer() {
    require_setup
    [[ -f packaging/jarvis.iss ]] || die "нет packaging/jarvis.iss"
    local ver dist="$OUT/dist/Jarvis" setup rc=0
    ver="$(project_version)"
    [[ -f "$dist/Jarvis.exe" ]] || die "нет $dist/Jarvis.exe — сначала: scripts/wine_build.sh build"
    [[ -f build/jarvis.ico ]] || die "нет build/jarvis.ico — сначала: scripts/wine_build.sh build"
    setup="$OUT/Output/Jarvis-Setup-$ver.exe"
    rm -f "$setup"
    step "Inno Setup $INNO_VERSION → build/win/Output (сжатие lzma2/ultra64 — несколько минут)"
    w "$ISCC" /Q "/DVersion=$ver" "/DSourceDir=$REPO_W\\build\\win\\dist\\Jarvis" \
        "/DOutputDir=$REPO_W\\build\\win\\Output" "/DIconFile=$REPO_W\\build\\jarvis.ico" \
        "$REPO_W\\packaging\\jarvis.iss" || rc=$?
    ((rc == 0)) || die "ISCC: код $rc"
    [[ -f "$setup" ]] || die "ISCC отработал, а $setup нет"
    ok "${setup#"$REPO"/} — $(mib "$setup")"
    check_installer "$setup"
}

# --- all ---------------------------------------------------------------------------------------------------
cmd_all() {
    local trc=0 ver
    cmd_setup
    cmd_test || trc=$?
    ((trc == 0)) || warn "pytest: код $trc — сборка продолжается, разбор в build/win/pytest.log"
    cmd_build
    cmd_installer
    ver="$(project_version)"
    step "Итог"
    info "pytest:      $(grep -E '(passed|failed)' "$OUT/pytest.log" | tail -1)"
    info "onedir:      build/win/dist/Jarvis — $(human_size "$OUT/dist/Jarvis")"
    info "zip:         build/win/Jarvis-onedir-$ver.zip — $(mib "$OUT/Jarvis-onedir-$ver.zip")"
    info "установщик:  build/win/Output/Jarvis-Setup-$ver.exe — $(mib "$OUT/Output/Jarvis-Setup-$ver.exe")"
    return "$trc"
}

main() {
    local cmd="${1:-help}"
    (($#)) && shift
    case "$cmd" in
    setup | test | build | installer | all | py) ;;
    help | -h | --help)
        usage
        return 0
        ;;
    *)
        usage >&2
        return 2
        ;;
    esac
    trap cleanup EXIT
    if [[ "$cmd" != setup && "$cmd" != all ]] && command -v wineserver >/dev/null && [[ -d "$WINEPREFIX" ]]; then
        wineserver -k 2>/dev/null || true # процессы прошлого запуска могли остаться привязанными к старому Xvfb
    fi
    [[ "$cmd" == setup || "$cmd" == all ]] || start_display
    case "$cmd" in
    setup) cmd_setup ;;
    test) cmd_test "$@" ;;
    build) cmd_build ;;
    installer) cmd_installer ;;
    all) cmd_all ;;
    py)
        require_setup
        wpy "$@"
        ;;
    esac
}

main "$@"
