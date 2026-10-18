"""Столы: заготовки и сетка без перекрытий, стол «Главная», изменение и порядок, голосовое открытие, API."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.context import AppContext
from jarvis.desks import layout, presets
from jarvis.desks.service import DeskNotFoundError, Desks
from jarvis.services import DynamicCommand, DynamicProvider, UserFacingError
from tests.conftest import EventLog

CONTRACT_TYPES = {
    "avatar",
    "history",
    "control",
    "weather",
    "media",
    "tasks",
    "notes",
    "calendar",
    "clock",
    "timer",
    "stopwatch",
    "rates",
    "crypto",
    "stocks",
    "news",
    "system",
    "apps",
    "quick",
    "recent",
    "volume",
    "smarthome",
    "pet",
    "ai",
    "worldclock",
    "countdown",
    "network",
    "battery",
    "sticky",
    "plugins",
    "desk_links",
}


class FakeCommands:
    """Сервис команд для тестов: запоминает провайдеров и вызовы refresh_dynamic()."""

    def __init__(self) -> None:
        self.providers: dict[str, DynamicProvider] = {}
        self.refreshed = 0

    def register_provider(self, name: str, provider: DynamicProvider) -> None:
        self.providers[name] = provider

    def refresh_dynamic(self) -> None:
        self.refreshed += 1

    def commands(self) -> list[DynamicCommand]:
        return [c for provider in self.providers.values() for c in provider()]


def _desks(ctx: AppContext) -> Desks:
    service = ctx.desks
    assert isinstance(service, Desks)
    return service


# --- сетка и заготовки --------------------------------------------------------------------------------


def test_widget_catalog_matches_contract() -> None:
    assert set(layout.WIDGET_TYPES) == CONTRACT_TYPES
    for spec in layout.WIDGETS.values():
        assert 1 <= spec.min_w <= spec.w <= spec.max_w <= layout.COLUMNS, spec.type
        assert 1 <= spec.min_h <= spec.h <= spec.max_h <= layout.MAX_SPAN, spec.type
    catalog = layout.catalog()
    assert len(catalog) == len(CONTRACT_TYPES)
    assert all(item["name"] and item["category_title"] for item in catalog)


def test_twelve_presets_have_valid_layouts() -> None:
    items = presets.presets()
    assert [p["name"] for p in items] == [
        "Пустой",
        "Утро",
        "Работа",
        "Учёба",
        "Время и заметки",
        "Финансы",
        "Медиа",
        "Игры",
        "Компьютер",
        "Умный дом",
        "Питомец",
        "Только аватар",
    ]
    assert len({p["id"] for p in items}) == 12
    home = presets.preset(presets.HOME_PRESET)
    assert home is not None
    for preset in [*items, home]:
        widgets = [{**w, "id": f"w{i}"} for i, w in enumerate(preset["widgets"])]
        assert layout.validate(widgets) == [], preset["id"]
        assert all(w["type"] in CONTRACT_TYPES for w in widgets)
        assert all(w["x"] + w["w"] <= 4 for w in widgets)
        assert layout.compact(widgets) == widgets, (
            f"{preset['id']}: раскладка не должна меняться при уплотнении"
        )
        assert preset["description"] and preset["icon"]
    assert items[0]["widgets"] == []
    avatar_only = items[-1]["widgets"]
    assert avatar_only == [
        {"type": "avatar", "x": 0, "y": 0, "w": 4, "h": 4, "config": avatar_only[0]["config"]}
    ]


def test_presets_mention_their_widgets() -> None:
    by_id = {p["id"]: {w["type"] for w in p["widgets"]} for p in presets.presets()}
    assert {"weather", "calendar", "tasks", "rates"} <= by_id["morning"]
    assert {"tasks", "calendar", "recent", "apps"} <= by_id["work"]
    assert {"timer", "countdown", "tasks"} <= by_id["study"]
    assert {"clock", "calendar", "tasks", "notes"} <= by_id["time_notes"]
    assert {"crypto", "stocks", "rates"} <= by_id["finance"]
    assert {"media", "volume", "news", "quick"} <= by_id["media"]
    assert {"apps", "quick", "system"} <= by_id["games"]
    assert {"system", "calendar", "apps", "recent"} <= by_id["computer"]
    assert {"smarthome", "quick", "weather"} <= by_id["smart_home"]
    assert {"pet", "plugins"} <= by_id["pet"]


def test_next_session_date() -> None:
    import datetime as dt

    assert presets.next_session_date(dt.date(2026, 10, 18)) == "2027-01-10"
    assert presets.next_session_date(dt.date(2026, 2, 1)) == "2026-06-01"
    assert presets.next_session_date(dt.date(2026, 1, 5)) == "2026-01-10"


def test_validate_and_compact_fix_overlaps() -> None:
    broken = [
        {"id": "a", "type": "avatar", "x": 2, "y": 0, "w": 2, "h": 3, "config": {}},
        {"id": "b", "type": "weather", "x": 3, "y": 1, "w": 1, "h": 1, "config": {}},  # налез на аватар
        {"id": "c", "type": "news", "x": 3, "y": 0, "w": 2, "h": 2, "config": {}},  # за краем и поверх
        {"id": "a", "type": "clock", "x": -1, "y": -5, "w": 9, "h": 0},  # повтор id, мусор в размерах
        {"id": "d", "type": "nonsense", "x": 0, "y": 0, "w": 1, "h": 1},
    ]
    problems = layout.validate(broken)
    assert any("неизвестный тип" in p for p in problems)
    fixed = layout.compact(broken)
    assert layout.validate(fixed) == []
    assert [w["type"] for w in fixed] == ["avatar", "weather", "news", "clock"]  # порядок сохранён
    assert fixed[0] == {"id": "a", "type": "avatar", "x": 2, "y": 0, "w": 2, "h": 3, "config": {}}
    assert len({w["id"] for w in fixed}) == 4
    weather = fixed[1]
    assert weather["x"] == 3 and weather["y"] >= 3  # налезший виджет сдвинут вниз в своей колонке
    clock = fixed[3]
    assert (clock["w"], clock["h"], clock["x"]) == (4, 1, 0)  # ширина обрезана до 4, высота — до 1
    assert clock["config"] == {"seconds": False}  # настройки по умолчанию


def test_compact_keeps_priority_widget_in_place() -> None:
    widgets = [
        {"id": "a", "type": "tasks", "x": 0, "y": 0, "w": 1, "h": 2},
        {"id": "b", "type": "clock", "x": 0, "y": 1, "w": 1, "h": 1},
    ]
    fixed = layout.compact(widgets, first=["b"])
    assert (fixed[1]["x"], fixed[1]["y"]) == (0, 1)  # передвинутый остался, где просили
    assert (fixed[0]["x"], fixed[0]["y"]) == (0, 2)  # сосед уступил и сдвинулся вниз
    assert layout.validate(fixed) == []


def test_place_finds_free_spot() -> None:
    home = presets.preset(presets.HOME_PRESET)
    assert home is not None
    widgets = [{**w, "id": f"w{i}"} for i, w in enumerate(home["widgets"])]
    new = layout.place(widgets, {"type": "news", "w": 2, "h": 2})
    assert layout.validate([*widgets, new]) == []
    assert new["y"] >= layout.rows_used(widgets)  # «Главная» заполнена целиком — новый виджет ниже


# --- сервис -------------------------------------------------------------------------------------------


def test_default_desk_created_once(ctx: AppContext) -> None:
    desks = _desks(ctx)
    items = desks.list()
    assert len(items) == 1
    home = items[0]
    assert home["name"] == "Главная" and home["preset"] == "home" and home["icon"] == "home"
    assert layout.validate(home["widgets"]) == []
    avatar = next(w for w in home["widgets"] if w["type"] == "avatar")
    assert (avatar["x"], avatar["y"], avatar["w"], avatar["h"]) == (2, 0, 2, 3)
    types = {w["type"] for w in home["widgets"]}
    assert {"control", "history", "media", "weather", "tasks"} <= types
    assert all(w["id"] for w in home["widgets"])
    desks.ensure_default()
    assert len(desks.list()) == 1


def test_create_rename_delete(ctx: AppContext, events: EventLog) -> None:
    desks = _desks(ctx)
    games = desks.create(preset="games")
    assert games["name"] == "Игры" and games["icon"] == "gamepad" and games["position"] == 1
    assert layout.validate(games["widgets"]) == []
    second = desks.create(preset="games")
    assert second["name"] == "Игры 2"
    named = desks.create("Мой стол", "empty", icon="star")
    assert named["widgets"] == [] and named["icon"] == "star"
    assert events.of("desks.changed")[-1]["items"][-1]["name"] == "Мой стол"

    with pytest.raises(UserFacingError):
        desks.create(preset="no-such-preset")
    with pytest.raises(UserFacingError):
        desks.update(second["id"], {"name": "Игры"})  # имя занято
    with pytest.raises(UserFacingError):
        desks.update(second["id"], {"name": "   "})

    renamed = desks.update(second["id"], {"name": "Стримы", "icon": "obs"})
    assert renamed["name"] == "Стримы" and renamed["icon"] == "obs"
    moved = desks.update(named["id"], {"position": 0})
    assert moved["position"] == 0
    assert [d["name"] for d in desks.list()] == ["Мой стол", "Главная", "Игры", "Стримы"]

    desks.delete(games["id"])
    assert [d["position"] for d in desks.list()] == [0, 1, 2]
    with pytest.raises(DeskNotFoundError):
        desks.delete(games["id"])


def test_last_desk_cannot_be_deleted(ctx: AppContext) -> None:
    desks = _desks(ctx)
    only = desks.list()[0]
    with pytest.raises(UserFacingError, match="последний"):
        desks.delete(only["id"])


def test_update_widgets_are_compacted(ctx: AppContext) -> None:
    desks = _desks(ctx)
    desk = desks.create(preset="empty")
    updated = desks.update(
        desk["id"],
        {
            "widgets": [
                {"type": "weather", "x": 0, "y": 0, "w": 2, "h": 2},
                {"type": "clock", "x": 1, "y": 1, "w": 1, "h": 1},
            ]
        },
    )
    assert layout.validate(updated["widgets"]) == []
    assert all(w["id"] for w in updated["widgets"])
    with pytest.raises(UserFacingError, match="Неизвестный виджет"):
        desks.update(desk["id"], {"widgets": [{"type": "rocket"}]})


def test_add_update_remove_widget(ctx: AppContext) -> None:
    desks = _desks(ctx)
    desk = desks.create(preset="finance")
    added = desks.add_widget(desk["id"], {"type": "clock"})
    clock = added["widgets"][-1]
    assert clock["type"] == "clock" and clock["config"] == {"seconds": False}
    assert layout.validate(added["widgets"]) == []

    moved = desks.update_widget(desk["id"], clock["id"], {"x": 0, "y": 0, "config": {"seconds": True}})
    clock = next(w for w in moved["widgets"] if w["id"] == clock["id"])
    assert (clock["x"], clock["y"]) == (0, 0) and clock["config"] == {"seconds": True}
    assert layout.validate(moved["widgets"]) == []  # соседи подвинулись

    removed = desks.remove_widget(desk["id"], clock["id"])
    assert all(w["id"] != clock["id"] for w in removed["widgets"])
    with pytest.raises(DeskNotFoundError):
        desks.remove_widget(desk["id"], clock["id"])
    with pytest.raises(UserFacingError):
        desks.add_widget(desk["id"], {"type": "rocket"})


def test_save_all_reorders_and_drops_missing(ctx: AppContext) -> None:
    desks = _desks(ctx)
    home = desks.list()[0]
    work = desks.create(preset="work")
    desks.create(preset="media")
    saved = desks.save_all(
        [
            {**work, "name": "Офис"},
            {"id": home["id"], "name": home["name"], "widgets": home["widgets"][:2]},
            {"name": "Новый", "widgets": [{"type": "avatar", "x": 0, "y": 0, "w": 4, "h": 4}]},
        ]
    )
    assert [d["name"] for d in saved] == ["Офис", "Главная", "Новый"]
    assert len(saved[1]["widgets"]) == 2 and saved[2]["preset"] == "empty"
    with pytest.raises(UserFacingError):
        desks.save_all([])
    with pytest.raises(UserFacingError, match="дважды"):
        desks.save_all([{"name": "А"}, {"name": "а"}])


@pytest.mark.parametrize("phrase", ["игры", "Игры", "стол игры", "игровой", "открой стол игры", "игр"])
def test_open_fuzzy_publishes_event(ctx: AppContext, events: EventLog, phrase: str) -> None:
    desks = _desks(ctx)
    for preset_id in ("work", "games", "media", "finance"):
        desks.create(preset=preset_id)
    desk = desks.open(phrase)
    assert desk is not None and desk["name"] == "Игры"
    assert events.of("desk.open") == [{"id": desk["id"], "name": "Игры"}]
    assert desks.current_id() == desk["id"]


def test_open_by_id_aliases_and_misses(ctx: AppContext, events: EventLog) -> None:
    desks = _desks(ctx)
    media = desks.create(preset="media")
    smart = desks.create(preset="smart_home")
    assert desks.open(media["id"])["id"] == media["id"]  # type: ignore[index]
    assert desks.open("музыка")["id"] == media["id"]  # type: ignore[index]
    assert desks.open("умного дома")["id"] == smart["id"]  # type: ignore[index]
    assert desks.open("главную")["name"] == "Главная"  # type: ignore[index]
    before = len(events.of("desk.open"))
    assert desks.open("бухгалтерия") is None
    assert desks.open("стол") is None
    assert desks.open("") is None
    assert len(events.of("desk.open")) == before


async def test_dynamic_commands(ctx: AppContext, events: EventLog) -> None:
    fake = FakeCommands()
    ctx.commands = fake  # type: ignore[assignment]
    desks = _desks(ctx)
    await desks.start()
    assert "desks" in fake.providers and fake.refreshed == 1

    commands = fake.commands()
    assert [c.title for c in commands] == ["Открыть стол «Главная»"]
    home = commands[0]
    assert home.phrases == [
        "открой стол главная",
        "стол главная",
        "покажи стол главная",
        "переключи на стол главная",
    ]
    assert home.icon == "desk" and home.id.startswith("desks:")

    games = desks.create(preset="games")
    assert fake.refreshed == 2
    by_title = {c.title: c for c in fake.commands()}
    result = await by_title["Открыть стол «Игры»"].handler({})
    assert result.ok and result.title == "Открыт стол «Игры»" and result.icon == "desk"
    assert events.of("desk.open")[-1] == {"id": games["id"], "name": "Игры"}

    desks.delete(games["id"])
    assert fake.refreshed == 3
    stale = await by_title["Открыть стол «Игры»"].handler({})
    assert not stale.ok


def test_dynamic_command_phrases_are_safe(ctx: AppContext) -> None:
    desks = _desks(ctx)
    desks.create("Мой {стол}", "empty")
    phrases = [p for c in desks.dynamic_commands() for p in c.phrases]
    assert "открой стол мой стол" in phrases
    assert not any("{" in p for p in phrases)


# --- API ----------------------------------------------------------------------------------------------


def test_api_desks(client: TestClient) -> None:
    listing = client.get("/api/desks").json()
    assert [d["name"] for d in listing["items"]] == ["Главная"]
    assert listing["current"] == listing["items"][0]["id"]

    presets_list = client.get("/api/desks/presets").json()["items"]
    assert len(presets_list) == 12 and {"id", "name", "description", "icon", "widgets"} <= set(
        presets_list[1]
    )
    catalog = client.get("/api/desks/widgets").json()["items"]
    assert {w["type"] for w in catalog} == CONTRACT_TYPES

    created = client.post("/api/desks", json={"name": "", "preset": "finance"})
    assert created.status_code == 200
    desk: dict[str, Any] = created.json()
    assert desk["name"] == "Финансы" and {w["type"] for w in desk["widgets"]} >= {"crypto", "stocks"}
    assert client.post("/api/desks", json={"preset": "nope"}).status_code == 400

    patched = client.patch(f"/api/desks/{desk['id']}", json={"name": "Деньги"})
    assert patched.json()["name"] == "Деньги"
    assert client.get(f"/api/desks/{desk['id']}").json()["name"] == "Деньги"
    assert client.patch("/api/desks/missing", json={"name": "x"}).status_code == 404
    assert client.get("/api/desks/missing").status_code == 404

    with_widget = client.post(f"/api/desks/{desk['id']}/widgets", json={"type": "clock"}).json()
    widget_id = with_widget["widgets"][-1]["id"]
    changed = client.patch(f"/api/desks/{desk['id']}/widgets/{widget_id}", json={"config": {"seconds": True}})
    assert changed.json()["widgets"][-1]["config"] == {"seconds": True}
    assert client.delete(f"/api/desks/{desk['id']}/widgets/{widget_id}").status_code == 200
    assert client.delete(f"/api/desks/{desk['id']}/widgets/{widget_id}").status_code == 404

    assert client.post(f"/api/desks/{desk['id']}/open").json() == {"ok": True}
    assert client.get("/api/desks").json()["current"] == desk["id"]
    assert client.post("/api/desks/missing/open").status_code == 404

    items = client.get("/api/desks").json()["items"]
    saved = client.put("/api/desks", json={"items": list(reversed(items))}).json()["items"]
    assert [d["name"] for d in saved] == ["Деньги", "Главная"]

    assert client.delete(f"/api/desks/{desk['id']}").json() == {"ok": True}
    only = client.get("/api/desks").json()["items"]
    assert len(only) == 1
    last = client.delete(f"/api/desks/{only[0]['id']}")
    assert last.status_code == 400 and "последний" in last.json()["detail"]
