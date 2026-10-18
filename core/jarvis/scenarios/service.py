"""Сценарии: хранение, триггеры и выполнение (контракт — docs/CONTRACTS.md, раздел «Сценарии»).

Сценарии лежат в kv базы: ключ scenarios:item:<id>, значение — запись целиком (вместе со служебными armed_at —
с какого момента триггеры «взведены» — и marks — какие моменты расписания и события календаря уже обработаны).
Фоновая проверка раз в ~3 секунды смотрит триггеры включённых сценариев: расписание, интервалы, программы
(platform.is_running), отсутствие человека (platform.idle_seconds), заряд батареи (platform.system_stats,
раз в минуту) и события календаря (ctx.notes). Фразы сценариев — динамические команды (поставщик
"scenarios"), как и «запусти сценарий …», «включи / выключи сценарий …».

Выполнение — исполнителем команд (те же действия, слоты и подтверждения), затем «Что сказать после». Итог —
событие scenario.run, запись в истории, тост «Сценарий «…»» и голос (кроме тихого режима). Фразу,
сказанную человеку, в историю и тост записывает диспетчер, как у любой команды. Один сценарий не выполняется
дважды одновременно. Расписание, пропущенное, пока компьютер спал или Jarvis был выключен, срабатывает один
раз, если опоздание меньше 10 минут, иначе пропускается. Идущий сценарий можно остановить: stop_run(id) или
stop_all() («Стоп») — итог «Остановлено» (data.cancelled).
"""

import asyncio
import contextlib
import copy
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz, process

from jarvis.bus import Event
from jarvis.commands.model import Action, Command, Entry
from jarvis.db import loads, new_id
from jarvis.scenarios import templates
from jarvis.scenarios.model import (
    MANUAL,
    MAX_NAME,
    ScenarioIn,
    ScenarioPatch,
    clean_actions,
    clean_conditions,
    clean_icon,
    clean_name,
    clean_say,
    clean_triggers,
    fill_address,
    toast_icon,
    trigger_key,
    trigger_label,
)
from jarvis.scenarios.schedule import conditions_ok, last_occurrence
from jarvis.service_base import BaseService
from jarvis.services import DynamicCommand, ExecResult, Source, UserFacingError
from jarvis.text import normalize

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

PROVIDER = "scenarios"
KV_PREFIX = "scenarios:item:"
MAX_SCENARIOS = 100
TICK_S = 3.0
LATE_S = 600.0  # расписание и календарь: опоздание больше 10 минут (компьютер спал) — пропуск
SLEEP_GAP_S = 30.0  # между проверками прошло больше — компьютер спал (или ядро стояло)
BATTERY_POLL_S = 60.0
BATTERY_HYSTERESIS = 3  # заряд поднялся на 3% выше порога — началась новая разрядка
ACTIVE_IDLE_S = 120.0  # без ввода дольше 2 минут — человек не за компьютером (интервал «активности» стоит)
BREAK_RESET_S = 600.0  # перерыв дольше 10 минут обнуляет счёт интервала «активности»
RETURN_IDLE_S = 10.0  # ввод за последние 10 секунд — человек вернулся
AWAY_MIN_S = 300.0  # отсутствие от 5 минут запоминается для «Что пропустили»
MISSED_KEEP_S = 86400.0
MARKS_KEEP_S = 2 * 86400.0
INTERNAL = ("armed_at", "marks")
SCENARIO_SOURCE = "scenario"  # источник записи в истории
AUTO_SOURCE: Source = "plugin"  # источник для исполнителя, когда сценарий запустился сам

RUN_PHRASES = ("запусти сценарий {text}", "выполни сценарий {text}", "запуск сценария {text}")
ENABLE_PHRASES = ("включи сценарий {text}", "активируй сценарий {text}")
DISABLE_PHRASES = ("выключи сценарий {text}", "отключи сценарий {text}", "деактивируй сценарий {text}")
_SCENARIO_WORD = re.compile(r"^(?:сценари[йяюе]|сценарии)\s+")


class ScenarioNotFoundError(UserFacingError, LookupError):
    """Сценария (или шаблона) с таким id нет — API отвечает 404."""


