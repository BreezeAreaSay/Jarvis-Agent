"""Столы — страницы главного экрана с виджетами (как в Luxify: «Главная», «Игры», «Работа»…).

Хранятся в таблице desks: id, position (порядок вкладок), data — JSON {name, icon, preset, widgets}.
При первом запуске создаётся «Главная». Голосом стол открывается фразами «открой стол Игры», «стол игры»,
«покажи стол работа» — для каждого стола регистрируется динамическая команда (провайдер "desks").
События: desks.changed {items}, desk.open {id, name}.
"""

from __future__ import annotations  # метод list() не должен заслонять list в аннотациях класса

import copy
import logging
import threading
from typing import TYPE_CHECKING, Any

from jarvis.db import dumps, loads, new_id
from jarvis.desks import layout, match, presets
from jarvis.service_base import BaseService
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

PROVIDER = "desks"
MAX_DESKS = 30
MAX_NAME = 40
CURRENT_KEY = "desks:current"
OPEN_PHRASES = ("открой стол {name}", "стол {name}", "покажи стол {name}", "переключи на стол {name}")


class DeskNotFoundError(LookupError):
    """Стола с таким id нет (API отвечает 404)."""


def _clean_name(raw: Any) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise UserFacingError("Название стола не может быть пустым")
    if len(name) > MAX_NAME:
        raise UserFacingError(f"Название стола — не длиннее {MAX_NAME} символов")
    return name


def _clean_icon(raw: Any, default: str) -> str:
    icon = str(raw or "").strip()[:40]
    return icon or default


def _phrase_name(name: str) -> str:
    """Название для шаблона фразы: без фигурных скобок (это слоты) и лишних знаков."""
    return " ".join(name.replace("{", " ").replace("}", " ").lower().split())


