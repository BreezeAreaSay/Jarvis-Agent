"""Сравнение уже канонических путей без ввода-вывода: для политики, которая не трогает диск.

Пути приходят из preview абсолютными и с раскрытыми ссылками; здесь только разбор на части с учётом
разделителей и регистра ОС.
"""

from typing import Literal

OsFamily = Literal["windows", "posix"]


def _parts(path: str, os_family: OsFamily) -> list[str]:
    if os_family == "windows":
        path = path.replace("/", "\\").casefold()
        return [part for part in path.split("\\") if part]
    return [part for part in path.split("/") if part]


def is_absolute(path: str, os_family: OsFamily) -> bool:
    """Абсолютный путь этой ОС: `/…` или `C:\\…` и `\\\\server\\share…` (Windows)."""
    if os_family == "posix":
        return path.startswith("/")
    path = path.replace("/", "\\")
    drive = len(path) >= 3 and path[0].isalpha() and path[1:3] == ":\\"
    return drive or path.startswith("\\\\")


def unsupported_form(path: str, os_family: OsFamily) -> bool:
    """Путь Windows, который нельзя честно сравнить с зонами по строке: `\\\\?\\…`, `\\\\.\\…`, UNC
    (`\\\\server\\share`, в том числе `\\\\localhost\\C$`) и двоеточие после буквы диска (поток NTFS
    `file::$DATA`). Политика такие пути запрещает, инструменты их не принимают."""
    if os_family != "windows":
        return False
    path = path.replace("/", "\\")
    return path.startswith("\\\\") or ":" in path[2:]


def is_within(path: str, root: str, os_family: OsFamily) -> bool:
    """Путь совпадает с корнем или лежит внутри него (по частям, а не по префиксу строки)."""
    path_parts, root_parts = _parts(path, os_family), _parts(root, os_family)
    return path_parts[: len(root_parts)] == root_parts


def name_of(path: str, os_family: OsFamily) -> str:
    """Последняя часть пути с исходным регистром."""
    separator = "\\" if os_family == "windows" else "/"
    if os_family == "windows":
        path = path.replace("/", "\\")
    parts = [part for part in path.split(separator) if part]
    return parts[-1] if parts else ""
