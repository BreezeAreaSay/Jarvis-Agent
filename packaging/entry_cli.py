"""Точка входа jarvis-cli.exe (консольный): весь CLI и скрытая подкоманда `mcp` — сервер pc для мозга.

Рантайм-хук PySide6 для этого exe убран в jarvis.spec (он импортирует QtCore при каждом старте). Здесь — его
нужная часть без импорта Qt: где лежат плагины Qt (platforms, styles) внутри бандла.
"""

import os
import sys

# Qt нужен только этим подкомандам; остальным (mcp — сервер мозга, ask, doctor…) переменную не ставим:
# её унаследуют программы, которые они открывают, и чужой Qt загрузит наши плагины.
QT_COMMANDS = {"selftest", "run"}


def _qt_env() -> None:
    meipass = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not meipass:
        return
    if not QT_COMMANDS.intersection(sys.argv[1:2]):
        return
    qt_dir = (
        os.path.join(meipass, "PySide6")
        if sys.platform == "win32"
        else os.path.join(meipass, "PySide6", "Qt")
    )
    os.environ.setdefault("QT_PLUGIN_PATH", os.path.join(qt_dir, "plugins"))


_qt_env()

from jarvis.cli import main  # noqa: E402 — после настройки окружения Qt

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
