"""Сервис команд: встроенные, паки, свои и динамические команды; разбор фраз, выполнение, подтверждения.

Источники: встроенные (jarvis.commands.library), паки (jarvis/resources/packs, установленные —
settings.packs), свои (таблица commands), динамические (поставщики register_provider: столы, плагины).
Выключенные встроенные, паковые и динамические — settings.disabled_commands. Изменения — события
commands.changed / packs.changed.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz, process

from jarvis import paths
from jarvis.ai.format import one_line
from jarvis.commands import builder, validate
from jarvis.commands.apps import AppResolver
from jarvis.commands.executor import Executor
from jarvis.commands.library import builtin_commands
from jarvis.commands.matcher import SLOT_RE, Index, Matcher, display_title
from jarvis.commands.model import CATEGORIES, Command, CommandIn, Entry
from jarvis.commands.packs import PackStore
from jarvis.commands.recorder import Recorder
from jarvis.commands.sites import SiteResolver
from jarvis.commands.textutil import normalize, slugify
from jarvis.db import dumps, loads, now
from jarvis.platform.base import AppInfo
from jarvis.service_base import BaseService
from jarvis.services import (
    CommandMatch,
    DynamicCommand,
    DynamicProvider,
    ExecResult,
    MatchedPart,
    Source,
    UserFacingError,
)
from jarvis.settings import Settings

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

APPS_STALE_S = 1800.0  # список «Пуска» обновляется в фоне не чаще раза в полчаса
APPS_RETRY_S = 60.0
DESKS_CACHE_S = 5.0
_SOURCE_ORDER = {"user": 0, "pack": 1, "dynamic": 2, "builtin": 3}


class CommandNotFoundError(UserFacingError, LookupError):
    """Команды (или пака) с таким id нет — API отвечает 404, остальным — текст для человека."""


class Commands(BaseService):
    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self._lock = threading.RLock()
        self.apps = AppResolver()
        self.sites = SiteResolver()
        self.packs = PackStore(paths.resource_dir() / "packs")
        self.executor = Executor(self)
        self.recorder = Recorder(ctx)
        self._providers: dict[str, DynamicProvider] = {}
        self._dynamic: list[Entry] = []
        self._user: dict[str, Command] = self._load_user()
        self._entries: dict[str, Entry] = {}
        self._index = Index([])
        self._apps_task: asyncio.Task[Any] | None = None
        self._apps_tried_at = 0.0  # последняя попытка прочитать «Пуск» (после ошибки — не чаще раза в минуту)
        self._desks: tuple[float, list[str]] = (0.0, [])
        self._matcher = Matcher(
            {
                "app": self._resolve_app,
                "site": self._resolve_site,
                "desk": self._resolve_desk,
            },
            self._wake_words,
        )
        self._rebuild()
        ctx.settings.on_change(self._on_settings)

    # --- жизненный цикл ---------------------------------------------------------------------------
    async def start(self) -> None:
        self._apps_tried_at = time.time()
        self._apps_task = self.ctx.spawn(self.refresh_apps(), "commands-apps")
        self.refresh_dynamic()

    async def stop(self) -> None:
        if self.recorder.recording:
            self.recorder.stop()
        if self._apps_task is not None and not self._apps_task.done():
            self._apps_task.cancel()

    # --- источники --------------------------------------------------------------------------------
    def _load_user(self) -> dict[str, Command]:
        out: dict[str, Command] = {}
        for row in self.ctx.db.query("SELECT id, data FROM commands ORDER BY updated"):
            try:
                command = Command.model_validate({**(loads(row["data"], {}) or {}), "id": row["id"]})
            except ValueError as e:
                log.error("своя команда %s не читается: %s", row["id"], e)
                continue
            out[command.id] = command.model_copy(update={"category": "user"})
        return out

    def _rebuild(self) -> None:
        """Собрать все команды и индекс для поиска (новые объекты подменяют старые целиком)."""
        with self._lock:
            settings = self.ctx.settings.value
            disabled = set(settings.disabled_commands)

            def entry(command: Command, source: Any, **kw: Any) -> Entry:
                if command.id in disabled and command.enabled:
                    command = command.model_copy(update={"enabled": False})
                return Entry(command, source, **kw)

            entries: dict[str, Entry] = {}

            def add(e: Entry) -> None:
                if e.id in entries:
                    log.warning("команда %s уже есть — повтор пропущен", e.id)
                    return
                entries[e.id] = e

            for item in builtin_commands():
                add(entry(item.command, "builtin", exclude=item.exclude, no_question=item.no_question))
            for pack_id in settings.packs:
                pack = self.packs.get(pack_id)
                if pack is None:
                    continue
                for command in pack.commands:
                    add(entry(command, "pack", pack=pack.id))
            for dyn in self._dynamic:
                add(entry(dyn.command, "dynamic", handler=dyn.handler))
            for command in self._user.values():
                add(Entry(command, "user"))
            self._entries = entries
            self._index = Index([e for e in entries.values() if e.command.enabled])

    def _changed(self, packs: bool = False) -> None:
        self.ctx.bus.publish("commands.changed", {})
        if packs:
            self.ctx.bus.publish("packs.changed", {})

    def _on_settings(self, old: Settings, new: Settings) -> None:
        packs = old.packs != new.packs
        if packs or old.disabled_commands != new.disabled_commands:
            self._rebuild()
            self._changed(packs)

    def register_provider(self, name: str, provider: DynamicProvider) -> None:
        with self._lock:
            self._providers[name] = provider

    def refresh_dynamic(self) -> None:
        """Перечитать динамические команды у всех поставщиков (можно звать из любого потока)."""
        with self._lock:
            dynamic: list[Entry] = []
            for name, provider in list(self._providers.items()):
                try:
                    items = list(provider() or [])
                except Exception:
                    log.exception("поставщик команд %s упал", name)
                    continue
                for item in items:
                    if not isinstance(item, DynamicCommand) or not item.id or not item.phrases:
                        continue
                    command = Command(
                        id=item.id,
                        name=item.title or item.id,
                        category=item.category or "plugins",
                        phrases=[str(p) for p in item.phrases if str(p).strip()],
                        icon=item.icon or "plugin",
                    )
                    dynamic.append(Entry(command, "dynamic", handler=item.handler))
            self._dynamic = dynamic
            self._rebuild()
        self._changed()

    # --- поиск ------------------------------------------------------------------------------------
    def _wake_words(self) -> frozenset[str]:
        return frozenset(normalize(w) for w in self.ctx.settings.value.voice.wake_words if w.strip())

    def _resolve_app(self, text: str) -> tuple[dict[str, Any], float] | None:
        found = self.apps.resolve(text)
        return (found.slots(), found.score) if found is not None and found.score >= 80 else None

    def _resolve_site(self, text: str) -> tuple[dict[str, Any], float] | None:
        found = self.sites.resolve(text)
        return (found.slots(), found.score) if found is not None and found.score >= 80 else None

    def _desk_names(self) -> list[str]:
        at, names = self._desks
        if time.monotonic() - at < DESKS_CACHE_S:
            return names
        try:
            names = [str(d.get("name", "")) for d in self.ctx.desks.list() if d.get("name")]
        except Exception:
            names = []
        self._desks = (time.monotonic(), names)
        return names

    def _resolve_desk(self, text: str) -> tuple[dict[str, Any], float] | None:
        query = normalize(text)
        if not query:
            return None
        names = self._desk_names()
        if names:
            hit = process.extractOne(query, [normalize(n) for n in names], scorer=fuzz.ratio, score_cutoff=80)
            if hit is not None:
                return {"desk": names[hit[2]]}, float(hit[1])
        if len(query.split()) <= 2:
            return {"desk": text.strip()}, 83.0  # стола нет в списке — исполнитель скажет «не найден»
        return None

    def match(self, text: str) -> CommandMatch | None:
        self._refresh_apps_if_stale()
        found = self._matcher.match(self._index, text)
        if found is None or "?" not in text or all(self._informational(p.command_id) for p in found.parts):
            return found
        # «открыть браузер?», «выключить компьютер?», «можешь закрыть окно?» — вопрос, а не просьба: действие
        # не выполняем, отвечает ИИ; справки («какая погода?», «сколько времени?») — как обычно
        return None

    def _informational(self, command_id: str) -> bool:
        """Команда только сообщает (время, погода, громкость, напоминания) и ничего не меняет."""
        entry = self._entries.get(command_id)
        if entry is None or entry.handler is not None:
            return False
        return all(a.type in INFO_ACTIONS for a in entry.command.actions)

    # --- выполнение -------------------------------------------------------------------------------
    async def execute(self, match: CommandMatch, source: Source) -> ExecResult:
        return await self._execute_parts(match.parts, source, 0)

    async def run_text(self, text: str, source: Source) -> ExecResult | None:
        found = self.match(text)
        return None if found is None else await self.execute(found, source)

    async def run_command(
        self, command_id: str, slots: dict[str, Any] | None = None, source: Source = "ui"
    ) -> ExecResult:
        entry = self._entries.get(command_id)
        if entry is None:
            raise CommandNotFoundError(f"Команда «{command_id}» не найдена")
        values = dict(slots or {})
        part = MatchedPart(command_id, display_title(entry, values), "", 100.0, values)
        return await self._execute_parts([part], source, 0)

    async def run_nested(self, target: str, slots: dict[str, Any], source: Source, depth: int) -> ExecResult:
        """Jarvis.Command: другая команда по id или по фразе."""
        entry = self._entries.get(target)
        if entry is not None:
            part = MatchedPart(entry.id, display_title(entry, slots), "", 100.0, dict(slots))
            return await self._execute_parts([part], source, depth)
        found = self.match(target)
        if found is None:
            raise UserFacingError(f"Не нашёл команду «{target}»")
        return await self._execute_parts(found.parts, source, depth)

    async def _execute_parts(self, parts: list[MatchedPart], source: Source, depth: int) -> ExecResult:
        results: list[ExecResult] = []
        for i, part in enumerate(parts):
            entry = self._entries.get(part.command_id)
            if entry is None:
                results.append(
                    ExecResult(
                        False,
                        part.title,
                        "Команда больше не существует",
                        icon="error",
                        command_ids=[part.command_id],
                    )
                )
                break
            title = display_title(entry, part.slots)
            if not entry.command.enabled:
                results.append(
                    ExecResult(
                        False, title, "Команда выключена", icon=entry.command.icon, command_ids=[entry.id]
                    )
                )
                break
            needs = entry.command.confirm
            detail = ""
            if not needs and source in ("ai", "agent"):
                detail = await self._ai_risk(entry, part)
                needs = bool(detail)
            if needs and not await self._confirm(entry, title, part, source, detail):
                results.append(
                    ExecResult(
                        False,
                        title,
                        "Отменено",
                        icon=entry.command.icon,
                        command_ids=[entry.id],
                        data={"cancelled": True},
                    )
                )
                break
            result = await self.executor.run_entry(entry, part.slots, source, depth)
            results.append(result)
            if not result.ok:
                rest = [p.text or p.title for p in parts[i + 1 :]]
                if rest and result.data.get("agent_task"):  # дальше фразы — тоже агенту, по порядку
                    result.data["agent_task"] += " Потом: " + "; ".join(rest) + "."
                break
        return _combine(results, parts)

    async def _confirm(
        self, entry: Entry, title: str, part: MatchedPart, source: Source, detail: str = ""
    ) -> bool:
        detail = detail or (f"Команда: «{part.text}»" if part.text else "")
        return await self.ctx.confirmations.request(
            title=f"{title}?", detail=detail, kind="command", source=source
        )

    async def _ai_risk(self, entry: Entry, part: MatchedPart) -> str:
        """Команда по просьбе ИИ, которой можно обойти его же запреты: консоль, запуск файла, опасные
        клавиши (Win+R, Shift+Delete…), текст в окно терминала или номер карты. Вернёт, что показать
        человеку; "" — можно без вопросов."""
        from jarvis.ai.policy import RISKY_KEYS, has_card_number, is_shell_window, normalize_combo
        from jarvis.commands.executor import substitute

        for action in entry.command.actions:
            try:
                value = substitute(action.value, part.slots)
            except Exception:
                value = action.value
            if action.type == "Launch.Shell":
                return f"Команда консоли: «{one_line(value, 140)}»"
            if action.type in ("System.Shutdown", "System.Restart"):  # питание — только с разрешения
                from jarvis.commands import numbers

                what = "Выключение" if action.type == "System.Shutdown" else "Перезагрузка"
                delay = numbers.parse_duration(value or "0") or 0.0
                return f"{what} компьютера" + (f" через {numbers.duration_text(delay)}" if delay >= 1 else "")
            if action.type == "Launch.File":
                return f"Открыть файл: «{one_line(value, 140)}»"
            if action.type == "Keys.Press" and normalize_combo(value) in RISKY_KEYS:
                return f"Сочетание клавиш {normalize_combo(value)}"
            if action.type in ("Keys.Type", "Keys.Paste"):
                if has_card_number(value):
                    return "Ввод номера карты"
                try:
                    fg = await self.ctx.run_platform(self.ctx.platform.foreground_window)
                except Exception:
                    fg = None
                if fg is not None and is_shell_window(fg.title, fg.exe):
                    return f"Текст в «{fg.title}»: «{one_line(value, 140)}»"
        return ""

    # --- для ИИ -----------------------------------------------------------------------------------
    def describe(self, limit: int = 300) -> list[dict[str, Any]]:
        """Краткий список включённых команд для ИИ и пульта: id, title, example, phrases (до трёх), category,
        icon, slots. Свои, паки и динамические — первыми; отдельные «Открыть Spotify» / «Открыть YouTube» —
        в самом конце (для ИИ хватает общих builtin.apps.open и builtin.sites.open со слотами)."""

        def rank(e: Entry) -> tuple[int, int]:
            per_item = e.source == "builtin" and e.id.startswith(("builtin.app.", "builtin.site."))
            return int(per_item), _SOURCE_ORDER.get(e.source, 9)

        items = []
        for e in sorted(self._index.entries, key=rank)[: max(0, limit)]:
            phrases = e.command.phrases
            item: dict[str, Any] = {
                "id": e.id,
                "title": e.command.name,
                "example": phrases[0] if phrases else "",
                "phrases": list(phrases[:3]),
                "category": e.command.category,
                "icon": e.command.icon,
            }
            slots = sorted({m for p in phrases for m in SLOT_RE.findall(p)})
            if slots:
                item["slots"] = slots
            items.append(item)
        return items

    def app_commands(self, apps: Iterable[str] = (), packs: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Паки команд для программ (процесс «Telegram.exe» или id пака) — подсказка ИИ и агенту.

        [{pack, name, app, installed, items: [{id, name, phrase, keys}]}]: у установленного пака — его
        включённые команды (run_command по id выполнит именно их), у неустановленного — только сочетания
        клавиш и адреса его команд (keys), чтобы нажимать их самому. keys — «ctrl+f», «steam://open/games»;
        пусто — другое.
        """
        exes = {a.casefold() for a in apps if a}
        wanted = set(packs)
        installed = set(self.ctx.settings.value.packs)
        out: list[dict[str, Any]] = []
        for pack in self.packs.all():
            if pack.id not in wanted and (not pack.app or pack.app.casefold() not in exes):
                continue
            items: list[dict[str, Any]] = []
            if pack.id in installed:
                for entry in self._entries.values():
                    if entry.pack == pack.id and entry.command.enabled:
                        items.append(_command_hint(entry.command))
            else:
                items = [h for h in (_command_hint(c) for c in pack.commands) if h["keys"]]
            if items:
                out.append(
                    {
                        "pack": pack.id,
                        "name": pack.name,
                        "app": pack.app,
                        "installed": pack.id in installed,
                        "items": items,
                    }
                )
        return out

    def added_commands(self, limit: int = 40) -> list[dict[str, Any]]:
        """Команды, которые человек добавил сам: свои и сценарии (включённые) — чтобы ИИ знал о них."""
        out = [
            _command_hint(e.command)
            for e in self._entries.values()
            if e.command.enabled and (e.source == "user" or e.command.category == "scenarios")
        ]
        return out[: max(0, limit)]

    # --- список и правка --------------------------------------------------------------------------
    def get_entry(self, command_id: str) -> Entry | None:
        return self._entries.get(command_id)

    def info(self, command_id: str) -> dict[str, Any]:
        entry = self._entries.get(command_id)
        if entry is None:
            raise CommandNotFoundError(f"Команда «{command_id}» не найдена")
        return entry.info()

    def list_commands(self, q: str = "", category: str = "") -> dict[str, Any]:
        entries = list(self._entries.values())
        words = normalize(q).split()
        items = []
        for e in entries:
            if category and not _in_category(e, category):
                continue
            if words:
                hay = normalize(" ".join([e.command.name, e.id, e.pack or "", *e.command.phrases]))
                if not all(w in hay for w in words):
                    continue
            items.append(e.info())
        counts: dict[str, int] = {}
        for e in entries:
            counts[e.command.category] = counts.get(e.command.category, 0) + 1
        order = list(CATEGORIES)
        categories = [
            {"id": cid, "title": CATEGORIES.get(cid) or cid[:1].upper() + cid[1:], "count": n}
            for cid, n in sorted(
                counts.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[0])
            )
        ]
        return {"items": items, "categories": categories, "total": len(entries)}

    def _save_user(self, command: Command) -> None:
        self.ctx.db.upsert(
            "commands", {"id": command.id, "data": dumps(command.model_dump(mode="json")), "updated": now()}
        )

    def create(self, data: CommandIn) -> dict[str, Any]:
        with self._lock:
            base = "user." + slugify(data.name or "command")
            command_id, n = base, 2
            while command_id in self._entries or command_id in self._user:
                command_id, n = f"{base}_{n}", n + 1
            command = validate.user_command(data, command_id)
            self._save_user(command)
            self._user[command.id] = command
            self._rebuild()
        self._changed()
        return self.info(command.id)

    def update(self, command_id: str, data: CommandIn) -> dict[str, Any]:
        with self._lock:
            entry = self._entries.get(command_id)
            if entry is None:
                raise CommandNotFoundError(f"Команда «{command_id}» не найдена")
            if entry.source != "user":
                raise UserFacingError("Встроенные команды и команды паков не редактируются — создайте свою")
            command = validate.user_command(data, command_id)
            self._save_user(command)
            self._user[command_id] = command
            self._rebuild()
        self._changed()
        return self.info(command_id)

    def set_enabled(self, command_id: str, enabled: bool) -> dict[str, Any]:
        entry = self._entries.get(command_id)
        if entry is None:
            raise CommandNotFoundError(f"Команда «{command_id}» не найдена")
        if entry.source == "user":
            with self._lock:
                command = self._user[command_id].model_copy(update={"enabled": bool(enabled)})
                self._save_user(command)
                self._user[command_id] = command
                self._rebuild()
            self._changed()
        else:
            disabled = [x for x in self.ctx.settings.value.disabled_commands if x != command_id]
            if not enabled:
                disabled.append(command_id)
            self.ctx.settings.patch({"disabled_commands": disabled})  # пересборка — в _on_settings
        return self.info(command_id)

    def delete(self, command_id: str) -> None:
        with self._lock:
            entry = self._entries.get(command_id)
            if entry is None:
                raise CommandNotFoundError(f"Команда «{command_id}» не найдена")
            if entry.source != "user":
                raise UserFacingError("Удалить можно только свою команду — встроенную можно выключить")
            self.ctx.db.delete("commands", command_id)
            self._user.pop(command_id, None)
            self._rebuild()
        self._changed()

    # --- паки -------------------------------------------------------------------------------------
    def packs_json(self) -> dict[str, Any]:
        installed = set(self.ctx.settings.value.packs)
        packs = self.packs.all()
        return {
            "items": [p.to_json(p.id in installed) for p in packs],
            "totals": {"packs": len(packs), "commands": sum(len(p.commands) for p in packs)},
        }

    def set_pack(self, pack_id: str, installed: bool) -> dict[str, Any]:
        pack = self.packs.get(pack_id)
        if pack is None:
            raise CommandNotFoundError(f"Пак «{pack_id}» не найден")
        current = [p for p in self.ctx.settings.value.packs if p != pack_id]
        if installed:
            current.append(pack_id)
        if current != self.ctx.settings.value.packs:
            self.ctx.settings.patch({"packs": current})  # пересборка и события — в _on_settings
        return pack.to_json(installed)

    # --- программы --------------------------------------------------------------------------------
    async def refresh_apps(self, force: bool = False) -> list[AppInfo]:
        apps = await self.ctx.run_platform(self.ctx.platform.list_apps, force)
        self.apps.set_apps(list(apps), time.time())
        return self.apps.apps

    def _refresh_apps_if_stale(self) -> None:
        now_ = time.time()
        if self.apps.loaded_at and now_ - self.apps.loaded_at < APPS_STALE_S:
            return
        if now_ - self._apps_tried_at < APPS_RETRY_S:
            return
        if self._apps_task is not None and not self._apps_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._apps_tried_at = now_
        self._apps_task = self.ctx.spawn(self.refresh_apps(), "commands-apps")

    async def apps_json(self, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or not self.apps.loaded_at:
            await self.refresh_apps(force=refresh)
        return [a.to_json() for a in self.apps.apps]

    # --- запись и «Создать с помощью AI» -----------------------------------------------------------
    def record_start(self) -> None:
        self.recorder.start()

    def record_stop(self) -> list[dict[str, str]]:
        return self.recorder.stop()

    async def build(self, description: str) -> dict[str, Any]:
        return await builder.build(self, description)


_HINT_KEYS = frozenset({"Keys.Press", "Launch.Url"})
_HINT_SKIP = frozenset({"App.Focus", "Wait"})


def _command_hint(command: Command) -> dict[str, Any]:
    """Команда для подсказки ИИ: id, имя, первая фраза и её клавиши/адрес, если команда только из них."""
    keys: list[str] = []
    for action in command.actions:
        if action.type in _HINT_SKIP:
            continue
        if action.type not in _HINT_KEYS or not action.value:
            keys = []
            break
        keys.append(action.value)
    return {
        "id": command.id,
        "name": command.name,
        "phrase": command.phrases[0] if command.phrases else "",
        "keys": " → ".join(keys),
    }


def _in_category(entry: Entry, category: str) -> bool:
    if entry.command.category == category:
        return True
    if category in ("user", "mine"):
        return entry.source == "user"
    if category in ("packs", "pack"):
        return entry.source == "pack"
    if category == "dynamic":
        return entry.source == "dynamic"
    return False


def _combine(results: list[ExecResult], parts: list[MatchedPart]) -> ExecResult:
    """Итог фразы из нескольких команд: «Свернуть все окна и Включить музыку»."""
    if not results:
        return ExecResult(False, "Команда", "Нечего выполнять", icon="error")
    if len(parts) == 1:
        return results[0]
    ok = all(r.ok for r in results) and len(results) == len(parts)
    title = " и ".join(r.title for r in results)
    if ok:
        infos = [r.message for r in results if r.message and r.message != "Выполнено"]
        message = "; ".join(infos) or "Выполнено"
    else:
        failed = next((r for r in results if not r.ok), results[-1])
        message = f"{failed.title}: {failed.message}" if len(results) > 1 else failed.message
    says = [r.say for r in results if r.say]
    data: dict[str, Any] = {
        "parts": [
            {"command_id": (r.command_ids or [""])[0], "ok": r.ok, "title": r.title, "message": r.message}
            for r in results
        ]
    }
    if any(r.data.get("cancelled") for r in results):
        data["cancelled"] = True
    handoff = next((r.data.get("agent_task") for r in results if not r.ok), None)
    if handoff:
        data["agent_task"] = handoff  # не вышло у команды — агенту (вместе с остатком фразы)
    ids = [cid for r in results for cid in r.command_ids]
    return ExecResult(
        ok, title, message, icon=results[0].icon, say=". ".join(says), command_ids=ids, data=data
    )


# действия, которые только сообщают: такую команду можно и вопросом («какая погода?»)
INFO_ACTIONS = frozenset(
    {
        "Info.Time",
        "Info.Date",
        "Info.Weather",
        "Info.Volume",
        "Info.Media",
        "Info.Battery",
        "Info.System",
        "Jarvis.Reminders",
        "Jarvis.Briefing",
        "Speak",
    }
)
