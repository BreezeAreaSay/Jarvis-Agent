"""Классы данных результата инструмента — по происхождению (ADR 0028).

Источники: объявленный вид результата (`ToolDefinition.output_data`), пути из preview и пути в самом
результате, зоны политики (секреты), `cloud.private_roots` и личные папки пользователя. Модель в этом не
участвует: класс ставит Jarvis там, где данные входят в задачу.

- путь в `private_roots` (ресурс вызова или путь в результате) → `private`;
- зона секретов или имя файла секрета (`.env`, `*.pem` …) → `secrets`;
- содержимое файла кода (по расширению или имени) → ещё и `source_code`;
- путь в личной папке (Документы, Рабочий стол, Загрузки …) → `personal_data`.

Проверяется весь результат по порядку — строки и ключи, без предела: модель видит начало результата, и
класс не может потеряться из-за длины. В коротких строках (командная строка процесса, текст ошибки) пути
ищутся и внутри.
"""

import re
from collections.abc import Iterator, Sequence

from pydantic import JsonValue

from jarvis.core.policy import PolicyZones
from jarvis.domain.paths import is_absolute, is_within, name_of
from jarvis.domain.privacy import CODE_EXTENSIONS, CODE_FILE_NAMES, DataClass
from jarvis.domain.tools import EffectKind, ToolDefinition, ToolPreview, ToolResult

EMBEDDED_SCAN_CHARS = 2048  # в строках не длиннее пути ищутся и внутри; длинное — содержимое файла
_EMBEDDED = {
    "posix": re.compile(r"(?<![\w.~:/-])/[^\s\"'<>|,;]+"),
    "windows": re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s\"'<>|,;]*"),
}


class DataClassifier:
    def __init__(
        self, zones: PolicyZones, *, private_roots: Sequence[str] = (), personal_roots: Sequence[str] = ()
    ) -> None:
        """Корни — канонические пути той же ОС, что и зоны (их строит сборка приложения)."""
        self._zones = zones
        self._private = tuple(private_roots)
        self._personal = tuple(personal_roots)

    def classify(
        self, definition: ToolDefinition, preview: ToolPreview, result: ToolResult | None
    ) -> frozenset[DataClass]:
        found = set(definition.output_data)
        content = DataClass.FILE_CONTENT in definition.output_data
        resources = [effect.resource for effect in preview.effects if effect.kind is EffectKind.READ]
        for path in resources:
            if not self._is_path(path):
                continue
            found |= self._path_classes(path)
            if content and self._is_code(path):
                found.add(DataClass.SOURCE_CODE)
        if result is not None:
            for text in _strings(result.output):
                for path in self._paths(text):
                    found |= self._path_classes(path)
        return frozenset(found)

    def _paths(self, text: str) -> Iterator[str]:
        if self._is_path(text):
            yield text
        elif len(text) <= EMBEDDED_SCAN_CHARS and ("/" in text or "\\" in text):
            for match in _EMBEDDED[self._zones.os_family].finditer(text):
                yield match.group(0)

    def _path_classes(self, path: str) -> set[DataClass]:
        found: set[DataClass] = set()
        family = self._zones.os_family
        if any(is_within(path, root, family) for root in self._private):
            found.add(DataClass.PRIVATE)
        if self._zones.is_secret(path):
            found.add(DataClass.SECRETS)
        if any(is_within(path, root, family) for root in self._personal):
            found.add(DataClass.PERSONAL_DATA)
        return found

    def _is_path(self, value: str) -> bool:
        return is_absolute(value, self._zones.os_family)

    def _is_code(self, path: str) -> bool:
        name = name_of(path, self._zones.os_family).casefold()
        stem, dot, extension = name.rpartition(".")
        return (bool(dot) and bool(stem) and f".{extension}" in CODE_EXTENSIONS) or name in CODE_FILE_NAMES


def _strings(value: JsonValue) -> Iterator[str]:
    """Все строки результата и ключи объектов — в порядке документа (как их видит модель)."""
    stack: list[JsonValue] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            for key, inner in reversed(list(item.items())):
                stack.extend((inner, key))
