"""Инвентарь компьютера для прямых команд: приложения, которые можно запустить, и известные папки
(ADR 0030). Запуск — только того, что есть в инвентаре: произвольного пути или команды из текста нет."""

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

AppKind = Literal["shortcut", "executable", "desktop_entry"]


class KnownFolder(StrEnum):
    HOME = "home"
    DOWNLOADS = "downloads"
    DOCUMENTS = "documents"
    DESKTOP = "desktop"
    PICTURES = "pictures"
    MUSIC = "music"
    VIDEOS = "videos"


class AppEntry(BaseModel, frozen=True, extra="forbid"):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")  # «google-chrome»
    name: str = Field(min_length=1)  # как в меню «Пуск»: «Google Chrome»
    aliases: list[str] = []  # другие имена: «хром», «chrome»
    target: str = Field(min_length=1)  # что открыть: ярлык, исполняемый файл, desktop-файл
    kind: AppKind


def name_key(text: str) -> str:
    """Ключ для сравнения имён приложений и папок: регистр, «ё», знаки ™®©, кавычки, дефисы и
    подчёркивания не важны. Одинаковый в Router и в инструменте запуска."""
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"[™®©\"«»'`]", "", text)
    text = re.sub(r"[-_]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
