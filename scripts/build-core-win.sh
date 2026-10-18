#!/usr/bin/env bash
# Готовит ядро Jarvis для Windows-установщика прямо на Linux, без Wine:
#   build/core-win/python/  — CPython 3.12 для Windows (пакет NuGet «python») + колёса зависимостей win_amd64
#   build/core-win/app/     — пакет jarvis (исходники + .pyc, байткод один для Windows и Linux при той же версии Python)
# Колёса ставит uv под платформу Windows (маркеры sys_platform == 'win32' выбирают pywin32, pycaw, winrt…).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/build/core-win"
PY_VERSION="${PY_VERSION:-3.12.10}"
CACHE="${JARVIS_BUILD_CACHE:-$ROOT/build/cache}"
UV="${UV:-uv}"
command -v "$UV" >/dev/null || UV="$HOME/.local/bin/uv"

mkdir -p "$CACHE"
rm -rf "$OUT"
mkdir -p "$OUT"

echo "==> Python $PY_VERSION для Windows (NuGet)"
NUPKG="$CACHE/python.$PY_VERSION.nupkg"
if [ ! -s "$NUPKG" ]; then
  curl -fsSL -o "$NUPKG.part" "https://api.nuget.org/v3-flatcontainer/python/$PY_VERSION/python.$PY_VERSION.nupkg"
  mv "$NUPKG.part" "$NUPKG"
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
(cd "$TMP" && unzip -q "$NUPKG" 'tools/*')
mv "$TMP/tools" "$OUT/python"
# тесты, idle, tkinter и прочее, что ядру не нужно (−40 МБ)
rm -rf "$OUT/python/Lib/test" "$OUT/python/Lib/idlelib" "$OUT/python/Lib/tkinter" "$OUT/python/Lib/turtledemo" \
  "$OUT/python/Lib/ensurepip" "$OUT/python/tcl" "$OUT/python/DLLs/_tkinter.pyd" "$OUT/python/DLLs/tcl86t.dll" \
  "$OUT/python/DLLs/tk86t.dll" "$OUT/python/include" "$OUT/python/libs"

echo "==> Зависимости (колёса win_amd64)"
REQ="$TMP/requirements.txt"
(cd "$ROOT/core" && "$UV" export --frozen --no-dev --no-hashes --no-emit-project --no-header -o "$REQ" >/dev/null)
"$UV" pip install \
  --python-platform x86_64-pc-windows-msvc \
  --python-version 3.12 \
  --only-binary :all: \
  --target "$OUT/python/Lib/site-packages" \
  -r "$REQ"

# pywin32 без postinstall: DLL кладём рядом с python.exe, как это сделал бы pywin32_postinstall
if [ -d "$OUT/python/Lib/site-packages/pywin32_system32" ]; then
  cp "$OUT/python/Lib/site-packages/pywin32_system32/"*.dll "$OUT/python/"
fi

echo "==> Пакет jarvis"
mkdir -p "$OUT/app"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/core/jarvis" "$OUT/app/"
PYTHON_HOST="${PYTHON_HOST:-python3.12}"
if command -v "$PYTHON_HOST" >/dev/null; then
  "$PYTHON_HOST" -m compileall -q -j 0 "$OUT/app/jarvis" || true
fi

# удалить кэши байткода зависимостей другой платформы и лишнее (обёртки консольных скриптов — ядру не нужны)
rm -rf "$OUT/python/Lib/site-packages/Scripts" "$OUT/python/Lib/site-packages/bin"
find "$OUT/python/Lib/site-packages" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$OUT/python/Lib/site-packages" -path '*/tests/*' -name '*.py' -delete 2>/dev/null || true

du -sh "$OUT/python" "$OUT/app" | sed 's/^/    /'
echo "==> Готово: $OUT"
