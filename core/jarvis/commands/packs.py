"""Паки команд: готовые наборы для программ (Spotify, OBS, Photoshop…) в jarvis/resources/packs/*.json.

Файл пака: {id, name, app (процесс для переключения, «Spotify.exe»), icon, description, commands: [...]};
команды — как в docs/CONTRACTS.md §2, id внутри пака короткий («next») — в ядре он становится
«pack.<пак>.<id>».
Установленные паки — settings.packs.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jarvis.commands.model import Command
from jarvis.commands.textutil import slugify

log = logging.getLogger(__name__)


@dataclass
class Pack:
    id: str
    name: str
    app: str
    icon: str
    description: str
    commands: list[Command] = field(default_factory=list)

    def to_json(self, installed: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "app": self.app,
            "icon": self.icon,
            "description": self.description,
            "commands_count": len(self.commands),
            "installed": installed,
            "commands": [{"id": c.id, "name": c.name, "phrases": list(c.phrases)} for c in self.commands],
        }


def parse_pack(raw: dict[str, Any], fallback_id: str = "") -> Pack:
    """JSON пака → Pack; неверный формат — ValueError с причиной."""
    pack_id = str(raw.get("id") or fallback_id).strip()
    if not pack_id:
        raise ValueError("у пака нет id")
    icon = str(raw.get("icon") or "")
    commands: list[Command] = []
    seen: set[str] = set()
    for i, item in enumerate(raw.get("commands") or [], 1):
        if not isinstance(item, dict):
            raise ValueError(f"команда {i}: ожидается объект")
        short = str(item.get("id") or slugify(str(item.get("name", "")))).strip()
        full = f"pack.{pack_id}.{short}"
        if full in seen:
            raise ValueError(f"повтор id команды «{short}»")
        seen.add(full)
        try:
            command = Command.model_validate(
                {**item, "id": full, "category": "packs", "icon": item.get("icon") or icon, "enabled": True}
            )
        except ValidationError as e:
            raise ValueError(f"команда «{short}»: {e.errors()[0].get('msg', e)}") from e
        if not command.name or not command.phrases or not command.actions:
            raise ValueError(f"команда «{short}»: нужны name, phrases и actions")
        commands.append(command)
    if not commands:
        raise ValueError("в паке нет команд")
    return Pack(
        pack_id,
        str(raw.get("name") or pack_id),
        str(raw.get("app") or ""),
        icon,
        str(raw.get("description") or ""),
        commands,
    )


class PackStore:
    """Все паки из каталога ресурсов (читаются один раз)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._packs: dict[str, Pack] = {}
        self.errors: dict[str, str] = {}
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                pack = parse_pack(raw, path.stem)
            except (OSError, ValueError) as e:
                self.errors[path.name] = str(e)
                log.error("пак %s не читается: %s", path.name, e)
                continue
            if pack.id in self._packs:
                self.errors[path.name] = f"повтор id пака «{pack.id}»"
                log.error("пак %s: повтор id «%s»", path.name, pack.id)
                continue
            self._packs[pack.id] = pack

    def all(self) -> list[Pack]:
        return sorted(self._packs.values(), key=lambda p: p.name.casefold())

    def get(self, pack_id: str) -> Pack | None:
        return self._packs.get(pack_id)
