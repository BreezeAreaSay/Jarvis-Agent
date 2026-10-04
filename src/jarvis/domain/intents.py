"""Интенты прямых команд и разрешённые сущности (ADR 0026, ADR 0030).

Прямая команда исполняется без модели: Router распознаёт интент в тексте пользователя и разрешает его
сущности по доверенным источникам — инвентарю приложений, известным папкам, разбору URL, рабочей папке.
Аргументов из вывода модели здесь не бывает.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class IntentId(StrEnum):
    APP_LAUNCH = "app.launch"  # запустить приложение из инвентаря
    URL_OPEN = "url.open"  # открыть http(s)-адрес в браузере по умолчанию
    FOLDER_OPEN = "folder.open"  # открыть папку в проводнике
    FS_CURRENT = "fs.current"  # текущая рабочая папка
    FS_LIST = "fs.list"  # содержимое папки
    FS_SEARCH = "fs.search"  # найти файл по имени
    PROCESS_LIST = "process.list"  # запущенные процессы, можно с фильтром по имени


class EntityKind(StrEnum):
    APP = "app"  # ID приложения из инвентаря
    FOLDER = "folder"  # путь к папке (известная папка, рабочая папка или путь из команды)
    URL = "url"  # нормализованный http(s)-адрес
    PATTERN = "pattern"  # шаблон имени файла для поиска
    PROCESS = "process"  # подстрока имени процесса


class ResolvedEntity(BaseModel, frozen=True, extra="forbid"):
    kind: EntityKind
    value: str = Field(min_length=1)  # ID, путь, адрес, шаблон
    label: str = ""  # для человека: «Google Chrome», «Загрузки»
    source: str = Field(
        min_length=1
    )  # откуда: «inventory.alias», «folder.known», «command», «working_directory»