class Scenarios(BaseService):
    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self.clock: Callable[[], float] = time.time  # тесты подменяют время
        self.tick_s = TICK_S
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = self._load()
        self._task: asyncio.Task[None] | None = None
        self._runs: set[asyncio.Task[Any]] = set()
        self._busy: dict[str, object] = {}  # сценарии, которые сейчас выполняются (id → метка запуска)
        self._actions: dict[str, asyncio.Future[Any]] = {}  # их действия: «Стоп» отменяет их
        self._stopping: set[str] = set()  # остановлены до начала действий
        self._offs: list[Callable[[], None]] = []
        self._started_at: float | None = None
        self._last_tick: float | None = None
        self._start_fired = False
        # состояние триггеров в памяти, ключ — (id сценария, ключ триггера)
        self._interval_base: dict[tuple[str, str], float] = {}
        self._active: dict[tuple[str, str], float] = {}
        self._away: dict[tuple[str, str], float] = {}
        self._battery_low: set[tuple[str, str]] = set()
        self._battery_at = -math.inf
        self._apps: dict[str, bool] = {}  # программа (casefold) → была запущена при прошлой проверке
        # уход от компьютера и напоминания за это время — для «Что пропустили»
        self._idle_start: float | None = None
        self._last_away: float | None = None
        self._missed: deque[dict[str, Any]] = deque(maxlen=50)
        self._subscribe()
        try:
            ctx.commands.register_provider(PROVIDER, self.dynamic_commands)
        except Exception:
            log.exception("сценарии: голосовые команды не зарегистрированы")

    # --- жизненный цикл ---------------------------------------------------------------------------
    def _subscribe(self) -> None:
        # напоминания запоминаются с самого начала, «При запуске Jarvis» — по core.ready (после всех сервисов)
        self._offs = [
            self.ctx.bus.on("reminder.fired", self._on_reminder),
            self.ctx.bus.on("core.ready", self._on_ready),
        ]

    async def start(self) -> None:
        if not self._offs:
            self._subscribe()
        self._started_at = self.clock()
        self._task = self.ctx.spawn(self._loop(), "scenarios")

    async def stop(self) -> None:
        for off in self._offs:
            off()
        self._offs = []
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for run in list(self._runs):
            run.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("проверка сценариев упала")
            await asyncio.sleep(self.tick_s)

    async def wait_idle(self) -> None:
        """Дождаться сценариев, запущенных триггерами (для тестов)."""
        while self._runs:
            await asyncio.gather(*list(self._runs), return_exceptions=True)

    # --- хранение ---------------------------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            rows = self.ctx.db.query("SELECT key, value FROM kv WHERE key LIKE ?", (KV_PREFIX + "%",))
        except Exception:
            log.exception("сценарии не прочитаны")
            return {}
        items: list[dict[str, Any]] = []
        for row in rows:
            data = loads(row["value"], None)
            if isinstance(data, dict) and data.get("id"):
                items.append(_with_defaults(data))
        items.sort(key=lambda r: float(r.get("created") or 0))
        return {str(r["id"]): r for r in items}

    def _save(self, rec: dict[str, Any]) -> None:
        self.ctx.db.kv_set(KV_PREFIX + rec["id"], rec)

    @staticmethod
    def public(rec: dict[str, Any]) -> dict[str, Any]:
        """Сценарий для API — без служебных полей."""
        return {k: copy.deepcopy(v) for k, v in rec.items() if k not in INTERNAL}

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.public(r) for r in self._items.values()]

    def get(self, scenario_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._items.get(scenario_id)
            return self.public(rec) if rec is not None else None

    def require(self, scenario_id: str) -> dict[str, Any]:
        found = self.get(scenario_id)
        if found is None:
            raise ScenarioNotFoundError("Сценарий не найден")
        return found

    def _changed(self, op: str, *, item: dict[str, Any] | None = None, item_id: str = "") -> None:
        data: dict[str, Any] = {"op": op}
        if item is not None:
            data["item"] = item
        if item_id:
            data["id"] = item_id
        self.ctx.bus.publish("scenarios.changed", data)

    def _refresh_commands(self) -> None:
        try:
            self.ctx.commands.refresh_dynamic()
        except Exception:
            log.exception("сценарии: голосовые команды не обновлены")

    def _address(self) -> str:
        return str(self.ctx.settings.value.profile.address or "").strip()

    # --- изменение --------------------------------------------------------------------------------
    def create(self, data: ScenarioIn | dict[str, Any], *, template: str = "") -> dict[str, Any]:
        body = data if isinstance(data, ScenarioIn) else ScenarioIn.model_validate(data)
        fields = {
            "name": clean_name(body.name),
            "icon": clean_icon(body.icon),
            "enabled": bool(body.enabled),
            "triggers": clean_triggers(body.triggers),
            "conditions": clean_conditions(body.conditions),
            "actions": clean_actions(body.actions),
            "say": clean_say(body.say),
        }
        _require_work(fields)
        with self._lock:
            if len(self._items) >= MAX_SCENARIOS:
                raise UserFacingError(f"Сценариев уже {MAX_SCENARIOS} — удалите ненужные")
            self._check_name(fields["name"])
            now = self.clock()
            rec: dict[str, Any] = {
                "id": new_id(),
                **fields,
                "template": template or (body.template if templates.get(body.template) else ""),
                "last_run": None,
                "last_ok": None,
                "last_trigger": "",
                "last_error": "",
                "runs": 0,
                "created": now,
                "updated": now,
                "armed_at": now,
                "marks": {},
            }
            self._items[rec["id"]] = rec
            self._save(rec)
            item = self.public(rec)
        self._changed("add", item=item)
        self._refresh_commands()
        return item

    def update(self, scenario_id: str, patch: ScenarioPatch | dict[str, Any]) -> dict[str, Any]:
        """Изменить сценарий: только поля, пришедшие в patch (включить/выключить — {enabled})."""
        body = patch if isinstance(patch, ScenarioPatch) else ScenarioPatch.model_validate(patch)
        given = body.model_fields_set
        with self._lock:
            old = self._items.get(scenario_id)
            if old is None:
                raise ScenarioNotFoundError("Сценарий не найден")
            new = copy.deepcopy(old)
            if body.name is not None:
                new["name"] = clean_name(body.name)
                self._check_name(new["name"], exclude=scenario_id)
            if body.icon is not None:
                new["icon"] = clean_icon(body.icon)
            if body.enabled is not None:
                new["enabled"] = bool(body.enabled)
            if body.triggers is not None:
                new["triggers"] = clean_triggers(body.triggers)
            if "conditions" in given:
                new["conditions"] = clean_conditions(body.conditions)
            if body.actions is not None:
                new["actions"] = clean_actions(body.actions)
            if body.say is not None:
                new["say"] = clean_say(body.say)
            _require_work(new)
            now = self.clock()
            rearm = new["triggers"] != old["triggers"] or (new["enabled"] and not old["enabled"])
            if rearm:
                # новые триггеры (или снова включённый сценарий) не догоняют то, что было до этого момента
                new["armed_at"] = now
                self._reset_state(scenario_id)
            new["updated"] = now
            self._items[scenario_id] = new
            self._save(new)
            item = self.public(new)
        self._changed("update", item=item)
        if any(new[k] != old[k] for k in ("name", "enabled", "triggers", "icon")):
            self._refresh_commands()
        return item

    def delete(self, scenario_id: str) -> None:
        with self._lock:
            if self._items.pop(scenario_id, None) is None:
                raise ScenarioNotFoundError("Сценарий не найден")
            self.ctx.db.delete("kv", KV_PREFIX + scenario_id, key="key")
            self._reset_state(scenario_id)
        self._changed("delete", item_id=scenario_id)
        self._refresh_commands()

    def _check_name(self, name: str, exclude: str = "") -> None:
        key = name.casefold()
        if any(r["name"].casefold() == key for sid, r in self._items.items() if sid != exclude):
            raise UserFacingError(f"Сценарий «{name}» уже есть — выберите другое название")

    def _unique_name(self, base: str) -> str:
        taken = {r["name"].casefold() for r in self._items.values()}
        if base.casefold() not in taken:
            return base
        n = 2
        while True:
            suffix = f" {n}"
            name = base[: MAX_NAME - len(suffix)] + suffix
            if name.casefold() not in taken:
                return name
            n += 1

    def _reset_state(self, scenario_id: str) -> None:
        for table in (self._interval_base, self._active, self._away):
            for key in [k for k in table if k[0] == scenario_id]:
                del table[key]
        self._battery_low = {k for k in self._battery_low if k[0] != scenario_id}

    # --- шаблоны ----------------------------------------------------------------------------------
    def templates(self) -> list[dict[str, Any]]:
        address = self._address()
        with self._lock:
            used = {r.get("template") for r in self._items.values()}
        return [{**templates.render(t, address), "added": t["id"] in used} for t in templates.TEMPLATES]

    def from_template(self, template_id: str) -> dict[str, Any]:
        """Включённый сценарий из шаблона; имя занято — с номером: «Доброе утро 2»."""
        template = templates.get(template_id)
        if template is None:
            raise ScenarioNotFoundError(f"Шаблон «{template_id}» не найден")
        data = templates.render(template, self._address())
        with self._lock:
            name = self._unique_name(data["name"])
        body = ScenarioIn.model_validate({**data, "name": name, "enabled": True})
        return self.create(body, template=template_id)

    # --- голосовые команды ------------------------------------------------------------------------
    def dynamic_commands(self) -> list[DynamicCommand]:
        with self._lock:
            recs = list(self._items.values())
        if not recs:
            return []
        out: list[DynamicCommand] = []
        for rec in recs:
            phrases = [p for t in rec["triggers"] if t["type"] == "phrase" for p in t["phrases"]]
            if not rec["enabled"] or not phrases:
                continue
            out.append(
                DynamicCommand(
                    id=f"{PROVIDER}:{rec['id']}",
                    title=_title(rec["name"]),
                    phrases=list(dict.fromkeys(phrases)),
                    handler=self._phrase_handler(rec["id"]),
                    category="scenarios",
                    icon=toast_icon(rec["icon"]),
                )
            )
        out += [
            DynamicCommand(
                id=f"{PROVIDER}:run",
                title="Запустить сценарий",
                phrases=list(RUN_PHRASES),
                handler=self._run_by_name,
                category="scenarios",
                icon="ai",
            ),
            DynamicCommand(
                id=f"{PROVIDER}:enable",
                title="Включить сценарий",
                phrases=list(ENABLE_PHRASES),
                handler=self._enable_by_name,
                category="scenarios",
                icon="ai",
            ),
            DynamicCommand(
                id=f"{PROVIDER}:disable",
                title="Выключить сценарий",
                phrases=list(DISABLE_PHRASES),
                handler=self._disable_by_name,
                category="scenarios",
                icon="ai",
            ),
        ]
        return out

    def find(self, query: str) -> dict[str, Any] | None:
        """Сценарий по названию из фразы (нечётко): «доброе утро», «игровой» → «Игровой режим»."""
        text = _SCENARIO_WORD.sub("", normalize(query or ""))
        if len(text) < 2:
            return None
        with self._lock:
            recs = list(self._items.values())
        names = [normalize(r["name"]) for r in recs]
        for rec, name in zip(recs, names, strict=True):
            if name == text:
                return self.public(rec)
        hit = process.extractOne(text, names, scorer=fuzz.WRatio, score_cutoff=80)
        return self.public(recs[hit[2]]) if hit is not None else None

    def _phrase_handler(self, scenario_id: str) -> Callable[[dict[str, Any]], Any]:
        async def handler(slots: dict[str, Any]) -> ExecResult:
            # «Выполнить» в списке команд (источник ui) — ручной запуск: без проверки условий
            trigger = MANUAL if slots.get("_source") == "ui" else "phrase"
            return await self._run_command(scenario_id, slots, trigger)

        return handler

    async def _run_by_name(self, slots: dict[str, Any]) -> ExecResult:
        name = str(slots.get("text") or slots.get("query") or "").strip()
        found = self.find(name)
        if found is None:
            message = f"Не нашёл сценарий «{name}»" if name else "Не расслышал название сценария"
            return ExecResult(False, "Запустить сценарий", message, icon="ai")
        return await self._run_command(found["id"], {"_source": slots.get("_source")}, MANUAL)

    async def _enable_by_name(self, slots: dict[str, Any]) -> ExecResult:
        return self._toggle_by_name(slots, True)

    async def _disable_by_name(self, slots: dict[str, Any]) -> ExecResult:
        return self._toggle_by_name(slots, False)

    def _toggle_by_name(self, slots: dict[str, Any], enabled: bool) -> ExecResult:
        name = str(slots.get("text") or slots.get("query") or "").strip()
        verb = "Включить" if enabled else "Выключить"
        found = self.find(name)
        if found is None:
            message = f"Не нашёл сценарий «{name}»" if name else "Не расслышал название сценария"
            return ExecResult(False, f"{verb} сценарий", message, icon="ai")
        title, icon = _title(found["name"]), toast_icon(found["icon"])
        state = "включён" if enabled else "выключен"
        if found["enabled"] == enabled:
            return ExecResult(True, title, f"Уже {state}", icon=icon)
        self.update(found["id"], {"enabled": enabled})
        return ExecResult(
            True, title, state.capitalize(), icon=icon, say=f"Сценарий «{found['name']}» {state}"
        )

    async def _run_command(self, scenario_id: str, slots: dict[str, Any], trigger: str) -> ExecResult:
        """Сценарий из фразы (через диспетчер команд): историю, тост и голос делает диспетчер."""
        with self._lock:
            rec = copy.deepcopy(self._items.get(scenario_id))
        if rec is None:
            return ExecResult(False, "Сценарий", "Такого сценария больше нет", icon="error")
        title, icon = _title(rec["name"]), toast_icon(rec["icon"])
        if trigger != MANUAL and not conditions_ok(rec["conditions"], self.clock()):
            return ExecResult(False, title, "Сейчас условия сценария не выполняются", icon=icon)
        token = self._claim(scenario_id)
        if token is None:
            return ExecResult(False, title, "Сценарий уже выполняется", icon=icon)
        source = slots.get("_source") or "voice"
        clean = {k: v for k, v in slots.items() if not str(k).startswith("_")}
        return await self._execute(rec, trigger, clean, source, token, record=False)

    # --- выполнение -------------------------------------------------------------------------------
    def running(self, scenario_id: str) -> bool:
        return scenario_id in self._busy

    def _claim(self, scenario_id: str) -> object | None:
        if scenario_id in self._busy:
            return None
        token = object()
        self._busy[scenario_id] = token
        return token

    def _release(self, scenario_id: str, token: object) -> None:
        if self._busy.get(scenario_id) is token:
            del self._busy[scenario_id]

    def stop_run(self, scenario_id: str) -> bool:
        """Остановить идущий сценарий (его действия отменяются). False — он сейчас не выполняется."""
        task = self._actions.get(scenario_id)
        if task is not None and not task.done():
            task.cancel()
        elif scenario_id in self._busy:
            self._stopping.add(scenario_id)  # запущен, но до действий ещё не дошёл — не начнёт их
        else:
            return False
        log.info("сценарий %s остановлен", scenario_id)
        return True

    def stop_all(self) -> int:
        """«Стоп»: остановить все идущие сценарии. Вернёт, сколько остановлено."""
        return sum(self.stop_run(scenario_id) for scenario_id in {*self._actions, *self._busy})

    async def run(self, scenario_id: str, *, source: Source = "ui") -> ExecResult:
        """Запустить сейчас (кнопка «Запустить»): условия не проверяются, выключенный тоже выполняется."""
        with self._lock:
            rec = copy.deepcopy(self._items.get(scenario_id))
        if rec is None:
            raise ScenarioNotFoundError("Сценарий не найден")
        token = self._claim(scenario_id)
        if token is None:
            raise UserFacingError(f"Сценарий «{rec['name']}» уже выполняется")
        return await self._execute(rec, MANUAL, {}, source, token, record=True, label="Запуск вручную")

    def _fire(self, rec: dict[str, Any], trigger: dict[str, Any], slots: dict[str, Any], now: float) -> bool:
        """Триггер сработал: проверить условия и запустить в фоне (если сценарий уже не выполняется)."""
        sid = rec["id"]
        if not conditions_ok(rec["conditions"], now):
            log.info("сценарий «%s»: %s — условия не выполнены", rec["name"], trigger_label(trigger))
            return False
        token = self._claim(sid)
        if token is None:
            log.info("сценарий «%s» ещё выполняется — %s пропущено", rec["name"], trigger_label(trigger))
            return False
        coro = self._execute(
            copy.deepcopy(rec),
            str(trigger["type"]),
            slots,
            AUTO_SOURCE,
            token,
            record=True,
            label=trigger_label(trigger),
        )
        task = self.ctx.spawn(coro, f"scenario-{sid}")
        self._runs.add(task)

        def done(t: "asyncio.Task[Any]") -> None:
            self._runs.discard(t)
            self._release(sid, token)  # задачу отменили до начала — метка не должна остаться

        task.add_done_callback(done)
        log.info("сценарий «%s» запущен: %s", rec["name"], trigger_label(trigger))
        return True

    async def _execute(
        self,
        rec: dict[str, Any],
        trigger: str,
        slots: dict[str, Any],
        source: Any,
        token: object,
        *,
        record: bool,
        label: str = "",
    ) -> ExecResult:
        sid = rec["id"]
        title, icon = _title(rec["name"]), toast_icon(rec["icon"])
        stopped = ExecResult(False, title, "Остановлено", icon=icon, data={"cancelled": True})
        # действия — отдельной задачей: stop_run() отменяет только их, итог («Остановлено») получает вызвавший
        actions: asyncio.Future[ExecResult] | None = None
        try:
            if sid in self._stopping:
                result = stopped
            else:
                actions = asyncio.ensure_future(self._run_actions(rec, slots, source, icon))
                self._actions[sid] = actions
                result = await actions
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if actions is None or not actions.cancelled() or (current is not None and current.cancelling()):
                raise  # отменили сам запуск (выход ядра) — не только действия
            result = stopped
        except Exception as e:
            log.exception("сценарий «%s» упал", rec["name"])
            result = ExecResult(False, title, f"Ошибка: {e}", icon="error")
        finally:
            if actions is not None and self._actions.get(sid) is actions:
                del self._actions[sid]
            self._stopping.discard(sid)
            self._release(sid, token)
        after = fill_address(rec["say"], self._address()) if rec["say"] else ""
        if result.ok:
            say = " ".join(s for s in (result.say, after) if s)
            message = result.message if result.message and result.message != "Выполнено" else after
        else:
            say, message = "", result.message or "Не получилось"
        out = ExecResult(
            result.ok,
            title,
            message or "Выполнено",
            icon=icon,
            say=say,
            command_ids=[f"{PROVIDER}:{sid}"],
            data={**result.data, "scenario_id": sid, "trigger": trigger},
        )
        self._finish(rec, trigger, out)
        if record:
            await self._report(out, label or trigger)
        return out

    async def _run_actions(
        self, rec: dict[str, Any], slots: dict[str, Any], source: Any, icon: str
    ) -> ExecResult:
        title = _title(rec["name"])
        if not rec["actions"]:
            return ExecResult(True, title, "Выполнено", icon=icon)
        executor = getattr(self.ctx.commands, "executor", None)
        if executor is None:
            return ExecResult(False, title, "Действия сейчас недоступны", icon="error")
        command = Command(
            id=f"{PROVIDER}:{rec['id']}",
            name=rec["name"],
            category="scenarios",
            actions=[Action.model_validate(a) for a in rec["actions"]],
            icon=icon,
        )
        # сценарий — свой, как команда человека: доступны все действия, включая «Команду консоли»
        return await executor.run_entry(Entry(command, "user"), dict(slots), source)

    def _finish(self, rec: dict[str, Any], trigger: str, result: ExecResult) -> None:
        sid = rec["id"]
        item = None
        with self._lock:
            current = self._items.get(sid)
            if current is not None:  # сценарий могли удалить, пока он выполнялся
                current.update(
                    last_run=self.clock(),
                    last_ok=result.ok,
                    last_trigger=trigger,
                    last_error="" if result.ok else result.message,
                    runs=int(current.get("runs") or 0) + 1,
                )
                self._save(current)
                item = self.public(current)
        if item is not None:
            self._changed("update", item=item)
        event: dict[str, Any] = {"id": sid, "name": rec["name"], "trigger": trigger, "ok": result.ok}
        if not result.ok:
            event["error"] = result.message
        if result.data.get("cancelled"):
            event["cancelled"] = True
        self.ctx.bus.publish("scenario.run", event)

    async def _report(self, result: ExecResult, label: str) -> None:
        """История, тост и голос для сценария, который запустился сам или кнопкой."""
        status = "ok" if result.ok else ("cancelled" if result.data.get("cancelled") else "error")
        item: dict[str, Any] = {
            "ts": self.clock(),
            "source": SCENARIO_SOURCE,
            "text": label,
            "kind": "command",
            "title": result.title,
            "status": status,
            "detail": result.message,
            "icon": result.icon,
            "command_id": result.command_ids[0] if result.command_ids else "",
        }
        try:
            item["id"] = self.ctx.db.insert("history", item)
            self.ctx.bus.publish("history.added", {"item": item})
        except Exception:
            log.exception("сценарий: запись в историю не удалась")
        self.ctx.bus.publish(
            "toast",
            {
                "title": result.title,
                "message": result.message,
                "status": "ok" if result.ok else "error",
                "icon": result.icon,
                "ts": time.time(),
            },
        )
        if result.ok and result.say and not self.ctx.settings.value.voice.silent_mode:
            try:
                await asyncio.wait_for(self.ctx.tts.say(result.say), 10.0)
            except Exception:
                log.exception("сценарий: фраза не озвучена")

    # --- триггеры ---------------------------------------------------------------------------------
    def _on_ready(self, event: Event) -> None:
        if not self._start_fired:
            self._start_fired = True
            self.fire_start()

    def fire_start(self) -> list[str]:
        """Триггер «При запуске Jarvis» (событие core.ready, один раз за запуск ядра)."""
        now = self.clock()
        fired = []
        with self._lock:
            recs = [r for r in self._items.values() if r["enabled"]]
        for rec in recs:
            trigger = next((t for t in rec["triggers"] if t["type"] == "jarvis_start"), None)
            if trigger is not None and self._fire(rec, trigger, {}, now):
                fired.append(rec["id"])
        return fired

    def _on_reminder(self, event: Event) -> None:
        item = event.data.get("item") if isinstance(event.data, dict) else None
        text = str(item.get("text") or "").strip() if isinstance(item, dict) else ""
        if text and isinstance(item, dict):
            self._missed.append(
                {"ts": self.clock(), "text": text, "kind": str(item.get("kind") or "reminder")}
            )

    def missed_since(self, since: float) -> list[dict[str, Any]]:
        """Напоминания и события, сработавшие с момента since (не старше суток)."""
        floor = max(since, self.clock() - MISSED_KEEP_S)
        return [dict(i) for i in self._missed if i["ts"] >= floor]

    def away_since(self) -> float | None:
        """Когда человек в последний раз ушёл от компьютера (известно, пока есть триггеры с простоем)."""
        return self._last_away

    async def tick(self) -> list[str]:
        """Проверить триггеры включённых сценариев. Вернёт запущенные: ["<id>:<тип триггера>", …]."""
        now = self.clock()
        if self._started_at is None:
            self._started_at = now
        prev, self._last_tick = self._last_tick, now
        gap = now - prev if prev is not None else 0.0
        slept = prev is not None and gap > max(SLEEP_GAP_S, self.tick_s * 4)
        with self._lock:
            snapshot = [r for r in self._items.values() if r["enabled"]]
        if not snapshot:
            self._apps.clear()
            return []
        probe = await self._probe(snapshot, now)
        events = self._calendar_events(snapshot, now)
        self._track_away(probe.get("idle"), now, prev, gap, slept)
        # дальше без await: записи не поменяются, пока триггеры проверяются
        fired: list[str] = []
        with self._lock:
            recs = [r for r in self._items.values() if r["enabled"]]
            for rec in recs:
                marks_before = dict(rec["marks"])
                for trigger in rec["triggers"]:
                    slots = self._check(rec, trigger, now, gap, slept, probe, events)
                    if slots is not None and self._fire(rec, trigger, slots, now):
                        fired.append(f"{rec['id']}:{trigger['type']}")
                if rec["marks"] != marks_before:
                    _prune_marks(rec, now)
                    self._save(rec)
        # программы, которые больше никто не ждёт (или не проверились), забываем: новый триггер начнёт заново
        self._apps = dict(probe.get("apps") or {})
        return fired

    def _check(
        self,
        rec: dict[str, Any],
        trigger: dict[str, Any],
        now: float,
        gap: float,
        slept: bool,
        probe: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Сработал ли триггер; да — слоты для действий ({app}, {number}, {text}), нет — None."""
        kind = trigger["type"]
        key = (rec["id"], trigger_key(trigger))
        if kind == "time":
            return self._check_time(rec, trigger, now)
        if kind == "interval":
            return self._check_interval(rec, trigger, key, now, gap, slept, probe.get("idle"))
        if kind == "idle_return":
            return self._check_return(trigger, key, now, gap, slept, probe.get("idle"))
        if kind == "battery_low":
            return self._check_battery(trigger, key, probe)
        if kind in ("app_open", "app_close"):
            app = str(trigger["app"]).casefold()
            running = (probe.get("apps") or {}).get(app)
            before = self._apps.get(app)
            if running is None or before is None or running == before:
                return None  # первая проверка — только запоминаем, что запущено
            opened = kind == "app_open" and running
            closed = kind == "app_close" and not running
            return {"app": trigger["app"]} if opened or closed else None
        if kind == "calendar_event":
            return self._check_calendar(rec, trigger, now, events)
        return None  # phrase — динамическая команда, jarvis_start — fire_start()

    def _check_time(self, rec: dict[str, Any], trigger: dict[str, Any], now: float) -> dict[str, Any] | None:
        occurrence = last_occurrence(trigger["at"], trigger.get("weekdays"), now)
        if occurrence is None:
            return None
        mark = trigger_key(trigger)
        handled = max(float(rec.get("armed_at") or 0), float(rec["marks"].get(mark) or 0))
        if occurrence <= handled:
            return None
        rec["marks"][mark] = occurrence
        late = now - occurrence
        if late > LATE_S:
            log.info("сценарий «%s»: %s пропущено — опоздание %d мин", rec["name"], trigger["at"], late // 60)
            return None
        return {}

    def _check_interval(
        self,
        rec: dict[str, Any],
        trigger: dict[str, Any],
        key: tuple[str, str],
        now: float,
        gap: float,
        slept: bool,
        idle: float | None,
    ) -> dict[str, Any] | None:
        period = float(trigger["minutes"]) * 60
        if trigger.get("only_active"):
            if idle is None:
                return None
            total = self._active.get(key, 0.0)
            if idle >= BREAK_RESET_S or (slept and gap >= BREAK_RESET_S):
                total = 0.0  # долгий перерыв — глаза отдохнули, считаем заново
            elif idle < ACTIVE_IDLE_S and not slept:
                total += min(gap, self.tick_s * 2)
            if total >= period:
                self._active[key] = 0.0
                return {"number": trigger["minutes"]}
            self._active[key] = total
            return None
        base = self._interval_base.get(key)
        if base is None:
            base = max(float(rec.get("armed_at") or 0), self._started_at or now)
            self._interval_base[key] = base
        if now - base >= period:
            self._interval_base[key] = now  # после сна — один раз, без «догонялок»
            return {"number": trigger["minutes"]}
        return None

    def _check_return(
        self,
        trigger: dict[str, Any],
        key: tuple[str, str],
        now: float,
        gap: float,
        slept: bool,
        idle: float | None,
    ) -> dict[str, Any] | None:
        if idle is None:
            return None
        need = float(trigger["after_min"]) * 60
        away = self._away.get(key)
        if away is None:
            if idle >= need:
                away = now - idle
            elif slept and gap >= need:
                away = now - gap  # компьютер спал — человек тоже был не здесь
            else:
                return None
            self._away[key] = away
        if idle >= RETURN_IDLE_S:
            return None
        del self._away[key]
        minutes = max(1, round((now - idle - away) / 60))
        return {"number": minutes, "_away_since": away}

    def _check_battery(
        self, trigger: dict[str, Any], key: tuple[str, str], probe: dict[str, Any]
    ) -> dict[str, Any] | None:
        if "battery" not in probe:
            return None  # батарея проверяется раз в минуту
        level, charging = probe["battery"]
        if level is None:
            return None  # компьютер без батареи
        threshold = float(trigger["percent"])
        if charging is True or level >= threshold + BATTERY_HYSTERESIS:
            self._battery_low.discard(key)  # зарядили — следующая разрядка снова предупредит
            return None
        if level <= threshold and key not in self._battery_low:
            self._battery_low.add(key)
            return {"number": round(level)}
        return None

    def _check_calendar(
        self, rec: dict[str, Any], trigger: dict[str, Any], now: float, events: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        before = float(trigger["before_min"]) * 60
        prefix = trigger_key(trigger)
        armed = float(rec.get("armed_at") or 0)
        found = None
        for event in events:
            moment = float(event["start"]) - before
            if moment > now:
                continue
            mark = f"{prefix}|{event['id']}|{int(float(event['start']))}"
            if mark in rec["marks"]:
                continue
            rec["marks"][mark] = moment
            if moment <= armed or now - moment > LATE_S:
                continue  # событие было до включения сценария или уже давно
            if found is None:
                found = {"text": event.get("title") or "", "number": trigger["before_min"]}
        return found

    def _track_away(
        self, idle: float | None, now: float, prev: float | None, gap: float, slept: bool
    ) -> None:
        if slept and gap >= AWAY_MIN_S and self._idle_start is None and prev is not None:
            self._idle_start = prev
        if idle is None:
            return
        if idle >= AWAY_MIN_S and self._idle_start is None:
            self._idle_start = now - idle
        elif idle < RETURN_IDLE_S and self._idle_start is not None:
            self._last_away, self._idle_start = self._idle_start, None

    async def _probe(self, recs: list[dict[str, Any]], now: float) -> dict[str, Any]:
        """Одним заходом в поток платформы: простой, запущенные программы, заряд (если нужны)."""
        triggers = [t for r in recs for t in r["triggers"]]
        need_idle = any(
            t["type"] == "idle_return" or (t["type"] == "interval" and t.get("only_active")) for t in triggers
        )
        apps = {
            str(t["app"]).casefold(): str(t["app"])
            for t in triggers
            if t["type"] in ("app_open", "app_close")
        }
        need_battery = (
            any(t["type"] == "battery_low" for t in triggers) and now - self._battery_at >= BATTERY_POLL_S
        )
        if not (need_idle or apps or need_battery):
            return {}
        platform = self.ctx.platform

        def probe() -> dict[str, Any]:
            out: dict[str, Any] = {}
            if need_idle:
                with contextlib.suppress(Exception):
                    out["idle"] = float(platform.idle_seconds())
            if apps:
                states: dict[str, bool] = {}
                for key, name in apps.items():
                    with contextlib.suppress(Exception):
                        states[key] = bool(platform.is_running(name))
                out["apps"] = states
            if need_battery:
                with contextlib.suppress(Exception):
                    stats = platform.system_stats()
                    out["battery"] = (stats.battery, stats.charging)
            return out

        try:
            result = await self.ctx.run_platform(probe)
        except Exception:
            log.exception("сценарии: состояние компьютера не получено")
            return {}
        if need_battery:
            self._battery_at = now
        return result

    def _calendar_events(self, recs: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        befores = [t["before_min"] for r in recs for t in r["triggers"] if t["type"] == "calendar_event"]
        if not befores:
            return []
        events_list = getattr(self.ctx.notes, "events_list", None)
        if not callable(events_list):
            return []
        try:
            items = events_list(now - LATE_S - 60, now + max(befores) * 60 + 60)
        except Exception:
            log.exception("сценарии: события календаря не прочитаны")
            return []
        return [e for e in items if not e.get("all_day")]


def _title(name: str) -> str:
    return f"Сценарий «{name}»"


def _require_work(fields: dict[str, Any]) -> None:
    if not fields["actions"] and not fields["say"]:
        raise UserFacingError("Добавьте действие или фразу, которую скажет Jarvis")


def _with_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Запись из базы с недостающими полями (на случай старого формата)."""
    created = float(data.get("created") or time.time())
    defaults: dict[str, Any] = {
        "name": "Сценарий",
        "icon": "sparkles",
        "enabled": True,
        "triggers": [],
        "conditions": {"weekdays": [], "time_from": "", "time_to": ""},
        "actions": [],
        "say": "",
        "template": "",
        "last_run": None,
        "last_ok": None,
        "last_trigger": "",
        "last_error": "",
        "runs": 0,
        "created": created,
        "updated": created,
        "armed_at": created,
        "marks": {},
    }
    out = {"id": str(data["id"])}
    for key, default in defaults.items():
        value = data.get(key)
        out[key] = default if value is None else value
    if not isinstance(out["marks"], dict):
        out["marks"] = {}
    return out


def _prune_marks(rec: dict[str, Any], now: float) -> None:
    """Отметки только для нынешних триггеров; события календаря — за последние двое суток."""
    keys = {trigger_key(t) for t in rec["triggers"]}
    rec["marks"] = {
        k: v
        for k, v in rec["marks"].items()
        if k.split("|", 1)[0] in keys and ("|" not in k or float(v) >= now - MARKS_KEEP_S)
    }
