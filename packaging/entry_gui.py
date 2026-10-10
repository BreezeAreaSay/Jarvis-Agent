"""Точка входа Jarvis.exe (оконный, без консоли): без аргументов — `jarvis run`.

Под оконным exe sys.stdout/sys.stderr — None: печатать нельзя, всё — в лог (jarvis.log).
"""

import sys

from jarvis.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["run"]))