class Desks(BaseService):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self._lock = threading.RLock()

    # --- жизненный цикл ---------------------------------------------------------------------------
    async def start(self) -> None:
        self.ensure_default()
        try:
            self.ctx.commands.register_provider(PROVIDER, self.dynamic_commands)
            self.ctx.commands.refresh_dynamic()
        except Exception:
            log.exception("столы: голосовые команды не зарегистрированы")

    # --- чтение -----------------------------------------------------------------------------------
    def ensure_default(self) -> None:
        """Первый запуск: стол «Главная»."""
        with self._lock:
            if self.ctx.db.query_one("SELECT 1 AS x FROM desks LIMIT 1") is not None:
                return
            home = presets.preset(presets.HOME_PRESET)
            assert home is not None
            desk = self._new_desk(home["name"], home["icon"], presets.HOME_PRESET, home["widgets"])
            self._write(desk, 0)
            log.info("создан стол «%s»", desk["name"])

    def list(self) -> list[dict[str, Any]]:
        self.ensure_default()
        rows = self.ctx.db.query("SELECT id, position, data FROM desks ORDER BY position, rowid")
        return [self._row_to_desk(row) for row in rows]

    def get(self, desk_id: str) -> dict[str, Any] | None:
        row = self.ctx.db.query_one("SELECT id, position, data FROM desks WHERE id=?", (desk_id,))
        return self._row_to_desk(row) if row else None

    def require(self, desk_id: str) -> dict[str, Any]:
        desk = self.get(desk_id)
        if desk is None:
            raise DeskNotFoundError(desk_id)
        return desk

    def presets(self) -> list[dict[str, Any]]:
        return presets.presets()

    def widget_types(self) -> list[dict[str, Any]]:
        return layout.catalog()

    def current_id(self) -> str | None:
        current = self.ctx.db.kv_get(CURRENT_KEY)
        if current and self.get(str(current)) is not None:
            return str(current)
        desks = self.list()
        return desks[0]["id"] if desks else None

    # --- изменение --------------------------------------------------------------------------------
    def create(self, name: str = "", preset: str = "empty", icon: str = "") -> dict[str, Any]:
        """Новый стол из заготовки (в конец списка). Имя занято — добавится номер: «Игры 2»."""
        preset_id = preset or "empty"
        template = presets.preset(preset_id)
        if template is None:
            raise UserFacingError(f"Нет такой заготовки стола: {preset_id}")
        with self._lock:
            existing = self.list()
            if len(existing) >= MAX_DESKS:
                raise UserFacingError(f"Столов уже {MAX_DESKS} — удалите ненужные")
            base = _clean_name(name or template["name"])
            desk = self._new_desk(
                self._unique_name(base, existing),
                _clean_icon(icon, template["icon"]),
                preset_id,
                template["widgets"],
            )
            position = max((d["position"] for d in existing), default=-1) + 1
            self._write(desk, position)
        self._changed()
        return self.require(desk["id"])

    def update(self, desk_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Изменить название, иконку, раскладку или место в списке (position)."""
        with self._lock:
            desk = self.require(desk_id)
            if "name" in patch and patch["name"] is not None:
                name = _clean_name(patch["name"])
                others = [d for d in self.list() if d["id"] != desk_id]
                if any(d["name"].casefold() == name.casefold() for d in others):
                    raise UserFacingError(f"Стол «{name}» уже есть")
                desk["name"] = name
            if "icon" in patch and patch["icon"] is not None:
                desk["icon"] = _clean_icon(patch["icon"], desk["icon"])
            if "widgets" in patch and patch["widgets"] is not None:
                desk["widgets"] = self._clean_widgets(patch["widgets"])
            position = desk["position"]
            if patch.get("position") is not None:
                position = self._move(desk_id, int(patch["position"]))
            self._write(desk, position)
        self._changed()
        return self.require(desk_id)

    def delete(self, desk_id: str) -> None:
        with self._lock:
            self.require(desk_id)
            if len(self.list()) <= 1:
                raise UserFacingError("Нельзя удалить последний стол")
            self.ctx.db.delete("desks", desk_id)
            self._renumber()
        self._changed()

    def save_all(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Сохранить все столы разом: порядок, названия, раскладки. Столов не из списка больше не будет."""
        if not items:
            raise UserFacingError("Нужен хотя бы один стол")
        if len(items) > MAX_DESKS:
            raise UserFacingError(f"Столов не больше {MAX_DESKS}")
        with self._lock:
            current = {d["id"]: d for d in self.list()}
            names: set[str] = set()
            prepared: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for raw in items:
                if not isinstance(raw, dict):
                    raise UserFacingError("Неверный формат стола")
                desk_id = str(raw.get("id") or "").strip()[:40]
                if desk_id in seen_ids:
                    desk_id = ""  # один id дважды — второй стол сохраняем как новый
                old = current.get(desk_id)
                name = _clean_name(raw.get("name") or (old["name"] if old else ""))
                if name.casefold() in names:
                    raise UserFacingError(f"Стол «{name}» встречается дважды")
                names.add(name.casefold())
                widgets_raw = raw.get("widgets")
                if widgets_raw is None:
                    widgets_raw = old["widgets"] if old else []
                desk_id = desk_id or new_id()
                seen_ids.add(desk_id)
                prepared.append(
                    {
                        "id": desk_id,
                        "name": name,
                        "icon": _clean_icon(raw.get("icon"), old["icon"] if old else "desk"),
                        "preset": str(raw.get("preset") or (old["preset"] if old else "empty"))[:40],
                        "widgets": self._clean_widgets(widgets_raw),
                    }
                )
            keep = {d["id"] for d in prepared}
            for desk_id in current:
                if desk_id not in keep:
                    self.ctx.db.delete("desks", desk_id)
            for position, desk in enumerate(prepared):
                self._write(desk, position)
        self._changed()
        return self.list()

    def add_widget(self, desk_id: str, widget: dict[str, Any]) -> dict[str, Any]:
        """Добавить виджет на свободное место (или на x, y из запроса, если там свободно)."""
        widget_type = str(widget.get("type") or "")
        if not layout.is_known_type(widget_type):
            raise UserFacingError(f"Неизвестный виджет: {widget_type or '—'}")
        with self._lock:
            desk = self.require(desk_id)
            if len(desk["widgets"]) >= layout.MAX_WIDGETS:
                raise UserFacingError("На столе уже слишком много виджетов")
            spec = layout.spec_for(widget_type)
            new = {
                "type": widget_type,
                "w": widget.get("w") or spec.w,
                "h": widget.get("h") or spec.h,
                "x": widget.get("x"),
                "y": widget.get("y"),
                "config": widget.get("config") if isinstance(widget.get("config"), dict) else spec.config,
            }
            new["id"] = layout.new_widget_id()
            desk["widgets"].append(layout.place(desk["widgets"], new))
            self._write(desk, desk["position"])
        self._changed()
        return self.require(desk_id)

    def update_widget(self, desk_id: str, widget_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Изменить настройки (config — слиянием), размер или место одного виджета."""
        with self._lock:
            desk = self.require(desk_id)
            widgets = desk["widgets"]
            target = next((w for w in widgets if w["id"] == widget_id), None)
            if target is None:
                raise DeskNotFoundError(widget_id)
            if isinstance(patch.get("config"), dict):
                target["config"] = {**target.get("config", {}), **patch["config"]}
            for key in ("x", "y", "w", "h"):
                if patch.get(key) is not None:
                    target[key] = patch[key]
            # передвинутый виджет встаёт куда просили, соседи уступают место
            desk["widgets"] = layout.compact(widgets, first=[widget_id])
            self._write(desk, desk["position"])
        self._changed()
        return self.require(desk_id)

    def remove_widget(self, desk_id: str, widget_id: str) -> dict[str, Any]:
        with self._lock:
            desk = self.require(desk_id)
            before = len(desk["widgets"])
            desk["widgets"] = [w for w in desk["widgets"] if w["id"] != widget_id]
            if len(desk["widgets"]) == before:
                raise DeskNotFoundError(widget_id)
            self._write(desk, desk["position"])
        self._changed()
        return self.require(desk_id)

    # --- открыть стол -----------------------------------------------------------------------------
    def find(self, query: str) -> dict[str, Any] | None:
        """Стол по id или по названию (нечётко): «игры», «стол игры», «игровой» → «Игры»."""
        text = str(query or "").strip()
        if not text:
            return None
        desk = self.get(text)
        if desk is not None:
            return desk
        desks = self.list()
        aliases = {d["id"]: presets.aliases_for(d.get("preset", "")) for d in desks}
        return match.best_match(text, desks, aliases)

    def open(self, desk_id_or_name: str) -> dict[str, Any] | None:
        """Показать стол в интерфейсе (событие desk.open). None — такого стола нет."""
        desk = self.find(desk_id_or_name)
        if desk is None:
            return None
        self.ctx.db.kv_set(CURRENT_KEY, desk["id"])
        self.ctx.bus.publish("desk.open", {"id": desk["id"], "name": desk["name"]})
        return desk

    # --- голосовые команды ------------------------------------------------------------------------
    def dynamic_commands(self) -> list[DynamicCommand]:
        commands: list[DynamicCommand] = []
        for desk in self.list():
            spoken = _phrase_name(desk["name"])
            if not spoken:
                continue
            command_id = f"{PROVIDER}:{desk['id']}"
            commands.append(
                DynamicCommand(
                    id=command_id,
                    title=f"Открыть стол «{desk['name']}»",
                    phrases=[phrase.format(name=spoken) for phrase in OPEN_PHRASES],
                    handler=self._open_handler(desk["id"], command_id),
                    category="desks",
                    icon="desk",
                )
            )
        return commands

    def _open_handler(self, desk_id: str, command_id: str) -> Any:
        async def handler(slots: dict[str, Any]) -> ExecResult:
            desk = self.open(desk_id) if self.get(desk_id) is not None else None
            if desk is None:
                return ExecResult(False, "Открыть стол", "Такого стола больше нет", icon="desk")
            return ExecResult(True, f"Открыт стол «{desk['name']}»", icon="desk", command_ids=[command_id])

        return handler

    # --- внутреннее -------------------------------------------------------------------------------
    def _new_desk(self, name: str, icon: str, preset: str, widgets: list[dict[str, Any]]) -> dict[str, Any]:
        fresh = [{**copy.deepcopy(w), "id": layout.new_widget_id()} for w in widgets]
        return {
            "id": new_id(),
            "name": name,
            "icon": icon,
            "preset": preset,
            "widgets": layout.compact(fresh),
        }

    def _clean_widgets(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise UserFacingError("Виджеты стола — это список")
        if len(raw) > layout.MAX_WIDGETS:
            raise UserFacingError(f"На столе не больше {layout.MAX_WIDGETS} виджетов")
        for item in raw:
            if not isinstance(item, dict):
                raise UserFacingError("Неверный формат виджета")
            widget_type = str(item.get("type") or "")
            if not layout.is_known_type(widget_type):
                raise UserFacingError(f"Неизвестный виджет: {widget_type or '—'}")
        return layout.compact(raw)

    @staticmethod
    def _unique_name(base: str, desks: list[dict[str, Any]]) -> str:
        taken = {d["name"].casefold() for d in desks}
        if base.casefold() not in taken:
            return base
        n = 2
        while f"{base} {n}".casefold() in taken:
            n += 1
        return f"{base} {n}"[:MAX_NAME]

    def _row_to_desk(self, row: dict[str, Any]) -> dict[str, Any]:
        data = loads(row["data"], {}) or {}
        widgets = data.get("widgets") if isinstance(data.get("widgets"), list) else []
        return {
            "id": row["id"],
            "name": str(data.get("name") or "Стол"),
            "icon": str(data.get("icon") or "desk"),
            "preset": str(data.get("preset") or "empty"),
            "position": int(row["position"]),
            "widgets": widgets,
        }

    def _write(self, desk: dict[str, Any], position: int) -> None:
        data = {k: desk[k] for k in ("name", "icon", "preset", "widgets")}
        self.ctx.db.upsert("desks", {"id": desk["id"], "position": int(position), "data": dumps(data)})

    def _renumber(self) -> None:
        rows = self.ctx.db.query("SELECT id FROM desks ORDER BY position, rowid")
        for position, row in enumerate(rows):
            self.ctx.db.update("desks", row["id"], {"position": position})

    def _move(self, desk_id: str, position: int) -> int:
        ids = [r["id"] for r in self.ctx.db.query("SELECT id FROM desks ORDER BY position, rowid")]
        ids.remove(desk_id)
        position = max(0, min(len(ids), position))
        ids.insert(position, desk_id)
        for index, other in enumerate(ids):
            if other != desk_id:
                self.ctx.db.update("desks", other, {"position": index})
        return position

    def _changed(self) -> None:
        self.ctx.bus.publish("desks.changed", {"items": self.list()})
        try:
            self.ctx.commands.refresh_dynamic()
        except Exception:
            log.exception("столы: голосовые команды не обновились")
